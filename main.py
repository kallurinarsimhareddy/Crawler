"""Entry point for the career page crawler.

Reads the company sheet, crawls every company through
:class:`~crawler.crawler_engine.CrawlerEngine`, and writes every deliverable::

    python main.py                              # full run over input/companies.csv
    python main.py --limit 50                   # first 50 companies, for a smoke test
    python main.py --workers 16                 # more concurrency
    python main.py --no-browser                 # HTTP only, no headless Chromium
    python main.py --input other.csv            # a different sheet
    python main.py --preview                    # just show what would be crawled

Outputs, all under ``--output-dir`` (``output/`` by default):

===============================  ==========================================
``jobs.xlsx``                    One row per job found.
``failed_companies.csv``         Every company that produced no jobs.
``no_open_jobs.csv``             ...of those, the boards that are simply empty.
``unsupported_platforms.csv``    ...the platforms with no adapter yet.
``technical_failures.csv``       ...the boards that could not be read.
``unknown_platforms.csv``        ...the URLs nothing recognised.
``summary.json``                 The whole run as data, for diffing.
``unknown_platforms/``           Evidence dumps for boards worth adapting.
===============================  ==========================================

Two reports are printed at the end: the coverage table — platform, companies,
jobs found, success rate, failures, time taken — and the failure breakdown that
says what to fix next.

Nothing stops the run. Every company is attempted; a failure is recorded against
that company and the crawl moves to the next.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from loguru import logger

from config.settings import SETTINGS, configure, default_workers
from crawler.crawler_engine import CrawlerEngine, CrawlResult, Outcome
from crawler.csv_reader import DEFAULT_CSV_PATH, CompanyRecord, read_companies
from crawler.platform_detector import Platform
from exporters.excel_exporter import export_jobs
from exporters.failure_exporter import export_failures
from exporters.report_exporter import (
    coverage_by_platform,
    export_outcome_reports,
    export_summary,
)
from utils.http import build_session

#: Directory holding this script, so paths resolve from any working directory.
PROJECT_ROOT: Path = Path(__file__).resolve().parent

#: Attempts per HTTP request during a bulk run. Lower than the adapters' own
#: default: across a thousand companies, a board that needs four attempts is
#: usually down rather than busy, and the retries cost more than they recover.
BULK_RETRIES: int = 2

#: Width of the report's label column.
_LABEL: int = 34

#: URLs inside an error message, which are per-company noise when grouping.
_URL_IN_MESSAGE: re.Pattern[str] = re.compile(r"https?://\S+")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Crawl every company's careers page.")
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT / DEFAULT_CSV_PATH,
        help="Input CSV (default: input/companies.csv)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "output",
        help="Directory for every report (default: output/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Workbook path, overriding --output-dir (default: <output-dir>/jobs.xlsx)",
    )
    parser.add_argument(
        "--failures",
        type=Path,
        default=None,
        help="Failure CSV path, overriding --output-dir",
    )
    parser.add_argument("--limit", type=int, default=0, help="Crawl only the first N companies")
    parser.add_argument(
        "--retries", type=int, default=BULK_RETRIES, help=f"HTTP attempts (default: {BULK_RETRIES})"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=default_workers(),
        help=f"Companies crawled at once (default: {default_workers()}; 1 is sequential)",
    )
    parser.add_argument(
        "--per-host-delay",
        type=float,
        default=0.35,
        help="Minimum seconds between two crawls of the same host (default: 0.35)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Never fall back to headless Chromium, even when a board renders client-side",
    )
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="Do not search a company's website for its careers page",
    )
    parser.add_argument(
        "--no-diagnostics",
        action="store_true",
        help="Do not write evidence dumps for boards that could not be read",
    )
    parser.add_argument(
        "--diagnostics-limit",
        type=int,
        default=60,
        help="Ceiling on evidence dumps per run (default: 60)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"),
        help="Console log level (default: WARNING — the reports carry the detail)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Also write a full DEBUG log here (default: <output-dir>/crawl.log)",
    )
    parser.add_argument(
        "--preview", action="store_true", help="List what would be crawled, then exit"
    )
    return parser.parse_args(list(argv))


def _configure_logging(level: str, log_file: Path) -> None:
    """Point loguru at a quiet console and a complete file.

    A thousand-company crawl emits tens of thousands of log lines. At DEBUG on
    the console those both bury the report and measurably slow the run, since
    every worker contends on stderr. The console therefore carries only what
    needs a human, and the full trace goes to a file where it can be searched
    after the fact.

    Args:
        level: Console log level.
        log_file: Where the full DEBUG log goes. Failing to open it is not
            fatal — the run matters more than its log.
    """
    logger.remove()
    # backtrace/diagnose off on the console: loguru's variable dump is many
    # screens per failure, and with a few hundred failures it buries the run
    # report entirely. The file sink below keeps the full trace.
    logger.add(
        sys.stderr,
        level=level,
        format="<level>{level: <8}</level> | {message}",
        backtrace=False,
        diagnose=False,
    )

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level="DEBUG",
            rotation="50 MB",
            retention=3,
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )
    except OSError as exc:
        print(f"WARNING: could not open the log file {log_file}: {exc}", file=sys.stderr)


def preview(records: Sequence[CompanyRecord], engine: CrawlerEngine) -> None:
    """Print the platform mix without crawling anything.

    Args:
        records: Companies read from the sheet.
        engine: Engine whose seed selection decides which URL is used.
    """
    counts: Counter = Counter()
    for record in records:
        _, _, platform = engine.select_seed(record)
        counts[platform] += 1

    supported = set(engine.supported_platforms)
    print(f"{len(records)} companies\n")
    for platform, count in counts.most_common():
        mark = " " if platform in supported else "*"
        print(f"  {mark} {platform.value:<22}{count:>6}")
    print("\n  * no adapter registered")


def _elapsed(seconds: float) -> str:
    """Render a duration as ``1h 04m 09s``.

    Args:
        seconds: Elapsed seconds.

    Returns:
        The formatted duration.
    """
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _reason_family(error: str) -> str:
    """Reduce an error message to the class of problem it represents.

    Full messages carry URLs and ids, so grouping on them yields one bucket per
    company. The leading exception type plus the first clause is what actually
    distinguishes one kind of failure from another.

    Args:
        error: The recorded error message.

    Returns:
        A short, groupable description.
    """
    kind, _, detail = error.partition(": ")

    if "no adapter for" in error:
        return error
    if "bot challenge" in error or "Human Verification" in error:
        return f"{kind}: anti-bot interstitial"
    if "client-side" in error or "browser-driven" in error:
        return f"{kind}: needs a browser"
    if "legacy" in error:
        return f"{kind}: legacy portal, no public endpoint"

    # Every message names the URL that failed, which is unique per company and
    # would put each failure in its own bucket. Status codes are kept: an
    # estate answering 403 is a different problem from one answering 500.
    head = _URL_IN_MESSAGE.sub("<url>", detail)
    head = head.split(": '")[0].split(" (")[0].strip()

    return f"{kind}: {head[:80]}" if head else kind


def coverage_report(pairs: Sequence[Tuple[CompanyRecord, CrawlResult]]) -> None:
    """Print the per-platform coverage table.

    This is the table that says where the crawler stands and where the next
    day's work is: a platform with many companies and a low success rate is
    worth an adapter, and one that is slow is worth a look regardless of how
    well it does.

    Args:
        pairs: ``(record, result)`` for every company attempted.
    """
    coverage = coverage_by_platform(result for _, result in pairs)

    print("\n" + "=" * 78)
    print("COVERAGE BY PLATFORM")
    print("=" * 78)
    print(
        f"{'Platform':<26}{'Companies':>10}{'Jobs Found':>12}"
        f"{'Success %':>11}{'Failures':>10}{'Time':>9}"
    )
    print("-" * 78)

    for item in coverage:
        print(
            f"{item.platform[:26]:<26}{item.companies:>10}{item.jobs:>12}"
            f"{item.success_rate:>10.1f}%{item.failures:>10}{_elapsed(item.seconds):>9}"
        )

    print("-" * 78)
    totals_companies = sum(item.companies for item in coverage)
    totals_jobs = sum(item.jobs for item in coverage)
    totals_produced = sum(item.produced for item in coverage)
    totals_failures = sum(item.failures for item in coverage)
    overall = (totals_produced / totals_companies * 100.0) if totals_companies else 0.0
    print(
        f"{'ALL PLATFORMS':<26}{totals_companies:>10}{totals_jobs:>12}"
        f"{overall:>10.1f}%{totals_failures:>10}"
    )
    print("=" * 78)


def report(
    pairs: Sequence[Tuple[CompanyRecord, CrawlResult]],
    total_jobs: int,
    seconds: float,
    engine: CrawlerEngine,
) -> None:
    """Print the run report.

    Args:
        pairs: ``(record, result)`` for every company attempted.
        total_jobs: Jobs written, after cross-company deduplication.
        seconds: Wall-clock duration of the crawl.
        engine: Engine used, for the list of registered adapters.
    """
    results = [result for _, result in pairs]
    succeeded = [result for result in results if result.ok]
    failed = [result for result in results if not result.ok]
    produced = [result for result in results if result.jobs]
    supported = set(engine.supported_platforms)

    print("\n" + "=" * 74)
    print("CRAWL REPORT")
    print("=" * 74)
    print(f"{'Total companies processed':<{_LABEL}}{len(results):>8}")
    print(f"{'Total jobs extracted':<{_LABEL}}{total_jobs:>8}")
    print(f"{'Companies crawled successfully':<{_LABEL}}{len(succeeded):>8}")
    print(f"{'  of which returned jobs':<{_LABEL}}{len(produced):>8}")
    print(f"{'  of which returned nothing':<{_LABEL}}{len(succeeded) - len(produced):>8}")
    print(f"{'Companies that failed':<{_LABEL}}{len(failed):>8}")
    print(f"{'Found via career discovery':<{_LABEL}}{sum(1 for r in results if r.discovered):>8}")
    print(f"{'Time taken':<{_LABEL}}{_elapsed(seconds):>8}")

    outcomes = Counter(result.outcome.value for result in results)
    print("\n" + "-" * 74)
    print("OUTCOMES")
    print("-" * 74)
    for outcome in Outcome:
        print(f"  {outcome.value:<26}{outcomes.get(outcome.value, 0):>6}")

    by_platform: Counter = Counter()
    for result in results:
        by_platform[result.platform.value] += len(result.jobs)

    print("\n" + "-" * 74)
    print("JOBS BY PLATFORM")
    print("-" * 74)
    print(f"{'Platform':<24}{'Companies':>11}{'Jobs':>9}{'Produced':>10}{'Failed':>9}")
    counts_by_platform: Dict[str, List[CrawlResult]] = defaultdict(list)
    for result in results:
        counts_by_platform[result.platform.value].append(result)

    for name in sorted(counts_by_platform, key=lambda key: -by_platform[key]):
        group = counts_by_platform[name]
        print(
            f"{name:<24}{len(group):>11}{by_platform[name]:>9}"
            f"{sum(1 for r in group if r.jobs):>10}{sum(1 for r in group if not r.ok):>9}"
        )

    print("\n" + "-" * 74)
    print("FAILURE REASONS BY PLATFORM")
    print("-" * 74)
    if not failed:
        print("  none")
    else:
        grouped: Dict[str, Counter] = defaultdict(Counter)
        for result in failed:
            grouped[result.platform.value][_reason_family(result.error or "unknown")] += 1

        for name in sorted(grouped, key=lambda key: -sum(grouped[key].values())):
            print(f"\n  {name} ({sum(grouped[name].values())})")
            for reason, count in grouped[name].most_common():
                print(f"      {count:>5}  {reason}")

    print("\n" + "-" * 74)
    print("PLATFORMS DETECTED WITH NO ADAPTER")
    print("-" * 74)
    unsupported = Counter(
        result.platform.value
        for result in results
        if result.platform not in supported and result.platform is not Platform.UNKNOWN
    )
    unresolved = sum(1 for result in results if result.platform is Platform.UNKNOWN)

    if unsupported:
        for name, count in unsupported.most_common():
            print(f"  {name:<24}{count:>6}")
    else:
        print("  none")
    print(f"\n  {'No usable URL in the sheet':<24}{unresolved:>6}")
    print("=" * 74)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the crawl and write both outputs.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``1`` if the input sheet could not be read.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    output_dir: Path = args.output_dir
    workbook_path: Path = args.output or output_dir / "jobs.xlsx"
    failures_path: Path = args.failures or output_dir / "failed_companies.csv"

    _configure_logging(args.log_level, args.log_file or output_dir / "crawl.log")

    # The library defaults are inert on purpose — see config.settings. A *run*
    # opts into concurrency, the browser, discovery and diagnostics here, once.
    configure(
        max_workers=max(1, args.workers),
        per_host_delay=max(0.0, args.per_host_delay),
        browser_fallback=not args.no_browser,
        discover_careers=not args.no_discover,
        diagnostics=not args.no_diagnostics,
        diagnostics_limit=max(0, args.diagnostics_limit),
        diagnostics_dir=output_dir / "unknown_platforms",
        output_dir=output_dir,
        retries=args.retries,
    )

    try:
        records: List[CompanyRecord] = read_companies(args.input)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.limit > 0:
        records = records[: args.limit]

    engine = CrawlerEngine(session_factory=lambda: build_session(retries=args.retries))

    if args.preview:
        preview(records, engine)
        return 0

    from crawler.diagnostics import reset as reset_diagnostics

    reset_diagnostics()

    logger.info(
        "Crawling {} company(ies) from {} with {} worker(s), browser {}",
        len(records),
        args.input,
        SETTINGS.max_workers,
        "on" if SETTINGS.browser_fallback else "off",
    )
    started = time.monotonic()
    results = engine.crawl_all(records)
    seconds = time.monotonic() - started

    pairs: List[Tuple[CompanyRecord, CrawlResult]] = list(zip(records, results))

    # Deduplicate across companies exactly as CrawlerEngine.crawl does, so the
    # workbook and the reported total always agree.
    seen: set = set()
    jobs = []
    for result in results:
        for job in result.jobs:
            if job.key in seen:
                continue
            seen.add(job.key)
            jobs.append(job)

    coverage_report(pairs)
    report(pairs, len(jobs), seconds, engine)

    outputs = (
        (str(workbook_path), lambda: export_jobs(jobs, workbook_path)),
        (str(failures_path), lambda: export_failures(pairs, failures_path)),
        (f"{output_dir}/*.csv", lambda: export_outcome_reports(pairs, output_dir)),
        (
            str(output_dir / "summary.json"),
            lambda: export_summary(
                pairs, jobs, seconds, output_dir / "summary.json", engine.supported_platforms
            ),
        ),
    )

    for label, write in outputs:
        try:
            write()
            print(f"Wrote {label}")
        except OSError as exc:
            # One unwritable output must not cost the others.
            print(f"ERROR: could not write {label}: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
