"""What the crawler is doing, or last did.

    python -m crawler.status
    python -m crawler.status --failures      # what is blocked, and why
    python -m crawler.status --recover       # release claims a dead run left

At sixty-five companies a run finishes before you can ask about it. At twelve
thousand it runs for hours, and "is it working" becomes a question the system
has to be able to answer without reading a log. Everything here comes from
SQLite, so it answers while a run is in flight and after one has died.

:option:`--recover` is the safe recovery mechanism: a run killed mid-flight
leaves companies marked ``running`` with nobody working on them, and this
returns those to ``pending`` so the next run picks them up. It only touches
claims older than the lease, so it is safe to use while a healthy run is going.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, Optional, Sequence

from store import Database, migrate
from store.database import DEFAULT_DATABASE_PATH
from store.queue import DEFAULT_LEASE_SECONDS, CrawlQueue, DiscoveryQueue
from store.repositories import CompanyRepository, JobRepository

__all__ = ["main", "report"]


def _bar(count: int, total: int, width: int = 28) -> str:
    """A proportion, drawn.

    Args:
        count: This slice.
        total: The whole.
        width: Characters at full.

    Returns:
        The bar.
    """
    if total <= 0:
        return ""
    filled = int(round(width * count / total))
    return "#" * filled + "." * (width - filled)


def report(database: Database, failures: bool = False) -> str:
    """Render the current state.

    Args:
        database: The store.
        failures: Whether to list what is blocked and why.

    Returns:
        The report.
    """
    companies = CompanyRepository(database)
    jobs = JobRepository(database)
    crawl = CrawlQueue(database)
    discovery = DiscoveryQueue(database)

    rule = "=" * 78
    lines = [rule, "CAREERCRAWLER STATUS", rule, ""]

    total = companies.count()
    lines += [
        "  Companies",
        "  " + "-" * 74,
        f"    known                        {total:>10,}",
        f"    with a stored board          {_scalar(database, 'SELECT COUNT(*) AS n FROM companies WHERE it_link != %s', ('',)):>10,}",
        "",
    ]

    crawl_stats = crawl.stats()
    queued = sum(crawl_stats.values())
    lines += ["  Crawl queue", "  " + "-" * 74]
    if queued:
        for state, count in crawl_stats.items():
            lines.append(f"    {state:<16}{count:>10,}  {_bar(count, queued)}")
    else:
        lines.append("    (nothing queued)")
    lines.append("")

    discovery_stats = discovery.stats()
    discovered = sum(discovery_stats.values())
    lines += ["  Discovery queue", "  " + "-" * 74]
    if discovered:
        for state, count in discovery_stats.items():
            if count:
                lines.append(f"    {state:<16}{count:>10,}")
    else:
        lines.append("    (nothing queued -- discovery is a separate, explicit step)")
    lines.append("")

    lines += [
        "  Jobs",
        "  " + "-" * 74,
        f"    total                        {jobs.count():>10,}",
        f"    active                       {jobs.count('active'):>10,}",
        f"    closed                       {jobs.count('closed'):>10,}",
        "",
    ]

    attempts = database.one(
        "SELECT COUNT(*) AS n, AVG(seconds) AS mean, SUM(browser_used) AS browsers, "
        "SUM(jobs_found) AS found FROM crawl_attempts"
    ) or {}
    if attempts.get("n"):
        lines += [
            "  Attempts",
            "  " + "-" * 74,
            f"    recorded                     {int(attempts['n'] or 0):>10,}",
            f"    average seconds              {float(attempts.get('mean') or 0):>10.1f}",
            f"    browser visits               {int(attempts.get('browsers') or 0):>10,}",
            f"    postings seen                {int(attempts.get('found') or 0):>10,}",
            "",
        ]

    reasons = database.query(
        "SELECT last_reason AS reason, COUNT(*) AS n FROM crawl_queue "
        "WHERE last_reason != '' GROUP BY last_reason ORDER BY n DESC LIMIT 12"
    )
    if reasons:
        lines += ["  Failure reasons", "  " + "-" * 74]
        for row in reasons:
            lines.append(f"    {str(row['reason'])[:40]:<42}{int(row['n']):>10,}")
        lines.append("")

    stale = database.one(
        "SELECT COUNT(*) AS n FROM crawl_queue WHERE state = 'running'"
    ) or {}
    if int(stale.get("n") or 0):
        lines += [
            f"  {int(stale['n'])} company(ies) marked running.",
            "  If no crawl is in flight, release them with:  python -m crawler.status --recover",
            "",
        ]

    if failures:
        blocked = database.query(
            "SELECT q.company_key, c.company_name, q.state, q.last_reason, q.attempts "
            "FROM crawl_queue q JOIN companies c ON c.company_key = q.company_key "
            "WHERE q.state IN ('failed', 'blocked') ORDER BY q.last_reason LIMIT 60"
        )
        lines += [rule, "BLOCKED AND FAILED", rule]
        for row in blocked:
            lines.append(
                f"  {str(row['company_name'])[:30]:<32}{str(row['state']):<10}"
                f"{str(row['last_reason'])[:30]}"
            )
        if not blocked:
            lines.append("  none")
        lines.append("")

    return "\n".join(lines)


def _scalar(database: Database, sql: str, parameters: Sequence[Any] = ()) -> int:
    """One count.

    Args:
        database: The store.
        sql: A query selecting ``n``. ``%s`` marks a placeholder, so the
            surrounding f-string does not have to fight SQLite's ``?``.
        parameters: Bound values.

    Returns:
        The count.
    """
    row = database.one(sql.replace("%s", "?"), parameters)
    return int(row["n"]) if row else 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m crawler.status",
        description="Report the crawler's queue, jobs and failures.",
    )
    parser.add_argument("--failures", action="store_true",
                        help="List every blocked and failed company with its reason")
    parser.add_argument("--recover", action="store_true",
                        help="Return stale 'running' claims to pending")
    parser.add_argument("--lease", type=float, default=DEFAULT_LEASE_SECONDS,
                        help="With --recover, how old a claim must be (seconds)")
    parser.add_argument("--database", default=None, help="Database file")
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the status report.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0``.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    database = Database(args.database or DEFAULT_DATABASE_PATH)
    migrate(database)

    try:
        if args.recover:
            released = CrawlQueue(database).release_stale(lease_seconds=args.lease)
            released += DiscoveryQueue(database).release_stale(lease_seconds=args.lease)
            print(f"\nReleased {released} stale claim(s) back to pending.\n")

        print(report(database, failures=args.failures))
    finally:
        database.close()

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
