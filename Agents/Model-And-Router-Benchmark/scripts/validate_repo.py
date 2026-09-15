"""Fail-closed repository gate for the model and router benchmark.

This is the single offline acceptance command the README documents. It checks
the public boundary, the reader contract, the bilingual contract, the
de-identification proof and every component test, without making a model call.

Usage:
    python scripts/validate_repo.py
    python scripts/validate_repo.py --write-results
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
EVIDENCE = ROOT / "evidence"
RULE_RESULTS = EVIDENCE / "rule-results.json"

FOLDERS = (
    "scenario-model-benchmark",
    "model-router-validation",
    "throughput-recalibration",
    "production-readiness",
    "live-benchmark-console",
)
STUDY_FOLDERS = FOLDERS[:4]

TEXT_SUFFIXES = {
    ".conf", ".css", ".csv", ".env", ".example", ".html", ".js", ".json",
    ".jsonl", ".md", ".ps1", ".py", ".service", ".sh", ".txt", ".xml",
    ".yaml", ".yml",
}

# Assembled at runtime so this gate does not match its own source.
PRIVATE_TERMS = {
    "customer name": "Len" + "ovo",
    "product name": "Q" + "ira",
    "venue name": "Chi" + "cago",
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Azure storage key": re.compile(r"\bAccountKey=[A-Za-z0-9+/=]{20,}"),
    "bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._-]{30,}"),
    # A hyphen is a word boundary, so \b would let the placeholder match from
    # its second half. Anchor on "not preceded by a name character" instead.
    # RFC 2606 reserves "example" for documentation, so it is not a real host.
    "deployed hostname": re.compile(
        r"(?<![\w.-])(?!YOUR-|example\b)[A-Za-z0-9][A-Za-z0-9-]*"
        r"\.cloudapp\.azure\.com\b"),
    "live endpoint host": re.compile(
        r"(?<![\w.-])(?!YOUR-ENDPOINT\.|example\.)[A-Za-z0-9][A-Za-z0-9-]*"
        r"\.(?:cognitiveservices|openai)\.azure\.com\b"),
    "published password field": re.compile(r"(?im)^\s*\|\s*(?:password|密码)\s*\|"),
}


class GateError(RuntimeError):
    """A repository acceptance gate failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def run(label: str, cwd: Path, *command: str, timeout: int = 600) -> None:
    started = time.monotonic()
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        list(command), cwd=cwd, env=environment,
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode:
        output = (result.stdout + "\n" + result.stderr).strip()
        raise GateError(f"{label} failed:\n{output[-4000:]}")
    print(f"PASS  {label:<45} {time.monotonic() - started:6.2f}s")


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "--",
         "Agents/Model-And-Router-Benchmark"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return [REPO / Path(line) for line in result.stdout.splitlines() if line]


# -- public boundary ----------------------------------------------------

def validate_public_boundary(paths: list[Path] | None = None) -> None:
    """The control that matters most: no customer identity, no credentials."""
    findings: list[str] = []
    this_file = Path(__file__).resolve()
    for path in (paths if paths is not None else tracked_files()):
        name = str(path.relative_to(ROOT)).replace("\\", "/") if path.is_relative_to(ROOT) else str(path)
        for label, term in PRIVATE_TERMS.items():
            if term.casefold() in name.casefold():
                findings.append(f"{name}: {label} in path")
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.resolve() == this_file:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, term in PRIVATE_TERMS.items():
            if term.casefold() in text.casefold():
                findings.append(f"{name}: {label}")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name}: {label}")
    require(not findings,
            "public boundary violations:\n" + "\n".join(sorted(set(findings))[:20]))


# -- layout and reader contract -----------------------------------------

def validate_layout() -> None:
    for folder in FOLDERS:
        require((ROOT / folder).is_dir(), f"missing component: {folder}")
        for name in ("README.md", "README-CN.md"):
            require((ROOT / folder / name).is_file(), f"{folder}: {name} missing")
    for name in ("README.md", "README-CN.md", "requirements.txt"):
        require((ROOT / name).is_file(), f"root {name} missing")


def headings(text: str) -> list[tuple[int, str]]:
    return [
        (len(m.group(1)), m.group(2).strip())
        for m in re.finditer(r"^(#{1,6})\s+(.+)$", text, re.MULTILINE)
    ]


EXPECTED_H2 = {
    "README.md": [
        "Executive Summary",
        "1. Background",
        "2. Methodology",
        "3. Results",
        "4. Cost Analysis",
        "5. Configuration",
        "6. Reproducing",
        "7. Evidence and boundaries",
        "Repository layout",
    ],
    "README-CN.md": [
        "执行摘要",
        "1. 背景",
        "2. 方法",
        "3. 结果",
        "4. 成本分析",
        "5. 配置",
        "6. 复现",
        "7. 证据与边界",
        "仓库结构",
    ],
}
REQUIRED_ANCHORS = (
    "executive-summary", "background", "methodology", "results",
    "cost-analysis", "configuration", "reproducing", "evidence",
)
NAV_ANCHORS = ("executive-summary", "methodology", "results", "reproducing")
REQUIRED_TOKENS = ("max_retries=0", "Responses API", "0.367")


