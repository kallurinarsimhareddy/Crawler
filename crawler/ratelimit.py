"""How fast one site may be asked, however many workers are running.

The distinction this exists to enforce: **workers are concurrent companies, not
concurrent requests to a company.** Twenty workers on a sheet where a hundred
and thirty companies share ``myworkdayjobs.com`` means twenty simultaneous
requests to Workday unless something says otherwise. That is both rude and the
fastest way to be rate-limited into failing all hundred and thirty.

    >>> limiter = DomainLimiter(RateLimitConfig(min_delay=1.0, max_concurrent=2))
    >>> with limiter.hold("https://acme.wd1.myworkdayjobs.com/External"):
    ...     pass                      # at most two of these run at once

Two independent controls, because they answer different questions:

* ``max_concurrent`` — how many requests to one domain may be *in flight*.
* ``min_delay`` — how long between two requests *starting* on one domain.

A domain is the crawler's grouping, not a public-suffix answer: ``acme.wd1``
and ``other.wd3`` on ``myworkdayjobs.com`` are one vendor and are paced as one.
Unrelated domains never wait for each other, which is what keeps concurrency
useful.

This generalises the per-host delay :class:`crawler.crawler_engine._HostThrottle`
already applies. That one paces; this one also bounds concurrency and can be
told to back a whole domain off after a 429.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Final, Iterator, Optional
from urllib.parse import urlsplit

from loguru import logger

__all__ = ["DomainLimiter", "RateLimitConfig"]

#: Vendor domains where many unrelated companies share one host. Grouping by
#: the registrable domain rather than the full hostname is what stops two
#: hundred Workday tenants being treated as two hundred independent sites.
_SHARED_VENDORS: Final[tuple] = (
    "myworkdayjobs.com", "myworkdaysite.com", "icims.com", "greenhouse.io",
    "lever.co", "adp.com", "ultipro.com", "paycomonline.net", "paylocity.com",
    "dayforcehcm.com", "taleo.net", "successfactors.com", "smartrecruiters.com",
    "jobvite.com", "bamboohr.com", "ashbyhq.com", "workable.com", "csod.com",
    "eightfold.ai", "avature.net", "peopleadmin.com", "applicantpro.com",
    "saashr.com", "isolvedhire.com", "recruitingbypaycor.com",
)


@dataclass(frozen=True)
class RateLimitConfig:
    """How hard one domain may be pushed.

    Attributes:
        min_delay: Minimum seconds between two requests starting on one
            domain. ``0`` paces nothing.
        max_concurrent: Requests to one domain that may be in flight at once.
        burst: Requests allowed to ignore ``min_delay`` when a domain has been
            idle, so a single company is not slowed for no reason.
    """

    min_delay: float = 1.0
    max_concurrent: int = 2
    burst: int = 1


class _Domain:
    """One domain's gate.

    Args:
        config: The policy to enforce.
    """

    def __init__(self, config: RateLimitConfig) -> None:
        self.semaphore = threading.BoundedSemaphore(max(1, config.max_concurrent))
        self.lock = threading.Lock()
        self.next_free = 0.0
        self.tokens = max(0, config.burst)


class DomainLimiter:
    """Paces requests per domain, across every worker thread.

    Args:
        config: The policy applied to every domain.
    """

    def __init__(self, config: Optional[RateLimitConfig] = None) -> None:
        self.config = config or RateLimitConfig()
        self._domains: Dict[str, _Domain] = {}
        self._lock = threading.Lock()
        self._requests = 0
        self._throttled = 0
        self._waited = 0.0

    # -- naming --------------------------------------------------------------

    @staticmethod
    def domain_of(url: str) -> str:
        """The key a URL is paced under.

        Args:
            url: The URL about to be requested.

        Returns:
            The vendor domain when the host belongs to one, else the hostname.
            ``""`` for anything unparseable, which is never throttled.
        """
        raw = str(url or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = f"https://{raw}"

        try:
            host = (urlsplit(raw).hostname or "").lower()
        except ValueError:
            return ""

        if not host:
            return ""

        for vendor in _SHARED_VENDORS:
            if host == vendor or host.endswith(f".{vendor}"):
                return vendor

        return host[4:] if host.startswith("www.") else host

    def _gate(self, domain: str) -> _Domain:
        """This domain's gate, created on first sight.

        Args:
            domain: The domain key.

        Returns:
            Its gate.
        """
        with self._lock:
            gate = self._domains.get(domain)
            if gate is None:
                gate = _Domain(self.config)
                self._domains[domain] = gate
            return gate

    # -- the gate ------------------------------------------------------------

    @contextmanager
    def hold(self, url: str) -> Iterator[None]:
        """Wait for permission to request ``url``, then hold it for the call.

        Args:
            url: The URL about to be requested.

        Yields:
            Nothing; the block runs once the domain allows it.
        """
        domain = self.domain_of(url)
        if not domain:
            yield
            return

        gate = self._gate(domain)
        gate.semaphore.acquire()
        try:
            self._pace(gate)
            with self._lock:
                self._requests += 1
            yield
        finally:
            gate.semaphore.release()

    def _pace(self, gate: _Domain) -> None:
        """Block until this domain's next slot, then reserve the one after.

        Args:
            gate: The domain's gate.
        """
        # Deliberately not short-circuited on min_delay: back_off() sets
        # next_free directly, and a domain that asked us to slow down must be
        # honoured even when no steady-state pacing is configured.
        while True:
            with gate.lock:
                now = time.monotonic()

                if gate.tokens > 0 and now >= gate.next_free:
                    # An idle domain lets a burst through unslowed.
                    gate.tokens -= 1
                    gate.next_free = now + max(0.0, self.config.min_delay)
                    return

                if now >= gate.next_free:
                    gate.next_free = now + max(0.0, self.config.min_delay)
                    return

                wait = gate.next_free - now

            with self._lock:
                self._throttled += 1
                self._waited += wait
            time.sleep(wait)

    def back_off(self, url: str, seconds: float) -> None:
        """Defer a whole domain, after it asked us to slow down.

        A 429 is about the site, not the company: every other company on that
        vendor should feel it too, or the next twenty workers walk into the
        same wall.

        Args:
            url: Any URL on the domain.
            seconds: How long to defer it.
        """
        domain = self.domain_of(url)
        if not domain or seconds <= 0:
            return

        gate = self._gate(domain)
        with gate.lock:
            gate.next_free = max(gate.next_free, time.monotonic() + float(seconds))
            gate.tokens = 0
        logger.info("Backing off {} for {:.0f}s", domain, seconds)

    # -- reporting -----------------------------------------------------------

    def stats(self) -> Dict[str, float]:
        """What the limiter has done.

        Returns:
            Counts and total seconds spent waiting.
        """
        with self._lock:
            return {
                "domains": len(self._domains),
                "requests": self._requests,
                "throttled": self._throttled,
                "seconds_waiting": round(self._waited, 2),
            }
