"""Measure what a worker count actually costs, without risking a run.

    python -m crawler.benchmark --workers 6  --limit 200
    python -m crawler.benchmark --workers 10 --limit 200
    python -m crawler.benchmark --workers 16 --limit 200 --host-concurrency 4

Raising ``--workers`` from six is not a decision anyone should take from first
principles: the costs are a browser per worker thread, concurrent pressure on
whichever hosts the slice happens to contain, and a failure rate that is a
property of the roster as much as of the crawler. All three are measurable, and
none of them are predictable. So this measures them on a slice, and the slices
are comparable because everything except ``--workers`` is held still.

**It cannot write.** The run is always a dry run: the Sheets client
authenticates with the read-only scope, so Google itself refuses a write, and
no database is opened. The checkpoint goes to a temporary file that is thrown
away, so the production one is neither read nor written and a benchmark can
never be mistaken for progress on the week's crawl. The consequence is that
``jobs persisted`` is always zero here -- persistence is the one thing a dry run
does not exercise, and the report says so rather than implying otherwise.

The numbers worth comparing between runs are elapsed time per company, peak RSS,
peak Chromium count, and the failure mix. If failures climb as workers climb,
the roster is being pushed too hard and the limiter or the worker count is
wrong -- and that is exactly the question this exists to answer.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

__all__ = ["Benchmark", "Sampler", "main", "render"]


# ---------------------------------------------------------------------------
# Sampling the things that only exist while the run is running
# ---------------------------------------------------------------------------


def resident_bytes() -> Optional[int]:
    """This process's resident memory, if the platform will say.

    Returns:
        Bytes, or ``None`` where neither route is available. Tried in order of
        how much they can be trusted: psutil if it happens to be installed,
        then the platform call, then nothing. Nothing is a fine answer -- a
        missing number is better than an invented one.
    """
    try:  # pragma: no cover - only when psutil is installed
        import psutil

        return int(psutil.Process().memory_info().rss)
    except Exception:  # noqa: BLE001 - psutil is not a dependency
        pass

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return int(counters.WorkingSetSize)
        except Exception:  # noqa: BLE001 - measurement must never end a run
            return None
        return None

    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes; macOS reports bytes.
        return int(peak) if sys.platform == "darwin" else int(peak) * 1024
    except Exception:  # noqa: BLE001 - as above
        return None


def chromium_processes() -> Optional[int]:
    """How many Chromium processes exist right now.

    Counted across the machine rather than per-parent, because Playwright's
    browsers are helper trees and attributing them precisely costs more than
    the number is worth. On a benchmark host running nothing else this is the
    crawler's; anywhere else it is an upper bound, and the report says so.

    Returns:
        The count, or ``None`` when it could not be determined.
    """
    try:
        if os.name == "nt":
            found = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=20,
            ).stdout
            return sum(1 for line in found.splitlines() if "chrome.exe" in line.lower())

        found = subprocess.run(
            ["ps", "-e", "-o", "comm="], capture_output=True, text=True, timeout=20
        ).stdout
        return sum(
            1
            for line in found.splitlines()
            if "chrom" in line.lower() or "headless_shell" in line.lower()
        )
    except Exception:  # noqa: BLE001 - a missing number is not a failed run
        return None


class Sampler:
    """Watches memory and browser count while a run is in flight.

    Both are peaks rather than averages: what decides whether a worker count
    fits on a box is the worst moment, not the typical one.

    Args:
        interval: Seconds between samples.
    """

    def __init__(self, interval: float = 0.5) -> None:
        self.interval = max(0.05, float(interval))
        self.peak_rss: Optional[int] = None
        self.peak_chromium: Optional[int] = None
        self.samples = 0

        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, name="benchmark-sampler", daemon=True
        )

    def _record(self) -> None:
        """Take one sample of each measurement."""
        rss = resident_bytes()
        if rss is not None:
            self.peak_rss = max(self.peak_rss or 0, rss)

        chromium = chromium_processes()
        if chromium is not None:
            self.peak_chromium = max(self.peak_chromium or 0, chromium)

        self.samples += 1

    def _watch(self) -> None:
        """Sample until asked to stop."""
        while not self._stop.wait(self.interval):
            try:
                self._record()
            except Exception:  # noqa: BLE001 - sampling must not end a run
                logger.debug("A benchmark sample failed", exc_info=True)

    def __enter__(self) -> "Sampler":
        """Take a first sample, then start watching."""
        self._record()
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop watching, after one last sample."""
        self._stop.set()
        self._thread.join(timeout=10)
        try:
            self._record()
        except Exception:  # noqa: BLE001 - as above
            pass