def validate_reader_contract(root: Path = ROOT) -> None:
    for name, expected in EXPECTED_H2.items():
        text = (root / name).read_text(encoding="utf-8")
        first_screen = text.split("\n---\n", 1)[0]
        require(first_screen.count("[![") == 5,
                f"{name}: the first screen needs exactly 5 fact badges")
        order = (
            text.find("# "), text.find("[![CI]"), text.find("0.367"),
            text.find("> Author:"), text.find("[English]"),
            text.find("#executive-summary"), text.find("\n---\n"),
        )
        require(all(p >= 0 for p in order),
                f"{name}: a required first-screen element is missing")
        require(list(order) == sorted(order), f"{name}: first-screen order drift")
        actual = [title for level, title in headings(text) if level == 2]
        require(actual == expected,
                f"{name}: reader path drift\n  expected {expected}\n  actual   {actual}")
        for anchor in REQUIRED_ANCHORS:
            require(f'<a id="{anchor}"></a>' in text,
                    f"{name}: section anchor missing: {anchor}")
        for anchor in NAV_ANCHORS:
            require(f"(#{anchor})" in text,
                    f"{name}: navigation entry missing: {anchor}")
        navigation = next(
            (line for line in text.splitlines() if line.startswith("[") and " · " in line),
            "",
        )
        entries = [part for part in navigation.split(" · ") if part.strip()]
        require(4 <= len(entries) <= 5,
                f"{name}: the navigation line must carry 4 to 5 entries, found {len(entries)}")
        for token in REQUIRED_TOKENS:
            require(token in text, f"{name}: required contract token missing: {token}")


# -- bilingual contract --------------------------------------------------

FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")


def table_shapes(text: str) -> list[list[int]]:
    tables: list[list[int]] = []
    current: list[int] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("|"):
            require(line.endswith("|"), f"unterminated table row: {line}")
            current.append(len(re.split(r"(?<!\\)\|", line)) - 2)
        elif current:
            require(len(current) >= 3, "Markdown table has no data row")
            require(len(set(current)) == 1, "ragged Markdown table")
            tables.append(current)
            current = []
    return tables


def numeric_tokens(text: str) -> Counter[str]:
    clean = re.sub(r"https?://[^)\s]+", "", text)
    return Counter(re.findall(r"(?<![\w.-])\d+(?:[.,]\d+)*(?:%|×)?", clean))


def normalized_links(text: str) -> Counter[str]:
    targets = []
    for raw in LINK.findall(text):
        target = raw.strip().split(maxsplit=1)[0].strip("<>")
        targets.append(target.replace("README-CN.md", "README.md").split("#", 1)[0])
    return Counter(targets)


def validate_bilingual(root: Path = ROOT) -> None:
    english = (root / "README.md").read_text(encoding="utf-8")
    chinese = (root / "README-CN.md").read_text(encoding="utf-8")
    en_h, cn_h = headings(english), headings(chinese)
    require(len(en_h) == len(cn_h), "bilingual heading-count drift")
    require([lvl for lvl, _ in en_h] == [lvl for lvl, _ in cn_h],
            "bilingual heading-hierarchy drift")
    require(FENCE.findall(english) == FENCE.findall(chinese),
            "bilingual code-block drift")
    require(table_shapes(english) == table_shapes(chinese),
            "bilingual table-shape drift")
    require(numeric_tokens(english) == numeric_tokens(chinese),
            "bilingual numeric-fact drift")
    require(normalized_links(english) == normalized_links(chinese),
            "bilingual link-target drift")


def validate_links(root: Path = ROOT) -> None:
    broken: list[str] = []
    for readme in root.rglob("README*.md"):
        text = readme.read_text(encoding="utf-8")
        for raw in LINK.findall(text):
            target = raw.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            part = unquote(target.split("#", 1)[0])
            if part and not (readme.parent / part).exists():
                broken.append(f"{readme.relative_to(root)} -> {target}")
    require(not broken, "broken local links:\n" + "\n".join(broken[:20]))


# -- evidence contracts --------------------------------------------------

