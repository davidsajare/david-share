"""
Live benchmark console for the candidate model comparison.

A single-file HTTP server (standard library only) that drives the study
harness in this folder tree and streams each measurement to a browser as it
completes. It exists because a 1,400-line report is the wrong artefact for a
workshop room: the same numbers, produced live and plotted, are easier to
trust and much easier to discuss.

Two modes:
  live    AZURE_OPENAI_ENDPOINT is set, requests go to the real deployments.
  replay  No endpoint configured. The console serves the recorded runs in
          replay/ so the UI is fully demonstrable with no credentials and no
          network. Replay data is clearly labelled as such in the UI.

No tools and no web search are attached in either mode.

Usage:
    python server.py --port 8080 [--host 0.0.0.0]
"""

from __future__ import annotations

import argparse
import csv
import http.client
import io
import json
import os
import queue
import re
import secrets
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import bench_core
from bench_core import ConsoleError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
REPLAY = ROOT / "replay"
HISTORY = Path(os.environ.get("BENCH_HISTORY_DIR", ROOT / "history"))
RUNNER_URL = os.environ.get("BENCH_RUNNER_URL", "").rstrip("/")

MAX_BODY_BYTES = 1 << 20
RUN_RETENTION = 8
HISTORY_RETENTION = 200
MAX_ACTIVE_RUNS = int(os.environ.get("BENCH_MAX_ACTIVE_RUNS", "2"))
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("BENCH_ALLOWED_ORIGIN", "").split(",")
    if origin.strip()
}

# A run executes on the Sweden Central runner, but the history the workshop
# reads lives here. Mirroring must therefore survive a closed tab, so a
# background worker polls the runner until the record exists locally.
MIRROR_POLL_SECONDS = 5
MIRROR_POLL_MAX_SECONDS = 30
# The SSE relay already assumes an hour is the longest a run can take.
MIRROR_DEADLINE_SECONDS = int(os.environ.get("BENCH_MIRROR_DEADLINE", "3700"))
MAX_MIRROR_WORKERS = 16
RECONCILE_INTERVAL_SECONDS = 60
RECONCILE_TIMEOUT_SECONDS = 5
RECONCILE_BUDGET_SECONDS = 15
RECONCILE_MAX_IMPORTS = 20

_runs: dict[str, dict] = {}
_runs_lock = threading.Lock()
_history_write_lock = threading.Lock()
_mirror_lock = threading.Lock()
_mirroring: set[str] = set()
_reconciled_at = 0.0
_migrated = False


# --------------------------------------------------------------------------
# Mode
# --------------------------------------------------------------------------

def endpoint_configured() -> bool:
    return bool(os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip())


def runner_configured() -> bool:
    return bool(RUNNER_URL)


def server_mode() -> str:
    return "live" if endpoint_configured() or runner_configured() else "replay"


def endpoint_label() -> str | None:
    """Host only. The full endpoint is a resource identifier, not a secret,
    but there is no reason to project it on a screen either."""
    raw = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    if not raw:
        return os.environ.get("BENCH_RUNNER_LABEL") if runner_configured() else None
    host = urlparse(raw).hostname or raw
    parts = host.split(".")
    if len(parts) > 2:
        return f"{parts[0][:3]}***.{'.'.join(parts[1:])}"
    return host


