"""The nine tabs version 3 maintains, and how to fit them onto a live spreadsheet.

The spreadsheet is the operator's, not the crawler's. It may already have tabs
the crawler did not create, columns somebody added by hand, and formatting that
took an afternoon. So this module describes what the crawler *wants* and
reconciles it with what is *there*, without ever removing anything::

    >>> from sheets.schema import MASTER_COMPANIES, plan_columns
    >>> plan = plan_columns(MASTER_COMPANIES, ["Company Name", "Notes", "Website"])
    >>> plan.index_of("company_name"), plan.index_of("website")
    (0, 2)
    >>> plan.unknown_headers
    ['Notes']

Three rules make that safe.

**Headers are matched on a normalised form.** ``"Career page url"``,
``"Careers / Jobs URL"`` and ``"career_url"`` all reduce to one key, so a column
the operator spelled their own way is adopted rather than duplicated beside it.
This is deliberately the same reasoning :mod:`crawler.csv_reader` applies to the
input CSV.

**A column the crawler does not know is preserved.** ``"Notes"`` above keeps its
position and its contents; every write goes around it.

**A column the crawler needs and the sheet lacks is appended** to the right of
everything already there — never inserted, so no existing column changes index
and no formula referencing one breaks.

Two design decisions in the tabs themselves are worth knowing, because they are
what keeps a Sheets-only store inside Google's ten-million-cell ceiling:

* :data:`JOB_HISTORY` holds **one row per job identity, updated in place**, not
  one row per job per week. At 237,000 postings the latter would add twelve
  million rows a year and exhaust the whole spreadsheet in about five weeks.
* :data:`FAILURES` holds **the latest run only**. Historical failure counts live
  in :data:`WEEKLY_RUNS`, which is one row per run.

Every tab that is updated rather than appended carries a **key column** — the
last column in each specification. Sheets has no transactions, so a run that
dies half-way through a write leaves the tab partly updated; keying every write
on a stable identity makes the next run repair it rather than duplicate it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Final, List, Optional, Sequence, Tuple

from loguru import logger

__all__ = [
    "ALL_TABS",
    "CURRENT_JOBS",
    "DASHBOARD",
    "DISCOVERY_CONFIG",
    "FAILURES",
    "JOB_HISTORY",
    "MASTER_COMPANIES",
    "NEW_COMPANY_DISCOVERY",
    "IT_KEYWORDS",
    "NEW_LAST_WEEK",
    "WEEKLY_RUNS",
    "Column",
    "ColumnPlan",
    "TabSpec",
    "a1_range",
    "column_letter",
    "default_tab_titles",
    "looks_like_a_default_title",
    "match_title",
    "normalise_header",
    "plan_columns",
    "tab_by_key",
]

#: Anything that is not a letter or a digit, for comparing headers.
_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: The alphabet A1 notation names columns with.
_LETTERS: Final[str] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: Titles Google gives a brand-new, untouched tab, in the locales this is
#: likely to meet. A tab with one of these names *and no data at all* is the
#: spreadsheet's factory default rather than anything the operator made, so it
#: is safe to rename and reuse instead of leaving it lying about.
_DEFAULT_TITLES: Final[Tuple[str, ...]] = (
    "sheet1", "sheet", "sheet1copy",
    "blad1",       # Dutch
    "hoja1",       # Spanish
    "feuille1",    # French
    "tabelle1",    # German
    "foglio1",     # Italian
    "planilha1",   # Portuguese
    "ark1",        # Danish / Norwegian
    "blad1kopia",  # Swedish
    "sayfa1",      # Turkish
    "arkusz1",     # Polish
    "list1",       # Czech
)


def normalise_header(header: object) -> str:
    """Reduce a header or title to a comparable key.

    Args:
        header: The text as it appears in the sheet, or a field name.

    Returns:
        The text lowercased with punctuation and spacing removed, so
        ``"Career Page URL"``, ``"career_url"`` and ``"Careers / Jobs URL"``
        all collapse together.
    """
    return _NON_ALNUM.sub("", str(header or "").strip().lower())


def looks_like_a_default_title(title: str) -> bool:
    """Whether a tab title is one Google assigns to a new, untouched tab.

    Args:
        title: The tab's title.

    Returns:
        ``True`` for ``"Sheet1"`` and its equivalents in other locales.
    """
    return normalise_header(title) in _DEFAULT_TITLES


def column_letter(index: int) -> str:
    """Name a column by its zero-based index.

    Args:
        index: ``0`` for column A.

    Returns:
        The A1-notation letter, e.g. ``"A"``, ``"Z"``, ``"AA"``.

    Raises:
        ValueError: If ``index`` is negative.
    """
    if index < 0:
        raise ValueError(f"Column index cannot be negative: {index}")

    letters = ""
    position = index
    while True:
        letters = _LETTERS[position % 26] + letters
        position = position // 26 - 1
        if position < 0:
            break
    return letters


def a1_range(
    title: str,
    first_row: int = 1,
    first_column: int = 0,
    last_row: Optional[int] = None,
    last_column: Optional[int] = None,
) -> str:
    """Build an A1-notation range, quoting the tab title.

    Note what omitting *both* ends means, because it is not "everything"::

        a1_range("T", 1, 0)              -> "'T'!A1"      one cell
        a1_range("T", 2, 0, None, 3)     -> "'T'!A2:D"    columns A-D, row 2 down
        a1_range("T", 1, 0, 1, 5)        -> "'T'!A1:F1"   just the header row

    A bare start cell is what :meth:`sheets.client.SheetsClient.write` wants —
    an anchor for a block whose extent the data decides. It is emphatically not
    what a *read* wants: reading ``"'T'!A2"`` returns a single cell, and code
    that used it to ask "does this tab have any rows yet?" would answer no for
    any tab whose first cell happens to be blank. Give a read at least one end.

    Args:
        title: The tab's title, which may contain spaces or an apostrophe.
        first_row: One-based first row.
        first_column: Zero-based first column.
        last_row: One-based last row, or ``None`` to run to the end of the tab.
        last_column: Zero-based last column, or ``None`` to run to its last
            column.

    Returns:
        The range, e.g. ``"'MASTER_COMPANIES'!A1:P8276"``.
    """
    # An apostrophe in a title is escaped by doubling it. Getting this wrong
    # addresses a different tab, or none at all.
    quoted = "'" + str(title).replace("'", "''") + "'"

    start = f"{column_letter(first_column)}{first_row}"
    if last_row is None and last_column is None:
        return f"{quoted}!{start}"

    end_column = column_letter(last_column) if last_column is not None else ""
    end_row = str(last_row) if last_row is not None else ""
    return f"{quoted}!{start}:{end_column}{end_row}"


@dataclass(frozen=True)
class Column:
    """One column the crawler maintains.

    Attributes:
        field: The crawler's own name for the value.
        header: The header written when the column has to be created.
        aliases: Other spellings that mean this column, so an operator's own
            wording is adopted rather than duplicated.
        note: What the column holds, for the documentation and the setup report.
    """

    field: str
    header: str
    aliases: Tuple[str, ...] = ()
    note: str = ""

    def keys(self) -> Tuple[str, ...]:
        """Every normalised spelling that identifies this column.

        Returns:
            The comparison keys, most canonical first.
        """
        seen: List[str] = []
        for spelling in (self.header, self.field, *self.aliases):
            key = normalise_header(spelling)
            if key and key not in seen:
                seen.append(key)
        return tuple(seen)


@dataclass(frozen=True)
class TabSpec:
    """What the crawler wants one tab to look like.

    Attributes:
        key: The crawler's name for the tab.
        title: The tab's title, created when absent.
        columns: The columns the crawler maintains, in creation order.
        aliases: Other titles that mean this tab, so an existing tab is adopted
            rather than a second one created beside it.
        identity_field: The field whose value identifies a row, for updating in
            place rather than appending a duplicate. ``""`` for append-only
            tabs, where each run's rows are a historical record.
        purpose: One line describing the tab, for the setup report.
        frozen_rows: Rows to freeze when the tab is created.
    """

    key: str
    title: str
    columns: Tuple[Column, ...]
    aliases: Tuple[str, ...] = ()
    identity_field: str = ""
    purpose: str = ""
    frozen_rows: int = 1

    @property
    def headers(self) -> Tuple[str, ...]:
        """The headers, in the order the crawler would create them.

        Returns:
            The header strings.
        """
        return tuple(column.header for column in self.columns)

    @property
    def fields(self) -> Tuple[str, ...]:
        """The field names, in specification order.

        Returns:
            The field names.
        """
        return tuple(column.field for column in self.columns)

    @property
    def append_only(self) -> bool:
        """Whether rows accumulate rather than being updated in place.

        Returns:
            ``True`` when the tab has no identity column.
        """
        return not self.identity_field

    def title_keys(self) -> Tuple[str, ...]:
        """Every normalised title that identifies this tab.

        Returns:
            The comparison keys.
        """
        seen: List[str] = []
        for spelling in (self.title, self.key, *self.aliases):
            key = normalise_header(spelling)
            if key and key not in seen:
                seen.append(key)
        return tuple(seen)


@dataclass
class ColumnPlan:
    """How a specification maps onto the columns a live tab actually has.

    Attributes:
        spec: The specification this plan is for.
        title: The live tab's actual title.
        existing_headers: Its header row as read, verbatim.
        mapping: Field name to zero-based column index.
        appended_headers: Headers that had to be added, in order.
        unknown_headers: Headers present in the sheet that the crawler does not
            manage. Recorded so a run can report that they were preserved.
    """

    spec: TabSpec
    title: str
    existing_headers: List[str] = field(default_factory=list)
    mapping: Dict[str, int] = field(default_factory=dict)
    appended_headers: List[str] = field(default_factory=list)
    unknown_headers: List[str] = field(default_factory=list)

    @property
    def header_row(self) -> List[str]:
        """The header row as it will be once any appends are written.

        Returns:
            Existing headers untouched, with new ones on the end.
        """
        return [*self.existing_headers, *self.appended_headers]

    @property
    def width(self) -> int:
        """How many columns the tab will have.

        Returns:
            The column count.
        """
        return len(self.header_row)

    @property
    def is_satisfied(self) -> bool:
        """Whether the live tab already has every column the crawler needs.

        Returns:
            ``True`` when nothing has to be written. This is what makes
            initialisation idempotent: a second run finds every plan satisfied
            and issues no request at all.
        """
        return not self.appended_headers

    def index_of(self, field_name: str) -> Optional[int]:
        """Where one of the crawler's fields lives.

        Args:
            field_name: The field.

        Returns:
            Its zero-based column index, or ``None`` if it is not mapped.
        """
        return self.mapping.get(field_name)

    def contiguous_runs(self) -> List[Tuple[int, List[str]]]:
        """Group the crawler's columns into unbroken stretches.

        A write covers a rectangle, so writing the crawler's fields as one
        block would also write every column between them — including any the
        operator added by hand. Splitting the write at each foreign column is
        what keeps a ``Notes`` column in the middle of a tab untouched.

        Returns:
            ``(first_column_index, fields)`` per stretch, left to right. For a
            tab the crawler created there is exactly one stretch.
        """
        by_index = sorted((index, field) for field, index in self.mapping.items())

        runs: List[Tuple[int, List[str]]] = []
        for index, field_name in by_index:
            if runs and index == runs[-1][0] + len(runs[-1][1]):
                runs[-1][1].append(field_name)
            else:
                runs.append((index, [field_name]))

        return runs

    def row_for(self, values: Dict[str, object]) -> List[object]:
        """Lay one record out across the live tab's columns.

        Positions the crawler does not manage are left as ``None``, so a
        hand-added column keeps its contents even though the crawler writes the
        row around it.

        Args:
            values: Field name to value.

        Returns:
            A row as wide as the tab.
        """
        row: List[object] = [None] * self.width

        for field_name, index in self.mapping.items():
            if field_name in values:
                value = values[field_name]
                row[index] = "" if value is None else value

        return row


def plan_columns(spec: TabSpec, existing_headers: Sequence[object]) -> ColumnPlan:
    """Work out where each of the crawler's fields lives on a live tab.

    Args:
        spec: What the crawler wants.
        existing_headers: The tab's header row as read. Empty for a tab that
            does not exist yet, in which case every column is "appended".

    Returns:
        The plan. Existing columns keep their positions, missing ones are
        appended to the right, and columns the crawler does not know are
        recorded as preserved and otherwise ignored.
    """
    headers = [str(header or "").strip() for header in existing_headers]

    # Trailing blanks record how far somebody once scrolled, not columns.
    # Appending after them would leave a hole in the header row.
    while headers and not headers[-1]:
        headers.pop()

    by_key: Dict[str, int] = {}
    for index, header in enumerate(headers):
        key = normalise_header(header)
        if key:
            # First occurrence wins, so a duplicated header cannot shadow the
            # original and send writes to the wrong column.
            by_key.setdefault(key, index)

    plan = ColumnPlan(spec=spec, title=spec.title, existing_headers=headers)
    claimed: set = set()

    for column in spec.columns:
        for key in column.keys():
            index = by_key.get(key)
            if index is not None and index not in claimed:
                plan.mapping[column.field] = index
                claimed.add(index)
                break
        else:
            plan.mapping[column.field] = len(headers) + len(plan.appended_headers)
            plan.appended_headers.append(column.header)

    plan.unknown_headers = [
        header for index, header in enumerate(headers) if index not in claimed and header
    ]

    if plan.appended_headers:
        logger.debug(
            "Tab {!r}: {} column(s) to add: {}",
            spec.title,
            len(plan.appended_headers),
            ", ".join(plan.appended_headers),
        )
    if plan.unknown_headers:
        logger.debug(
            "Tab {!r}: preserving {} column(s) the crawler does not manage: {}",
            spec.title,
            len(plan.unknown_headers),
            ", ".join(plan.unknown_headers),
        )

    return plan


def match_title(spec: TabSpec, existing_titles: Sequence[str]) -> Optional[str]:
    """Find the live tab corresponding to a specification.

    Args:
        spec: What the crawler is looking for.
        existing_titles: Every tab title in the spreadsheet.

    Returns:
        The matching title as the spreadsheet spells it, or ``None`` when the
        tab does not exist and will have to be created.
    """
    by_key = {normalise_header(title): title for title in existing_titles}

    for key in spec.title_keys():
        if key in by_key:
            return by_key[key]

    return None


def tab_by_key(key: str) -> Optional[TabSpec]:
    """Look up a tab specification by the crawler's name for it.

    Args:
        key: The specification's key, e.g. ``"master_companies"``.

    Returns:
        The specification, or ``None``.
    """
    for spec in ALL_TABS:
        if spec.key == key:
            return spec
    return None


def default_tab_titles() -> Tuple[str, ...]:
    """The titles the crawler creates, in order.

    Returns:
        The tab titles.
    """
    return tuple(spec.title for spec in ALL_TABS)


# ---------------------------------------------------------------------------
# The nine tabs.
#
# Aliases matter more than headers: they are what lets the crawler adopt a
# column or tab the operator already had, rather than adding a near-duplicate
# beside it. Each tab's key column is last, and is what makes a write
# idempotent in a store that has no transactions.
# ---------------------------------------------------------------------------

MASTER_COMPANIES: Final[TabSpec] = TabSpec(
    key="master_companies",
    title="MASTER_COMPANIES",
    aliases=("Master Companies", "Companies", "Company List", "Master Company List", "Master"),
    identity_field="company_key",
    purpose="The permanent company list. Everything else is derived from this.",
    columns=(
        Column("company_name", "Company Name", ("Company", "Name", "Employer")),
        Column("website", "Website", ("Company Website", "Web Site", "Domain", "URL")),
        Column("career_url", "Career Page URL",
               ("Career page url", "Careers / Jobs URL", "Careers URL", "Careers Page", "Jobs URL")),
        Column("it_link", "IT Link", ("IT LINK", "ATS Link", "ATS URL", "Job Board URL")),
        Column("platform", "ATS / Platform", ("ATS", "Platform", "ATS Platform", "System")),
        Column("department", "Department", ("Dept", "Function", "Division")),
        Column("country", "Country"),
        Column("location", "Location", ("City", "HQ", "Office")),
        Column("industry", "Industry", ("Sector", "Category")),
        Column("status", "Status", ("Company Status",)),
        Column("source", "Source", ("Origin", "Added Via")),
        Column("active_jobs", "Open Jobs", ("Active Jobs", "Job Count", "Jobs")),
        Column("first_seen", "First Seen", ("Added", "Date Added")),
        Column("last_checked", "Last Checked", ("Last Crawled", "Last Run")),
        Column("last_outcome", "Last Outcome", ("Outcome", "Result", "Crawl Result")),
        # What the board's own search controls say. Written by
        # crawler.job_filters, and only when filter detection is switched on --
        # a run without it leaves every one of these cells exactly as it found
        # them, because a blank incoming value never overwrites a stored one.
        Column("filters_detected", "Filters Detected", ("Has Filters", "Filters Found"),
               note="TRUE when the board offers any search control."),
        Column("filter_count", "Filter Count", ("Filters", "Number of Filters")),
        Column("filter_types", "Filter Types", ("Filter Type",),
               note="What each control appears to narrow by: department, location, and so on."),
        Column("filter_labels", "Filter Labels", ("Filter Names",),
               note="Each control's label, exactly as the board publishes it."),
        Column("filter_values", "Filter Values", ("Filter Options",),
               note="The first few options each control offers."),
        Column("filter_detection_method", "Filter Detection",
               ("Filter Method", "Filter Detection Method"),
               note="How the controls were read: select, checkboxes, links, tabs, JSON or a rendered DOM."),
        Column("filter_confidence", "Filter Confidence", ("Filter Score",),
               note="Mean confidence of the type inference, 0 to 1."),
        Column("filter_blocked", "Filter Blocked", ("Filter Block", "Filter Blocker"),
               note="Set when the board could not be read for filters, so 'none found' is not confused with 'never seen'."),
        Column("company_key", "Company Key", ("Key", "ID", "Company ID"),
               note="Identity. Written by the crawler; do not edit."),
    ),
)

CURRENT_JOBS: Final[TabSpec] = TabSpec(
    key="current_jobs",
    title="CURRENT_JOBS",
    aliases=("Current Jobs", "Jobs", "Open Jobs", "Active Jobs", "Live Jobs"),
    identity_field="job_key",
    purpose="Every IT/technology posting currently open across MASTER_COMPANIES.",
    columns=(
        Column("company_name", "Company", ("Company Name", "Employer")),
        Column("job_title", "Job Title", ("Title", "Role", "Position")),
        Column("job_url", "Job URL", ("Link", "Posting URL", "Apply URL")),
        Column("career_url", "Career Page URL", ("Board URL", "Careers URL")),
        Column("platform", "ATS / Platform", ("ATS", "Platform")),
        Column("department", "Department", ("Dept", "Function", "Team")),
        Column("location", "Location"),
        Column("country", "Country"),
        Column("workplace_type", "Remote / Hybrid / On-site",
               ("Workplace Type", "Work Model", "Remote")),
        Column("employment_type", "Employment Type", ("Job Type", "Contract Type")),
        Column("posted_date", "Posted Date", ("Date Posted",),
               note="Only where the board publishes one. Never inferred."),
        Column("first_seen", "First Seen", ("First Seen Date",),
               note="When this crawler first observed the posting."),
        Column("last_seen", "Last Seen", ("Last Seen Date",)),
        Column("job_id", "Job ID", ("Requisition ID", "Req ID")),
        Column("industry", "Industry", ("Sector",)),
        Column("source", "Source"),
        Column("run_id", "Crawl Run", ("Run ID", "Crawl Timestamp")),
        Column("job_key", "Job Key", ("Job UID", "Key"),
               note="Identity. Written by the crawler; do not edit."),
    ),
)

JOB_HISTORY: Final[TabSpec] = TabSpec(
    key="job_history",
    title="JOB_HISTORY",
    aliases=("Job History", "History", "All Jobs", "Job Archive"),
    identity_field="job_key",
    purpose=(
        "Every posting ever seen, one row each, updated in place. "
        "This is what makes 'new this week' answerable."
    ),
    columns=(
        Column("job_key", "Job Key", ("Job UID", "Key"),
               note="Identity. Written by the crawler; do not edit."),
        Column("company_name", "Company", ("Company Name",)),
        Column("company_key", "Company Key", ("Company ID",)),
        Column("job_title", "Job Title", ("Title", "Role")),
        Column("job_url", "Job URL", ("Link",)),
        Column("url_key", "URL Key", ("Canonical URL",),
               note="Canonical URL, so a board rewriting its links is not a closure."),
        Column("content_key", "Content Key", ("Fingerprint",),
               note="Company/title/location hash, the last-resort re-link."),
        Column("platform", "ATS / Platform", ("ATS", "Platform")),
        Column("department", "Department", ("Dept",)),
        Column("location", "Location"),
        Column("country", "Country"),
        Column("status", "Status", ("Job Status",)),
        Column("first_seen", "First Seen", ("First Seen Date",)),
        Column("last_seen", "Last Seen", ("Last Seen Date",)),
        Column("closed_at", "Closed At", ("Closed", "Date Closed")),
        Column("first_run_id", "First Run", ("First Run ID",)),
        Column("last_run_id", "Last Run", ("Last Run ID",)),
    ),
)

NEW_LAST_WEEK: Final[TabSpec] = TabSpec(
    key="new_last_week",
    title="NEW_LAST_WEEK",
    aliases=("New Last Week", "New Jobs", "Weekly Job Changes", "Job Changes", "Weekly Changes"),
    identity_field="",  # append-only: each week's rows are a historical record
    purpose="What changed this week: postings that appeared, reopened or closed.",
    columns=(
        Column("week_start", "Week Start", ("Week",)),
        Column("week_end", "Week End"),
        Column("change", "Change", ("Event", "Type", "Change Type"),
               note="new, reopened or closed."),
        Column("company_name", "Company", ("Company Name",)),
        Column("job_title", "Job Title", ("Title", "Role")),
        Column("job_url", "Job URL", ("Link",)),
        Column("platform", "ATS / Platform", ("ATS", "Platform")),
        Column("department", "Department", ("Dept",)),
        Column("location", "Location"),
        Column("country", "Country"),
        Column("industry", "Industry"),
        Column("first_seen", "First Seen"),
        Column("last_seen", "Last Seen"),
        Column("run_id", "Crawl Run", ("Run ID",)),
        Column("job_key", "Job Key", ("Job UID",)),
    ),
)

NEW_COMPANY_DISCOVERY: Final[TabSpec] = TabSpec(
    key="new_company_discovery",
    title="NEW_COMPANY_DISCOVERY",
    aliases=("New Company Discovery", "New Companies", "Discovery", "Discovered Companies",
             "Prospects"),
    identity_field="discovery_key",
    purpose=(
        "Companies found on the public web that are not in MASTER_COMPANIES. "
        "Nothing here is promoted without review."
    ),
    columns=(
        Column("week_start", "Week Start", ("Week",)),
        Column("week_end", "Week End"),
        Column("company_name", "Company", ("Company Name",)),
        Column("industry", "Industry", ("Sector", "Category")),
        Column("website", "Website", ("Domain",)),
        Column("career_url", "Career Page URL", ("Careers URL",)),
        Column("platform", "ATS / Platform", ("ATS", "Platform")),
        Column("sample_titles", "Relevant Job Titles", ("Job Titles", "Roles")),
        Column("sample_job_url", "Job URL", ("Sample Job", "Link")),
        Column("job_count", "Jobs Found", ("Job Count",)),
        Column("location", "Location"),
        Column("country", "Country"),
        Column("discovery_source", "Discovery Source", ("Source", "Found Via")),
        Column("attribution", "Attribution", ("Credit",),
               note="Where a source's terms require it to be credited."),
        Column("confidence", "Confidence", ("Score",)),
        Column("status", "Status", ("Review Status", "Approved"),
               note="pending_review, approved or rejected. Edit this column to approve."),
        Column("discovered_at", "Discovered At", ("Discovered", "Date Found")),
        Column("discovery_key", "Discovery Key", ("Key",),
               note="Identity. Written by the crawler; do not edit."),
    ),
)

DISCOVERY_CONFIG: Final[TabSpec] = TabSpec(
    key="discovery_config",
    title="DISCOVERY_CONFIG",
    aliases=("Discovery Config", "Industry Config", "Industries", "Config", "Settings"),
    identity_field="industry",
    purpose="Which industries to search for, and where. Edited by you, read by the crawler.",
    columns=(
        Column("industry", "Industry", ("Name", "Category", "Sector")),
        Column("keywords", "Keywords", ("Terms", "Search Terms"),
               note="Comma-separated."),
        Column("countries", "Countries", ("Country",),
               note="Comma-separated."),
        Column("enabled", "Enabled", ("Active", "On"),
               note="TRUE or FALSE."),
    ),
)

IT_KEYWORDS: Final[TabSpec] = TabSpec(
    key="it_keywords",
    title="IT_KEYWORDS",
    aliases=("IT Keywords", "Keywords", "ERP Keywords", "Tech Keywords",
             "Keyword Config", "IT Keyword Config"),
    identity_field="keyword",
    purpose="The technology and ERP terms that mark a posting as IT. Edited by "
            "you, reloaded by the crawler on every run.",
    columns=(
        Column("keyword", "Keyword", ("Term", "Word", "Technology"),
               note="The term to look for, e.g. SAP or PeopleSoft."),
        Column("category", "Category", ("Group", "Type", "Family"),
               note="What the term belongs to, e.g. ERP Platforms."),
        Column("enabled", "Enabled", ("Active", "On", "Use"),
               note="TRUE or FALSE. Blank counts as TRUE."),
        Column("match_type", "Match Type", ("Match", "Mode"),
               note="How to match: 'phrase' for whole words, 'substring' to "
                    "match inside a longer word."),
        Column("notes", "Notes", ("Comment", "Note", "Remarks"),
               note="Yours. The crawler never reads or writes this."),
    ),
)

WEEKLY_RUNS: Final[TabSpec] = TabSpec(
    key="weekly_runs",
    title="WEEKLY_RUNS",
    aliases=("Weekly Runs", "Runs", "Run History", "Crawl Runs"),
    identity_field="run_id",
    purpose="One row per run: when, how long, how much, and how well it went.",
    columns=(
        Column("run_id", "Run ID", ("Run",),
               note="Identity. Written by the crawler; do not edit."),
        Column("week_start", "Week Start", ("Week",)),
        Column("week_end", "Week End"),
        Column("started_at", "Started", ("Start Time",)),
        Column("finished_at", "Finished", ("End Time",)),
        Column("duration", "Duration", ("Elapsed", "Runtime")),
        Column("mode", "Mode", ("Run Mode",)),
        Column("status", "Status", ("Run Status",)),
        Column("companies_total", "Companies Total", ("Total Companies",)),
        Column("companies_checked", "Companies Checked", ("Checked",)),
        Column("companies_succeeded", "Succeeded", ("Successful",)),
        Column("companies_failed", "Failed", ("Failures",)),
        Column("companies_with_jobs", "With Jobs", ("Companies With Jobs",)),
        Column("companies_no_jobs", "No Open Jobs", ("Empty Boards",)),
        Column("jobs_active", "Jobs Active", ("Active Jobs", "Total Jobs")),
        Column("jobs_new", "Jobs New", ("New Jobs",)),
        Column("jobs_closed", "Jobs Closed", ("Closed Jobs",)),
        Column("companies_discovered", "Companies Discovered", ("New Companies",)),
        Column("success_rate", "Success Rate", ("Success %",)),
        Column("notes", "Notes", ("Comment",)),
    ),
)

DASHBOARD: Final[TabSpec] = TabSpec(
    key="dashboard",
    title="DASHBOARD",
    aliases=("Dashboard", "Summary", "Metrics", "Overview", "Stats"),
    identity_field="",  # rewritten whole each run
    purpose="Current totals and the week-by-week history behind them.",
    columns=(
        Column("section", "Section", ("Group", "Category")),
        Column("metric", "Metric", ("Name", "Measure")),
        Column("value", "Value", ("Count", "Total")),
        Column("week_start", "Week", ("Week Start",)),
        Column("updated_at", "Updated", ("Last Updated",)),
    ),
)

FAILURES: Final[TabSpec] = TabSpec(
    key="failures",
    title="FAILURES",
    aliases=("Failures", "Errors", "Failed Companies", "Problems"),
    identity_field="company_key",
    purpose=(
        "Why companies could not be read on the latest run. "
        "Replaced each run; the historical counts live in WEEKLY_RUNS."
    ),
    columns=(
        Column("company_name", "Company", ("Company Name",)),
        Column("website", "Website", ("Domain",)),
        Column("crawled_url", "Crawled URL", ("URL", "Attempted URL")),
        Column("platform", "ATS / Platform", ("ATS", "Platform")),
        Column("failure_type", "Failure Type", ("Blocker", "Error Type", "Reason"),
               note="403, 404, 429, Cloudflare, AWS WAF, CAPTCHA, bad URL, ..."),
        Column("http_status", "HTTP Status", ("Status Code",)),
        Column("detail", "Detail", ("Error", "Message", "Failure Reason")),
        Column("browser_required", "Browser Required", ("Needs Browser",)),
        Column("retryable", "Retryable", ("Can Retry",)),
        Column("run_id", "Crawl Run", ("Run ID",)),
        Column("checked_at", "Checked At", ("Last Checked",)),
        Column("company_key", "Company Key", ("Key",),
               note="Identity. Written by the crawler; do not edit."),
    ),
)

#: Every tab the crawler maintains, in the order it creates them.
ALL_TABS: Final[Tuple[TabSpec, ...]] = (
    MASTER_COMPANIES,
    CURRENT_JOBS,
    JOB_HISTORY,
    NEW_LAST_WEEK,
    NEW_COMPANY_DISCOVERY,
    DISCOVERY_CONFIG,
    IT_KEYWORDS,
    WEEKLY_RUNS,
    DASHBOARD,
    FAILURES,
)
