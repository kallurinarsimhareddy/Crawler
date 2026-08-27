"""Show the weekly diff working, over a scripted sequence of runs.

    python -m sheets.diff_test              # in memory, no spreadsheet needed
    python -m sheets.diff_test --live       # against the real sheet, then clean up
    python -m sheets.diff_test --live --dry-run

The default needs no credentials and no network: it runs the comparison against
an in-memory fake and prints what each week concluded. It is the fastest way to
see the behaviour that matters, in particular the two cases a naive diff gets
wrong.

**Week 3 is a blocked board.** The crawler could not read the company at all, so
it may not conclude that anything closed. The naive rule — "anything I did not
see is gone" — would report every posting at that company as closed, and then
report them all as new again the following week. On the reference sheet 389 of
8,275 companies fail on a given Friday, so that mistake would manufacture
roughly eleven thousand phantom closures a week.

**Week 5 rewrites a URL.** The board changes its links, so every posting arrives
under a new identity. Matching on the canonical URL and the content fingerprint
recognises them as the postings they already were, instead of reporting a
closure and an opening for each.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from loguru import logger

from sheets._cli import add_common_arguments, configure_logging, connect, report_failure
from sheets.client import SheetsClient
from sheets.jobs import JobRepository, observation

__all__ = ["SCENARIO", "main", "run_scenario"]

#: Every row this command writes carries this prefix, so cleanup can find them.
MARKER: str = "__diff_test__"

COMPANY = f"domain:{MARKER}.example"


def _job(key: str, title: str, url: str) -> Dict[str, str]:
    """Build one observation for the scenario."""
    return observation(
        job_key=key,
        company_key=COMPANY,
        company_name="Diff Test Ltd",
        job_title=title,
        job_url=url,
        url_key=url,
        content_key=f"{MARKER}:{title.lower()}",
        platform="Generic HTML",
        location="Austin, TX",
        country="United States",
    )


#: Week by week: ``(label, observations, companies successfully read, expectation)``.
SCENARIO: Tuple[Tuple[str, List[Dict[str, str]], set, str], ...] = (
    (
        "week 1 — first crawl",
        [
            _job(f"{MARKER}-a", "Platform Engineer", "https://diff.example/jobs/a"),
            _job(f"{MARKER}-b", "Data Analyst", "https://diff.example/jobs/b"),
        ],
        {COMPANY},
        "both postings are new",
    ),
    (
        "week 2 — nothing changed",
        [
            _job(f"{MARKER}-a", "Platform Engineer", "https://diff.example/jobs/a"),
            _job(f"{MARKER}-b", "Data Analyst", "https://diff.example/jobs/b"),
        ],
        {COMPANY},
        "nothing new, nothing closed",
    ),
    (
        "week 3 — board blocked (Cloudflare)",
        [],
        set(),
        "no closures: the board was never read",
    ),
    (
        "week 4 — one role filled, one added",
        [
            _job(f"{MARKER}-a", "Platform Engineer", "https://diff.example/jobs/a"),
            _job(f"{MARKER}-c", "Security Engineer", "https://diff.example/jobs/c"),
        ],
        {COMPANY},
        "1 new, 1 closed",
    ),
    (
        "week 5 — board rewrote every URL",
        [
            _job(f"{MARKER}-a2", "Platform Engineer", "https://diff.example/careers/a"),
            _job(f"{MARKER}-c2", "Security Engineer", "https://diff.example/careers/c"),
        ],
        {COMPANY},
        "0 new, 0 closed, 2 re-linked",
    ),
    (
        "week 6 — a closed posting reopens",
        [
            _job(f"{MARKER}-a2", "Platform Engineer", "https://diff.example/careers/a"),
            _job(f"{MARKER}-c2", "Security Engineer", "https://diff.example/careers/c"),
            _job(f"{MARKER}-b", "Data Analyst", "https://diff.example/jobs/b"),
        ],
        {COMPANY},
        "1 reopened",
    ),
)


def run_scenario(
    client: SheetsClient, dry_run: bool = False
) -> List[Tuple[str, Dict[str, int], str]]:
    """Play the scenario through a repository.

    Args:
        client: The Sheets client — real or fake.
        dry_run: Write nothing. The comparison is still computed, but because
            nothing is stored, every week is compared against an empty history.

    Returns:
        ``(label, counts, expectation)`` per week.
    """
    jobs = JobRepository(client)
    results: List[Tuple[str, Dict[str, int], str]] = []

    for index, (label, observations, crawled, expectation) in enumerate(SCENARIO, start=1):
        applied = jobs.apply(
            observations,
            crawled,
            run_id=f"{MARKER}-run-{index}",
            dry_run=dry_run,
        )
        results.append((label, applied.changes.summary(), expectation))

    return results


def _offline_client() -> SheetsClient:
    """Build a client over an in-memory spreadsheet with the nine tabs.

    Returns:
        The client.
    """
    from sheets.init import initialise
    from tests._fake_sheets import FakeSheetsService

    service = FakeSheetsService({"Sheet1": []})
    client = SheetsClient(service, "offline", sleep=lambda _seconds: None)
    initialise(client)
    return client


def _cleanup(client: SheetsClient) -> int:
    """Blank every row this command wrote.

    Args:
        client: The Sheets client.

    Returns:
        How many rows were cleared.
    """
    from sheets.schema import CURRENT_JOBS, JOB_HISTORY, NEW_LAST_WEEK
    from sheets.storage import TabStore

    cleared = 0
    for spec in (JOB_HISTORY, CURRENT_JOBS, NEW_LAST_WEEK):
        store = TabStore(client, spec)
        records = store.read()
        keep = [
            record.values
            for record in records
            if MARKER not in "".join(str(value) for value in record.values.values())
        ]
        removed = len(records) - len(keep)
        if removed:
            store.replace(keep, dry_run=False)
            cleared += removed

    return cleared


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m sheets.diff_test",
        description="Demonstrate the weekly diff over six scripted weeks.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run against the real spreadsheet instead of an in-memory one",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="With --live, leave the test rows in place instead of cleaning up",
    )
    return add_common_arguments(parser).parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Play the scenario and print what each week concluded.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` when every week matched its expectation, ``1`` otherwise.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    configure_logging(args.log_level)

    account = "offline (in-memory spreadsheet)"

    if args.live:
        connection, code = connect(args)
        if connection is None:
            return code
        client = connection.client
        account = connection.account
    else:
        client = _offline_client()

    try:
        results = run_scenario(client, dry_run=args.dry_run)
        cleared = 0
        if args.live and not args.dry_run and not args.keep:
            cleared = _cleanup(client)
    except Exception as exc:  # noqa: BLE001 - reported with the API's wording
        return report_failure(exc, account)

    rule = "=" * 88
    print(rule)
    print("WEEKLY DIFF" + (" — DRY RUN (nothing written)" if args.dry_run else ""))
    print(rule)
    print(f"  running against  {account}")
    print("-" * 88)
    print(f"  {'Week':<38}{'new':>5}{'reopen':>8}{'same':>6}{'closed':>8}{'relink':>8}{'held':>6}")
    print("-" * 88)

    failures = 0
    for label, counts, expectation in results:
        print(
            f"  {label[:37]:<38}{counts['jobs_new']:>5}{counts['jobs_reopened']:>8}"
            f"{counts['jobs_still_active']:>6}{counts['jobs_closed']:>8}"
            f"{counts['jobs_relinked']:>8}{counts['closures_withheld']:>6}"
        )
        print(f"      expected: {expectation}")

    print("-" * 88)

    # The dry run compares every week against an empty history, so the
    # scripted expectations only hold when the writes actually happen.
    if not args.dry_run:
        checks = [
            ("week 1 reports both as new", results[0][1]["jobs_new"] == 2),
            ("week 2 reports nothing", results[1][1]["jobs_new"] == 0 and results[1][1]["jobs_closed"] == 0),
            ("week 3 closes nothing", results[2][1]["jobs_closed"] == 0),
            ("week 3 withholds closures", results[2][1]["closures_withheld"] == 2),
            ("week 4 finds 1 new, 1 closed", results[3][1]["jobs_new"] == 1 and results[3][1]["jobs_closed"] == 1),
            ("week 5 re-links rather than churning", results[4][1]["jobs_relinked"] == 2),
            ("week 5 reports no new and no closed", results[4][1]["jobs_new"] == 0 and results[4][1]["jobs_closed"] == 0),
            ("week 6 reopens the closed posting", results[5][1]["jobs_reopened"] == 1),
        ]
        for label, passed in checks:
            print(f"  [{'PASS' if passed else 'FAIL'}]  {label}")
            failures += 0 if passed else 1
        print("-" * 88)

    if args.live and not args.dry_run and not args.keep:
        print(f"  cleaned up {cleared} test row(s)")
    print(rule)

    return 0 if failures == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
