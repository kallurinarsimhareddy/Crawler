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
        browser_budget: Companies per run allowed to use the browser. A browser
            visit costs seconds rather than milliseconds, so a bulk run caps
            how much of its wall clock can go that way. ``0`` means no cap.
        discover_careers: Whether a company with no usable board URL should
            have its website searched for a careers page.
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
    discover_careers: bool = False
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
