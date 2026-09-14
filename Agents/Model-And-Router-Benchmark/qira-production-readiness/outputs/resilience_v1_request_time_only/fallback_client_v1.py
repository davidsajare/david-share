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
  proactive  (<cache-lookup-value key="ptu-utilization">) — after every successful
             primary response the x-ratelimit-remaining-* headers are read; when the
             worst of request/token utilization is at or above --threshold, the NEXT
             request is routed straight to the fallback without touching the primary.

The object exposes .responses.create and .chat.completions.create, so it is a
drop-in for the AzureOpenAI client inside harness.run_one. The decision taken for
the most recent call on the current thread is available from last_decision(), so
a runner can attach it to the measurement record.

Mid-stream failures are not retried (neither does APIM's retry policy); they remain
recorded errors.

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

_STATE = threading.local()


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


def last_decision() -> Decision | None:
    return getattr(_STATE, "decision", None)


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
    """Worst-case utilization from the rate-limit headers, as the APIM policy computes it."""
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
    def __init__(self, client, chain: list[str], policy: str = "reactive", threshold: float = 0.8):
        if len(chain) < 1:
            raise ValueError("chain needs at least one deployment")
        if policy not in ("reactive", "proactive", "none"):
            raise ValueError("policy must be reactive, proactive or none")
        self._client = client
        self.chain = list(chain)
        self.policy = policy
        self.threshold = threshold
        self._lock = threading.Lock()
        self._utilization = 0.0  # analog of the APIM cached "ptu-utilization"
        self.responses = _Surface(self, ("responses",))
        self.chat = _Chat(self)
        self.max_retries = getattr(client, "max_retries", None)

    @property
    def utilization(self) -> float:
        with self._lock:
            return self._utilization

    def _create(self, resource, kwargs):
        decision = Decision(chain=list(self.chain), policy=self.policy)
        _STATE.decision = decision
        start = time.perf_counter()
        order = list(range(len(self.chain)))
        if self.policy == "proactive" and len(self.chain) > 1:
            with self._lock:
                decision.utilization_before = self._utilization
            if decision.utilization_before is not None and decision.utilization_before >= self.threshold:
                decision.proactive_skip = True
                order = order[1:]  # route straight to the fallback, like set-backend-service paygo-backend
        if self.policy == "none":
            order = order[:1]

        last_exc = None
        for position, idx in enumerate(order):
            deployment = self.chain[idx]
            attempt = {"deployment": deployment, "position": idx}
            try:
                if self.policy == "proactive" and idx == 0:
                    raw = resource(self._client).with_raw_response.create(**{**kwargs, "model": deployment})
                    util = _utilization(raw.headers)
                    if util is not None:
                        with self._lock:
                            self._utilization = util
                    attempt["utilization_after"] = util
                    stream = raw.parse()
                else:
                    stream = resource(self._client).create(**{**kwargs, "model": deployment})
                attempt["status"] = 200
                decision.attempts.append(attempt)
                decision.served_by = deployment
                decision.fallback_overhead_ms = round((time.perf_counter() - start) * 1000, 1) if position else 0.0
                return stream
            except (RateLimitError, APIStatusError, APITimeoutError, APIConnectionError) as exc:
                status = getattr(exc, "status_code", None)
                attempt["status"] = status or type(exc).__name__
                attempt["retry_after_s"] = _retry_after(exc)
                decision.attempts.append(attempt)
                last_exc = exc
                retryable = isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError)) or (status is not None and status >= 500)
                if isinstance(exc, RateLimitError) and idx == 0:
                    with self._lock:
                        self._utilization = 1.0  # APIM: cache-store-value ptu-utilization=100 on 429
                if not retryable or position == len(order) - 1:
                    break
        decision.exhausted = True
        decision.fallback_overhead_ms = round((time.perf_counter() - start) * 1000, 1)
        raise last_exc


def build_fallback_client(chain: list[str], policy: str, threshold: float = 0.8):
    """Build on top of harness.build_client so auth, endpoint and max_retries=0 stay identical."""
    import harness
    base, endpoint = harness.build_client()
    return FallbackClient(base, chain, policy, threshold), endpoint