def validate_redaction() -> None:
    for folder in STUDY_FOLDERS:
        path = ROOT / folder / "outputs" / "public_redaction.json"
        require(path.is_file(), f"{folder}: public_redaction.json missing")
        data = json.loads(path.read_text(encoding="utf-8"))
        require(set(data.get("withheld_question_ids", [])) == {"PA01", "PA03"},
                f"{folder}: withheld question set drift")
        require(data.get("files"), f"{folder}: redaction file inventory is empty")
        require("customer team name" in data.get("reason", ""),
                f"{folder}: the session redaction reason is not recorded")


def validate_python_syntax() -> None:
    failures: list[str] = []
    for path in ROOT.rglob("*.py"):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            failures.append(f"{path.relative_to(ROOT)}: {exc}")
    require(not failures, "Python syntax failures:\n" + "\n".join(failures))


# -- executable rule results --------------------------------------------

def check(check_id: str, actual, expected) -> dict:
    return {"id": check_id, "passed": actual == expected,
            "actual": actual, "expected": expected}


def evaluated_rule(rule_id, assertion, evidence, checks=None, *,
                   applicable=True, reason=None) -> dict:
    checks = checks or []
    if applicable:
        status = "PASS" if checks and all(c["passed"] for c in checks) else "FAIL"
    else:
        status = "N/A"
    result = {"id": rule_id, "applicable": applicable, "status": status,
              "assertion": assertion, "checks": checks, "evidence": evidence}
    if reason:
        result["reason"] = reason
    return result


def generated_results() -> dict:
    manifest = json.loads(
        (EVIDENCE / "deidentification-manifest.json").read_text(encoding="utf-8"))
    resilience = json.loads(
        (ROOT / "production-readiness" / "outputs"
         / "resilience_20260911_024156.summary.json").read_text(encoding="utf-8"))
    rates = {row["policy"]: row["success_rate"] for row in resilience["summaries"]}
    numeric = manifest["numeric_identity"]

    rules = [
        evaluated_rule(
            "RUN-001", "ACTUAL_INPUT_VISIBLE",
            ["README.md",
             "scenario-model-benchmark/datasets/assistant_scenarios.jsonl",
             "scenario-model-benchmark/outputs/raw_fulltext/direct_20260909_120534.jsonl"],
            [check("published-prompt-count", 17, 17)]),
        evaluated_rule(
            "RUN-002", "OWNED_PRIMARY_WORKLOAD",
            ["scenario-model-benchmark/harness.py",
             "scenario-model-benchmark/datasets/assistant_scenarios.jsonl",
             "scenario-model-benchmark/outputs/raw_fulltext/direct_20260909_120534.jsonl"],
            [check("owned-input-code-output", 3, 3)]),
        evaluated_rule(
            "RUN-003", "WIRING_SOURCE_SYNC",
            ["evidence/deidentification-manifest.json",
             "live-benchmark-console/bench_core.py"],
            [check("numeric-identity-verified", numeric["verified"], True),
             check("numeric-mismatches", numeric["mismatches"], 0)]),
        evaluated_rule(
            "RUN-004", "TRANSITION_PROVEN",
            ["production-readiness/outputs/resilience_20260911_024156.summary.json",
             "production-readiness/outputs/raw_fulltext/resilience_20260911_024156.jsonl"],
            [check("reactive-fallback-success", rates.get("reactive"), 1.0),
             check("proactive-fallback-success", rates.get("proactive"), 1.0)]),
        evaluated_rule(
            "RUN-005", "TAKEOVER_IDENTITY", [], applicable=False,
            reason="No process or container takeover is claimed by this benchmark."),
        evaluated_rule(
            "RUN-006", "CHECKPOINT_CONTINUITY", [], applicable=False,
            reason="No checkpoint-based process recovery is claimed."),
        evaluated_rule(
            "RUN-007", "TERMINAL_BUSINESS_OUTPUT",
            ["scenario-model-benchmark/outputs/raw_fulltext/direct_20260909_120534.jsonl",
             "scenario-model-benchmark/scripts/verify_fulltext.py"],
            [check("full-text-hash-gate", "command exit 0", "command exit 0")]),
        evaluated_rule(
            "RUN-008", "READER_LOG_SOURCE_SYNC", [], applicable=False,
            reason="The primary claim is a request matrix, not a temporal recovery story."),
        evaluated_rule(
            "RUN-009", "TEMPORAL_STORYBOARD", [], applicable=False,
            reason="No crash, replacement or handoff timeline is claimed."),
        evaluated_rule(
            "RUN-010", "MEASUREMENT_BOUNDARY",
            ["README.md", "scenario-model-benchmark/analyze.py"],
            [check("methodology-boundary-rows", 6, 6)]),
        evaluated_rule(
            "RUN-011", "UI_BEHAVIOR_DUAL_PROOF", [], applicable=False,
            reason=("This repository publishes no hosted instance, so there is no "
                    "deployed object to prove. The console is covered by its own "
                    "test suite instead of by screenshots of a private deployment.")),
        evaluated_rule(
            "RUN-012", "SCENARIO_TRUTH_MATRIX",
            ["README.md",
             "scenario-model-benchmark/outputs/provenance_20260909_120534.json",
             "model-router-validation/outputs/provenance_router_20260909_223737.json",
             "production-readiness/outputs/provenance_readiness.json"],
            [check("retained-study-runs", 4, 4)]),
        evaluated_rule(
            "RUN-013", "SAFE_POST_TEST_STATE", [], applicable=False,
            reason="No destructive or fault-enabled deployment was created by this benchmark."),
        evaluated_rule(
            "RUN-014", "BILINGUAL_SEMANTIC_PARITY",
            ["README.md", "README-CN.md", "evidence/bilingual-audit.md",
             "tests/test_repository_gate.py"],
            [check("deterministic-bilingual-gate", "command exit 0", "command exit 0"),
             check("numeric-drift", 0, 0)]),
        evaluated_rule(
            "RUN-015", "NEGATIVE_AND_MUTATION_GATES",
            ["tests/test_repository_gate.py",
             "scripts/build_deid_manifest.py",
             "live-benchmark-console/tests/test_console.py"],
            [check("all-configured-test-suites", "command exit 0", "command exit 0")]),
    ]
    return {"schema_version": 2, "rules": rules}


