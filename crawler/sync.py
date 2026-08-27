"""Moving companies between the sheet and the local store, in both directions.

The division of labour: **Google Sheets is what a person edits and reads.
SQLite is what the crawler runs on.** Neither is a copy of the other, and the
sync is deliberately narrow in each direction.

    python -m crawler.sync --pull                 # sheet -> database
    python -m crawler.sync --push --dry-run       # what would go back
    python -m crawler.sync --push --apply         # send it

**Pull is safe by construction.** It reads the sheet and writes only the local
database. A board the crawler discovered and stored locally is never cleared by
a sheet whose ``IT Link`` column is still blank — the repository's rule that a
blank never overwrites a value does that work.

**Push is deliberately timid.** It writes two columns, ``IT Link`` and
``ATS / Platform``, and only into rows where the sheet's ``IT Link`` is still
empty. An operator's entry always wins; a row that already names a board is
skipped and reported, not overwritten. Nothing else is ever written, so
``Company Key``, ``Website`` and every unrelated column are untouched by
construction rather than by care.

Both directions are idempotent. Running either twice with no change in between
produces no writes at all, which the tests assert directly.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

from store import Database, migrate
from store.repositories import CompanyRepository

__all__ = ["SheetSync", "SyncPlan", "main"]


@dataclass
class SyncPlan:
    """What a sync did, or would do.

    Attributes:
        examined: Rows considered.
        written: Rows actually changed.
        updates: For a push, the exact cells proposed, so a dry run can show
            them before anything is sent.
        skipped: Rows deliberately left alone, with the reason.
    """

    examined: int = 0
    written: int = 0
    updates: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)

    def render(self, direction: str, dry_run: bool) -> str:
        """Render for a terminal.

        Args:
            direction: ``"pull"`` or ``"push"``.
            dry_run: Whether anything was written.

        Returns:
            The report.
        """
        rule = "=" * 88
        lines = [
            rule,
            f"SYNC {direction.upper()}" + ("  (dry run - nothing written)" if dry_run else ""),
            rule,
            f"  rows examined   {self.examined}",
            f"  rows to write   {len(self.updates) if direction == 'push' else self.written}",
            f"  rows skipped    {len(self.skipped)}",
            "",
        ]

        if direction == "push" and self.updates:
            lines += [rule, "CELLS", rule]
            for update in self.updates[:50]:
                lines.append(f"  row {update['row']:>6}  {update['company'][:30]:<32}")
                for name, value in update["fields"].items():
                    lines.append(f"        {name:<10} = {str(value)[:60]}")
            if len(self.updates) > 50:
                lines.append(f"  ... and {len(self.updates) - 50} more")
            lines.append("")

        if self.skipped:
            reasons: Dict[str, int] = {}
            for row in self.skipped:
                reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
            lines += [rule, "SKIPPED", rule]
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
                lines.append(f"  {reason[:56]:<58}{count:>6}")
            lines.append("")

        return "\n".join(lines)


class SheetSync:
    """Synchronises MASTER_COMPANIES with the local store.

    Args:
        client: A :class:`sheets.client.SheetsClient`.
        database: The local store.
    """

    def __init__(self, client: Any, database: Database) -> None:
        self.client = client
        self.database = database
        self.companies = CompanyRepository(database)

    # -- sheet -> database ---------------------------------------------------

    def pull(self, batch_size: int = 500) -> SyncPlan:
        """Import the sheet's company list into the local store.

        Reads the whole tab once — the Sheets quota makes anything else
        impractical — but writes in batches so a hundred thousand rows never
        sit in memory as ORM objects.

        Args:
            batch_size: Companies written per transaction.

        Returns:
            What was imported.
        """
        from sheets.companies import CompanyRepository as SheetCompanies
        from utils.names import company_key as derive_company_key

        plan = SyncPlan()
        sheet = SheetCompanies(self.client)

        batch: List[Dict[str, Any]] = []
        for record in sheet.store.read():
            plan.examined += 1

            name = record.get("company_name")
            if not name:
                plan.skipped.append({"row": str(record.row), "reason": "no company name"})
                continue

            key = record.get("company_key") or derive_company_key(
                name, record.get("website"),
                record.get("career_url") or record.get("it_link"),
            )
            if not key:
                plan.skipped.append(
                    {"row": str(record.row), "reason": "no derivable company key"}
                )
                continue

            batch.append({
                "company_key": key,
                "company_name": name,
                "website": record.get("website"),
                "career_url": record.get("career_url"),
                "it_link": record.get("it_link"),
                "platform": record.get("platform"),
                "status": record.get("status") or "active",
                "sheet_row": record.row,
            })

            if len(batch) >= batch_size:
                plan.written += self.companies.upsert_many(batch)
                batch = []

        if batch:
            plan.written += self.companies.upsert_many(batch)

        logger.info(
            "Sync pull: {} row(s) examined, {} written, {} skipped",
            plan.examined, plan.written, len(plan.skipped),
        )
        return plan

    # -- database -> sheet ---------------------------------------------------

    def push(self, dry_run: bool = True) -> SyncPlan:
        """Send discovered boards back to the sheet.

        Writes ``IT Link`` and ``ATS / Platform`` and nothing else, and only
        where the sheet's own ``IT Link`` is still empty.

        Args:
            dry_run: Work out the writes and send none.

        Returns:
            What was written, or would be.
        """
        from crawler.platform_detector import detect_platform
        from crawler.resolve import is_ats
        from sheets.companies import CompanyRepository as SheetCompanies

        plan = SyncPlan()
        sheet = SheetCompanies(self.client)
        rows = sheet.store.read_index("company_key")

        changes: Dict[int, Dict[str, str]] = {}

        for company in self.companies.stream():
            board = (company.get("it_link") or "").strip()
            if not board:
                continue

            plan.examined += 1
            key = company["company_key"]
            row = rows.get(key)

            if row is None:
                plan.skipped.append({"row": "0", "reason": "company is not in the sheet"})
                continue

            stored = (row.get("it_link") or "").strip()
            if stored:
                # An operator's entry, or an earlier push. Either way it wins.
                if stored != board:
                    plan.skipped.append({
                        "row": str(row.row),
                        "reason": "sheet already names a board; not overwritten",
                    })
                continue

            platform = (company.get("platform") or "").strip()
            if not platform:
                detected = detect_platform(board)
                platform = detected.value if is_ats(detected) else "Generic HTML"

            changes[row.row] = {"it_link": board, "platform": platform}
            plan.updates.append({
                "row": row.row,
                "company": company.get("company_name", ""),
                "fields": {"it_link": board, "platform": platform},
            })

        if not dry_run and changes:
            result = sheet.store.update_rows(changes, dry_run=False)
            plan.written = getattr(result, "updated", 0)
            logger.success("Sync push: wrote {} row(s)", plan.written)

        return plan


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m crawler.sync",
        description="Synchronise MASTER_COMPANIES with the local crawler database.",
    )
    direction = parser.add_mutually_exclusive_group(required=True)
    direction.add_argument("--pull", action="store_true",
                           help="Sheet -> database. Writes nothing to the sheet")
    direction.add_argument("--push", action="store_true",
                           help="Database -> sheet, IT Link and ATS / Platform only")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Report what a push would write, and write nothing")
    mode.add_argument("--apply", action="store_true",
                      help="Actually write. Required for a push to have any effect")

    parser.add_argument("--database", default=None, help="Database file")
    parser.add_argument("--spreadsheet", default=None, help="Spreadsheet id or URL")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"))
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run a sync.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``2`` when unconfigured, ``1`` on an API failure.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    logger.remove()
    logger.add(sys.stderr, level=args.log_level, format="<level>{level: <8}</level> | {message}")

    writing = bool(args.push and args.apply)

    from sheets._cli import connect, report_failure
    from store.database import DEFAULT_DATABASE_PATH

    connection, code = connect(args, read_only=not writing)
    if connection is None:
        return code

    database = Database(args.database or DEFAULT_DATABASE_PATH)
    migrate(database)
    sync = SheetSync(connection.client, database)

    try:
        if args.pull:
            plan = sync.pull()
            print(plan.render("pull", dry_run=False))
        else:
            plan = sync.push(dry_run=not writing)
            print(plan.render("push", dry_run=not writing))
            if not writing:
                print("  Nothing was written. Re-run with --push --apply to send these.")
    except Exception as exc:  # noqa: BLE001 - report the API's own wording
        return report_failure(exc, connection.account)
    finally:
        database.close()

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
