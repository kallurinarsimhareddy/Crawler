"""Split a run's outcome into the reports each kind of failure actually needs.

Version 1 wrote one ``failed_companies.csv`` holding everything that produced no
jobs. That is four different problems in one file, and they need four different
responses:

===========================  ==========================================
``no_open_jobs.csv``         The board was read and is genuinely empty.
                             Nothing to fix.
``unsupported_platforms.csv`` The platform was identified but no adapter
                             handles it. Fix: write the adapter.
``technical_failures.csv``   The board could not be read — blocked,
                             timed out, changed shape. Fix: retry, use
                             the browser, or repair the adapter.
``unknown_platforms.csv``    Nothing recognisable behind the URL, or no
                             usable URL at all. Fix: the input sheet, or
                             career-page discovery.
===========================  ==========================================

``failed_companies.csv`` is still written, unchanged, by
:mod:`exporters.failure_exporter` — it is the union of all four and several
downstream habits depend on it.

Alongside them, :func:`export_summary` writes ``summary.json``: the run's
totals, per-platform coverage, timings and grouped failure reasons, in a shape
meant to be diffed between runs rather than read once.
"""

from __future__ import annotations

import csv
import json
import platform as platform_module
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Final, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from crawler.crawler_engine import CrawlResult, Outcome
from crawler.platform_detector import Platform
from exporters._atomic import write_atomically
from models.job import Job

__all__ = [
    "OUTCOME_FILES",
    "REPORT_COLUMNS",
    "PlatformCoverage",
    "coverage_by_platform",
    "export_outcome_reports",
    "export_summary",
    "summarise",
]

#: Column headers for the four outcome reports, in order.
REPORT_COLUMNS: Final[Tuple[str, ...]] = (
    "Company",
    "Website",
    "Career URL",
    "IT LINK",
    "Crawled URL",
    "Seed Field",
    "Platform",
    "Outcome",
    "Detail",
    "Seconds",
)

#: Which outcome lands in which file.
OUTCOME_FILES: Final[Dict[Outcome, str]] = {
    Outcome.NO_JOBS: "no_open_jobs.csv",
    Outcome.UNSUPPORTED: "unsupported_platforms.csv",
    Outcome.TECHNICAL: "technical_failures.csv",
    Outcome.UNKNOWN: "unknown_platforms.csv",
}

#: Reason recorded for a company whose board was read and is simply empty.
_NO_JOBS_DETAIL: Final[str] = "crawled successfully; board advertises no jobs"


class PlatformCoverage:
    """How one platform fared across a run.

    Attributes:
        platform: The platform's label.
        companies: Companies whose seed URL resolved to it.
        jobs: Postings extracted from them.
        produced: Companies that yielded at least one posting.
        no_jobs: Companies read successfully that advertise nothing.
        unsupported: Companies with no adapter for this platform.
        technical: Companies whose board could not be read.
        seconds: Total wall-clock time spent on them.
    """

    __slots__ = (
        "platform",
        "companies",
        "jobs",
        "produced",
        "no_jobs",
        "unsupported",
        "technical",
        "seconds",
    )

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.companies = 0
        self.jobs = 0
        self.produced = 0
        self.no_jobs = 0
        self.unsupported = 0
        self.technical = 0
        self.seconds = 0.0

    @property
    def failures(self) -> int:
        """Companies that produced nothing for a reason worth acting on."""
        return self.unsupported + self.technical

    @property
    def success_rate(self) -> float:
        """Share of this platform's companies that yielded postings, 0–100.

        Returns:
            The percentage, or ``0.0`` when no company used this platform.
        """
        return (self.produced / self.companies * 100.0) if self.companies else 0.0

    def add(self, result: CrawlResult) -> None:
        """Fold one company's outcome into this platform's totals.

        Args:
            result: The company's outcome.
        """
        self.companies += 1
        self.jobs += len(result.jobs)
        self.seconds += result.seconds

        outcome = result.outcome
        if outcome is Outcome.JOBS:
            self.produced += 1
        elif outcome is Outcome.NO_JOBS:
            self.no_jobs += 1
        elif outcome is Outcome.UNSUPPORTED:
            self.unsupported += 1
        else:
            self.technical += 1

    def to_dict(self) -> Dict[str, Any]:
        """Render as JSON-serialisable data.

        Returns:
            The platform's totals, keyed for ``summary.json``.
        """
        return {
            "platform": self.platform,
            "companies": self.companies,
            "jobs": self.jobs,
            "produced": self.produced,
            "no_open_jobs": self.no_jobs,
            "unsupported": self.unsupported,
            "technical_failures": self.technical,
            "failures": self.failures,
            "success_rate": round(self.success_rate, 1),
            "seconds": round(self.seconds, 1),
        }