# ---------------------------------------------------------------------------
# The measurement itself
# ---------------------------------------------------------------------------


@dataclass
class Benchmark:
    """One measured slice, and everything needed to compare it with another.

    Attributes:
        workers: Companies crawled at once.
        browser_budget: Concurrent browser rescues allowed. ``0`` is no cap.
        host_concurrency: Concurrent requests per hostname. ``0`` is no cap.
        companies: How many companies were asked for.
        seconds: Wall clock for the crawl.
        companies_attempted: How many were reached.
        companies_succeeded: ...of which were read.
        companies_failed: ...of which were not.
        observations: Postings seen.
        jobs_persisted: Postings written to SQLite. Always ``0`` here: a dry run
            opens no database, and reporting the number as though it had been
            measured would be worse than reporting a zero that is explained.
        blockers: Failure type to count, straight from the run.
        http: The limiter's counters.
        peak_rss: Highest resident memory observed, in bytes.
        peak_chromium: Highest Chromium process count observed.
        samples: How many samples were taken.
    """

    workers: int = 0
    browser_budget: int = 0
    host_concurrency: int = 0
    companies: int = 0
    seconds: float = 0.0
    companies_attempted: int = 0
    companies_succeeded: int = 0
    companies_failed: int = 0
    observations: int = 0
    jobs_persisted: int = 0
    blockers: Dict[str, int] = field(default_factory=dict)
    http: Dict[str, Any] = field(default_factory=dict)
    busiest_hosts: List[Dict[str, Any]] = field(default_factory=list)
    peak_rss: Optional[int] = None
    peak_chromium: Optional[int] = None
    samples: int = 0

    @property
    def seconds_per_company(self) -> float:
        """Wall clock divided by companies reached.

        Returns:
            Seconds, or ``0.0`` when nothing was attempted. This is the number
            that actually compares two worker counts: total elapsed is a
            property of the slice size as much as of the configuration.
        """
        if not self.companies_attempted:
            return 0.0
        return self.seconds / self.companies_attempted

    @property
    def failure_rate(self) -> float:
        """Share of attempted companies that could not be read, as a percentage."""
        if not self.companies_attempted:
            return 0.0
        return self.companies_failed / self.companies_attempted * 100.0

    def as_dict(self) -> Dict[str, Any]:
        """The whole measurement, for saving beside another one.

        Returns:
            Plain data.
        """
        return {
            "configuration": {
                "workers": self.workers,
                "browser_budget": self.browser_budget,
                "host_concurrency": self.host_concurrency,
            },
            "throughput": {
                "companies_requested": self.companies,
                "companies_attempted": self.companies_attempted,
                "seconds": round(self.seconds, 1),
                "seconds_per_company": round(self.seconds_per_company, 2),
            },
            "outcomes": {
                "succeeded": self.companies_succeeded,
                "failed": self.companies_failed,
                "failure_rate": round(self.failure_rate, 1),
                "observations": self.observations,
                "jobs_persisted": self.jobs_persisted,
                "blockers": dict(self.blockers),
            },
            "resources": {
                "peak_rss_mb": (
                    round(self.peak_rss / (1024 * 1024), 1) if self.peak_rss else None
                ),
                "peak_chromium_processes": self.peak_chromium,
                "samples": self.samples,
            },
            "http": dict(self.http),
            "busiest_hosts": list(self.busiest_hosts),
        }


