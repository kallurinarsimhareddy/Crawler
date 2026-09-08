"""Run-wide knobs, set once at startup and read from anywhere.

Some decisions are not an adapter's to make. Whether a run may spend ten
seconds of browser time on a company, how many companies run at once, and where
diagnostics are written are properties of *the run*, not of Workday or Lever.
Threading a settings object down through every ``fetch_jobs`` would change the
adapter contract that :class:`crawler.crawler_engine.JobFetcher` fixes, so the
run's choices live here instead and are read where they are needed::

    >>> from config.settings import SETTINGS, configure
    >>> configure(max_workers=16, browser_fallback=False)
    >>> SETTINGS.max_workers
    16

**Defaults are deliberately inert.** Importing an adapter and calling it must
not silently launch a browser, fan out across threads, probe a company's
website or write files to disk — all of which would make a library call do far
more than it was asked to, and would make the test suite touch the network.
So the shipped defaults are single-threaded, browserless and side-effect-free,
and :func:`main.main` opts the *run* into the version 2 behaviour::

    configure(max_workers=8, browser_fallback=True, discover_careers=True)

That is called once, at startup; nothing else should mutate the settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Final

from loguru import logger

__all__ = ["SETTINGS", "Settings", "configure"]

#: Project root, so relative output paths resolve from any working directory.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    """Everything a run decides for itself.

    Attributes:
        max_workers: Companies crawled concurrently. ``1`` restores the
            sequential behaviour of version 1.
        per_host_delay: Minimum seconds between two requests to the same host,
            enforced across workers. Keeps a board with many companies on it —
            ADP, Workday — from being hit in parallel by the whole pool.
        browser_fallback: Whether an adapter that finds nothing over HTTP may
            re-try the page in headless Chromium.
        browser_budget: Browser rescues allowed to run **at the same time**.
            ``0`` means no cap, which is the shipped default and what every run
            to date has effectively had.

            The wording changed with the meaning. This used to read "companies
            per run allowed to use the browser" -- a total, spent once and gone
            -- but nothing ever read the value, so no run has been governed by
            either reading. A concurrency cap is the more useful one: the cost a
            bulk run needs to bound is how much CPU, memory and network the
            renders take *at once*, and a total that is exhausted halfway
            through leaves the rest of the roster with no rescue at all.

            What it does not bound is the number of live Chromium processes.
            :mod:`utils.browser` keeps one browser per worker thread and closes
            it when the thread ends, so over a long enough batch every worker
            can still acquire one; this caps concurrent *renders*, not resident
            browsers. Bounding those means bounding workers.
        host_concurrency: HTTP requests to one hostname that may be in flight
            at once, across every worker. ``0`` means no limit.

            Workers are concurrent *companies*, not concurrent requests to one
            site -- but several companies routinely share a host, and the ones
            that do are the busiest in the ledger:
            ``jobs.smartrecruiters.com``, ``recruiting2.ultipro.com``,
            ``workforcenow.adp.com``. Without this, raising the worker count
            raises the pressure on those hosts one-for-one, which is how a
            vendor starts answering 403 instead of JSON.

            Defaults to ``6``, which equals the worker count production runs
            with, so it is a no-op today and becomes the thing that stops
            twenty workers arriving at one vendor twenty-wide. Hostname-level:
            608 Workday tenants stay 608 independent gates.

        discover_careers: Whether a company with no usable board URL should
            have its website searched for a careers page.
        detect_filters: Whether a board that was read should also have its own
            search controls read, for the filter columns in MASTER_COMPANIES.
            Off by default, and deliberately: it costs one extra request per
            company that was crawled successfully.
        filter_render_budget: Companies per run whose filters may be read in
            headless Chromium when the served markup had none. ADP, UltiPro and
            Eightfold build every control client-side, so static detection finds
            nothing on them -- but a browser visit costs seconds, so this is
            capped rather than unlimited. ``0`` means never render.
        diagnostics: Whether unreadable boards get an evidence dump written to
            ``diagnostics_dir``.
        diagnostics_dir: Where those dumps land.
        diagnostics_limit: Ceiling on dumps per run, so a bad sheet cannot fill
            the disk.
        output_dir: Where every report is written.
        retries: HTTP attempts per request, including the first.
    """

    max_workers: int = 1
    per_host_delay: float = 0.0
    browser_fallback: bool = False
    browser_budget: int = 0
    host_concurrency: int = 6
    discover_careers: bool = False
    detect_filters: bool = False
    filter_render_budget: int = 0
    diagnostics: bool = False
    diagnostics_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output" / "unknown_platforms")
    diagnostics_limit: int = 60
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output")
    retries: int = 2


#: The live settings for this process.
SETTINGS: Final[Settings] = Settings()


def configure(**overrides: Any) -> Settings:
    """Apply run-wide overrides to :data:`SETTINGS`.

    Args:
        **overrides: Field names from :class:`Settings`. ``None`` values are
            ignored, so an unset command-line flag leaves the default alone.

    Returns:
        The updated settings, for convenience.

    Raises:
        KeyError: If an override names no such setting, which is a programming
            error rather than a user one.
    """
    known = {item.name for item in fields(Settings)}

    for name, value in overrides.items():
        if name not in known:
            raise KeyError(f"Unknown setting {name!r}; expected one of {sorted(known)}")
        if value is None:
            continue
        setattr(SETTINGS, name, value)

    logger.debug(
        "Settings: workers={}, browser={}, discovery={}, diagnostics={}",
        SETTINGS.max_workers,
        SETTINGS.browser_fallback,
        SETTINGS.discover_careers,
        SETTINGS.diagnostics,
    )
    return SETTINGS


def default_workers() -> int:
    """Suggest a worker count for this machine.

    Crawling is I/O bound, so the useful degree of parallelism is well above
    the core count — but not unbounded, because every worker holds a
    connection pool and possibly a browser.

    Returns:
        A sensible default worker count.
    """
    cores = os.cpu_count() or 4
    return max(4, min(16, cores * 2))
