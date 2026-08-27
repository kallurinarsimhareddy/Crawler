"""Import the company CSV into ``MASTER_COMPANIES``.

    python -m sheets.import_companies --input "input\\companies.csv"
    python -m sheets.import_companies --input "input\\companies.csv" --dry-run
    python -m sheets.import_companies --limit 50

Safe to run as often as you like. A company already on the list is matched by
its domain rather than its name, so re-importing after editing the CSV adds the
new rows and moves nothing else. The second run of an unchanged CSV writes
nothing at all.

Two things the import will not do, both because the sheet is yours:

* a blank value in the CSV never clears a value already in the sheet, so
  importing an export that happens to lack the ``Website`` column cannot erase
  eight thousand websites;
* ``Department`` and ``Industry`` are never overwritten once they hold
  anything, because those are the columns a person curates and a CSV export
  usually cannot.

Encoding is handled: a CSV saved as Windows-1252 — the recurring ``0xA0``
problem — is decoded and reported rather than being fatal. The file on disk is
only ever read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from loguru import logger

from crawler.csv_reader import DEFAULT_CSV_PATH
from sheets._cli import add_common_arguments, configure_logging, connect, report_failure
from sheets.companies import CompanyRepository

__all__ = ["main"]

#: Project root, so the default input path resolves from any directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m sheets.import_companies",
        description="Import the company CSV into MASTER_COMPANIES. Safe to repeat.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT / DEFAULT_CSV_PATH,
        help="Input CSV (default: input/companies.csv)",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Import only the first N rows, for a smoke test"
    )
    return add_common_arguments(parser).parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Import the CSV.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``2`` when nothing is configured, ``1`` on failure.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    configure_logging(args.log_level)

    connection, code = connect(args)
    if connection is None:
        return code

    companies = CompanyRepository(connection.client)

    try:
        before = companies.count()

        if args.limit > 0:
            from crawler.csv_reader import read_companies_with_encoding

            rows, decoded = read_companies_with_encoding(args.input)
            result, collapsed = companies.import_rows(
                rows[: args.limit], dry_run=args.dry_run
            )
            encoding = decoded.encoding
        else:
            result, collapsed, encoding = companies.import_csv(
                args.input, dry_run=args.dry_run
            )

        after = before if args.dry_run else companies.count()
    except (FileNotFoundError, ValueError) as exc:
        print(f"\nCould not read the input file:\n  {exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - reported with the API's wording
        return report_failure(exc, connection.account)

    rule = "=" * 78
    print(rule)
    print("DRY RUN — nothing was written" if args.dry_run else "COMPANIES IMPORTED")
    print(rule)
    print(f"  source          {args.input}")
    print(f"  encoding        {encoding}")
    print(f"  tab             {companies.store.title}")
    print(f"  authenticated   {connection.account}")
    print("-" * 78)
    print(f"  inserted        {result.inserted:>8,}")
    print(f"  updated         {result.updated:>8,}")
    print(f"  unchanged       {result.unchanged:>8,}")
    if collapsed:
        print(f"  merged rows     {collapsed:>8,}   (two CSV rows naming one company)")
    if result.skipped:
        print(f"  skipped         {result.skipped:>8,}   (no usable company identity)")
    print("-" * 78)
    print(f"  companies before{before:>8,}")
    print(f"  companies after {after:>8,}")
    print(f"  API             {connection.client.stats.describe()}")
    print(rule)

    if not result.changed and not args.dry_run:
        print("Nothing changed — the sheet already matched the CSV.")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
