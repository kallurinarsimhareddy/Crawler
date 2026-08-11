"""Live coverage report: run every adapter against real URLs from the input sheet.

This is an integration harness, not a unit test — it makes real network calls and
is deliberately named so ``unittest discover`` (which matches ``test*.py``) skips
it. Run it explicitly::

    python -m tests.live_coverage              # 3 companies per platform
    python -m tests.live_coverage 5            # 5 companies per platform
    python -m tests.live_coverage 3 Greenhouse # one platform only

For each platform it samples companies from ``input/companies.csv``, crawls them
through :class:`~crawler.crawler_engine.CrawlerEngine` — so dispatch and failure
isolation are exercised exactly as they are in production — and reports how many
companies yielded jobs.

The harness opts into the same run settings :func:`main.main` does — the worker
pool and the headless-browser fallback — because a coverage figure measured
without them would not be the coverage a real run gets. Results go to their own
workbook, so a sample can never overwrite the deliverable a full run produced.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from loguru import logger

from config.settings import configure, default_workers
from crawler.crawler_engine import CrawlerEngine, CrawlResult
from crawler.csv_reader import DEFAULT_CSV_PATH, CompanyRecord, read_companies
from crawler.platform_detector import Platform
from exporters.excel_exporter import export_jobs
from utils.http import build_session

#: Companies sampled per platform unless told otherwise.
DEFAULT_SAMPLE: int = 3

#: Project root, so the harness runs from anywhere.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

#: Where sampled jobs land. Deliberately *not* ``jobs.xlsx``: that file is the
#: product of a full run, and a three-company-per-platform sample overwriting it
#: would quietly replace forty thousand postings with a few hundred.
SAMPLE_OUTPUT: Path = PROJECT_ROOT / "output" / "live_coverage.xlsx"


def group_by_platform(
    records: Sequence[CompanyRecord], engine: CrawlerEngine
) -> Dict[Platform, List[CompanyRecord]]:
    """Bucket every company by the platform its best URL resolves to.

    Args:
        records: Company records from the input sheet.
        engine: Engine whose seed selection decides which URL is used.

    Returns:
        Platform -> the companies detected as running on it.
    """
    buckets: Dict[Platform, List[CompanyRecord]] = defaultdict(list)

    for record in records:
        _, _, platform = engine.select_seed(record)
        buckets[platform].append(record)

    return buckets


def _sample(records: Sequence[CompanyRecord], limit: int) -> List[CompanyRecord]:
    """Take the first ``limit`` companies, spread across the sheet.

    Args:
        records: Companies on one platform.
        limit: How many to take.

    Returns:
        The sampled companies.
    """
    if len(records) <= limit:
        return list(records)

    step = max(1, len(records) // limit)
    return [records[index * step] for index in range(limit)]


def run(sample_size: int, only: Optional[str] = None) -> Tuple[List[CrawlResult], int]:
    """Crawl a sample of companies on every platform and report coverage.

    Args:
        sample_size: Companies to test per platform.
        only: Restrict the run to one platform label, case-insensitive.

    Returns:
        ``(results, exit_code)`` where the code is non-zero if any supported
        platform returned nothing at all.
    """
    # Measure what a real run would get, not what the inert library defaults
    # would. Diagnostics stay off: this harness is about coverage, and a
    # sampled run should not litter output/unknown_platforms/.
    configure(
        max_workers=default_workers(),
        per_host_delay=0.35,
        browser_fallback=True,
        discover_careers=True,
        diagnostics=False,
    )

    records = read_companies(PROJECT_ROOT / DEFAULT_CSV_PATH)
    engine = CrawlerEngine(session_factory=build_session)
    buckets = group_by_platform(records, engine)

    supported = set(engine.supported_platforms)
    targets = sorted(
        (platform for platform in buckets if platform in supported),
        key=lambda platform: platform.value.lower(),
    )
    if only:
        targets = [p for p in targets if p.value.lower() == only.lower()]
        if not targets:
            print(f"No companies detected on platform {only!r}")
            return [], 1

    print(f"\n{len(records)} companies in the sheet; {len(supported)} adapters registered")
    print(f"Testing up to {sample_size} company(ies) per platform\n")

    all_results: List[CrawlResult] = []
    rows: List[Tuple[str, int, int, int, int, str]] = []

    for platform in targets:
        sampled = _sample(buckets[platform], sample_size)
        results = engine.crawl_all(sampled)
        all_results.extend(results)

        jobs = sum(len(result.jobs) for result in results)
        failures = sum(1 for result in results if not result.ok)
        with_jobs = sum(1 for result in results if result.jobs)
        coverage = f"{with_jobs / len(results):.0%}" if results else "n/a"

        rows.append((platform.value, len(results), jobs, failures, with_jobs, coverage))

    print(f"\n{'Platform':<22}{'Companies':>10}{'Jobs':>8}{'Failures':>10}{'Produced':>10}{'Coverage':>10}")
    print("-" * 70)
    for name, tested, jobs, failures, with_jobs, coverage in rows:
        print(f"{name:<22}{tested:>10}{jobs:>8}{failures:>10}{with_jobs:>10}{coverage:>10}")
    print("-" * 70)

    total_tested = sum(row[1] for row in rows)
    total_jobs = sum(row[2] for row in rows)
    total_failures = sum(row[3] for row in rows)
    total_with_jobs = sum(row[4] for row in rows)
    overall = f"{total_with_jobs / total_tested:.0%}" if total_tested else "n/a"
    print(
        f"{'TOTAL':<22}{total_tested:>10}{total_jobs:>8}{total_failures:>10}"
        f"{total_with_jobs:>10}{overall:>10}"
    )

    dead = [row[0] for row in rows if row[2] == 0]
    if dead:
        print(f"\nPlatforms returning nothing: {', '.join(dead)}")

    failed = [result for result in all_results if not result.ok]
    if failed:
        print(f"\nFailures ({len(failed)}):")
        for result in failed[:20]:
            print(f"  {result.company[:28]:<30} {result.platform.value:<18} {(result.error or '')[:70]}")

    return all_results, 1 if dead else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the harness and write the sampled jobs to a workbook.

    Args:
        argv: Arguments without the program name. First is the sample size,
            second an optional platform filter.

    Returns:
        Process exit code.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    sample_size = int(args[0]) if args and args[0].isdigit() else DEFAULT_SAMPLE
    only = args[1] if len(args) > 1 else None

    results, code = run(sample_size, only)

    jobs = [job for result in results for job in result.jobs]
    if jobs:
        path = export_jobs(jobs, SAMPLE_OUTPUT)
        print(f"\nExported {len(jobs)} job(s) to {path}")

    return code


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    raise SystemExit(main())