def coverage_by_platform(results: Iterable[CrawlResult]) -> List[PlatformCoverage]:
    """Aggregate a run's results per platform.

    Args:
        results: Every result the run produced.

    Returns:
        One entry per platform seen, ordered by job count descending then by
        name, so the report reads most-productive first and is stable when two
        platforms tie.
    """
    coverage: Dict[str, PlatformCoverage] = {}

    for result in results:
        label = result.platform.value
        coverage.setdefault(label, PlatformCoverage(label)).add(result)

    return sorted(coverage.values(), key=lambda item: (-item.jobs, item.platform))


def _row(record: Mapping[str, str], result: CrawlResult) -> Tuple[str, ...]:
    """Render one company as a report row.

    Args:
        record: The company's input record, for the sheet's original URLs.
        result: The company's outcome.

    Returns:
        The row, in :data:`REPORT_COLUMNS` order.
    """
    return (
        result.company or str(record.get("company") or ""),
        str(record.get("website") or ""),
        str(record.get("career_url") or ""),
        str(record.get("it_link") or ""),
        result.seed_url,
        "discovered" if result.discovered else result.seed_field,
        result.platform.value,
        result.outcome.value,
        result.error or _NO_JOBS_DETAIL,
        f"{result.seconds:.1f}",
    )


def _write_csv(path: Path, rows: Sequence[Sequence[str]], fallback_when_locked: bool) -> Path:
    """Write one report as CSV.

    Args:
        path: Destination.
        rows: Rows, without the header.
        fallback_when_locked: Whether to write beside a locked destination.

    Returns:
        The path actually written.

    Raises:
        OSError: If the file cannot be written at all.
    """

    def write(temporary: Path) -> None:
        """Write the CSV to a temporary file.

        Args:
            temporary: Where to write, before it is swapped into place.
        """
        # utf-8-sig so Excel opens accented company names correctly on Windows.
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(REPORT_COLUMNS)
            writer.writerows(rows)

    return write_atomically(path, write, fallback_when_locked)


def export_outcome_reports(
    pairs: Iterable[Tuple[Mapping[str, str], CrawlResult]],
    output_dir: Path | str,
    fallback_when_locked: bool = True,
) -> Dict[Outcome, Path]:
    """Write one CSV per kind of non-productive outcome.

    Every file is written, including the empty ones: a report that vanishes
    when it has nothing to say is indistinguishable from one that failed to be
    written, and an operator comparing two runs needs to see the zero.

    Args:
        pairs: ``(input record, crawl result)`` for every company attempted.
        output_dir: Directory to write into. Created if absent.
        fallback_when_locked: Whether to write beside a locked destination.

    Returns:
        Outcome -> the path actually written for it.

    Raises:
        OSError: If a file cannot be written at all.
    """
    directory = Path(output_dir)
    grouped: Dict[Outcome, List[Sequence[str]]] = {outcome: [] for outcome in OUTCOME_FILES}

    for record, result in pairs:
        outcome = result.outcome
        if outcome is Outcome.JOBS:
            continue
        grouped[outcome].append(_row(record, result))

    written: Dict[Outcome, Path] = {}
    for outcome, filename in OUTCOME_FILES.items():
        rows = grouped[outcome]
        written[outcome] = _write_csv(directory / filename, rows, fallback_when_locked)
        logger.info("Wrote {} company(ies) to {}", len(rows), written[outcome])

    return written


