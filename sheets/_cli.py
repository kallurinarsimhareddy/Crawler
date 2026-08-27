"""Argument parsing and connection setup shared by the Sheets commands.

Four commands need the same four things — a spreadsheet, credentials, a client,
and a ``--dry-run`` flag that is honoured all the way down — and getting any of
them subtly different between commands is how a "safe" command turns out to
write. So they are written once, here.

A dry run authenticates with the **read-only** scope. That is not decoration:
it means a command invoked with ``--dry-run`` is refused a write by Google
itself, rather than relying on every code path below having remembered to check
the flag.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from loguru import logger

from sheets.auth import CredentialsError, build_service, resolve_credentials, resolve_spreadsheet_id
from sheets.client import SheetsClient

__all__ = ["Connection", "add_common_arguments", "configure_logging", "connect"]


@dataclass
class Connection:
    """A ready client and an account of how it was obtained.

    Attributes:
        client: The Sheets client.
        account: The identity authenticated as.
        spreadsheet_id: The spreadsheet.
        read_only: Whether the credentials can write at all.
    """

    client: SheetsClient
    account: str
    spreadsheet_id: str
    read_only: bool


def add_common_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add the arguments every Sheets command takes.

    Args:
        parser: The parser to extend.

    Returns:
        The same parser, for chaining.
    """
    parser.add_argument(
        "--spreadsheet",
        default=None,
        help="Spreadsheet id or URL (default: CAREERCRAWLER_SPREADSHEET_ID)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and write nothing (authenticates read-only)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"),
        help="Console log level (default: INFO)",
    )
    return parser


def configure_logging(level: str) -> None:
    """Point loguru at a single, quiet console sink.

    Args:
        level: The console log level.
    """
    logger.remove()
    logger.add(sys.stderr, level=level, format="<level>{level: <8}</level> | {message}")


def connect(args: argparse.Namespace) -> Tuple[Optional[Connection], int]:
    """Resolve credentials and build a client.

    Args:
        args: Parsed arguments carrying ``spreadsheet`` and ``dry_run``.

    Returns:
        ``(connection, exit_code)``. On failure the connection is ``None`` and
        the code is ``2`` for "not configured" or ``1`` for anything else, with
        the reason already printed.
    """
    read_only = bool(getattr(args, "dry_run", False))

    try:
        spreadsheet_id = resolve_spreadsheet_id(getattr(args, "spreadsheet", None))
        credentials = resolve_credentials(read_only=read_only)
    except CredentialsError as exc:
        print(f"\nNot configured yet.\n\n{exc}\n", file=sys.stderr)
        return None, 2

    try:
        service = build_service(read_only=read_only, credentials=credentials.credentials)
    except CredentialsError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return None, 2

    return (
        Connection(
            client=SheetsClient(service, spreadsheet_id),
            account=credentials.account,
            spreadsheet_id=spreadsheet_id,
            read_only=read_only,
        ),
        0,
    )


def report_failure(exc: BaseException, account: str = "") -> int:
    """Print an API failure in terms the operator can act on.

    Args:
        exc: What went wrong.
        account: The service account in use, if any.

    Returns:
        ``1``, for use as an exit code.
    """
    print(f"\nThe spreadsheet could not be used:\n  {exc}\n", file=sys.stderr)

    message = str(exc).lower()
    if "permission" in message or "403" in message:
        if account:
            print(
                f"Share the spreadsheet as an Editor with:\n  {account}\n",
                file=sys.stderr,
            )
    if "no " in message and "tab" in message:
        print("Run:  python -m sheets.init\n", file=sys.stderr)

    return 1