#: Failure labels worth calling out separately in the report. These are the ones
#: that mean "the site pushed back", as opposed to "the site was broken", and
#: they are the ones that should be watched as workers rise.
_PUSHBACK: Sequence[str] = (
    "403 forbidden",
    "429 rate limited",
    "cloudflare challenge",
    "bot challenge",
    "aws waf",
    "captcha",
    "server error",
)


def render(measured: Benchmark) -> str:
    """Render a measurement for a terminal.

    Args:
        measured: What was measured.

    Returns:
        The report.
    """
    rule = "=" * 78
    lines = [rule, "CAREERCRAWLER BENCHMARK — dry run, nothing was written", rule, ""]

    lines.append("  Configuration")
    lines.append("  " + "-" * 74)
    lines.append(f"    {'Workers':<34}{measured.workers}")
    lines.append(
        f"    {'Browser budget':<34}"
        f"{measured.browser_budget or 'unlimited'}"
    )
    lines.append(
        f"    {'Host concurrency':<34}"
        f"{measured.host_concurrency or 'unlimited'}"
    )

    lines.append("")
    lines.append("  Throughput")
    lines.append("  " + "-" * 74)
    lines.append(f"    {'Companies requested':<34}{measured.companies}")
    lines.append(f"    {'Companies attempted':<34}{measured.companies_attempted}")
    lines.append(f"    {'Elapsed':<34}{measured.seconds:.1f}s")
    lines.append(
        f"    {'Seconds per company':<34}{measured.seconds_per_company:.2f}"
        "        <- compare this between runs"
    )

    lines.append("")
    lines.append("  Outcomes")
    lines.append("  " + "-" * 74)
    lines.append(f"    {'Succeeded':<34}{measured.companies_succeeded}")
    lines.append(
        f"    {'Failed':<34}{measured.companies_failed} "
        f"({measured.failure_rate:.1f}%)"
    )
    lines.append(f"    {'Jobs observed':<34}{measured.observations}")
    lines.append(
        f"    {'Jobs persisted':<34}{measured.jobs_persisted}"
        "             <- always 0: a dry run opens no database"
    )

    pushback = sum(measured.blockers.get(label, 0) for label in _PUSHBACK)
    lines.append(
        f"    {'Of which the site pushed back':<34}{pushback}"
        "        <- watch this as workers rise"
    )
    for label, count in sorted(measured.blockers.items(), key=lambda item: -item[1]):
        lines.append(f"      {label[:36]:<36}{count}")

    lines.append("")
    lines.append("  Resources")
    lines.append("  " + "-" * 74)
    rss = (
        f"{measured.peak_rss / (1024 * 1024):.0f} MB"
        if measured.peak_rss
        else "unavailable"
    )
    lines.append(f"    {'Peak resident memory':<34}{rss}")
    lines.append(
        f"    {'Peak Chromium processes':<34}"
        f"{measured.peak_chromium if measured.peak_chromium is not None else 'unavailable'}"
        "          <- machine-wide, an upper bound"
    )
    lines.append(f"    {'Samples taken':<34}{measured.samples}")

    if measured.http:
        lines.append("")
        lines.append("  HTTP and the per-host limiter")
        lines.append("  " + "-" * 74)
        for key in (
            "requests", "domains", "peak_active", "waits",
            "seconds_waiting_for_a_slot", "max_concurrent",
        ):
            if key in measured.http:
                lines.append(f"    {key.replace('_', ' '):<34}{measured.http[key]}")

    if measured.busiest_hosts:
        lines.append("")
        lines.append("  Hosts that waited longest")
        lines.append("  " + "-" * 74)
        for row in measured.busiest_hosts[:8]:
            lines.append(
                f"    {str(row.get('host'))[:40]:<42}"
                f"{row.get('requests')} req, peak {row.get('peak_active')}, "
                f"{row.get('seconds_waiting')}s waiting"
            )

    lines.append("")
    lines.append(rule)
    return "\n".join(lines)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m crawler.benchmark",
        description=(
            "Measure what a worker count costs on a slice of the roster. "
            "Always a dry run: nothing is written to Sheets, no database is "
            "opened, and the production checkpoint is neither read nor written."
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=6,
        help="Companies crawled at once (default: 6, production's value)",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Companies to crawl (default: 200, one production batch)",
    )
    parser.add_argument(
        "--browser-budget", type=int, default=0,
        help="Concurrent browser rescues allowed (default: 0, no cap)",
    )
    parser.add_argument(
        "--host-concurrency", type=int, default=6,
        help="Concurrent requests per hostname (default: 6)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=200, help="Companies per batch"
    )
    parser.add_argument(
        "--per-host-delay", type=float, default=0.35,
        help="Seconds between two crawls of one host",
    )
    parser.add_argument("--retries", type=int, default=2, help="HTTP attempts per request")
    parser.add_argument("--no-browser", action="store_true", help="Never use Chromium")
    parser.add_argument(
        "--json", type=Path, default=None, metavar="PATH",
        help="Also write the measurement here, for comparing runs",
    )
    parser.add_argument("--spreadsheet", default=None, help="Spreadsheet id or URL")
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"),
        help="Console log level (default: WARNING, so the report is readable)",
    )
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Measure one configuration.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``2`` when the spreadsheet is not configured, ``1``
        on failure.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    logger.remove()
    logger.add(sys.stderr, level=args.log_level, format="<level>{level: <8}</level> | {message}")

    from config.settings import SETTINGS, configure
    from crawler.crawler_engine import CrawlerEngine
    from crawler.weekly_run import WeeklyRun
    from sheets._cli import connect
    from utils.http import build_session, limiter_stats, reset_host_limiter

    # Forced, not defaulted. The read-only scope means Google refuses a write
    # even if something below asks for one.
    args.dry_run = True

    connection, code = connect(args)
    if connection is None:
        return code

    configure(
        max_workers=max(1, args.workers),
        per_host_delay=max(0.0, args.per_host_delay),
        browser_fallback=not args.no_browser,
        browser_budget=max(0, args.browser_budget),
        host_concurrency=max(0, args.host_concurrency),
        discover_careers=True,
        detect_filters=False,
        diagnostics=False,
        retries=args.retries,
    )

    # One run's numbers, not the process's.
    reset_host_limiter()

    engine = CrawlerEngine(session_factory=lambda: build_session(retries=SETTINGS.retries))

    with tempfile.TemporaryDirectory() as scratch:
        # Deliberately not the production checkpoint. A dry run would not write
        # it, but pointing somewhere else means a benchmark cannot be confused
        # with progress on the week's crawl even by accident.
        runner = WeeklyRun(
            connection.client,
            engine=engine,
            checkpoint_path=Path(scratch) / "benchmark-checkpoint.json",
            batch_size=max(1, args.batch_size),
            database=None,
        )

        started = time.monotonic()
        with Sampler() as sampler:
            summary = runner.execute(
                limit=max(1, args.limit),
                resume=False,
                dry_run=True,
                run_id="benchmark",
            )
        elapsed = time.monotonic() - started

    limiter_found = limiter_stats()
    from utils.http import host_limiter

    limiter = host_limiter()

    measured = Benchmark(
        workers=SETTINGS.max_workers,
        browser_budget=SETTINGS.browser_budget,
        host_concurrency=SETTINGS.host_concurrency,
        companies=args.limit,
        seconds=elapsed,
        companies_attempted=summary.companies_attempted,
        companies_succeeded=summary.companies_succeeded,
        companies_failed=summary.companies_failed,
        observations=summary.observations,
        jobs_persisted=summary.jobs_persisted,
        blockers=dict(summary.blockers),
        http=limiter_found,
        busiest_hosts=limiter.busiest(8) if limiter is not None else [],
        peak_rss=sampler.peak_rss,
        peak_chromium=sampler.peak_chromium,
        samples=sampler.samples,
    )

    print(render(measured))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(measured.as_dict(), indent=2), encoding="utf-8")
        print(f"\nMeasurement written to {args.json}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