def runner_json(method: str, path: str, payload: dict | None = None,
                timeout: int = 30) -> tuple[int, dict]:
    """Call the same-region runner through the local SSH tunnel."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        RUNNER_URL + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException):
            # A truncated error body is still an error, not a reason to fail
            # the caller with an exception raised inside an except block.
            raw = ""
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw or exc.reason}
    except URLError as exc:
        raise ConsoleError(
            "The Sweden Central benchmark runner is offline. Start benchmark-vm "
            "and verify benchmark-reverse-tunnel.service on the runner VM."
        ) from exc
    except (OSError, http.client.HTTPException, json.JSONDecodeError,
            UnicodeDecodeError) as exc:
        # A wedged tunnel answers the connect and then stalls or drops, so the
        # failure surfaces during the read as TimeoutError or a reset rather
        # than as URLError. Those must not reach the handler as a 500.
        raise ConsoleError(
            "The Sweden Central benchmark runner did not answer. Check "
            "benchmark-reverse-tunnel.service on the runner VM."
        ) from exc


# --------------------------------------------------------------------------
# Run lifecycle
# --------------------------------------------------------------------------

def _prune_runs() -> None:
    if len(_runs) <= RUN_RETENTION:
        return
    finished = [(r["finished_at"], rid) for rid, r in _runs.items() if r.get("finished_at")]
    finished.sort()
    for _, rid in finished[: max(0, len(_runs) - RUN_RETENTION)]:
        _runs.pop(rid, None)


def start_run(request: dict) -> str:
    if server_mode() != "live":
        raise ConsoleError(
            "This console is running in replay mode: AZURE_OPENAI_ENDPOINT is not set, "
            "so there is nothing to measure against. Load your .env and restart to run live."
        )
    catalog_data = bench_core.catalog()
    plan = bench_core.build_plan(request, catalog_data)

    run_id = secrets.token_hex(8)
    events: queue.Queue = queue.Queue()
    state = {
        "run_id": run_id,
        "plan": plan,
        "cancel": threading.Event(),
        "events": events,
        "records": [],
        "summaries": [],
        "started_at": time.time(),
        "finished_at": None,
        "error": None,
    }
    with _runs_lock:
        active = sum(1 for run in _runs.values() if run.get("finished_at") is None)
        if active >= MAX_ACTIVE_RUNS:
            raise ConsoleError(
                f"{active} benchmark run(s) are already active; the service limit is "
                f"{MAX_ACTIVE_RUNS}. Wait for one to finish or stop it before starting another."
            )
        _prune_runs()
        _runs[run_id] = state

    thread = threading.Thread(target=_run_worker, args=(state,), daemon=True)
    thread.start()
    return run_id


def _run_worker(state: dict) -> None:
    plan = state["plan"]
    events = state["events"]

    def emit(kind: str, payload: dict) -> None:
        if kind == "record":
            state["records"].append(payload)
        elif kind == "arm_summary":
            state["summaries"].append(payload)
        events.put({"type": kind, **payload})

    try:
        harness = bench_core.load_harness()
        pricing = bench_core.load_pricing()
        registry = bench_core.load_registry()
        client, _ = harness.build_client()
        emit("run_start", {
            "total": plan.total_calls,
            "measured": plan.measured_calls,
            "arms": [a.name for a in plan.arms],
            "items": [i["id"] for i in plan.items],
            "iterations": plan.iterations,
            "concurrency": plan.concurrency,
            "max_output_tokens": plan.max_output_tokens,
            "headroom": plan.headroom,
            "warmup": plan.warmup,
            "api": plan.arms[0].api if plan.arms else "responses",
        })
        bench_core.execute_plan(plan, client, pricing, registry, harness,
                                emit, state["cancel"])
        summaries = state["summaries"]
        priced = [s["cost_per_1k_requests"] for s in summaries if s.get("cost_per_1k_requests")]
        baseline = max(priced) if priced else None
        for s in summaries:
            s["value_ratio"] = bench_core.value_score(s, baseline)
        state["finished_at"] = time.time()
        saved = save_history(state)
        events.put({
            "type": "done",
            "cancelled": state["cancel"].is_set(),
            "summaries": summaries,
            "totals": run_totals(summaries),
            "saved": bool(saved),
            "records": len(state["records"]),
        })
    except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
        state["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        events.put({"type": "error", "message": state["error"]})
    finally:
        state["finished_at"] = state.get("finished_at") or time.time()
        events.put(None)


# --------------------------------------------------------------------------
# Run history
# --------------------------------------------------------------------------

# \A and \Z, not ^ and $: Python's $ also matches before a trailing newline,
# and this value reaches filesystem globs.
RUN_ID_RE = re.compile(r"\A[0-9a-f]{8,32}\Z")


def run_totals(summaries: list[dict]) -> dict:
    """What this session actually consumed and what it would be billed."""
    priced = [s["cost_usd_total"] for s in summaries if s.get("cost_usd_total") is not None]
    return {
        "ok": sum(s.get("ok") or 0 for s in summaries),
        "errors": sum(s.get("errors") or 0 for s in summaries),
        "truncated": sum(s.get("truncated") or 0 for s in summaries),
        "prompt_tokens": sum(s.get("prompt_tokens_total") or 0 for s in summaries),
        "cached_tokens": sum(s.get("cached_tokens_total") or 0 for s in summaries),
        "reasoning_tokens": sum(s.get("reasoning_tokens_total") or 0 for s in summaries),
        "output_tokens": sum(s.get("output_tokens_total") or 0 for s in summaries),
        "total_tokens": sum(s.get("total_tokens") or 0 for s in summaries),
        "cost_usd": round(sum(priced), 8) if priced else None,
        # True only when every arm in the run had a confirmed list price.
        "cost_complete": bool(summaries) and len(priced) == len(summaries),
    }


def save_history(state: dict) -> Path | None:
    """
    Persist a finished run so it can be reopened later.

    Each run is one self-contained JSON file: the customer gets a portal that
    shows what was measured before as well as what is being measured now, and
    every past run stays separately inspectable instead of being averaged into
    a single rolling view.
    """
    summaries = state.get("summaries") or []
    if not summaries:
        return None
    plan = state["plan"]
    started = state.get("started_at") or time.time()
    finished = state.get("finished_at") or time.time()
    record = {
        "run_id": state["run_id"],
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
        "finished_at": datetime.fromtimestamp(finished, tz=timezone.utc).isoformat(),
        "duration_s": round(finished - started, 1),
        "mode": "live",
        "cancelled": state["cancel"].is_set(),
        "endpoint": endpoint_label(),
        "regions": bench_core.load_deployment_facts()["regions"],
        "dataset": plan.dataset,
        "arms": [a.name for a in plan.arms],
        "items": [i["id"] for i in plan.items],
        "iterations": plan.iterations,
        "concurrency": plan.concurrency,
        "warmup": plan.warmup,
        "headroom": plan.headroom,
        "max_output_tokens": plan.max_output_tokens,
        "api": plan.arms[0].api if plan.arms else "responses",
        "records": len(state["records"]),
        "totals": run_totals(summaries),
        "summaries": summaries,
        "rows": state["records"],
    }
    HISTORY.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = HISTORY / f"run_{stamp}_{state['run_id']}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf-8", newline="\n")
    _prune_history()
    return path


def _prune_history() -> None:
    # A record written by an import that raced a delete converges here. Match
    # on the run id, not the stamp: a re-import whose started_at was
    # unparseable files under a different stamp and must still go.
    deleted = {p.stem.rsplit("_", 1)[-1] for p in HISTORY.glob("run_*.deleted")}
    files = sorted(HISTORY.glob("run_*.json"))
    if deleted:
        surviving = []
        for path in files:
            if path.stem.rsplit("_", 1)[-1] in deleted:
                path.unlink(missing_ok=True)
            else:
                surviving.append(path)
        files = surviving
    for path in files[:max(0, len(files) - HISTORY_RETENTION)]:
        path.unlink(missing_ok=True)
    markers = sorted(HISTORY.glob("run_*.deleted"))
    for path in markers[:max(0, len(markers) - HISTORY_RETENTION)]:
        path.unlink(missing_ok=True)


def run_is_deleted(run_id: str) -> bool:
    """A run the operator removed here must not come back on the next sync."""
    if not RUN_ID_RE.match(run_id or "") or not HISTORY.is_dir():
        return False
    return next(iter(HISTORY.glob(f"run_*_{run_id}.deleted")), None) is not None


def history_index() -> list[dict]:
    """Metadata for every saved run, newest first. Never loads the rows."""
    entries = []
    if not HISTORY.is_dir():
        return entries
    for path in sorted(HISTORY.glob("run_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entries.append({k: data.get(k) for k in (
            "run_id", "started_at", "finished_at", "duration_s", "mode", "dataset",
            "arms", "items", "iterations", "concurrency", "records", "totals",
            "cancelled", "endpoint", "regions", "api")})
    return entries


def history_run(run_id: str) -> dict | None:
    if not RUN_ID_RE.match(run_id or ""):
        return None
    for path in HISTORY.glob("run_*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if record.get("run_id") == run_id:
            return record
    return None


def delete_history_run(run_id: str) -> bool:
    if not RUN_ID_RE.match(run_id or ""):
        return False
    removed = False
    with _history_write_lock:
        for path in sorted(HISTORY.glob("run_*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if record.get("run_id") != run_id:
                continue
            # The run still exists on the runner, so without a marker the next
            # reconciliation would mirror it again and Delete would do nothing.
            # Write the marker first: a delete that cannot be recorded has to
            # fail rather than quietly come back later. The marker is empty.
            marker = history_path(f"{path.stem}.deleted")
            if marker is None:
                return False
            try:
                marker.touch()
            except OSError:
                return False
            path.unlink(missing_ok=True)
            removed = True
    return removed


def mirrored_run_path(run_id: str) -> Path | None:
    """Cheap check for an already-mirrored run: the id is in the file name."""
    if not RUN_ID_RE.match(run_id or "") or not HISTORY.is_dir():
        return None
    return next(iter(HISTORY.glob(f"run_*_{run_id}.json")), None)


def history_path(name: str) -> Path | None:
    """Resolve a history file name, refusing anything that leaves the folder.

    Every id reaching this point already matched RUN_ID_RE, so this is defence
    in depth rather than the control itself: it keeps the guarantee local to
    the one place a name becomes a path.
    """
    root = HISTORY.resolve()
    candidate = (HISTORY / name).resolve()
    if candidate.parent != root or not candidate.name:
        return None
    return candidate


def migrate_history_filenames() -> int:
    """Name every saved run after its run id.

    Mirrors used to carry a random suffix, so the id was not in the name and
    the check above could not see them. Left alone they would be imported a
    second time and show up twice in Past runs. One idempotent pass fixes the
    names, and drops a legacy file whose run is already stored correctly.
    """
    if not HISTORY.is_dir():
        return 0
    changed = 0
    for path in sorted(HISTORY.glob("run_*.json")):
        try:
            run_id = json.loads(path.read_text(encoding="utf-8")).get("run_id", "")
        except (json.JSONDecodeError, OSError, AttributeError):
            continue
        if not RUN_ID_RE.match(run_id or "") or path.stem.endswith(f"_{run_id}"):
            continue
        parts = path.stem.split("_")
        if len(parts) < 4:
            continue
        target = history_path("_".join(parts[:3]) + f"_{run_id}.json")
        if target is None:
            continue
        try:
            if target.exists():
                path.unlink()
            else:
                path.rename(target)
        except OSError:
            continue
        changed += 1
    return changed


def ensure_history_migrated() -> None:
    """Run the rename pass once per process, before anything trusts a name."""
    global _migrated
    with _history_write_lock:
        if _migrated:
            return
        _migrated = True
    migrate_history_filenames()


def run_stamp(started_at: str | None) -> str:
    """The UTC stamp a mirrored record is filed under."""
    try:
        started = datetime.fromisoformat((started_at or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        started = datetime.now(timezone.utc)
    return started.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")


def retention_would_drop(name: str) -> bool:
    """True when a record filed under this name would be pruned on arrival.

    Retention deletes oldest-first, so importing such a record writes a file
    that is removed in the same breath. Answering before the fetch keeps
    reconciliation from asking the runner for it once a minute for ever.
    """
    if not HISTORY.is_dir():
        return False
    kept = sorted(HISTORY.glob("run_*.json"))
    return len(kept) >= HISTORY_RETENTION and name <= kept[0].name


def import_runner_history(run_id: str, timeout: int = 30) -> Path | None:
    """Mirror a completed runner record onto the portal VM.

    Three callers race for the same run: the SSE relay, the background mirror
    worker and reconciliation. Writing under the run id makes a repeat import
    replace the record instead of adding a duplicate row to Past runs.
    """
    if not RUN_ID_RE.match(run_id or ""):
        return None
    if mirrored_run_path(run_id) is not None or run_is_deleted(run_id):
        return None
    status, record = runner_json("GET", f"/api/history/{run_id}", timeout=timeout)
    if status != 200 or not record or record.get("run_id") != run_id:
        return None
    stamp = run_stamp(record.get("started_at"))
    with _history_write_lock:
        # Re-checked under the lock: a delete may have landed while the record
        # was in flight, and it must win.
        if mirrored_run_path(run_id) is not None or run_is_deleted(run_id):
            return None
        HISTORY.mkdir(parents=True, exist_ok=True)
        # run_id matched RUN_ID_RE above; history_path re-checks containment.
        path = history_path(f"run_{stamp}_{run_id}.json")
        if path is None or retention_would_drop(path.name):
            return None
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _prune_history()
    return path


def _mirror_worker(run_id: str) -> None:
    deadline = time.monotonic() + MIRROR_DEADLINE_SECONDS
    delay = MIRROR_POLL_SECONDS
    try:
        while time.monotonic() < deadline:
            time.sleep(delay)
            delay = min(delay * 2, MIRROR_POLL_MAX_SECONDS)
            if run_is_deleted(run_id):
                return
            try:
                if import_runner_history(run_id) is not None:
                    return
            except ConsoleError:
                # Runner briefly unreachable. Keep waiting; the record is
                # persisted on the runner and stays fetchable.
                continue
            except OSError:
                continue
            if mirrored_run_path(run_id) is not None:
                return
    finally:
        with _mirror_lock:
            _mirroring.discard(run_id)


def mirror_runner_run(run_id: str) -> bool:
    """Capture a runner-side run locally even if nobody is watching the stream."""
    if not RUN_ID_RE.match(run_id or "") or run_is_deleted(run_id):
        return False
    with _mirror_lock:
        # A run that errors never reaches the runner's history, so its worker
        # polls until the deadline. Cap them rather than let repeated failed
        # submissions accumulate threads; reconciliation still backfills.
        if run_id in _mirroring or len(_mirroring) >= MAX_MIRROR_WORKERS:
            return False
        _mirroring.add(run_id)
    try:
        threading.Thread(target=_mirror_worker, args=(run_id,), daemon=True).start()
    except RuntimeError:
        with _mirror_lock:
            _mirroring.discard(run_id)
        return False
    return True


def reconcile_runner_history(force: bool = False) -> int:
    """Backfill runner runs this portal never saw, e.g. across a restart.

    Best effort by definition: it must never be the reason a reader cannot see
    the local history, and it must not keep a reader waiting, so the whole
    pass shares one wall-clock budget and transport failures are swallowed.
    """
    global _reconciled_at
    now = time.monotonic()
    if not force and _reconciled_at and now - _reconciled_at < RECONCILE_INTERVAL_SECONDS:
        return 0
    _reconciled_at = now
    deadline = now + RECONCILE_BUDGET_SECONDS
    try:
        ensure_history_migrated()
        status, payload = runner_json(
            "GET", "/api/history", timeout=RECONCILE_TIMEOUT_SECONDS)
    except (ConsoleError, OSError):
        return 0
    if status != 200:
        return 0
    imported = 0
    attempts = 0
    for entry in (payload or {}).get("runs", [])[:HISTORY_RETENTION]:
        # Attempts, not successes: a record the portal declines still costs a
        # round trip, and the reader is waiting for all of them.
        if attempts >= RECONCILE_MAX_IMPORTS or time.monotonic() >= deadline:
            break
        run_id = (entry or {}).get("run_id", "")
        if (not RUN_ID_RE.match(run_id or "")
                or mirrored_run_path(run_id) is not None
                or run_is_deleted(run_id)
                or retention_would_drop(
                    f"run_{run_stamp((entry or {}).get('started_at'))}_{run_id}.json")):
            continue
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            if import_runner_history(
                    run_id,
                    timeout=max(1, int(min(RECONCILE_TIMEOUT_SECONDS, remaining)))
            ) is not None:
                imported += 1
        except (ConsoleError, OSError):
            break
    return imported


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------

def load_replay() -> dict:
    path = REPLAY / "replay_pack.json"
    if not path.is_file():
        return {"available": False, "runs": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["available"] = True
    return data


def load_catalog() -> dict:
    """Use the live study catalog, or the embedded catalog in no-LFS replay mode."""
    try:
        return bench_core.catalog()
    except ConsoleError:
        if server_mode() != "replay" and not runner_configured():
            raise
        replay = load_replay()
        catalog = replay.get("catalog")
        if not catalog:
            raise
        return catalog


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------

CSV_COLUMNS = [
    "arm", "deployment", "effort", "item_id", "scenario", "iteration", "billing_model",
    "model_actually_served", "ttft_ms", "e2e_ms", "decode_ms", "tpot_ms",
    "decode_tps", "prompt_tokens", "cached_tokens", "reasoning_tokens",
    "completion_tokens", "answer_budget", "max_output_tokens", "cost_usd",
    "status", "truncated", "error",
]


def records_to_csv(records: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow(record)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}
STATIC_FILES = {
    "index.html": (STATIC / "index.html", CONTENT_TYPES[".html"]),
    "app.js": (STATIC / "app.js", CONTENT_TYPES[".js"]),
    "styles.css": (STATIC / "styles.css", CONTENT_TYPES[".css"]),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LiveBenchmarkConsole/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - quieter console output
        if self.path.startswith("/api/events"):
            return
        super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str, status=200, filename=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ConsoleError("Request body too large.")
        body = self.rfile.read(length) if length > 0 else b""
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ConsoleError("State-changing requests require Content-Type: application/json.")
        if length <= 0:
            return {}
        return json.loads(body.decode("utf-8"))

    def _require_same_site(self):
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            raise ConsoleError("Cross-site state-changing requests are not allowed.")
        origin = self.headers.get("Origin")
        if not origin:
            return
        if origin.rstrip("/") not in ALLOWED_ORIGINS:
            raise ConsoleError("Request Origin does not match this portal.")

    def _serve_static(self, relative: str):
        # The UI has three immutable assets. A fixed map is simpler and leaves
        # no request-controlled value in a filesystem expression.
        entry = STATIC_FILES.get(relative)
        if entry is None:
            self._send_json({"error": "Not found"}, 404)
            return
        target, content_type = entry
        self._send_text(target.read_text(encoding="utf-8"), content_type)

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route in ("/", "/index.html"):
                self._serve_static("index.html")
            elif route.startswith("/static/"):
                self._serve_static(route[len("/static/"):])
            elif route == "/api/catalog":
                data = load_catalog()
                data["mode"] = server_mode()
                data["endpoint"] = endpoint_label()
                data["runner"] = "remote" if runner_configured() else "local"
                data["replay"] = load_replay().get("available", False)
                self._send_json(data)
            elif route == "/api/replay":
                self._send_json(load_replay())
            elif route == "/api/history":
                if runner_configured():
                    reconcile_runner_history()
                self._send_json({"runs": history_index()})
            elif route.startswith("/api/history/"):
                run = history_run(route[len("/api/history/"):])
                self._send_json(run or {"error": "Unknown run"}, 200 if run else 404)
            elif route == "/api/events":
                self._stream_events(parse_qs(parsed.query).get("run_id", [""])[0])
            elif route == "/api/export":
                self._export(parse_qs(parsed.query).get("run_id", [""])[0])
            else:
                self._send_json({"error": "Not found"}, 404)
        except ConsoleError as exc:
            self._send_json({"error": str(exc)}, 400)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path).path
        try:
            if route == "/api/run":
                payload = self._read_json()
                self._require_same_site()
                if runner_configured():
                    status, response = runner_json("POST", "/api/run", payload)
                    if status == 200:
                        mirror_runner_run((response or {}).get("run_id", ""))
                    self._send_json(response, status)
                else:
                    run_id = start_run(payload)
                    self._send_json({"run_id": run_id})
            elif route == "/api/cancel":
                payload = self._read_json()
                self._require_same_site()
                run_id = (payload or {}).get("run_id", "")
                if runner_configured():
                    status, response = runner_json(
                        "POST", "/api/cancel", {"run_id": run_id})
                    self._send_json(response, status)
                else:
                    state = _runs.get(run_id)
                    if state:
                        state["cancel"].set()
                    self._send_json({"ok": bool(state)})
            else:
                self._send_json({"error": "Not found"}, 404)
        except ConsoleError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_DELETE(self):  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlparse(self.path).path
        try:
            self._require_same_site()
            if route.startswith("/api/history/"):
                removed = delete_history_run(route[len("/api/history/"):])
                self._send_json({"ok": removed}, 200 if removed else 404)
            else:
                self._send_json({"error": "Not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _export(self, run_id: str):
        state = _runs.get(run_id)
        records = state["records"] if state else None
        if records is None:
            saved = history_run(run_id)
            records = saved.get("rows") if saved else None
        if records is None:
            self._send_json({"error": "Unknown run"}, 404)
            return
        # Do not reflect a query-string value into Content-Disposition.
        self._send_text(records_to_csv(records), "text/csv; charset=utf-8",
                        filename=f"benchmark-run-{secrets.token_hex(4)}.csv")

    def _stream_events(self, run_id: str):
        state = _runs.get(run_id)
        if not state:
            if runner_configured():
                self._stream_runner_events(run_id)
                return
            self._send_json({"error": "Unknown run"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        events: queue.Queue = state["events"]
        try:
            while True:
                try:
                    event = events.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                payload = json.dumps(event, ensure_ascii=False, default=str)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Losing the reader must not destroy the measurement. When this
            # console is the same-region runner the reader is the portal relay,
            # which drops as soon as a browser tab closes. /api/cancel is the
            # only way a run ends early.
            pass
        finally:
            self.close_connection = True

    def _stream_runner_events(self, run_id: str):
        """Pass runner SSE through and mirror the finished run locally."""
        request = Request(
            RUNNER_URL + f"/api/events?run_id={run_id}",
            method="GET",
            headers={"Accept": "text/event-stream"},
        )
        try:
            remote = urlopen(request, timeout=3700)
        except HTTPError as exc:
            self._send_json({"error": exc.reason}, exc.code)
            return
        except URLError as exc:
            raise ConsoleError(
                "The Sweden Central benchmark runner is offline."
            ) from exc

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for line in remote:
                if line.startswith(b"data: "):
                    try:
                        event = json.loads(line[6:].decode("utf-8"))
                    except json.JSONDecodeError:
                        event = {}
                    if event.get("type") == "done":
                        # The runner writes history before emitting done. Mirror
                        # it before the browser refreshes its Past runs list.
                        # A blip here must not corrupt a stream whose 200 is
                        # already sent; the mirror worker retries either way.
                        try:
                            import_runner_history(run_id)
                        except (ConsoleError, OSError):
                            pass
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Deliberately not a cancel. A closed tab, a sleeping laptop or a
            # dropped Wi-Fi link must not destroy a measurement the room is
            # waiting for; the mirror worker still stores it. Stopping a run is
            # an explicit action, and the UI has a Stop button for it.
            pass
        finally:
            remote.close()
            self.close_connection = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address. Use 0.0.0.0 only behind a locked-down NSG.")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not ALLOWED_ORIGINS:
        ALLOWED_ORIGINS.update({
            f"http://127.0.0.1:{args.port}",
            f"http://localhost:{args.port}",
        })

    mode = server_mode()
    print(f"Live benchmark console - mode: {mode}")
    ensure_history_migrated()
    if mode == "live":
        print(f"  endpoint: {endpoint_label()}")
        try:
            harness_path = bench_core.find_harness()
            print(f"  harness:  {harness_path.parent.name}/harness.py")
        except ConsoleError as exc:
            print(f"  [warn] {exc}")
    else:
        print("  AZURE_OPENAI_ENDPOINT is not set; serving recorded runs only.")
    print(f"  open:     http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}/")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