def summarise(
    pairs: Sequence[Tuple[Mapping[str, str], CrawlResult]],
    jobs: Sequence[Job],
    seconds: float,
    supported: Optional[Iterable[Platform]] = None,
) -> Dict[str, Any]:
    """Assemble the whole run into one JSON-serialisable structure.

    Args:
        pairs: ``(input record, crawl result)`` for every company attempted.
        jobs: The deduplicated postings written to the workbook.
        seconds: Wall-clock duration of the crawl.
        supported: Platforms the engine had an adapter for, so the summary can
            name the ones detected without one.

    Returns:
        The summary, ready to serialise.
    """
    results = [result for _, result in pairs]
    coverage = coverage_by_platform(results)
    outcomes = Counter(result.outcome.value for result in results)

    reasons: Dict[str, Counter] = defaultdict(Counter)
    for result in results:
        if result.outcome in {Outcome.TECHNICAL, Outcome.UNSUPPORTED}:
            reasons[result.platform.value][_reason_family(result.error or "unknown")] += 1

    produced = sum(1 for result in results if result.jobs)
    registered = {item.value for item in (supported or ())}
    unsupported = Counter(
        result.platform.value
        for result in results
        if registered and result.platform.value not in registered
        and result.platform is not Platform.UNKNOWN
    )

    slowest = sorted(results, key=lambda result: -result.seconds)[:15]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform_module.platform(),
        },
        "totals": {
            "companies_processed": len(results),
            "jobs_extracted": len(jobs),
            "companies_producing_jobs": produced,
            "success_rate": round(produced / len(results) * 100.0, 1) if results else 0.0,
            "crawl_seconds": round(seconds, 1),
            "seconds_per_company": round(seconds / len(results), 2) if results else 0.0,
            "countries_identified": len({job.country for job in jobs if job.country}),
            "companies_via_discovery": sum(1 for result in results if result.discovered),
        },
        "outcomes": {
            outcome.value: outcomes.get(outcome.value, 0) for outcome in Outcome
        },
        "platforms": [item.to_dict() for item in coverage],
        "platforms_detected_without_an_adapter": dict(unsupported.most_common()),
        "failure_reasons": {
            name: dict(counter.most_common(12)) for name, counter in sorted(reasons.items())
        },
        "jobs_by_country": dict(
            Counter(job.country or "(unstated)" for job in jobs).most_common(25)
        ),
        "slowest_companies": [
            {
                "company": result.company,
                "platform": result.platform.value,
                "seconds": round(result.seconds, 1),
                "jobs": len(result.jobs),
            }
            for result in slowest
            if result.seconds > 0
        ],
    }


def _reason_family(error: str) -> str:
    """Reduce an error message to the class of problem it represents.

    Delegates to :func:`main._reason_family`, which is the single definition of
    this grouping, and degrades to a truncated message if importing it would be
    circular.

    Args:
        error: The recorded error message.

    Returns:
        A short, groupable description.
    """
    try:
        from main import _reason_family as group

        return group(error)
    except Exception:  # noqa: BLE001 - grouping must never break a report
        return error[:80]


def export_summary(
    pairs: Sequence[Tuple[Mapping[str, str], CrawlResult]],
    jobs: Sequence[Job],
    seconds: float,
    output_path: Path | str,
    supported: Optional[Iterable[Platform]] = None,
    fallback_when_locked: bool = True,
) -> Path:
    """Write ``summary.json`` for a run.

    Args:
        pairs: ``(input record, crawl result)`` for every company attempted.
        jobs: The deduplicated postings written to the workbook.
        seconds: Wall-clock duration of the crawl.
        output_path: Destination file. Parent directories are created.
        supported: Platforms the engine had an adapter for.
        fallback_when_locked: Whether to write beside a locked destination.

    Returns:
        The path actually written.

    Raises:
        OSError: If the file cannot be written at all.
    """
    payload = summarise(pairs, jobs, seconds, supported)

    def write(temporary: Path) -> None:
        """Write the JSON to a temporary file.

        Args:
            temporary: Where to write, before it is swapped into place.
        """
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )

    written = write_atomically(Path(output_path), write, fallback_when_locked)
    logger.success("Wrote the run summary to {}", written)
    return written
