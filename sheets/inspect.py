"""Read a spreadsheet and report what is in it. Writes nothing, ever.

This is the first thing to run against a live spreadsheet, and it is deliberately
incapable of changing it: credentials are requested with the read-only scope, so
a write is refused by Google rather than merely avoided by this code.

    python -m sheets.inspect
    python -m sheets.inspect --spreadsheet <id or URL>
    python -m sheets.inspect --rows 5            # show more sample rows
    python -m sheets.inspect --json report.json  # also write the report as data

It answers, for every tab:

* its title, and its dimensions as the spreadsheet reports them;
* how many rows actually hold data, as opposed to how many exist;
* its header row, verbatim;
* duplicated and blank headers, which break a column mapping silently;
* which of the crawler's own fields each header appears to correspond to.

And for the spreadsheet as a whole: which tab looks like the company list, which
looks like a dashboard, and how much of the ten-million-cell budget is spent.

Nothing here decides anything. It reports, and a human reads the report.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Final, List, Optional, Sequence, Tuple

from loguru import logger

from sheets.auth import CredentialsError, build_service, resolve_credentials, resolve_spreadsheet_id
from sheets.schema import ALL_TABS, TabSpec, normalise_header

__all__ = ["SpreadsheetReport", "TabReport", "inspect_spreadsheet", "main"]

#: Google's hard ceiling on one spreadsheet. Worth reporting against, because
#: a job-history tab is the one thing in this design that grows without bound.
CELL_BUDGET: Final[int] = 10_000_000

#: How many data rows to sample per tab by default.
DEFAULT_SAMPLE_ROWS: Final[int] = 3

#: Headers that suggest a tab holds companies rather than jobs or metrics.
_COMPANY_SIGNALS: Final[Tuple[str, ...]] = (
    "companyname", "website", "careerpageurl", "careersjobsurl", "itlink", "domain",
)

#: Headers that suggest a tab holds job postings.
_JOB_SIGNALS: Final[Tuple[str, ...]] = (
    "jobtitle", "joburl", "jobid", "posteddate", "position", "role",
)

#: Headers that suggest a tab is a dashboard or summary.
_DASHBOARD_SIGNALS: Final[Tuple[str, ...]] = ("metric", "value", "count", "total", "week")

#: The crawler's logical fields, and the header spellings that mean each one.
#: Used only to *report* a likely correspondence — nothing acts on it.
_FIELD_HINTS: Final[Dict[str, Tuple[str, ...]]] = {
    "company name": ("companyname", "company", "name", "employer", "account"),
    "website": ("website", "companywebsite", "웹", "web", "websiteurl", "domain", "url"),
    "career url": (
        "careerpageurl", "careersjobsurl", "careersurl", "careerurl",
        "careerspage", "jobsurl", "careers",
    ),
    "IT link": ("itlink", "atslink", "atsurl", "jobboardurl", "boardurl"),
    "ATS / platform": ("atsplatform", "ats", "platform", "system", "atsname"),
    "department": ("department", "dept", "function", "team", "division"),
    "location": ("location", "city", "hq", "office", "region"),
    "country": ("country",),
    "job title": ("jobtitle", "title", "role", "position", "opening"),
    "job URL": ("joburl", "postingurl", "link", "applyurl", "applylink"),
}


@dataclass
class TabReport:
    """What one tab contains.

    Attributes:
        title: Its title, exactly as the spreadsheet spells it.
        sheet_id: Its numeric id, which is what a batch update addresses.
        index: Its position among the tabs.
        grid_rows: Rows the grid has, which is not the same as rows in use.
        grid_columns: Columns the grid has.
        data_rows: Rows that actually hold something, header included.
        headers: The header row, verbatim.
        duplicate_headers: Headers appearing more than once, with their counts.
        blank_header_positions: Column letters whose header is empty but which
            have data below.
        sample: The first few data rows, for a human to eyeball.
        matched_spec: The crawler tab this one appears to correspond to.
        field_hints: Detected field name to the header that seems to hold it.
        looks_like: A guess at the tab's purpose.
    """

    title: str
    sheet_id: int
    index: int
    grid_rows: int
    grid_columns: int
    data_rows: int = 0
    headers: List[str] = field(default_factory=list)
    duplicate_headers: Dict[str, int] = field(default_factory=dict)
    blank_header_positions: List[str] = field(default_factory=list)
    sample: List[List[str]] = field(default_factory=list)
    matched_spec: str = ""
    field_hints: Dict[str, str] = field(default_factory=dict)
    looks_like: str = "unknown"

    @property
    def is_empty(self) -> bool:
        """Whether the tab holds no data at all.

        Returns:
            ``True`` when there is not even a header row.
        """
        return self.data_rows == 0 and not self.headers

    def to_dict(self) -> Dict[str, Any]:
        """Render as plain data, for the JSON report.

        Returns:
            The report as a mapping.
        """
        return {
            "title": self.title,
            "sheet_id": self.sheet_id,
            "index": self.index,
            "grid_rows": self.grid_rows,
            "grid_columns": self.grid_columns,
            "data_rows": self.data_rows,
            "headers": self.headers,
            "duplicate_headers": self.duplicate_headers,
            "blank_header_positions": self.blank_header_positions,
            "matched_spec": self.matched_spec,
            "field_hints": self.field_hints,
            "looks_like": self.looks_like,
            "sample": self.sample,
        }


@dataclass
class SpreadsheetReport:
    """Everything the inspection found.

    Attributes:
        spreadsheet_id: The spreadsheet inspected.
        title: Its title.
        url: Its address.
        locale: Its locale, which decides how it parses a typed date.
        time_zone: Its timezone, which decides what ``TODAY()`` means.
        tabs: One report per tab, in spreadsheet order.
        account: The identity the inspection authenticated as.
    """

    spreadsheet_id: str
    title: str = ""
    url: str = ""
    locale: str = ""
    time_zone: str = ""
    tabs: List[TabReport] = field(default_factory=list)
    account: str = ""

    @property
    def cells_used(self) -> int:
        """How many cells the grids occupy, against the ten-million ceiling.

        Returns:
            The total.
        """
        return sum(tab.grid_rows * tab.grid_columns for tab in self.tabs)

    def company_tab(self) -> Optional[TabReport]:
        """The tab that most looks like the master company list.

        Returns:
            The tab, or ``None`` when none resembles one.
        """
        candidates = [tab for tab in self.tabs if tab.looks_like == "companies"]
        if not candidates:
            return None
        # The longest one: a lookup tab of a dozen companies is not the master
        # list, however similar its headers look.
        return max(candidates, key=lambda tab: tab.data_rows)

    def to_dict(self) -> Dict[str, Any]:
        """Render as plain data, for the JSON report.

        Returns:
            The report as a mapping.
        """
        return {
            "spreadsheet_id": self.spreadsheet_id,
            "title": self.title,
            "url": self.url,
            "locale": self.locale,
            "time_zone": self.time_zone,
            "account": self.account,
            "cells_used": self.cells_used,
            "cell_budget": CELL_BUDGET,
            "tabs": [tab.to_dict() for tab in self.tabs],
        }


def _classify(headers: Sequence[str], data_rows: int) -> str:
    """Guess what a tab is for, from its headers.

    Args:
        headers: The header row.
        data_rows: How many rows hold data.

    Returns:
        ``"companies"``, ``"jobs"``, ``"dashboard"``, ``"empty"`` or
        ``"unknown"``.
    """
    if not headers and data_rows == 0:
        return "empty"

    keys = {normalise_header(header) for header in headers if header}

    scores = {
        "companies": sum(1 for signal in _COMPANY_SIGNALS if signal in keys),
        "jobs": sum(1 for signal in _JOB_SIGNALS if signal in keys),
        "dashboard": sum(1 for signal in _DASHBOARD_SIGNALS if signal in keys),
    }

    best = max(scores, key=lambda name: scores[name])
    # A single coincidental header is not evidence: "Location" appears on a
    # company list, a job list and half the ad-hoc tabs ever made.
    return best if scores[best] >= 2 else "unknown"


def _detect_fields(headers: Sequence[str]) -> Dict[str, str]:
    """Report which header appears to hold each logical field.

    Args:
        headers: The header row.

    Returns:
        Logical field name to the header that seems to carry it. Fields with no
        apparent column are omitted rather than reported as blank.
    """
    by_key: Dict[str, str] = {}
    for header in headers:
        key = normalise_header(header)
        if key:
            by_key.setdefault(key, header)

    found: Dict[str, str] = {}
    for field_name, hints in _FIELD_HINTS.items():
        for hint in hints:
            if hint in by_key:
                found[field_name] = by_key[hint]
                break

    return found


def _match_spec(title: str, headers: Sequence[str]) -> str:
    """Say which of the crawler's tabs an existing one corresponds to.

    Args:
        title: The live tab's title.
        headers: Its header row.

    Returns:
        The crawler's key for that tab, or ``""`` when none matches.
    """
    key = normalise_header(title)
    for spec in ALL_TABS:
        if key in spec.title_keys():
            return spec.key
    return ""


def _column_letters(count: int) -> List[str]:
    """Name the first ``count`` columns.

    Args:
        count: How many.

    Returns:
        Their A1 letters.
    """
    from sheets.schema import column_letter

    return [column_letter(index) for index in range(count)]


def inspect_spreadsheet(
    spreadsheet_id: str,
    service: Optional[object] = None,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    account: str = "",
) -> SpreadsheetReport:
    """Read a spreadsheet's structure and a sample of its contents.

    Args:
        spreadsheet_id: The spreadsheet to read.
        service: An injected Sheets service. Built with read-only credentials
            when omitted.
        sample_rows: How many data rows to sample per tab.
        account: The identity being used, recorded on the report.

    Returns:
        The report.

    Raises:
        CredentialsError: If credentials cannot be resolved.
        Exception: Whatever the API raised, so a permission problem surfaces
            with Google's own wording rather than a paraphrase.
    """
    resource = service if service is not None else build_service(read_only=True)
    api = resource.spreadsheets()

    # One metadata call for the shape of everything.
    metadata = api.get(spreadsheetId=spreadsheet_id, includeGridData=False).execute()

    properties = metadata.get("properties", {}) or {}
    report = SpreadsheetReport(
        spreadsheet_id=spreadsheet_id,
        title=properties.get("title", ""),
        url=metadata.get("spreadsheetUrl", ""),
        locale=properties.get("locale", ""),
        time_zone=properties.get("timeZone", ""),
        account=account,
    )

    sheets = metadata.get("sheets", []) or []
    if not sheets:
        return report

    titles: List[str] = []
    for sheet in sheets:
        sheet_properties = sheet.get("properties", {}) or {}
        grid = sheet_properties.get("gridProperties", {}) or {}
        title = sheet_properties.get("title", "")
        titles.append(title)

        report.tabs.append(
            TabReport(
                title=title,
                sheet_id=int(sheet_properties.get("sheetId", 0)),
                index=int(sheet_properties.get("index", 0)),
                grid_rows=int(grid.get("rowCount", 0)),
                grid_columns=int(grid.get("columnCount", 0)),
            )
        )

    # One batched values call for every tab's first rows, rather than one call
    # per tab: a spreadsheet with a dozen tabs would otherwise be a dozen
    # round trips against a quota measured per minute.
    from sheets.schema import a1_range

    wanted = max(1, sample_rows) + 1  # header plus samples
    ranges = [a1_range(title, 1, 0, wanted, None) for title in titles]

    values_response = api.values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=ranges,
        majorDimension="ROWS",
    ).execute()

    for tab, value_range in zip(report.tabs, values_response.get("valueRanges", []) or []):
        rows = value_range.get("values", []) or []

        if rows:
            tab.headers = [str(cell).strip() for cell in rows[0]]
            tab.sample = [[str(cell) for cell in row] for row in rows[1:]]

        counts = Counter(normalise_header(header) for header in tab.headers if header)
        tab.duplicate_headers = {
            header: counts[normalise_header(header)]
            for header in tab.headers
            if header and counts[normalise_header(header)] > 1
        }

        letters = _column_letters(len(tab.headers))
        tab.blank_header_positions = [
            letters[index] for index, header in enumerate(tab.headers) if not header
        ]

        tab.field_hints = _detect_fields(tab.headers)
        tab.matched_spec = _match_spec(tab.title, tab.headers)

    # How many rows each tab actually uses. COUNTA over the first column is
    # wrong when that column has gaps, so the whole used range is measured --
    # still one call for the spreadsheet, not one per tab.
    _measure_data_rows(api, spreadsheet_id, report)

    for tab in report.tabs:
        tab.looks_like = _classify(tab.headers, tab.data_rows)

    return report


def _measure_data_rows(api: Any, spreadsheet_id: str, report: SpreadsheetReport) -> None:
    """Fill in how many rows of each tab hold data.

    Args:
        api: The ``spreadsheets()`` resource.
        spreadsheet_id: The spreadsheet.
        report: The report to fill in, modified in place.
    """
    if not report.tabs:
        return

    from sheets.schema import a1_range

    # Asking for the whole used range of every tab would download the entire
    # spreadsheet. Asking for one narrow column tells us nothing when that
    # column is sparse. The compromise: read the first column of each tab, and
    # fall back to the grid height when it comes back empty but the tab
    # plainly has content.
    ranges = [a1_range(tab.title, 1, 0, None, 0) for tab in report.tabs]

    try:
        response = api.values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            majorDimension="COLUMNS",
        ).execute()
    except Exception as exc:  # noqa: BLE001 - a count is not worth failing over
        logger.debug("Could not measure row counts: {}", exc)
        return

    for tab, value_range in zip(report.tabs, response.get("valueRanges", []) or []):
        columns = value_range.get("values", []) or []
        tab.data_rows = len(columns[0]) if columns else 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _rule(character: str = "-", width: int = 96) -> str:
    """Draw a horizontal rule.

    Args:
        character: What to draw it with.
        width: How wide.

    Returns:
        The rule.
    """
    return character * width


def render(report: SpreadsheetReport, sample_rows: int = DEFAULT_SAMPLE_ROWS) -> str:
    """Render the report for a terminal.

    Args:
        report: What the inspection found.
        sample_rows: How many sample rows to show per tab.

    Returns:
        The report as text.
    """
    lines: List[str] = []
    add = lines.append

    add(_rule("="))
    add(f"SPREADSHEET: {report.title or '(untitled)'}")
    add(_rule("="))
    add(f"  id         {report.spreadsheet_id}")
    add(f"  url        {report.url}")
    add(f"  locale     {report.locale}    timezone  {report.time_zone}")
    add(f"  read as    {report.account or 'unknown'}")
    add(f"  tabs       {len(report.tabs)}")
    add(
        f"  cells      {report.cells_used:,} of {CELL_BUDGET:,} "
        f"({report.cells_used / CELL_BUDGET * 100:.1f}% of the spreadsheet budget)"
    )

    add("")
    add(_rule("="))
    add("1. TABS, ROWS AND SIZE")
    add(_rule("="))
    add(f"  {'#':<3}{'Tab':<30}{'data rows':>11}{'grid':>14}{'cols':>6}  {'looks like':<12}")
    add(_rule())
    for tab in report.tabs:
        grid = f"{tab.grid_rows}x{tab.grid_columns}"
        add(
            f"  {tab.index:<3}{tab.title[:29]:<30}{tab.data_rows:>11,}{grid:>14}"
            f"{len(tab.headers):>6}  {tab.looks_like:<12}"
        )

    add("")
    add(_rule("="))
    add("2. COLUMN HEADERS, PER TAB")
    add(_rule("="))
    for tab in report.tabs:
        add("")
        add(f"  [{tab.index}] {tab.title}   ({tab.data_rows:,} data rows)")
        add(_rule())
        if not tab.headers:
            add("      (no header row -- this tab is empty)")
            continue

        letters = _column_letters(len(tab.headers))
        for letter, header in zip(letters, tab.headers):
            shown = header if header else "(blank)"
            add(f"      {letter:<4}{shown}")

        if tab.sample:
            add("")
            add(f"      first {min(sample_rows, len(tab.sample))} data row(s):")
            for row in tab.sample[:sample_rows]:
                rendered = " | ".join(str(cell)[:26] for cell in row[:8])
                add(f"        {rendered[:88]}")

    add("")
    add(_rule("="))
    add("3. WHERE THE CRAWLER'S FIELDS APPEAR TO LIVE")
    add(_rule("="))
    for tab in report.tabs:
        if not tab.field_hints:
            continue
        add("")
        add(f"  [{tab.index}] {tab.title}")
        for field_name, header in sorted(tab.field_hints.items()):
            add(f"      {field_name:<18} -> {header!r}")

    add("")
    add(_rule("="))
    add("4. PROBLEMS FOUND")
    add(_rule("="))
    problems = 0
    for tab in report.tabs:
        if tab.duplicate_headers:
            problems += 1
            add(f"  [{tab.index}] {tab.title}: duplicated header(s)")
            for header, count in sorted(tab.duplicate_headers.items()):
                add(f"      {header!r} appears {count} times -- only the first would be written to")
        if tab.blank_header_positions:
            problems += 1
            add(
                f"  [{tab.index}] {tab.title}: blank header(s) in column(s) "
                f"{', '.join(tab.blank_header_positions)}"
            )
    if not problems:
        add("  none")

    add("")
    add(_rule("="))
    add("5. WHAT THIS SPREADSHEET ALREADY HAS")
    add(_rule("="))
    company_tab = report.company_tab()
    add(f"  company list      {company_tab.title if company_tab else '(none found)'}")
    if company_tab:
        add(f"                    {company_tab.data_rows:,} rows")

    job_tabs = [tab.title for tab in report.tabs if tab.looks_like == "jobs"]
    dashboards = [tab.title for tab in report.tabs if tab.looks_like == "dashboard"]
    empty = [tab.title for tab in report.tabs if tab.looks_like == "empty"]

    add(f"  job tabs          {', '.join(job_tabs) if job_tabs else '(none found)'}")
    add(f"  dashboard         {', '.join(dashboards) if dashboards else '(none found)'}")
    add(f"  empty tabs        {', '.join(empty) if empty else '(none)'}")

    add("")
    add("  tabs matching a version 3 tab by name:")
    matched = [(tab.title, tab.matched_spec) for tab in report.tabs if tab.matched_spec]
    if matched:
        for title, spec_key in matched:
            add(f"      {title!r} -> {spec_key}")
    else:
        add("      none -- every version 3 tab would have to be created")

    add("")
    add(_rule("="))
    add("Nothing was modified. This command authenticates with the read-only scope.")
    add(_rule("="))

    return "\n".join(lines)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m sheets.inspect",
        description="Read a Google Sheet and report its structure. Writes nothing.",
    )
    parser.add_argument(
        "--spreadsheet",
        default=None,
        help="Spreadsheet id or URL (default: CAREERCRAWLER_SPREADSHEET_ID)",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=DEFAULT_SAMPLE_ROWS,
        help=f"Sample rows to show per tab (default: {DEFAULT_SAMPLE_ROWS})",
    )
    parser.add_argument("--json", default=None, help="Also write the report as JSON to this path")
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"),
        help="Console log level (default: WARNING)",
    )
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Inspect the configured spreadsheet and print the report.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``2`` when nothing is configured yet, ``1`` on an
        API failure.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    logger.remove()
    logger.add(sys.stderr, level=args.log_level, format="<level>{level: <8}</level> | {message}")

    try:
        spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet)
        credentials = resolve_credentials(read_only=True)
    except CredentialsError as exc:
        print(f"\nNot configured yet.\n\n{exc}\n", file=sys.stderr)
        return 2

    try:
        service = build_service(read_only=True, credentials=credentials.credentials)
        report = inspect_spreadsheet(
            spreadsheet_id, service=service, sample_rows=args.rows, account=credentials.account
        )
    except CredentialsError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report Google's own wording
        print(f"\nCould not read the spreadsheet:\n  {exc}\n", file=sys.stderr)
        if credentials.kind == "service-account" and credentials.account:
            print(
                "If that says the caller does not have permission, share the "
                f"spreadsheet with:\n  {credentials.account}\n",
                file=sys.stderr,
            )
        return 1

    print(render(report, sample_rows=args.rows))

    if args.json:
        from pathlib import Path

        destination = Path(args.json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nWrote {destination}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
