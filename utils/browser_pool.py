"""A bounded pool of reusable browsers.

Launching Chromium costs a second or two and something like a hundred
megabytes. :mod:`utils.browser` already avoids paying that per *page* by
keeping one browser per thread — which is right for a run of sixty-five
companies and wrong for one of twelve thousand, because twenty worker threads
then means twenty resident browsers whether or not twenty are ever busy.

This decouples the two. Browsers are pooled and handed out on demand, so a
crawl can run twenty HTTP workers against three browsers: the common case
needs no browser at all, and the ones that do are rare enough to queue.

    >>> pool = BrowserPool(PoolConfig(size=3))
    >>> with pool.acquire() as browser:
    ...     ...          # browser is None when none could be launched

Three safety properties, each with a test:

**Bounded.** ``size`` is a hard ceiling on live browsers, independent of worker
count. Asking for one when all are busy waits rather than launching another.

**Budgeted.** ``per_company`` and ``total`` cap how many *visits* may happen,
which is separate from how many browsers exist. A pathological board cannot
spend the whole run's browser time, and the run cannot spend hours rendering.

**Recoverable.** A driver that dies is discarded rather than handed to the next
caller, and a machine with no Chromium yields ``None`` instead of raising — the
crawler then reports those companies as needing a browser, which is what it
already does today.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

from loguru import logger

__all__ = ["BrowserPool", "PoolConfig"]


@dataclass(frozen=True)
class PoolConfig:
    """How much browser a run may use.

    Attributes:
        size: Live browsers at once. Deliberately small and deliberately
            independent of the HTTP worker count.
        per_company: Renders one company may cost, ``0`` for unlimited.
        total: Renders the whole run may cost, ``0`` for unlimited.
        acquire_timeout: Seconds to wait for a free browser before giving up
            and reporting the company as unrendered.
    """

    size: int = 2
    per_company: int = 2
    total: int = 0
    acquire_timeout: float = 120.0


def _default_launcher() -> Any:
    """Launch a browser through the existing driver.

    Returns:
        A browser handle, or ``None`` when one cannot be started.
    """
    from utils.browser import _browser  # noqa: PLC2701 - the driver's own launcher

    return _browser()


class BrowserPool:
    """Hands out a bounded number of reusable browsers.

    Args:
        config: How much browser the run may use.
        launcher: Builds one browser. Injected so tests never start Chromium.
    """

    def __init__(
        self,
        config: Optional[PoolConfig] = None,
        launcher: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config or PoolConfig()
        self._launch = launcher or _default_launcher

        self._idle: List[Any] = []
        self._live = 0
        self._lock = threading.Lock()
        self._free = threading.Semaphore(max(1, self.config.size))

        # Browsers abandoned mid-use. `acquire` hands the browser back in its
        # `finally`, so `discard` cannot simply drop it -- it has to be
        # remembered until that return happens.
        self._discarded: set = set()
        self._renders: Dict[str, int] = {}
        self._total_renders = 0
        self._launched = 0
        self._failures = 0
        self._closed = False

    # -- budgets -------------------------------------------------------------

    def may_render(self, company_key: str) -> bool:
        """Whether this company may spend another browser visit.

        Args:
            company_key: The company about to be rendered.

        Returns:
            ``True`` when both the per-company and run-wide budgets allow it.
        """
        with self._lock:
            if self.config.total and self._total_renders >= self.config.total:
                return False
            if not self.config.per_company:
                return True
            return self._renders.get(company_key, 0) < self.config.per_company

    def note_render(self, company_key: str) -> None:
        """Record that a browser visit was spent.

        Args:
            company_key: The company it was spent on.
        """
        with self._lock:
            self._renders[company_key] = self._renders.get(company_key, 0) + 1
            self._total_renders += 1

    # -- borrowing -----------------------------------------------------------

    @contextmanager
    def acquire(self) -> Iterator[Optional[Any]]:
        """Borrow a browser for the duration of the block.

        Yields:
            A browser, or ``None`` when the pool is exhausted or no browser
            could be launched. A caller must handle ``None`` rather than assume
            one: a machine without Chromium is a supported configuration.
        """
        if self._closed:
            yield None
            return

        if not self._free.acquire(timeout=max(0.1, self.config.acquire_timeout)):
            logger.debug("Browser pool: timed out waiting for a free browser")
            yield None
            return

        browser = self._take()
        try:
            yield browser
        finally:
            # Returned even when the block raised, so an exception in a caller
            # cannot leak a browser out of the pool.
            self._give_back(browser)
            self._free.release()

    def _take(self) -> Optional[Any]:
        """An idle browser, or a newly launched one.

        Returns:
            The browser, or ``None`` when launching failed.
        """
        with self._lock:
            if self._idle:
                return self._idle.pop()

        try:
            browser = self._launch()
        except Exception as exc:  # noqa: BLE001 - a missing browser is not fatal
            with self._lock:
                self._failures += 1
            logger.debug("Browser pool: launch failed ({})", exc)
            return None

        if browser is None:
            with self._lock:
                self._failures += 1
            return None

        with self._lock:
            self._launched += 1
            self._live += 1
        return browser

    def _give_back(self, browser: Optional[Any]) -> None:
        """Return a browser to the idle set.

        Args:
            browser: The browser, or ``None`` if none was handed out.
        """
        if browser is None:
            return
        with self._lock:
            if id(browser) in self._discarded:
                self._discarded.discard(id(browser))
                return
            if self._closed:
                self._close_one(browser)
                return
            self._idle.append(browser)

    def discard(self, browser: Optional[Any]) -> None:
        """Throw a browser away instead of reusing it.

        For a driver that crashed or hung: handing it to the next caller would
        turn one company's problem into every company's problem.

        Args:
            browser: The browser to abandon.
        """
        if browser is None:
            return
        with self._lock:
            if browser in self._idle:
                self._idle.remove(browser)
            self._discarded.add(id(browser))
            self._live = max(0, self._live - 1)
        self._close_one(browser)

    # -- teardown ------------------------------------------------------------

    def shutdown(self) -> None:
        """Close every browser. Safe to call more than once."""
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
            self._live = 0

        for browser in idle:
            self._close_one(browser)

    @staticmethod
    def _close_one(browser: Any) -> None:
        """Close one browser, ignoring a driver that has already gone.

        Args:
            browser: The browser to close.
        """
        try:
            close = getattr(browser, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001 - closing a dead driver is not news
            logger.debug("Browser pool: a browser could not be closed cleanly")

    # -- reporting -----------------------------------------------------------

    def stats(self) -> Dict[str, int]:
        """What the pool has done.

        Returns:
            Launches, failures, renders spent and how many browsers are live.
        """
        with self._lock:
            return {
                "launched": self._launched,
                "failures": self._failures,
                "live": self._live,
                "idle": len(self._idle),
                "renders": self._total_renders,
                "companies_rendered": len(self._renders),
            }