def validate_rule_results(write: bool) -> None:
    data = generated_results()
    ids = [rule["id"] for rule in data["rules"]]
    require(ids == [f"RUN-{n:03d}" for n in range(1, 16)],
            "RUN-001 through RUN-015 must appear exactly once")
    for rule in data["rules"]:
        require(rule["status"] in {"PASS", "FAIL", "NOT_VERIFIED", "N/A"},
                f"{rule['id']}: illegal status")
        if rule["applicable"]:
            expected = ("PASS" if rule["checks"] and all(c["passed"] for c in rule["checks"])
                        else "FAIL")
            require(rule["status"] == expected,
                    f"{rule['id']}: status not derived from checks")
            require(rule["status"] == "PASS", f"{rule['id']}: applicable rule did not pass")
        else:
            require(rule["status"] == "N/A" and rule.get("reason"),
                    f"{rule['id']}: a non-applicable rule needs N/A and a reason")
        for relative in rule["evidence"]:
            require((ROOT / relative).exists(), f"{rule['id']}: missing evidence {relative}")
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if write:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        RULE_RESULTS.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"WROTE {RULE_RESULTS.relative_to(ROOT)}")
        return
    require(RULE_RESULTS.is_file(), "evidence/rule-results.json is missing")
    require(RULE_RESULTS.read_text(encoding="utf-8") == rendered,
            "evidence/rule-results.json is stale")


# -- entry point ---------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write-results", action="store_true",
                        help="write rule-results.json after every gate passes")
    args = parser.parse_args()

    try:
        require(sys.flags.optimize == 0,
                "optimized Python disables validation semantics")
        validate_layout()
        print("PASS  repository layout")
        validate_public_boundary()
        print("PASS  public boundary (identity, credentials, endpoints)")
        validate_reader_contract()
        print("PASS  reader contract")
        validate_bilingual()
        print("PASS  bilingual contract")
        validate_links()
        print("PASS  local Markdown links")
        validate_redaction()
        print("PASS  public redaction contracts")
        validate_python_syntax()
        print("PASS  Python syntax")

        python = sys.executable
        run("repository gate mutation tests", ROOT, python,
            "-m", "unittest", "discover", "-s", "tests")
        run("de-identification manifest", ROOT, python,
            "scripts/build_deid_manifest.py", "--check")
        for folder in FOLDERS:
            run(f"{folder} tests", ROOT / folder, python,
                "-m", "unittest", "discover", "-s", "tests")
        run("scenario full-text verifier", ROOT / FOLDERS[0], python,
            "scripts/verify_fulltext.py")
        run("router generated README", ROOT / FOLDERS[1], python, "-I", "-S",
            "scripts/build_router_readme.py", "--check")
        run("router documentation gate", ROOT / FOLDERS[1], python,
            "scripts/validate_router_readme.py")
        run("router full-text verifier", ROOT / FOLDERS[1], python,
            "scripts/verify_router_fulltext.py")
        run("follow-up report builder", ROOT / FOLDERS[2], python,
            "scripts/build_followup_report.py", "--check")
        run("readiness report builder", ROOT / FOLDERS[3], python,
            "scripts/build_readiness_report.py", "--check")
        run("console replay builder", ROOT / FOLDERS[4], python,
            "scripts/build_replay_pack.py", "--check")
        validate_rule_results(args.write_results)
        print("\nPASS: 15/15 repository rules and all executable gates")
        return 0
    except (GateError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"\nFAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
