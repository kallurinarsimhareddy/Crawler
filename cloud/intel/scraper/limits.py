"""Bounds for a run: per-domain concurrency and request budgets, backoff, and the
run-wide budget (runtime, records, browser pages, AI calls).

Everything here is thread-safe: a run crawls several input URLs at once.

Retry policy (per request, :func:`is_transient`): only HTTP 408/429/500/502/503/504,
timeouts and connection errors are retried, at most ``max_retries`` times, with
exponential backoff capped at ``max_backoff_s``. A ``Retry-After`` longer than
the cap is not waited out — the page is reported as ``RATE_LIMITED``. Refusals
(401/403/404, robots, CAPTCHA, WAF, login, unsafe) are never retried.
"""

from __future__ import annotations

import re
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit

__all__ = ["DomainLimiter", "RunBudget", "TRANSIENT_STATUSES", "backoff_delay", "domain_key", "is_transient",
           "retry_after_seconds"]

TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_TRANSIENT_ERROR = re.compile(r"timeout|timed out|connection(?:error| reset| aborted| refused)|remotedisconnected"
                              r"|chunkedencodingerror|temporar", re.I)


def domain_key(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_transient(status: int, error: Optional[str]) -> bool:
    if error:
        return bool(_TRANSIENT_ERROR.search(error)) and not error.startswith(("unsafe target", "robots"))
    return status in TRANSIENT_STATUSES


def retry_after_seconds(headers: Optional[Mapping[str, Any]]) -> Optional[float]:
    value = None
    for key, item in dict(headers or {}).items():
        if str(key).lower() == "retry-after":
            value = str(item).strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 30.0, retry_after: Optional[float] = None) -> float:
    """Seconds to wait before retry number ``attempt`` (1-based)."""
    delay = min(cap, base * (2 ** (attempt - 1)))
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


class DomainLimiter:
    """At most ``concurrency`` requests in flight and ``max_requests`` in total per domain,
    plus a per-domain pause after a 429/503 so no domain is hammered."""

    def __init__(self, *, concurrency: int = 2, max_requests: int = 300,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.concurrency = max(1, concurrency)
        self.max_requests = max_requests
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._slots: Dict[str, threading.Semaphore] = {}
        self._count: Dict[str, int] = {}
        self._pause_until: Dict[str, float] = {}

    def requests(self, domain: str) -> int:
        with self._lock:
            return self._count.get(domain, 0)

    def acquire(self, url: str) -> bool:
        """Take a slot for ``url``'s domain. ``False`` when the domain's request budget is spent."""
        domain = domain_key(url)
        with self._lock:
            if self._count.get(domain, 0) >= self.max_requests:
                return False
            self._count[domain] = self._count.get(domain, 0) + 1
            slot = self._slots.setdefault(domain, threading.Semaphore(self.concurrency))
            wait = self._pause_until.get(domain, 0.0) - self._clock()
        slot.acquire()
        if wait > 0:
            self._sleep(wait)
        return True

    def release(self, url: str) -> None:
        with self._lock:
            slot = self._slots.get(domain_key(url))
        if slot is not None:
            slot.release()

    def pause(self, url: str, seconds: float) -> None:
        """Hold every request to ``url``'s domain for ``seconds`` (after a 429/503)."""
        domain = domain_key(url)
        with self._lock:
            self._pause_until[domain] = max(self._pause_until.get(domain, 0.0), self._clock() + seconds)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._count)


class RunBudget:
    """Run-wide limits shared by every crawl thread."""

    def __init__(self, *, max_runtime_s: float, max_records: int, max_browser_pages: int,
                 clock: Callable[[], float] = time.monotonic, already_records: int = 0) -> None:
        self._clock = clock
        self.deadline = clock() + max_runtime_s
        self.max_records = max_records
        self.max_browser_pages = max_browser_pages
        self._lock = threading.Lock()
        self.records = already_records
        self.browser_pages = 0
        self.requests = 0
        self.stop = threading.Event()   # set on cancel/pause: crawl threads stop at the next page
        self.reason: Optional[str] = None

    def expired(self) -> bool:
        return self._clock() >= self.deadline

    def exhausted(self) -> Optional[str]:
        """Why no more work may start, or ``None``."""
        if self.stop.is_set():
            return self.reason or "stopped"
        if self.expired():
            return "the run's runtime limit was reached"
        with self._lock:
            if self.records >= self.max_records:
                return f"the run's record limit ({self.max_records}) was reached"
        return None

    def add_records(self, count: int) -> int:
        """Reserve room for ``count`` records; returns how many fit."""
        with self._lock:
            room = max(0, self.max_records - self.records)
            taken = min(room, count)
            self.records += taken
            return taken

    def take_browser_page(self) -> bool:
        with self._lock:
            if self.browser_pages >= self.max_browser_pages:
                return False
            self.browser_pages += 1
            return True

    def count_request(self) -> None:
        with self._lock:
            self.requests += 1
