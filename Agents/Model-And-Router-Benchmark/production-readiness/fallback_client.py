#!/usr/bin/env python3
"""
Deterministic primary → fallback client with the semantics of the APIM policy in
config/apim-policy-ptu-routing.reference.xml, implemented in-process so it can be
measured by harness.run_one without standing up API Management.

Two mechanisms, matching the policy's two branches:

  reactive   (<retry condition="429">)  — if the current backend answers 429, a 5xx,
             a connection error or a timeout AT REQUEST TIME, the next backend in the
             chain is tried immediately (first-fast-retry). Retry-After is recorded,
             not slept on: the point of a fallback is not to wait.
             Measured on Azure OpenAI (2026-09-11): on the streaming Responses path a
             DataZoneStandard deployment reports its rate limit as an in-stream error
             event after an HTTP 200, not as a 429. The returned stream is therefore
             wrapped: an error raised BEFORE the first content delta also triggers the
             fallback (nothing has reached the user yet, so re-requesting is safe); an
             error after content has been delivered is not retried (it would duplicate
             output) and remains a recorded error. APIM's status-code retry would miss
             the in-stream case — see README section 3.
  proactive  (<cache-lookup-value key="ptu-utilization">) — after every successful
             primary response the x-ratelimit-remaining-* headers are read; when the
             worst of request/token utilization is at or above --threshold, the NEXT
             request is routed straight to the fallback without touching the primary.

The object exposes .responses.create and .chat.completions.create, so it is a
drop-in for the AzureOpenAI client inside harness.run_one. The decision taken for
the most recent call on the current thread is available from last_decision(), so
a runner can attach it to the measurement record.

Mid-stream failures after content has been delivered are not retried (neither does
APIM's retry policy); they remain recorded errors.

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, RateLimitError

_STATE = threading.local()
RATE_LIMIT_MARKERS = ("exceeded rate limit", "rate limit", "429")


@dataclass
class Decision:
    chain: list[str]
    policy: str
    served_by: str | None = None
    attempts: list[dict] = field(default_factory=list)
    proactive_skip: bool = False
    utilization_before: float | None = None
    fallback_overhead_ms: float = 0.0
    exhausted: bool = False
    in_stream_fallbacks: int = 0


def last_decision() -> Decision | None:
    return getattr(_STATE, "decision", None)


def is_rate_limit(exc) -> bool:
    if isinstance(exc, RateLimitError) or getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def _has_content(event) -> bool:
    """Same detection harness.run_one uses for the first text delta, on either surface."""
    if getattr(event, "type", None) == "response.output_text.delta":
        return bool(getattr(event, "delta", None))
    for choice in getattr(event, "choices", None) or []:
        delta = getattr(choice, "delta", None)
        if delta is not None and getattr(delta, "content", None):
            return True
    return False


def _retry_after(exc) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("x-ratelimit-reset-requests")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _utilization(headers) -> float | None:
    """Worst-case utilization from the rate-limit headers.

    Extends the reference APIM policy, which reads x-ratelimit-*-tokens only: on an RPM-bound
    deployment token utilization can sit near 10 % while requests are at 90 %, so both are read
    and the worse one drives the proactive decision.
    """
    if not headers:
        return None
    worst = None
    for kind in ("requests", "tokens"):
        remaining = headers.get(f"x-ratelimit-remaining-{kind}")
        limit = headers.get(f"x-ratelimit-limit-{kind}")
        if remaining is None or limit is None:
            continue
        try:
            remaining_f, limit_f = float(remaining), float(limit)
        except ValueError:
            continue
        if limit_f <= 0:
            continue
        used = max(0.0, min(1.0, (limit_f - remaining_f) / limit_f))
        worst = used if worst is None else max(worst, used)
    return worst


class _Surface:
    """Wraps one API surface (responses or chat.completions) of the underlying client."""

    def __init__(self, owner, path):
        self._owner = owner
        self._path = path  # ("responses",) or ("chat", "completions")

    def _resource(self, client):
        obj = client
        for part in self._path:
            obj = getattr(obj, part)
        return obj

    def create(self, **kwargs):
        return self._owner._create(self._resource, kwargs)


class _Chat:
    def __init__(self, owner):
        self.completions = _Surface(owner, ("chat", "completions"))


class FallbackClient:
    def __init__(self, client, chain: list[str], policy: str = "reactive", threshold: float = 0.8, utilization_ttl_s: float = 60.0):
        if len(chain) < 1:
            raise ValueError("chain needs at least one deployment")
        if policy not in ("reactive", "proactive", "none"):
            raise ValueError("policy must be reactive, proactive or none")
        self._client = client
        self.chain = list(chain)
        self.policy = policy
        self.threshold = threshold
        self.utilization_ttl_s = utilization_ttl_s  # APIM: cache-store-value duration="60"
        self._lock = threading.Lock()
        self._utilization = 0.0  # analog of the APIM cached "ptu-utilization"
        self._utilization_at = 0.0
        self.responses = _Surface(self, ("responses",))
        self.chat = _Chat(self)
        self.max_retries = getattr(client, "max_retries", None)

    def _set_utilization(self, value: float):
        with self._lock:
            self._utilization = value
            self._utilization_at = time.monotonic()

    @property
    def utilization(self) -> float:
        with self._lock:
            if time.monotonic() - self._utilization_at > self.utilization_ttl_s:
                return 0.0  # cache entry expired: probe the primary again, like APIM's default-value="0"
            return self._utilization

    def _create(self, resource, kwargs):
        decision = Decision(chain=list(self.chain), policy=self.policy)
        _STATE.decision = decision
        order = list(range(len(self.chain)))
        if self.policy == "proactive" and len(self.chain) > 1:
            decision.utilization_before = self.utilization
            if decision.utilization_before >= self.threshold:
                decision.proactive_skip = True
                order = order[1:]  # route straight to the fallback, like set-backend-service paygo-backend
        if self.policy == "none":
            order = order[:1]
        return _FallbackStream(self, resource, kwargs, order, decision)

    def _open(self, resource, kwargs, idx, attempt):
        deployment = self.chain[idx]
        if self.policy == "proactive" and idx == 0:
            raw = resource(self._client).with_raw_response.create(**{**kwargs, "model": deployment})
            util = _utilization(raw.headers)
            if util is not None:
                self._set_utilization(util)
            attempt["utilization_after"] = util
            attempt["ratelimit_headers"] = {k: v for k, v in raw.headers.items() if k.lower().startswith("x-ratelimit")} or None
            return raw.parse()
        return resource(self._client).create(**{**kwargs, "model": deployment})

    def _note_failure(self, exc, idx, attempt, phase):
        status = getattr(exc, "status_code", None)
        attempt["status"] = status or type(exc).__name__
        attempt["phase"] = phase
        attempt["rate_limit"] = is_rate_limit(exc)
        attempt["retry_after_s"] = _retry_after(exc)
        attempt["error"] = f"{type(exc).__name__}: {exc}"[:200]
        if attempt["rate_limit"] and idx == 0:
            self._set_utilization(1.0)  # APIM: cache-store-value ptu-utilization=100 on 429
        retryable = isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError)) or attempt["rate_limit"] \
            or (status is not None and status >= 500) or (phase == "stream" and isinstance(exc, APIError) and status is None)
        return retryable


class _FallbackStream:
    """Iterator over one backend's stream that switches backend on a failure seen before any content."""

    def __init__(self, owner, resource, kwargs, order, decision):
        self._owner, self._resource, self._kwargs, self._order, self._decision = owner, resource, kwargs, order, decision
        self._position = 0
        self._stream = None
        self._delivered = False
        self._start = time.perf_counter()

    def __iter__(self):
        return self

    def _open_current(self):
        owner, decision = self._owner, self._decision
        while self._position < len(self._order):
            idx = self._order[self._position]
            attempt = {"deployment": owner.chain[idx], "position": idx}
            try:
                self._stream = owner._open(self._resource, self._kwargs, idx, attempt)
                attempt["status"] = 200
                decision.attempts.append(attempt)
                decision.served_by = owner.chain[idx]
                decision.fallback_overhead_ms = round((time.perf_counter() - self._start) * 1000, 1) if self._position else 0.0
                return
            except (RateLimitError, APIStatusError, APITimeoutError, APIConnectionError) as exc:
                retryable = owner._note_failure(exc, idx, attempt, "request")
                decision.attempts.append(attempt)
                self._position += 1
                if not retryable or self._position >= len(self._order):
                    decision.exhausted = True
                    decision.fallback_overhead_ms = round((time.perf_counter() - self._start) * 1000, 1)
                    raise
        decision.exhausted = True
        raise RuntimeError("fallback chain exhausted")

    def __next__(self):
        if self._stream is None:
            self._open_current()
        while True:
            try:
                event = next(self._stream)
            except StopIteration:
                raise
            except APIError as exc:
                if self._delivered:
                    raise  # content already reached the user; re-requesting would duplicate output
                idx = self._order[self._position]
                attempt = self._decision.attempts[-1]
                retryable = self._owner._note_failure(exc, idx, attempt, "stream")
                self._position += 1
                if not retryable or self._position >= len(self._order):
                    self._decision.exhausted = True
                    self._decision.fallback_overhead_ms = round((time.perf_counter() - self._start) * 1000, 1)
                    raise
                self._decision.in_stream_fallbacks += 1
                self._stream = None
                self._open_current()
                continue
            if not self._delivered and _has_content(event):
                self._delivered = True
            return event


def build_fallback_client(chain: list[str], policy: str, threshold: float = 0.8, utilization_ttl_s: float = 60.0):
    """Build on top of harness.build_client so auth, endpoint and max_retries=0 stay identical."""
    import harness
    base, endpoint = harness.build_client()
    return FallbackClient(base, chain, policy, threshold, utilization_ttl_s), endpoint
