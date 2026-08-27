"""Talk to the Google Sheets API: batched, throttled, and unable to delete anything.

This module never imports the Google libraries. It is handed a service object
and calls it, which is what lets the whole package be tested offline against a
fake that records every request::

    >>> from sheets.client import SheetsClient
    >>> client = SheetsClient(service, spreadsheet_id)
    >>> client.tab_titles()
    ['MASTER_COMPANIES', 'CURRENT_JOBS', ...]

**Destructive requests are refused, not merely avoided.** Every structural
request passes through :meth:`SheetsClient.batch_update`, which rejects any
request naming a deletion — ``deleteSheet``, ``deleteDimension``,
``deleteRange``, ``cutPaste`` and the rest — by raising
:class:`DestructiveRequestError` before anything reaches the network. The
promise that the crawler cannot destroy an operator's spreadsheet is therefore
a property of the code rather than of the care taken while writing it, and
there is a test that asserts it.

**Reads and writes are batched.** The Sheets API bills per request against a
per-minute quota, not per cell, so a spreadsheet with nine tabs is read in one
call rather than nine. Writes are chunked by cell count, because a single
request has a size limit that 40,000 job rows would exceed.

**Rate limiting is handled by waiting.** A 429 or a 5xx is retried with
exponential backoff and honours ``Retry-After`` when the API sends it. A 4xx
that is not 429 is a mistake in the request and is raised immediately: retrying
a malformed range just makes the same mistake more times.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Final, List, Optional, Sequence, Tuple

from loguru import logger

__all__ = [
    "DESTRUCTIVE_REQUESTS",
    "DestructiveRequestError",
    "SheetsClient",
    "SheetsError",
]


class SheetsError(RuntimeError):
    """A Sheets API call failed and could not be retried into success."""


class DestructiveRequestError(SheetsError):
    """A request that would remove data was refused before being sent."""


#: Request kinds that remove or overwrite structure. The crawler has no
#: legitimate use for any of them: it creates tabs, appends columns and writes
#: values, and nothing else. Refusing them here means a future edit that
#: introduces one fails loudly in the tests rather than quietly in production.
DESTRUCTIVE_REQUESTS: Final[frozenset] = frozenset(
    {
        "deleteSheet",
        "deleteDimension",
        "deleteRange",
        "deleteBanding",
        "deleteFilterView",
        "deleteProtectedRange",
        "deleteNamedRange",
        "deleteConditionalFormatRule",
        "deleteDeveloperMetadata",
        "deleteDuplicates",
        "deleteDataSource",
        "deleteEmbeddedObject",
        "cutPaste",
        "moveDimension",
        "randomizeRange",
        "sortRange",
        "trimWhitespace",
    }
)

#: Statuses worth waiting out. 429 is the quota; the 5xx family is Google
#: having a moment.
_RETRY_STATUSES: Final[frozenset] = frozenset({429, 500, 502, 503, 504})

#: Attempts per call, including the first.
DEFAULT_RETRIES: Final[int] = 5

#: Base for the exponential backoff, in seconds.
_BACKOFF_BASE: Final[float] = 2.0

#: Ceiling on a single wait, so a long outage does not park a run for an hour.
#:
#: Sheets enforces its read and write quotas over a sixty-second sliding window,
#: so a ceiling below that cannot clear one: the retry lands inside the same
#: window and is refused again. This is set above it deliberately -- a run that
#: has exhausted its quota has nothing useful to do but wait for the window.
_BACKOFF_CEILING: Final[float] = 75.0

#: Cells per values write. The API caps a request's size rather than its row
#: count, and this is comfortably inside it for the widest tab here.
DEFAULT_CHUNK_CELLS: Final[int] = 100_000

#: Rows per values write, whichever limit is reached first.
DEFAULT_CHUNK_ROWS: Final[int] = 5_000


@dataclass
class ApiStats:
    """How much API traffic a run generated.

    Attributes:
        reads: Read calls made.
        writes: Write calls made.
        structural: Structural ``batchUpdate`` calls made.
        cells_written: Cells written.
        retries: Calls that had to be retried.
        seconds_waiting: Time spent in backoff.
    """

    reads: int = 0
    writes: int = 0
    structural: int = 0
    cells_written: int = 0
    retries: int = 0
    seconds_waiting: float = 0.0

    def describe(self) -> str:
        """Render as one line for the run report.

        Returns:
            Human-readable summary.
        """
        return (
            f"{self.reads} read(s), {self.writes} write(s), {self.structural} structural, "
            f"{self.cells_written:,} cell(s), {self.retries} retry(ies), "
            f"{self.seconds_waiting:.1f}s waiting"
        )


def _status_of(error: BaseException) -> Optional[int]:
    """Extract the HTTP status from whatever the API client raised.

    ``googleapiclient`` raises ``HttpError``, which carries the response on
    ``.resp``. Read defensively so this module needs no import of it, and so a
    fake in a test can raise anything shaped roughly right.

    Args:
        error: The exception.

    Returns:
        The status, or ``None`` when the error is not an HTTP one.
    """
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(error, "status_code", None)

    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(error: BaseException) -> Optional[float]:
    """Read a ``Retry-After`` header off an error, if it carries one.

    Args:
        error: The exception.

    Returns:
        Seconds to wait, or ``None``.
    """
    response = getattr(error, "resp", None)
    if response is None:
        return None

    try:
        value = response.get("retry-after")  # httplib2 headers are a dict
    except AttributeError:
        value = None

    if value is None:
        return None

    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class SheetsClient:
    """A batched, retrying, non-destructive client for one spreadsheet.

    Args:
        service: The Sheets API service resource, as
            :func:`sheets.auth.build_service` returns. Injected rather than
            built here so tests can pass a fake.
        spreadsheet_id: The spreadsheet to operate on.
        retries: Attempts per call, including the first.
        sleep: Injected sleeper, so tests exercise the backoff without waiting.
    """

    def __init__(
        self,
        service: Any,
        spreadsheet_id: str,
        retries: int = DEFAULT_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id
        self._retries = max(1, int(retries))
        self._sleep = sleep
        self._metadata: Optional[Dict[str, Any]] = None
        self.stats = ApiStats()

    @property
    def spreadsheet_id(self) -> str:
        """The spreadsheet this client addresses.

        Returns:
            Its id.
        """
        return self._spreadsheet_id

    # -- plumbing ------------------------------------------------------------

    def _call(self, request: Any, what: str) -> Any:
        """Execute one API request, waiting out the failures worth waiting out.

        Args:
            request: A prepared request with an ``execute`` method.
            what: What the call is doing, for the log and any error message.

        Returns:
            The decoded response.

        Raises:
            SheetsError: If the call fails permanently, or keeps failing.
        """
        last: Optional[BaseException] = None

        for attempt in range(1, self._retries + 1):
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001 - re-raised as SheetsError below
                last = exc
                status = _status_of(exc)

                if status is not None and status not in _RETRY_STATUSES:
                    # A 400 means the request is wrong, a 403 means the sheet is
                    # not shared with us, a 404 means it does not exist. None of
                    # those improve on a second attempt.
                    raise SheetsError(f"{what} failed: {exc}") from exc

                if attempt >= self._retries:
                    break

                wait = _retry_after(exc)
                if wait is None:
                    # Full jitter, so a run whose calls all rate-limit at once
                    # does not retry them all at once too.
                    wait = min(_BACKOFF_CEILING, _BACKOFF_BASE ** attempt)
                    wait = random.uniform(wait / 2.0, wait)

                self.stats.retries += 1
                self.stats.seconds_waiting += wait
                logger.warning(
                    "{} rate-limited or failed ({}), retrying in {:.1f}s [{}/{}]",
                    what,
                    status or type(exc).__name__,
                    wait,
                    attempt,
                    self._retries,
                )
                self._sleep(wait)

        raise SheetsError(f"{what} failed after {self._retries} attempt(s): {last}") from last

    def _sheets(self) -> Any:
        """The ``spreadsheets()`` resource.

        Returns:
            The resource.
        """
        return self._service.spreadsheets()

    # -- reading -------------------------------------------------------------

    def metadata(self, include_grid: bool = False, refresh: bool = False) -> Dict[str, Any]:
        """Read the spreadsheet's properties and the shape of every tab.

        Cached for the life of the client, because tab titles, sheet ids and
        grid sizes are consulted constantly -- every store resolves its title
        and checks its grid before appending -- and Sheets allows only sixty
        reads a minute per user. Any structural change invalidates the cache,
        so a tab created or resized mid-run is still seen.

        Args:
            include_grid: Whether to include cell data. Almost never wanted:
                it downloads the entire spreadsheet, and is never cached.
            refresh: Ignore the cache and re-read.

        Returns:
            The metadata.
        """
        if include_grid:
            self.stats.reads += 1
            return self._call(
                self._sheets().get(spreadsheetId=self._spreadsheet_id, includeGridData=True),
                "Reading spreadsheet metadata with cell data",
            )

        if self._metadata is not None and not refresh:
            return self._metadata

        self.stats.reads += 1
        self._metadata = self._call(
            self._sheets().get(spreadsheetId=self._spreadsheet_id, includeGridData=False),
            "Reading spreadsheet metadata",
        )
        return self._metadata

    def invalidate(self) -> None:
        """Forget the cached metadata, so the next call re-reads it."""
        self._metadata = None

    def tab_titles(self) -> List[str]:
        """Every tab title, in spreadsheet order.

        Returns:
            The titles.
        """
        return [
            (sheet.get("properties", {}) or {}).get("title", "")
            for sheet in self.metadata().get("sheets", []) or []
        ]

    def tab_ids(self) -> Dict[str, int]:
        """Every tab's title and numeric id.

        Returns:
            Title to sheet id. The id is what a structural request addresses.
        """
        result: Dict[str, int] = {}
        for sheet in self.metadata().get("sheets", []) or []:
            properties = sheet.get("properties", {}) or {}
            title = properties.get("title", "")
            if title:
                result[title] = int(properties.get("sheetId", 0))
        return result

    def read(self, a1: str) -> List[List[Any]]:
        """Read one range.

        Args:
            a1: The range, in A1 notation.

        Returns:
            Its rows. Trailing empty rows and cells are omitted by the API, so
            rows are not all the same length.
        """
        self.stats.reads += 1
        response = self._call(
            self._sheets().values().get(
                spreadsheetId=self._spreadsheet_id, range=a1, majorDimension="ROWS"
            ),
            f"Reading {a1}",
        )
        return response.get("values", []) or []

    def batch_read(self, ranges: Sequence[str]) -> List[List[List[Any]]]:
        """Read several ranges in one call.

        Args:
            ranges: The ranges, in A1 notation.

        Returns:
            One list of rows per range, in the order requested.
        """
        if not ranges:
            return []

        self.stats.reads += 1
        response = self._call(
            self._sheets().values().batchGet(
                spreadsheetId=self._spreadsheet_id,
                ranges=list(ranges),
                majorDimension="ROWS",
            ),
            f"Reading {len(ranges)} range(s)",
        )
        return [
            value_range.get("values", []) or []
            for value_range in response.get("valueRanges", []) or []
        ]

    # -- writing -------------------------------------------------------------

    def write(self, a1: str, values: Sequence[Sequence[Any]]) -> int:
        """Write values to a range, chunked if it is large.

        Args:
            a1: Where to start writing, in A1 notation. The range's start
                is used; its extent is determined by ``values``.
            values: Rows to write.

        Returns:
            How many cells were written.
        """
        rows = [list(row) for row in values]
        if not rows:
            return 0

        written = 0
        for chunk_range, chunk in self._chunks(a1, rows):
            self.stats.writes += 1
            response = self._call(
                self._sheets().values().update(
                    spreadsheetId=self._spreadsheet_id,
                    range=chunk_range,
                    valueInputOption="RAW",
                    body={"values": chunk},
                ),
                f"Writing {len(chunk)} row(s) to {chunk_range}",
            )
            written += int(response.get("updatedCells", 0) or 0)

        self.stats.cells_written += written
        return written

    def _chunks(
        self, a1: str, rows: List[List[Any]]
    ) -> List[Tuple[str, List[List[Any]]]]:
        """Split a write into requests the API will accept.

        Args:
            a1: The starting range.
            rows: Every row to write.

        Returns:
            ``(range, rows)`` pairs, each starting at the right row offset.
        """
        width = max((len(row) for row in rows), default=1) or 1
        per_chunk = max(1, min(DEFAULT_CHUNK_ROWS, DEFAULT_CHUNK_CELLS // width))

        if len(rows) <= per_chunk:
            return [(a1, rows)]

        title, _, cell = a1.rpartition("!")
        start_cell = cell.split(":")[0]
        start_row = int("".join(character for character in start_cell if character.isdigit()) or 1)
        start_column = "".join(character for character in start_cell if character.isalpha()) or "A"

        chunks: List[Tuple[str, List[List[Any]]]] = []
        for offset in range(0, len(rows), per_chunk):
            chunk = rows[offset : offset + per_chunk]
            chunk_range = f"{title}!{start_column}{start_row + offset}" if title else f"{start_column}{start_row + offset}"
            chunks.append((chunk_range, chunk))

        logger.debug("Split a {}-row write into {} request(s)", len(rows), len(chunks))
        return chunks

    def batch_write(self, updates: Sequence[Tuple[str, Sequence[Sequence[Any]]]]) -> int:
        """Write several ranges in one call.

        Args:
            updates: ``(range, rows)`` pairs.

        Returns:
            How many cells were written.
        """
        data = [
            {"range": a1, "values": [list(row) for row in rows]}
            for a1, rows in updates
            if rows
        ]
        if not data:
            return 0

        self.stats.writes += 1
        response = self._call(
            self._sheets().values().batchUpdate(
                spreadsheetId=self._spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ),
            f"Writing {len(data)} range(s)",
        )

        written = int(response.get("totalUpdatedCells", 0) or 0)
        self.stats.cells_written += written
        return written

    def append(self, a1: str, values: Sequence[Sequence[Any]]) -> int:
        """Append rows below whatever is already in a range.

        Args:
            a1: The range identifying the table to append to.
            values: Rows to append.

        Returns:
            How many cells were written.
        """
        rows = [list(row) for row in values]
        if not rows:
            return 0

        written = 0
        for _, chunk in self._chunks(a1, rows):
            self.stats.writes += 1
            response = self._call(
                self._sheets().values().append(
                    spreadsheetId=self._spreadsheet_id,
                    range=a1,
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": chunk},
                ),
                f"Appending {len(chunk)} row(s) to {a1}",
            )
            updates = response.get("updates", {}) or {}
            written += int(updates.get("updatedCells", 0) or 0)

        self.stats.cells_written += written
        return written

    # -- structure -----------------------------------------------------------

    def batch_update(self, requests: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply structural changes, refusing any that would remove data.

        Args:
            requests: Sheets API request objects.

        Returns:
            The response, or an empty mapping when there was nothing to do.

        Raises:
            DestructiveRequestError: If any request would delete or move
                existing content. Raised before anything is sent, so a batch
                containing one bad request performs none of them.
        """
        prepared = [request for request in requests if request]
        if not prepared:
            return {}

        for request in prepared:
            for kind in request:
                if kind in DESTRUCTIVE_REQUESTS:
                    raise DestructiveRequestError(
                        f"Refusing to send a {kind!r} request. Version 3 creates tabs, "
                        "appends columns and writes values; it never removes anything "
                        "from the operator's spreadsheet."
                    )

        self.stats.structural += 1
        response = self._call(
            self._sheets().batchUpdate(
                spreadsheetId=self._spreadsheet_id, body={"requests": prepared}
            ),
            f"Applying {len(prepared)} structural change(s)",
        )

        # A tab was added, renamed or resized, so anything cached about the
        # spreadsheet's shape is now wrong.
        self.invalidate()
        return response

    def add_tab(self, title: str, rows: int = 1000, columns: int = 26, frozen_rows: int = 1) -> None:
        """Create a tab.

        Args:
            title: Its title.
            rows: How many rows to allocate.
            columns: How many columns to allocate.
            frozen_rows: Rows to freeze, so the header stays put when scrolling.
        """
        self.batch_update(
            [
                {
                    "addSheet": {
                        "properties": {
                            "title": title,
                            "gridProperties": {
                                "rowCount": max(2, int(rows)),
                                "columnCount": max(1, int(columns)),
                                "frozenRowCount": max(0, int(frozen_rows)),
                            },
                        }
                    }
                }
            ]
        )
        logger.info("Created tab {!r}", title)

    def rename_tab(self, sheet_id: int, title: str) -> None:
        """Rename a tab.

        Args:
            sheet_id: The tab's numeric id.
            title: Its new title.
        """
        self.batch_update(
            [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": int(sheet_id), "title": title},
                        "fields": "title",
                    }
                }
            ]
        )
        logger.info("Renamed tab {} to {!r}", sheet_id, title)

    def ensure_size(self, sheet_id: int, rows: int, columns: int) -> None:
        """Grow a tab's grid so a write has somewhere to land.

        Only ever grows. A tab is never shrunk, because shrinking one is how
        data below the fold disappears.

        Args:
            sheet_id: The tab's numeric id.
            rows: The minimum rows wanted.
            columns: The minimum columns wanted.
        """
        self.batch_update(
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": int(sheet_id),
                            "gridProperties": {
                                "rowCount": max(2, int(rows)),
                                "columnCount": max(1, int(columns)),
                            },
                        },
                        "fields": "gridProperties.rowCount,gridProperties.columnCount",
                    }
                }
            ]
        )

    def format_header(self, sheet_id: int, columns: int, frozen_rows: int = 1) -> None:
        """Make a tab's first row look like a header.

        Bold, a light fill, and frozen so it stays visible. Cosmetic, and
        applied only when a tab is created — re-running initialisation does not
        re-impose formatting an operator may have changed on purpose.

        Args:
            sheet_id: The tab's numeric id.
            columns: How many columns to style.
            frozen_rows: Rows to freeze.
        """
        self.batch_update(
            [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": int(sheet_id),
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": max(1, int(columns)),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.92, "green": 0.94, "blue": 0.96},
                                "verticalAlignment": "MIDDLE",
                            }
                        },
                        "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment)",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": int(sheet_id),
                            "gridProperties": {"frozenRowCount": max(0, int(frozen_rows))},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ]
        )
