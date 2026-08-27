"""Prepare a spreadsheet to be version 3's data store, without disturbing it.

    python -m sheets.init                # create what is missing
    python -m sheets.init --dry-run      # say what it would do, change nothing
    python -m sheets.init --seed-config  # also seed DISCOVERY_CONFIG with defaults

Run it as often as you like. The second run does nothing at all — not "nothing
harmful", but literally no API request that changes anything, which is what
:option:`--dry-run` reports and what the tests assert.

**How an existing spreadsheet is treated.** Every rule here exists because the
spreadsheet belongs to the operator:

* A tab whose title matches one of version 3's — including by alias, so
  ``"Companies"`` is recognised as ``MASTER_COMPANIES`` — is **adopted**, not
  duplicated.
* A tab version 3 does not recognise is **left completely alone**.
* Columns already present keep their positions; missing ones are **appended to
  the right**. A column the crawler does not manage is never overwritten.
* Nothing is ever deleted. :class:`sheets.client.SheetsClient` refuses a
  deletion request before it reaches the network.

**The one exception is an empty default tab.** A spreadsheet Google has just
created contains a single ``Sheet1`` with nothing in it. That tab cannot be
deleted — a spreadsheet must always have at least one — and leaving it produces
a permanently empty tab beside the nine real ones. So if, and only if, a tab is
named like a factory default *and contains no data whatsoever*, it is **renamed**
into the first tab version 3 needs. A ``Sheet1`` with so much as one cell filled
in is treated as the operator's and left where it is.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Final, List, Optional, Sequence, Tuple

from loguru import logger

from sheets.auth import CredentialsError, build_service, resolve_credentials, resolve_spreadsheet_id
from sheets.client import SheetsClient
from sheets.schema import (
    ALL_TABS,
    DISCOVERY_CONFIG,
    IT_KEYWORDS,
    TabSpec,
    a1_range,
    looks_like_a_default_title,
    match_title,
    normalise_header,
    plan_columns,
)

__all__ = ["InitialisationReport", "TabOutcome", "initialise", "main"]

#: Rows allocated to a tab when it is created. Grown later as data arrives;
#: starting small keeps a fresh spreadsheet's cell count near zero.
DEFAULT_TAB_ROWS: Final[int] = 1000

#: A starter discovery configuration, written only into an empty
#: DISCOVERY_CONFIG and only when asked for. These are the industries the
#: brief named, so the operator has something to edit rather than a blank tab.
DEFAULT_DISCOVERY_CONFIG: Final[Tuple[Tuple[str, str, str, str], ...]] = (
    ("Information Technology", "IT, Information Technology, Software Engineer, Developer", "USA", "TRUE"),
    ("Software", "Software, SaaS, Software Engineer, Backend, Frontend, Full Stack", "USA", "TRUE"),
    ("AI", "AI, Artificial Intelligence, Machine Learning, ML, LLM, Deep Learning", "USA", "TRUE"),
    ("Machine Learning", "Machine Learning, ML Engineer, Data Scientist, MLOps", "USA", "TRUE"),
    ("Cybersecurity", "Security, Cybersecurity, InfoSec, SOC, Penetration Testing", "USA", "TRUE"),
    ("Cloud Computing", "Cloud, AWS, Azure, GCP, DevOps, Infrastructure, Kubernetes", "USA", "TRUE"),
    ("SaaS", "SaaS, Software Platform, Product Engineer", "USA", "TRUE"),
    ("FinTech", "FinTech, Payments, Banking Technology, Financial Software", "USA", "TRUE"),
    ("Healthcare Technology", "HealthTech, Health Technology, Medical Software, EHR", "USA", "TRUE"),
    ("Robotics", "Robotics, Automation, Embedded Systems, Controls", "USA", "TRUE"),
    ("Data Engineering", "Data Engineer, Data Platform, ETL, Analytics Engineer", "USA", "FALSE"),
    ("Semiconductors", "Semiconductor, Chip Design, VLSI, FPGA, ASIC", "USA", "FALSE"),
)


@dataclass
class TabOutcome:
    """What initialisation did, or would do, to one tab.

    Attributes:
        key: The crawler's name for the tab.
        title: The title it ended up with.
        action: ``"created"``, ``"renamed"``, ``"adopted"``, ``"extended"`` or
            ``"unchanged"``.
        headers: The header row after initialisation.
        appended_headers: Headers that had to be added to an existing tab.
        preserved_headers: Headers already present that the crawler does not
            manage, and did not touch.
        rows: How many data rows the tab already held.
        detail: A sentence for the report.
    """

    key: str
    title: str
    action: str
    headers: List[str] = field(default_factory=list)
    appended_headers: List[str] = field(default_factory=list)
    preserved_headers: List[str] = field(default_factory=list)
    rows: int = 0
    detail: str = ""

    @property
    def changed(self) -> bool:
        """Whether this tab required a change.

        Returns:
            ``True`` unless the tab was already exactly right.
        """
        return self.action != "unchanged"


@dataclass
class InitialisationReport:
    """Everything initialisation did.

    Attributes:
        spreadsheet_id: The spreadsheet.
        spreadsheet_title: Its title.
        url: Its address.
        account: The identity used.
        outcomes: One per version 3 tab, in creation order.
        untouched_tabs: Tabs the crawler does not manage and did not touch.
        seeded_config_rows: Discovery configuration rows written.
        seeded_keywords: IT keyword rows written into an empty IT_KEYWORDS.
        dry_run: Whether anything was actually sent.
        api: The client's traffic statistics.
    """

    spreadsheet_id: str
    spreadsheet_title: str = ""
    url: str = ""
    account: str = ""
    outcomes: List[TabOutcome] = field(default_factory=list)
    untouched_tabs: List[str] = field(default_factory=list)
    seeded_config_rows: int = 0
    seeded_keywords: int = 0
    dry_run: bool = False
    api: Optional[object] = None

    @property
    def changed(self) -> bool:
        """Whether initialisation had anything to do.

        Returns:
            ``True`` when any tab was created, renamed or extended.
        """
        return (
            any(outcome.changed for outcome in self.outcomes)
            or bool(self.seeded_config_rows)
            or bool(self.seeded_keywords)
        )

    @property
    def created(self) -> List[TabOutcome]:
        """Tabs that did not exist before.

        Returns:
            Those created or renamed into place.
        """
        return [item for item in self.outcomes if item.action in ("created", "renamed")]


def _read_headers(client: SheetsClient, titles: Sequence[str]) -> Dict[str, List[str]]:
    """Read the header row of every named tab, in one call.

    Args:
        client: The client.
        titles: Tab titles to read.

    Returns:
        Title to its header row. A tab with no header row maps to ``[]``.
    """
    if not titles:
        return {}

    ranges = [a1_range(title, 1, 0, 1, None) for title in titles]
    rows = client.batch_read(ranges)

    return {
        title: [str(cell).strip() for cell in (row[0] if row else [])]
        for title, row in zip(titles, rows)
    }


def _count_rows(client: SheetsClient, title: str) -> int:
    """How many rows of a tab hold anything.

    Args:
        client: The client.
        title: The tab.

    Returns:
        The row count, header included.
    """
    try:
        return len(client.read(a1_range(title, 1, 0, None, 0)))
    except Exception:  # noqa: BLE001 - a count is not worth failing initialisation over
        logger.debug("Could not count rows on {!r}", title)
        return 0


def _reusable_default_tab(
    client: SheetsClient,
    titles: Sequence[str],
    headers: Dict[str, List[str]],
    claimed: Sequence[str],
) -> Optional[str]:
    """Find an empty factory-default tab that can be renamed rather than left.

    Args:
        client: The client, for confirming the tab really is empty.
        titles: Every tab title in the spreadsheet.
        headers: Header rows already read.
        claimed: Titles already adopted by another specification.

    Returns:
        The title of a reusable tab, or ``None``. A tab qualifies only if its
        name is one Google assigns automatically *and* it contains no data at
        all — checked against the sheet, not merely against its header row.
    """
    for title in titles:
        if title in claimed or not looks_like_a_default_title(title):
            continue

        if headers.get(title):
            logger.debug("{!r} looks like a default tab but has a header row; leaving it", title)
            continue

        if _count_rows(client, title) > 0:
            logger.debug("{!r} looks like a default tab but holds data; leaving it", title)
            continue

        return title

    return None


def _seed_rows(client: SheetsClient, spec: TabSpec, title: str, dry_run: bool) -> int:
    """Write the starter discovery configuration into an empty config tab.

    Args:
        client: The client.
        spec: The configuration tab's specification.
        title: Its live title.
        dry_run: Whether to report rather than write.

    Returns:
        How many rows were written, or would be.
    """
    # Every managed column, from row 2 down. Reading a single anchor cell here
    # would report an empty tab whenever A2 alone happened to be blank, and
    # seeding would then write straight over the operator's own rows.
    existing = client.read(a1_range(title, 2, 0, None, len(spec.columns) - 1))
    if existing:
        logger.debug("{!r} already has {} row(s); not seeding", title, len(existing))
        return 0

    rows = [list(row) for row in DEFAULT_DISCOVERY_CONFIG]
    if dry_run:
        return len(rows)

    client.write(a1_range(title, 2, 0), rows)
    logger.success("Seeded {!r} with {} industry row(s)", title, len(rows))
    return len(rows)


#: The starter keyword list, written into an empty ``IT_KEYWORDS`` and never
#: again. This is a *seed*, not the configuration: once the tab exists the
#: operator owns it, and :func:`crawler.keywords.load_keywords` reads whatever
#: is there on every run. Adding a term is editing the sheet, not this list.
DEFAULT_IT_KEYWORDS: Final[Tuple[Tuple[str, str, str, str, str], ...]] = (
    ("SAP", "ERP Platforms", "TRUE", "phrase", ""),
    ("Oracle", "ERP Platforms", "TRUE", "phrase", ""),
    ("Workday", "ERP Platforms", "TRUE", "phrase", ""),
    ("PeopleSoft", "ERP Platforms", "TRUE", "phrase", ""),
    ("JDEdwards", "ERP Platforms", "TRUE", "phrase", ""),
    ("Epicor", "ERP Platforms", "TRUE", "phrase", ""),
    ("Infor", "ERP Platforms", "TRUE", "phrase", ""),
    ("NetSuite", "ERP Platforms", "TRUE", "phrase", ""),
    ("Sage", "ERP Platforms", "TRUE", "phrase", ""),
    ("Lawson", "ERP Platforms", "TRUE", "phrase", ""),
    ("Movex", "ERP Platforms", "TRUE", "phrase", ""),
    ("Baan", "ERP Platforms", "TRUE", "phrase", ""),
    ("Mapics", "ERP Platforms", "TRUE", "phrase", ""),
    ("Syspro", "ERP Platforms", "TRUE", "phrase", ""),
    ("Macola", "ERP Platforms", "TRUE", "phrase", ""),
    ("Acumatica", "ERP Platforms", "TRUE", "phrase", ""),
    ("Pronto", "ERP Platforms", "TRUE", "phrase", ""),
    ("MYOB", "ERP Platforms", "TRUE", "phrase", ""),
    ("Greentree", "ERP Platforms", "TRUE", "phrase", ""),
    ("Odoo", "ERP Platforms", "TRUE", "phrase", ""),
    ("Aptean", "ERP Platforms", "TRUE", "phrase", ""),
    ("Deltek", "ERP Platforms", "TRUE", "phrase", ""),
    ("Costpoint", "ERP Platforms", "TRUE", "phrase", ""),
    ("IFS", "ERP Platforms", "TRUE", "phrase", ""),
    ("Unit4", "ERP Platforms", "TRUE", "phrase", ""),
    ("Comarch", "ERP Platforms", "TRUE", "phrase", ""),
    ("Ramco", "ERP Platforms", "TRUE", "phrase", ""),
    ("BPCS", "ERP Platforms", "TRUE", "phrase", ""),
    ("MANMAN", "ERP Platforms", "TRUE", "phrase", ""),
    ("Visibility", "ERP Platforms", "TRUE", "phrase", ""),
    ("Expandable", "ERP Platforms", "TRUE", "phrase", ""),
    ("IQMS", "ERP Platforms", "TRUE", "phrase", ""),
    ("Abra", "ERP Platforms", "TRUE", "phrase", ""),
    ("Cyborg", "ERP Platforms", "TRUE", "phrase", ""),
    ("Ultipro", "ERP Platforms", "TRUE", "phrase", ""),
    ("Kronos", "ERP Platforms", "TRUE", "phrase", ""),
    ("Ceridian", "ERP Platforms", "TRUE", "phrase", ""),
    ("Paycom", "ERP Platforms", "TRUE", "phrase", ""),
    ("Paylocity", "ERP Platforms", "TRUE", "phrase", ""),
    ("Mincom", "ERP Platforms", "TRUE", "phrase", ""),
    ("Ellipse", "ERP Platforms", "TRUE", "phrase", ""),
    ("Maximo", "ERP Platforms", "TRUE", "phrase", ""),
    ("Tririga", "ERP Platforms", "TRUE", "phrase", ""),
    ("Archibus", "ERP Platforms", "TRUE", "phrase", ""),
    ("Famis", "ERP Platforms", "TRUE", "phrase", ""),
    ("Primavera", "ERP Platforms", "TRUE", "phrase", ""),
    ("Procore", "ERP Platforms", "TRUE", "phrase", ""),
    ("Viewpoint", "ERP Platforms", "TRUE", "phrase", ""),
    ("Timberline", "ERP Platforms", "TRUE", "phrase", ""),
    ("Aconex", "ERP Platforms", "TRUE", "phrase", ""),
    ("Unifier", "ERP Platforms", "TRUE", "phrase", ""),
    ("Aspentech", "ERP Platforms", "TRUE", "phrase", ""),
    ("Wonderware", "ERP Platforms", "TRUE", "phrase", ""),
    ("Osisoft", "ERP Platforms", "TRUE", "phrase", ""),
    ("Proficy", "ERP Platforms", "TRUE", "phrase", ""),
    ("GEAC", "ERP Platforms", "TRUE", "phrase", ""),
    ("Hansen", "ERP Platforms", "TRUE", "phrase", ""),
    ("Avante", "ERP Platforms", "TRUE", "phrase", ""),
    ("Made2Manage", "ERP Platforms", "TRUE", "phrase", ""),
    ("JobBOSS", "ERP Platforms", "TRUE", "phrase", ""),
    ("Plex", "ERP Platforms", "TRUE", "phrase", ""),
    ("Dexter", "ERP Platforms", "TRUE", "phrase", ""),
    ("Penta", "ERP Platforms", "TRUE", "phrase", ""),
    ("ComputerEase", "ERP Platforms", "TRUE", "phrase", ""),
    ("Acculynx", "ERP Platforms", "TRUE", "phrase", ""),
    ("JobNimbus", "ERP Platforms", "TRUE", "phrase", ""),
    ("BuilderTrend", "ERP Platforms", "TRUE", "phrase", ""),
    ("CoConstruct", "ERP Platforms", "TRUE", "phrase", ""),
    ("Hyphen", "ERP Platforms", "TRUE", "phrase", ""),
    ("Newforma", "ERP Platforms", "TRUE", "phrase", ""),
)



def seed_keywords(
    client: SheetsClient,
    title: str,
    dry_run: bool = False,
) -> int:
    """Write the starter keyword list into an empty ``IT_KEYWORDS``.

    Refuses to touch a tab that already holds rows, for the same reason
    :func:`seed_discovery_config` does: once an operator has edited the list it
    is theirs, and re-running initialisation must not overwrite it.

    Args:
        client: The Sheets client.
        title: The tab's live title.
        dry_run: Work out what would be written, and write nothing.

    Returns:
        How many rows were written, or would be.
    """
    spec = IT_KEYWORDS

    try:
        existing = client.read(a1_range(title, 2, 0, None, len(spec.columns) - 1))
    except Exception as exc:  # noqa: BLE001 - an absent tab is the empty case
        # On a dry run the tab was only *reported* as created, never actually
        # made, so reading it fails. That is not an error: nothing is there,
        # which is exactly the condition seeding wants.
        logger.debug("{!r} could not be read ({}); treating as empty", title, exc)
        existing = []

    if existing:
        logger.debug("{!r} already has {} row(s); not seeding", title, len(existing))
        return 0

    rows = [list(row) for row in DEFAULT_IT_KEYWORDS]
    if dry_run:
        return len(rows)

    client.write(a1_range(title, 2, 0), rows)
    logger.success("Seeded {!r} with {} keyword(s)", title, len(rows))
    return len(rows)


def initialise(
    client: SheetsClient,
    seed_config: bool = False,
    dry_run: bool = False,
    account: str = "",
    only: Sequence[str] = (),
    seed_keywords_too: bool = False,
) -> InitialisationReport:
    """Create or adopt every tab version 3 needs.

    Args:
        client: The Sheets client.
        seed_config: Whether to write a starter discovery configuration into an
            empty ``DISCOVERY_CONFIG``.
        dry_run: Report what would happen without sending anything that changes
            the spreadsheet.
        account: The identity being used, recorded on the report.
        only: Restrict the run to these tab titles or keys. Empty means every
            tab, which is the original behaviour.

            This exists because "create one new tab" and "bring every tab up to
            the current schema" are different intentions, and conflating them
            is how an operator who asked for the first quietly gets the second.
            A schema that has grown new columns since a spreadsheet was made
            would otherwise extend the company tab as a side effect.
        seed_keywords_too: Whether to write the starter keyword list into an
            empty ``IT_KEYWORDS``.

    Returns:
        What was done, or would be done.

    Raises:
        sheets.client.SheetsError: If the spreadsheet cannot be read or written.
    """
    metadata = client.metadata()
    properties = metadata.get("properties", {}) or {}

    report = InitialisationReport(
        spreadsheet_id=client.spreadsheet_id,
        spreadsheet_title=properties.get("title", ""),
        url=metadata.get("spreadsheetUrl", ""),
        account=account,
        dry_run=dry_run,
    )

    titles: List[str] = []
    sheet_ids: Dict[str, int] = {}
    for sheet in metadata.get("sheets", []) or []:
        sheet_properties = sheet.get("properties", {}) or {}
        title = sheet_properties.get("title", "")
        if title:
            titles.append(title)
            sheet_ids[title] = int(sheet_properties.get("sheetId", 0))

    headers = _read_headers(client, titles)
    claimed: List[str] = []

    wanted = {normalise_header(name) for name in only}
    specs = [
        spec for spec in ALL_TABS
        if not wanted
        or normalise_header(spec.title) in wanted
        or normalise_header(spec.key) in wanted
    ]

    for spec in specs:
        outcome = _initialise_tab(
            client, spec, titles, sheet_ids, headers, claimed, dry_run
        )
        report.outcomes.append(outcome)
        claimed.append(outcome.title)

        # A renamed or created tab is now part of the spreadsheet, so a later
        # specification must not adopt or rename it too.
        if outcome.action in ("created", "renamed") and outcome.title not in titles:
            titles.append(outcome.title)

    managed = {normalise_header(title) for title in claimed}
    report.untouched_tabs = [
        title for title in titles if normalise_header(title) not in managed
    ]

    if seed_keywords_too:
        for outcome in report.outcomes:
            if outcome.key == IT_KEYWORDS.key:
                report.seeded_keywords = seed_keywords(
                    client, outcome.title, dry_run=dry_run
                )

    if seed_config:
        config_outcome = next(
            (item for item in report.outcomes if item.key == DISCOVERY_CONFIG.key), None
        )
        if config_outcome is not None:
            report.seeded_config_rows = _seed_rows(
                client, DISCOVERY_CONFIG, config_outcome.title, dry_run
            )

    report.api = client.stats
    return report


def _initialise_tab(
    client: SheetsClient,
    spec: TabSpec,
    titles: List[str],
    sheet_ids: Dict[str, int],
    headers: Dict[str, List[str]],
    claimed: Sequence[str],
    dry_run: bool,
) -> TabOutcome:
    """Create, adopt or extend one tab.

    Args:
        client: The client.
        spec: What the crawler wants.
        titles: Every tab title currently in the spreadsheet.
        sheet_ids: Title to numeric id.
        headers: Title to header row.
        claimed: Titles already taken by an earlier specification.
        dry_run: Report rather than change.

    Returns:
        What happened to this tab.
    """
    available = [title for title in titles if title not in claimed]
    existing_title = match_title(spec, available)

    # -- the tab already exists ---------------------------------------------
    if existing_title is not None:
        existing_headers = headers.get(existing_title, [])
        plan = plan_columns(spec, existing_headers)
        rows = max(0, _count_rows(client, existing_title) - 1) if existing_headers else 0

        if plan.is_satisfied:
            return TabOutcome(
                key=spec.key,
                title=existing_title,
                action="unchanged",
                headers=plan.header_row,
                preserved_headers=plan.unknown_headers,
                rows=rows,
                detail="already has every column",
            )

        if not dry_run:
            sheet_id = sheet_ids.get(existing_title)
            if sheet_id is not None:
                # Grow the grid first: writing past a tab's last column fails.
                client.ensure_size(sheet_id, max(DEFAULT_TAB_ROWS, rows + 2), plan.width)
            client.write(a1_range(existing_title, 1, 0), [plan.header_row])

        action = "adopted" if not existing_headers else "extended"
        return TabOutcome(
            key=spec.key,
            title=existing_title,
            action=action,
            headers=plan.header_row,
            appended_headers=plan.appended_headers,
            preserved_headers=plan.unknown_headers,
            rows=rows,
            detail=(
                f"adopted, {len(plan.appended_headers)} header(s) written"
                if action == "adopted"
                else f"kept {len(plan.existing_headers)} existing column(s), "
                     f"appended {len(plan.appended_headers)}"
            ),
        )

    # -- the tab does not exist: reuse an empty default, or create it --------
    plan = plan_columns(spec, [])
    reusable = _reusable_default_tab(client, titles, headers, claimed)

    if reusable is not None:
        if not dry_run:
            sheet_id = sheet_ids[reusable]
            client.rename_tab(sheet_id, spec.title)
            client.ensure_size(sheet_id, DEFAULT_TAB_ROWS, plan.width)
            client.write(a1_range(spec.title, 1, 0), [plan.header_row])
            client.format_header(sheet_id, plan.width, spec.frozen_rows)

        # It is gone under its old name either way, so no later specification
        # should consider it.
        if reusable in titles:
            titles.remove(reusable)

        return TabOutcome(
            key=spec.key,
            title=spec.title,
            action="renamed",
            headers=plan.header_row,
            appended_headers=plan.header_row,
            detail=f"reused the empty default tab {reusable!r}",
        )

    if not dry_run:
        client.add_tab(spec.title, DEFAULT_TAB_ROWS, plan.width, spec.frozen_rows)
        client.write(a1_range(spec.title, 1, 0), [plan.header_row])

        # add_tab does not return the new id, and formatting needs it.
        new_id = client.tab_ids().get(spec.title)
        if new_id is not None:
            sheet_ids[spec.title] = new_id
            client.format_header(new_id, plan.width, spec.frozen_rows)

    return TabOutcome(
        key=spec.key,
        title=spec.title,
        action="created",
        headers=plan.header_row,
        appended_headers=plan.header_row,
        detail=f"created with {len(plan.header_row)} column(s)",
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(report: InitialisationReport) -> str:
    """Render the report for a terminal.

    Args:
        report: What initialisation did.

    Returns:
        The report as text.
    """
    lines: List[str] = []
    add = lines.append
    rule = "=" * 92

    add(rule)
    add("DRY RUN — nothing was changed" if report.dry_run else "SPREADSHEET INITIALISED")
    add(rule)
    add(f"  spreadsheet   {report.spreadsheet_title or '(untitled)'}")
    add(f"  id            {report.spreadsheet_id}")
    add(f"  url           {report.url}")
    add(f"  authenticated {report.account or 'unknown'}")
    add("")

    add(f"  {'Tab':<26}{'Action':<12}{'Cols':>5}{'Rows':>7}   Detail")
    add("-" * 92)
    for outcome in report.outcomes:
        add(
            f"  {outcome.title[:25]:<26}{outcome.action:<12}"
            f"{len(outcome.headers):>5}{outcome.rows:>7}   {outcome.detail}"
        )
    add("-" * 92)

    if not report.changed:
        add("  Nothing to do — every tab already had every column.")
    else:
        created = [item.title for item in report.created]
        extended = [item.title for item in report.outcomes if item.action == "extended"]
        if created:
            add(f"  created/renamed: {', '.join(created)}")
        if extended:
            add(f"  extended:        {', '.join(extended)}")
        if report.seeded_keywords:
            add(f"  seeded IT_KEYWORDS with {report.seeded_keywords} keyword(s)")
        if report.seeded_config_rows:
            add(f"  seeded:          {report.seeded_config_rows} DISCOVERY_CONFIG row(s)")

    preserved = [
        (outcome.title, outcome.preserved_headers)
        for outcome in report.outcomes
        if outcome.preserved_headers
    ]
    if preserved:
        add("")
        add("  Columns preserved that version 3 does not manage:")
        for title, columns in preserved:
            add(f"      {title}: {', '.join(columns)}")

    if report.untouched_tabs:
        add("")
        add(f"  Tabs left completely untouched: {', '.join(report.untouched_tabs)}")

    if report.api is not None:
        add("")
        add(f"  API: {report.api.describe()}")

    add(rule)
    return "\n".join(lines)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m sheets.init",
        description="Create the tabs version 3 needs. Safe to run repeatedly.",
    )
    parser.add_argument(
        "--spreadsheet",
        default=None,
        help="Spreadsheet id or URL (default: CAREERCRAWLER_SPREADSHEET_ID)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="TAB",
        help=(
            "Restrict to one tab, by title or key. Repeatable. Without it every "
            "tab is brought up to the current schema, which may extend existing "
            "tabs with columns added since the spreadsheet was made"
        ),
    )
    parser.add_argument(
        "--seed-keywords",
        action="store_true",
        help="Write the starter IT keyword list into an empty IT_KEYWORDS",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be done without changing anything",
    )
    parser.add_argument(
        "--seed-config",
        action="store_true",
        help="Also write a starter industry list into an empty DISCOVERY_CONFIG",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"),
        help="Console log level (default: INFO)",
    )
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Initialise the configured spreadsheet.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``2`` when nothing is configured, ``1`` on failure.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    logger.remove()
    logger.add(sys.stderr, level=args.log_level, format="<level>{level: <8}</level> | {message}")

    try:
        spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet)
        # A dry run still authenticates read-only, so it cannot write even by
        # accident, and so it fails early if the sheet is not shared with us.
        credentials = resolve_credentials(read_only=args.dry_run)
    except CredentialsError as exc:
        print(f"\nNot configured yet.\n\n{exc}\n", file=sys.stderr)
        return 2

    try:
        service = build_service(read_only=args.dry_run, credentials=credentials.credentials)
        client = SheetsClient(service, spreadsheet_id)
        report = initialise(
            client,
            seed_config=args.seed_config,
            only=args.only,
            seed_keywords_too=args.seed_keywords,
            dry_run=args.dry_run,
            account=credentials.account,
        )
    except CredentialsError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report the API's own wording
        print(f"\nCould not initialise the spreadsheet:\n  {exc}\n", file=sys.stderr)
        if credentials.kind == "service-account" and credentials.account:
            print(
                "If that mentions permission, share the spreadsheet as an Editor with:\n"
                f"  {credentials.account}\n",
                file=sys.stderr,
            )
        return 1

    print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
