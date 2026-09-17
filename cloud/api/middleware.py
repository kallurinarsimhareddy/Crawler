"""Request-level protections: rate limits, security headers, structured access logs.

**Rate limits** are token buckets held in this process:

* per client IP, for every request (``CAREERCLOUD_RATE_LIMIT_PER_MINUTE``);
* per signed-in user, for job creation (``CAREERCLOUD_JOB_CREATE_PER_HOUR``),
  enforced in the route. The per-user active-job cap from Phase 5B still applies
  on top.

They stop a single client from flooding one API process. Put the Cloudflare rate
limiting rule described in the README in front of them, since in-process buckets
are per process and reset on restart.

**Client IP.** Behind Cloudflare Tunnel every connection comes from 127.0.0.1,
so the real client is taken from ``CF-Connecting-IP``, but only when the peer
is loopback and ``CAREERCLOUD_TRUST_PROXY=cloudflare``. Otherwise the header is
ignored, so it cannot be spoofed.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable, Dict, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

__all__ = ["AccessLogMiddleware", "RateLimiter", "RateLimitMiddleware", "SecurityHeadersMiddleware", "client_ip"]

access_log = logging.getLogger("cloud.api.access")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class RateLimiter:
    """Token buckets keyed by string. Thread-safe; memory bounded by pruning idle keys."""

    def __init__(self, *, capacity: int, refill_per_second: float, clock: Callable[[], float] = time.monotonic) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("capacity must be >= 1 and refill rate positive")
        self._capacity = float(capacity)
        self._rate = refill_per_second
        self._clock = clock
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._last_prune = clock()

    def allow(self, key: str) -> Tuple[bool, float]:
        """Take a token. Returns ``(allowed, seconds_until_next_token)``."""
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - updated) * self._rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                allowed, wait = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed, wait = False, (1 - tokens) / self._rate
            if now - self._last_prune > 300:
                idle = self._capacity / self._rate
                self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < idle}
                self._last_prune = now
        return allowed, wait


def client_ip(request: Request, trust_proxy: str) -> str:
    peer = request.client.host if request.client else "unknown"
    if trust_proxy == "cloudflare" and peer in _LOOPBACK:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if forwarded:
            return forwarded
    return peer


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limiter: RateLimiter, trust_proxy: str, exempt_paths=("/api/v1/health",)) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._trust = trust_proxy
        self._exempt = frozenset(exempt_paths)

    async def dispatch(self, request: Request, call_next):
        if request.method != "OPTIONS" and request.url.path not in self._exempt:
            allowed, wait = self._limiter.allow(f"ip:{client_ip(request, self._trust)}")
            if not allowed:
                return JSONResponse(
                    {"detail": "too many requests; slow down"},
                    status_code=429,
                    headers={"Retry-After": str(max(1, int(wait + 0.999)))},
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, hsts: bool) -> None:
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        headers.setdefault("Cache-Control", "no-store")
        if self._hsts:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, trust_proxy: str) -> None:
        super().__init__(app)
        self._trust = trust_proxy

    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex[:16]
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            route = request.scope.get("route")
            access_log.info(
                "%s %s %s",
                request.method,
                getattr(route, "path", request.url.path),
                status,
                extra={
                    "fields": {
                        "request_id": request_id,
                        "method": request.method,
                        "route": getattr(route, "path", None),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "client_ip": client_ip(request, self._trust),
                        "user_id": getattr(request.state, "user_id", None),
                    }
                },
            )
