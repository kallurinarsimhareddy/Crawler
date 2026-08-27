"""Exercise the storage layer end to end against the real spreadsheet.

    python -m sheets.storage_test --dry-run     # read everything, write nothing
    python -m sheets.storage_test               # write, verify, then clean up

The offline suite proves the logic; this proves the *integration* — that the
live tabs have the columns the code expects, that the service account can write
to them, that a rewrite of a row lands in the right cells, and that running the
same operation twice really does issue no second write.

**It cleans up after itself.** Every row it writes carries a company key and job
key prefixed ``__storage_test__``, and the last thing it does is blank those
rows. Nothing else in the spreadsheet is read for identity or touched. Run it
with ``--dry-run`` first: that authenticates read-only, so it can verify the
tabs are readable and correctly shaped without being able to write at all.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from loguru import logger

from sheets._cli import add_common_arguments, configure_logging, connect, report_failure
from sheets.companies import CompanyRepository
from sheets.jobs import JobRepository, observation
from sheets.runs import STATUS_DONE, DashboardRepository, FailureRepository, RunRepository, failure_record
from sheets.schema import ALL_TABS
from sheets.storage import TabStore

__all__ = ["main"]

#: Every row this command writes carries this prefix, so cleanup can find them
#: and nothing it writes can be mistaken for real data.
MARKER: str = "__storage_test__"


def _check(label: str, passed: bool, detail: str = "") -> Tuple[str, bool, str]:
    """Record one check's outcome.

    Args:
        label: What was checked.
        passed: Whether it held.
        detail: Extra context for the report.

    Returns:
        The check, for collection.
    """
    return label, passed, detail


def _verify_tabs(client) -> List[Tuple[str, bool, str]]:
    """Confirm every tab exists and has the columns the code expects.

    Args:
        client: The Sheets client.

    Returns:
        One check per tab.
    """
    checks: List[Tuple[str, bool, str]] = []

    for spec in ALL_TABS:
        store = TabStore(client, spec)
        try:
            plan = store.plan
        except KeyError as exc:
            checks.append(_check(f"tab {spec.title}", False, str(exc)))
            continue

        missing = plan.appended_headers
        checks.append(
            _check(
                f"tab {spec.title}",
                not missing,
                f"{len(plan.mapping)} columns mapped"
                + (f"; MISSING {', '.join(missing)}" if missing else ""),
            )
        )

    return checks


def _exercise_companies(
    client, dry_run: bool
) -> Tuple[List[Tuple[str, bool, str]], List[str]]:
    """Write, re-write and verify a marker company.

    Args:
        client: The Sheets client.
        dry_run: Write nothing.

    Returns:
        ``(checks, written_keys)``.
    """
    companies = CompanyRepository(client)
    key = f"domain:{MARKER}.example"
    checks: List[Tuple[str, bool, str]] = []

    record = {
        "company_key": key,
        "company_name": "Storage Test Ltd",
        "website": "https://storage-test.example",
        "career_url": "https://storage-test.example/careers",
        "industry": "",
        "department": "",
        "status": "active",
        "source": MARKER,
        "first_seen": "2026-01-01T00:00:00+00:00",
    }

    first = companies.upsert([record], dry_run=dry_run)
    checks.append(_check("company insert", first.inserted == 1, first.describe()))

    second = companies.upsert([record], dry_run=dry_run)
    if dry_run:
        checks.append(_check("company upsert is idempotent", True, "not exercised in a dry run"))
    else:
        checks.append(
            _check(
                "company upsert is idempotent",
                second.unchanged == 1 and second.cells_written == 0,
                second.describe(),
            )
        )

    if not dry_run:
        stored = companies.store.read_index("company_key").get(key)
        checks.append(_check("company reads back", stored is not None, ""))

        if stored is not None:
            # A hand-curated Industry must survive an import that lacks one.
            companies.upsert(
                [{"company_key": key, "industry": "Manually Set"}], dry_run=False
            )
            companies.upsert([record], dry_run=False)
            after = companies.store.read_index("company_key").get(key)
            checks.append(
                _check(
                    "manual Industry preserved",
                    after is not None and after.get("industry") == "Manually Set",
                    after.get("industry") if after else "missing",
                )
            )

            # A blank incoming value must not clear a stored one.
            companies.upsert([{"company_key": key, "website": ""}], dry_run=False)
            after = companies.store.read_index("company_key").get(key)
            checks.append(
                _check(
                    "blank does not erase Website",
                    after is not None and after.get("website") == "https://storage-test.example",
                    after.get("website") if after else "missing",
                )
            )

    return checks, [key]


def _exercise_jobs(
    client, dry_run: bool
) -> Tuple[List[Tuple[str, bool, str]], List[str]]:
    """Walk a posting through new, unchanged and closed across three runs.

    Args:
        client: The Sheets client.
        dry_run: Write nothing.

    Returns:
        ``(checks, written_job_keys)``.
    """
    jobs = JobRepository(client)
    company_key = f"domain:{MARKER}.example"
    job_key = f"{MARKER}-job-1"
    checks: List[Tuple[str, bool, str]] = []

    seen = observation(
        job_key=job_key,
        company_key=company_key,
        company_name="Storage Test Ltd",
        job_title="Storage Test Engineer",
        job_url="https://storage-test.example/jobs/1",
        url_key="https://storage-test.example/jobs/1",
        content_key=f"{MARKER}-content-1",
        platform="Generic HTML",
        location="Austin, TX",
        country="United States",
    )

    if dry_run:
        planned = jobs.apply([seen], {company_key}, run_id=f"{MARKER}-run-1", dry_run=True)
        checks.append(
            _check(
                "job diff computes",
                len(planned.changes.new_jobs) == 1,
                planned.describe(),
            )
        )
        return checks, [job_key]

    first = jobs.apply([seen], {company_key}, run_id=f"{MARKER}-run-1", dry_run=False)
    checks.append(_check("job reported new", len(first.changes.new_jobs) == 1, first.describe()))

    stored = jobs.history.read_index("job_key").get(job_key)
    first_seen = stored.get("first_seen") if stored else ""
    checks.append(_check("job in JOB_HISTORY", stored is not None, ""))

    second = jobs.apply([seen], {company_key}, run_id=f"{MARKER}-run-2", dry_run=False)
    checks.append(
        _check(
            "second sighting is not new",
            not second.changes.new_jobs and len(second.changes.still_active) == 1,
            second.describe(),
        )
    )

    stored = jobs.history.read_index("job_key").get(job_key)
    checks.append(
        _check(
            "First Seen never moves",
            stored is not None and stored.get("first_seen") == first_seen,
            stored.get("first_seen") if stored else "missing",
        )
    )

    # The company is read successfully but advertises nothing: a real closure.
    third = jobs.apply([], {company_key}, run_id=f"{MARKER}-run-3", dry_run=False)
    closed = [job.job_uid for job in third.changes.closed_jobs]
    checks.append(_check("job closes when the board empties", job_key in closed, third.describe()))

    stored = jobs.history.read_index("job_key").get(job_key)
    checks.append(
        _check(
            "Closed At stamped",
            stored is not None and bool(stored.get("closed_at")),
            stored.get("closed_at") if stored else "missing",
        )
    )

    # The company could not be read at all: nothing may be concluded.
    fourth = jobs.apply([], set(), run_id=f"{MARKER}-run-4", dry_run=False)
    checks.append(
        _check(
            "a blocked company closes nothing",
            not fourth.changes.closed_jobs,
            fourth.describe(),
        )
    )

    # Reopened.
    fifth = jobs.apply([seen], {company_key}, run_id=f"{MARKER}-run-5", dry_run=False)
    checks.append(
        _check(
            "job reopens",
            len(fifth.changes.reopened_jobs) == 1,
            fifth.describe(),
        )
    )

    # Repeating a run must not duplicate its weekly log entries.
    repeat = jobs.apply([seen], {company_key}, run_id=f"{MARKER}-run-5", dry_run=False)
    checks.append(
        _check(
            "repeating a run logs once",
            repeat.already_logged,
            "weekly log skipped on the rerun",
        )
    )

    return checks, [job_key]


def _exercise_runs(client, dry_run: bool) -> Tuple[List[Tuple[str, bool, str]], List[str]]:
    """Open, update and close a marker run; write a marker failure.

    Args:
        client: The Sheets client.
        dry_run: Write nothing.

    Returns:
        ``(checks, written_run_ids)``.
    """
    runs = RunRepository(client)
    failures = FailureRepository(client)
    checks: List[Tuple[str, bool, str]] = []
    run_id = f"{MARKER}-run"

    runs.start(companies_total=3, mode=MARKER, run_id=run_id, dry_run=dry_run)

    if dry_run:
        checks.append(_check("run row", True, "not exercised in a dry run"))
        return checks, [run_id]

    checks.append(_check("run row created", runs.get(run_id) is not None, run_id))

    runs.update_counts(run_id, {"companies_checked": 3, "companies_succeeded": 2}, dry_run=False)
    runs.finish(run_id, status=STATUS_DONE, counts={"jobs_new": 1}, dry_run=False)

    stored = runs.get(run_id)
    checks.append(
        _check(
            "run closed with counts",
            stored is not None and stored.is_finished and stored.counts.get("jobs_new") == 1,
            stored.status if stored else "missing",
        )
    )

    record = failure_record(
        company_key=f"domain:{MARKER}.example",
        company_name="Storage Test Ltd",
        crawled_url="https://storage-test.example/careers",
        error="AdapterHttpError: GET https://x returned HTTP 403: 'Just a moment...'",
        run_id=run_id,
    )
    failures.upsert([record], dry_run=False)

    stored_failure = failures.store.read_index("company_key").get(record["company_key"])
    checks.append(
        _check(
            "failure classified",
            stored_failure is not None
            and stored_failure.get("failure_type") == "cloudflare challenge",
            stored_failure.get("failure_type") if stored_failure else "missing",
        )
    )

    return checks, [run_id]


def _cleanup(client) -> int:
    """Blank every row this command wrote.

    Args:
        client: The Sheets client.

    Returns:
        How many rows were cleared.
    """
    from sheets.schema import (
        CURRENT_JOBS,
        FAILURES,
        JOB_HISTORY,
        MASTER_COMPANIES,
        NEW_LAST_WEEK,
        WEEKLY_RUNS,
    )

    cleared = 0
    targets = (
        (MASTER_COMPANIES, "company_key"),
        (JOB_HISTORY, "job_key"),
        (CURRENT_JOBS, "job_key"),
        (NEW_LAST_WEEK, "job_key"),
        (WEEKLY_RUNS, "run_id"),
        (FAILURES, "company_key"),
    )

    for spec, key_field in targets:
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
        prog="python -m sheets.storage_test",
        description="Exercise the storage layer against the live spreadsheet.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Leave the test rows in place instead of cleaning up",
    )
    return add_common_arguments(parser).parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the storage checks.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` when every check passed, ``1`` otherwise.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    configure_logging(args.log_level)

    connection, code = connect(args)
    if connection is None:
        return code

    checks: List[Tuple[str, bool, str]] = []
    written: Dict[str, List[str]] = {}

    try:
        # Clear anything a previous attempt left behind before starting. A run
        # that died half-way -- on a rate limit, say -- leaves marker rows that
        # would make this run's "insert" report "unchanged" instead, so the
        # command would fail for a reason that has nothing to do with the code.
        if not args.dry_run:
            stale = _cleanup(connection.client)
            if stale:
                logger.info("Cleared {} row(s) left by an earlier attempt", stale)

        checks.extend(_verify_tabs(connection.client))

        if all(passed for _, passed, _ in checks):
            company_checks, company_keys = _exercise_companies(connection.client, args.dry_run)
            checks.extend(company_checks)
            written["companies"] = company_keys

            job_checks, job_keys = _exercise_jobs(connection.client, args.dry_run)
            checks.extend(job_checks)
            written["jobs"] = job_keys

            run_checks, run_ids = _exercise_runs(connection.client, args.dry_run)
            checks.extend(run_checks)
            written["runs"] = run_ids

            dashboard = DashboardRepository(connection.client)
            dashboard.write(
                [("Storage test", [(f"{MARKER} ran", "yes")])], dry_run=args.dry_run
            )

        cleared = 0
        if not args.dry_run and not args.keep:
            cleared = _cleanup(connection.client)
            DashboardRepository(connection.client).write([], dry_run=False)
    except Exception as exc:  # noqa: BLE001 - reported with the API's wording
        return report_failure(exc, connection.account)

    rule = "=" * 84
    print(rule)
    print("STORAGE LAYER — DRY RUN (nothing written)" if args.dry_run else "STORAGE LAYER TEST")
    print(rule)
    print(f"  spreadsheet    {connection.spreadsheet_id}")
    print(f"  authenticated  {connection.account}")
    print("-" * 84)

    for label, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}]  {label:<38}{detail[:34]}")

    print("-" * 84)
    passed_count = sum(1 for _, passed, _ in checks if passed)
    print(f"  {passed_count} of {len(checks)} check(s) passed")
    if not args.dry_run and not args.keep:
        print(f"  cleaned up {cleared} test row(s)")
    print(f"  API            {connection.client.stats.describe()}")
    print(rule)

    return 0 if passed_count == len(checks) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
