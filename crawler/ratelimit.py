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

**A gate is one hostname**, not one vendor. That is the opposite of what this
module originally assumed, and the ledger is what changed the answer: of 5,396
distinct hostnames the crawler has seen, 608 are Workday tenants —
``marmon.wd501``, ``flir.wd1``, ``jj.wd5`` and 605 more — carrying 161,409
postings, 45% of everything. Grouping those into one gate would put nearly half
the roster behind a single semaphore.

Hostname-level limiting still protects the vendors that genuinely pool many
companies onto one host, because those really are one host:
``jobs.smartrecruiters.com`` (22,328 postings), ``recruiting2.ultipro.com``
(11,717), ``workforcenow.adp.com`` (9,336), ``job-boards.greenhouse.io``
(8,638). They get one gate because they *are* one; Workday's tenants stay
parallel because they are not. Vendor grouping remains available behind
``RateLimitConfig.group_shared_vendors`` for anyone with evidence it is wanted.

This complements the per-host delay
:class:`crawler.crawler_engine._HostThrottle` already applies. That one spaces
out *companies*; this one bounds concurrent *requests* and can back a whole
host off after a 429. Run with ``min_delay=0`` at the HTTP layer so the two do
not stack.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Final, Iterator, List, Optional
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
            domain. ``0`` paces nothing, which is what the HTTP layer wants:
            :class:`crawler.crawler_engine._HostThrottle` already spaces
            companies out per host, and pacing every *request* on top of that
            would slow every paginated adapter for no further protection.
        max_concurrent: Requests to one domain that may be in flight at once.
        burst: Requests allowed to ignore ``min_delay`` when a domain has been
            idle, so a single company is not slowed for no reason.
        group_shared_vendors: Whether hosts belonging to one vendor share a
            gate. **Off**, and the ledger is why.

            Grouping sounds right and measures wrong. Of 5,396 distinct
            hostnames the crawler has seen, Workday accounts for 608 of them —
            ``marmon.wd501``, ``flir.wd1``, ``jj.wd5`` and 605 more, 161,409
            postings between them, 45% of everything. Collapsing those into one
            gate would put nearly half the roster behind a single semaphore.

            Hostname-level limiting already protects the vendors that actually
            pool companies onto one host, because they genuinely share one:
            ``jobs.smartrecruiters.com`` (22,328 postings),
            ``recruiting2.ultipro.com`` (11,717), ``workforcenow.adp.com``
            (9,336), ``job-boards.greenhouse.io`` (8,638). Those get one gate
            because they *are* one host, and Workday's tenants stay parallel
            because they are not.
    """

    min_delay: float = 1.0
    max_concurrent: int = 2
    burst: int = 1
    group_shared_vendors: bool = False


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

        #: What this host cost. ``peak`` is the number that says whether the
        #: cap ever bound — a limit nothing reached is a limit doing nothing,
        #: and at this scale that is worth being able to see per host rather
        #: than guessing from an aggregate.
        self.active = 0
        self.peak = 0
        self.requests = 0
        self.waits = 0
        self.waited = 0.0


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

        # Concurrency accounting, distinct from the pacing counters above:
        # `_throttled`/`_waited` are about min_delay, these are about the
        # semaphore. With min_delay=0 -- which is how the HTTP layer runs it --
        # only these ever move.
        self._active = 0
        self._peak = 0
        self._waits = 0
        self._waited_on_slot = 0.0

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

    def key_for(self, url: str) -> str:
        """The gate this URL belongs to, honouring the configuration.

        :meth:`domain_of` is unchanged and still groups shared vendors, because
        it is a published helper and something may rely on it. This is what the
        limiter itself uses, and it groups only when asked to.

        Args:
            url: The URL about to be requested.

        Returns:
            The gate key, or ``""`` for anything unparseable.
        """
        if self.config.group_shared_vendors:
            return self.domain_of(url)

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
        domain = self.key_for(url)
        if not domain:
            yield
            return

        gate = self._gate(domain)

        # Timed, because "waited" is the only honest evidence that the cap
        # bound anything. A semaphore that is never contended costs nothing and
        # should report nothing.
        started = time.monotonic()
        gate.semaphore.acquire()
        waited = time.monotonic() - started

        try:
            self._pace(gate)
            with gate.lock:
                gate.active += 1
                gate.peak = max(gate.peak, gate.active)
                gate.requests += 1
                if waited > 0:
                    gate.waits += 1
                    gate.waited += waited
            with self._lock:
                self._requests += 1
                self._active += 1
                self._peak = max(self._peak, self._active)
                if waited > 0:
                    self._waits += 1
                    self._waited_on_slot += waited
            yield
        finally:
            with gate.lock:
                gate.active -= 1
            with self._lock:
                self._active -= 1
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
                # Concurrency, as opposed to pacing.
                "active": self._active,
                "peak_active": self._peak,
                "waits": self._waits,
                "seconds_waiting_for_a_slot": round(self._waited_on_slot, 2),
                "max_concurrent": self.config.max_concurrent,
            }

    def busiest(self, limit: int = 10) -> List[Dict[str, object]]:
        """The hosts that cost the most, worst first.

        The aggregate says whether the cap bound anything at all; this says
        *where*, which is the question an operator actually has when a run is
        slower than it was.

        Args:
            limit: How many hosts to report.

        Returns:
            One record per host, ordered by time spent waiting for a slot.
        """
        with self._lock:
            gates = list(self._domains.items())

        rows = [
            {
                "host": host,
                "requests": gate.requests,
                "peak_active": gate.peak,
                "waits": gate.waits,
                "seconds_waiting": round(gate.waited, 2),
            }
            for host, gate in gates
        ]
        rows.sort(key=lambda row: (-float(row["seconds_waiting"]), -int(row["requests"])))
        return rows[: max(0, int(limit))]

    def describe(self) -> str:
        """One line for a run report.

        Returns:
            What the limiter allowed and what it cost.
        """
        found = self.stats()
        return (
            f"host concurrency: at most {found['max_concurrent']} per host "
            f"({found['requests']} request(s) across {found['domains']} host(s), "
            f"peak {found['peak_active']} in flight, {found['waits']} wait(s), "
            f"{found['seconds_waiting_for_a_slot']}s waiting)"
        )
