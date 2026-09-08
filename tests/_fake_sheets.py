"""An in-memory stand-in for the Google Sheets API.

The whole point of :mod:`sheets.client` taking an injected service is that the
Sheets layer can be tested without credentials, without a network, and without a
real spreadsheet to damage. This is the thing that gets injected.

It is a simulator rather than a mock: it holds actual cell values, applies
``addSheet``, ``updateSheetProperties`` and ``repeatCell`` to them, and answers
reads with what was written. That means a test can assert on the *state the
spreadsheet ends up in*, not merely on which methods were called — which is the
difference between proving initialisation is idempotent and proving that it
calls something twice.

**It models the destructive operations too, faithfully.** A shrinking
``gridProperties.rowCount`` really does drop the rows past the new boundary
here, exactly as Google does. That is deliberate and was learned the hard way:
this fake used to clamp such a request upwards, which made every test agree that
``ensure_size`` only ever grew while the real client was quietly shrinking a
production tab. A simulator that declines to reproduce a dangerous operation
cannot be used to prove the code never performs one.

It also records every request, so a test can assert what was *not* sent::

    >>> service = FakeSheetsService({"Sheet1": []})
    >>> initialise(SheetsClient(service, "id"))
    >>> service.destructive_requests()
    []
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "FakeHttpError",
    "FakeSheetsService",
    "FlakyFakeSheetsService",
]

#: An A1 range, as this fake needs to understand it: an optional quoted title,
#: then a start cell, then optionally an end cell.
_RANGE = re.compile(
    r"^(?:'(?P<quoted>(?:[^']|'')*)'|(?P<bare>[^!]+))?"
    r"(?:!)?(?P<start_col>[A-Z]*)(?P<start_row>\d*)"
    r"(?::(?P<end_col>[A-Z]*)(?P<end_row>\d*))?$"
)


class FakeHttpError(Exception):
    """Shaped like ``googleapiclient.errors.HttpError`` enough for the client.

    Args:
        status: The HTTP status to report.
        message: The error text.
        retry_after: A ``Retry-After`` value to advertise, if any.
    """

    def __init__(self, status: int, message: str = "", retry_after: Optional[str] = None) -> None:
        super().__init__(message or f"HTTP {status}")
        headers: Dict[str, str] = {"status": str(status)}
        if retry_after is not None:
            headers["retry-after"] = retry_after
        # `resp` mimics httplib2's response: a dict with a `.status` attribute.
        self.resp = _FakeResponse(status, headers)


class _FakeResponse(dict):
    """A dict with a ``status``, as httplib2 returns."""

    def __init__(self, status: int, headers: Dict[str, str]) -> None:
        super().__init__(headers)
        self.status = status


def _column_index(letters: str) -> int:
    """Turn column letters into a zero-based index.

    Args:
        letters: e.g. ``"A"``, ``"AA"``.

    Returns:
        The index.
    """
    index = 0
    for character in letters:
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1


class _Request:
    """A prepared call, executed when the client asks.

    Args:
        service: The fake to run against.
        kind: Which operation.
        payload: Its arguments.
    """

    def __init__(self, service: "FakeSheetsService", kind: str, payload: Dict[str, Any]) -> None:
        self._service = service
        self._kind = kind
        self._payload = payload

    def execute(self) -> Dict[str, Any]:
        """Run the call.

        Returns:
            A response shaped like the real API's.
        """
        return self._service._execute(self._kind, self._payload)


class _Values:
    """The ``spreadsheets().values()`` resource."""

    def __init__(self, service: "FakeSheetsService") -> None:
        self._service = service

    def get(self, spreadsheetId: str, range: str, majorDimension: str = "ROWS") -> _Request:  # noqa: A002,N803
        """Prepare a single-range read."""
        return _Request(self._service, "values.get", {"range": range, "major": majorDimension})

    def batchGet(  # noqa: N802
        self, spreadsheetId: str, ranges: Sequence[str], majorDimension: str = "ROWS"  # noqa: N803
    ) -> _Request:
        """Prepare a multi-range read."""
        return _Request(
            self._service, "values.batchGet", {"ranges": list(ranges), "major": majorDimension}
        )

    def update(  # noqa: N803
        self, spreadsheetId: str, range: str, valueInputOption: str, body: Dict[str, Any]  # noqa: A002
    ) -> _Request:
        """Prepare a single-range write."""
        return _Request(self._service, "values.update", {"range": range, "body": body})

    def batchUpdate(self, spreadsheetId: str, body: Dict[str, Any]) -> _Request:  # noqa: N802,N803
        """Prepare a multi-range write."""
        return _Request(self._service, "values.batchUpdate", {"body": body})

    def append(  # noqa: N803
        self,
        spreadsheetId: str,  # noqa: N803
        range: str,  # noqa: A002
        valueInputOption: str,  # noqa: N803
        insertDataOption: str,  # noqa: N803
        body: Dict[str, Any],
    ) -> _Request:
        """Prepare an append."""
        return _Request(self._service, "values.append", {"range": range, "body": body})


class _Spreadsheets:
    """The ``spreadsheets()`` resource."""

    def __init__(self, service: "FakeSheetsService") -> None:
        self._service = service
        self._values = _Values(service)

    def get(self, spreadsheetId: str, includeGridData: bool = False) -> _Request:  # noqa: N803
        """Prepare a metadata read."""
        return _Request(self._service, "get", {})

    def values(self) -> _Values:
        """The values sub-resource."""
        return self._values

    def batchUpdate(self, spreadsheetId: str, body: Dict[str, Any]) -> _Request:  # noqa: N802,N803
        """Prepare a structural update."""
        return _Request(self._service, "batchUpdate", {"body": body})


class FakeSheetsService:
    """An in-memory spreadsheet that behaves enough like the real API.

    Args:
        tabs: Starting content, title to rows. ``{"Sheet1": []}`` models the
            single empty tab a brand-new Google spreadsheet contains.
        title: The spreadsheet's title.
    """

    def __init__(
        self,
        tabs: Optional[Dict[str, List[List[Any]]]] = None,
        title: str = "CareerCrawler V3",
    ) -> None:
        self.title = title
        self.tabs: Dict[str, List[List[Any]]] = {
            name: [list(row) for row in rows] for name, rows in (tabs or {"Sheet1": []}).items()
        }
        self.sheet_ids: Dict[str, int] = {
            name: index for index, name in enumerate(self.tabs)
        }
        self.grid: Dict[str, Tuple[int, int]] = {name: (1000, 26) for name in self.tabs}
        self.frozen: Dict[str, int] = {name: 0 for name in self.tabs}
        self.formatted: List[str] = []

        #: Every request executed, as ``(kind, payload)``.
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        #: Every structural request, flattened to its kind.
        self.structural_kinds: List[str] = []

    # -- the resource surface the client uses --------------------------------

    def spreadsheets(self) -> _Spreadsheets:
        """The spreadsheets resource."""
        return _Spreadsheets(self)

    # -- assertions a test wants ---------------------------------------------

    def destructive_requests(self) -> List[str]:
        """Every structural request that would have removed something.

        Returns:
            Their kinds. Must always be empty.
        """
        from sheets.client import DESTRUCTIVE_REQUESTS

        return [kind for kind in self.structural_kinds if kind in DESTRUCTIVE_REQUESTS]

    def mutating_calls(self) -> List[str]:
        """Every call that changed the spreadsheet.

        Returns:
            Their kinds, so a test can assert a second run made none.
        """
        return [
            kind
            for kind, _ in self.calls
            if kind in ("batchUpdate", "values.update", "values.batchUpdate", "values.append")
        ]

    def headers_of(self, title: str) -> List[str]:
        """The header row of a tab.

        Args:
            title: The tab.

        Returns:
            Its first row, or ``[]``.
        """
        rows = self.tabs.get(title, [])
        return [str(cell) for cell in rows[0]] if rows else []

    # -- execution -----------------------------------------------------------

    def _execute(self, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Run one prepared call.

        Args:
            kind: Which operation.
            payload: Its arguments.

        Returns:
            A response shaped like the real API's.

        Raises:
            FakeHttpError: For a range naming a tab that does not exist, as the
                real API does with a 400.
        """
        self.calls.append((kind, payload))
        handler = getattr(self, f"_do_{kind.replace('.', '_')}")
        return handler(payload)

    def _do_get(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        """Metadata read."""
        return {
            "properties": {
                "title": self.title,
                "locale": "en_US",
                "timeZone": "Etc/GMT",
            },
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/fake/edit",
            "sheets": [
                {
                    "properties": {
                        "sheetId": self.sheet_ids[name],
                        "title": name,
                        "index": index,
                        "gridProperties": {
                            "rowCount": self.grid[name][0],
                            "columnCount": self.grid[name][1],
                            "frozenRowCount": self.frozen.get(name, 0),
                        },
                    }
                }
                for index, name in enumerate(self.tabs)
            ],
        }

    def _parse(self, a1: str) -> Tuple[str, int, int, Optional[int], Optional[int]]:
        """Break an A1 range into its parts.

        Args:
            a1: The range.

        Returns:
            ``(title, start_row, start_column, end_row, end_column)``, rows
            one-based and columns zero-based.

        Raises:
            FakeHttpError: If the range names a tab that does not exist.
        """
        match = _RANGE.match(a1.strip())
        if match is None:
            raise FakeHttpError(400, f"Unable to parse range: {a1}")

        title = match.group("quoted")
        title = title.replace("''", "'") if title is not None else match.group("bare")
        title = (title or "").strip()

        if title and title not in self.tabs:
            raise FakeHttpError(400, f"Unable to parse range: {a1}")

        if not title:
            title = next(iter(self.tabs))

        start_row = int(match.group("start_row") or 1)
        start_column = _column_index(match.group("start_col") or "A")
        end_row = int(match.group("end_row")) if match.group("end_row") else None
        end_column = _column_index(match.group("end_col")) if match.group("end_col") else None

        return title, start_row, start_column, end_row, end_column

    def _slice(self, a1: str, major: str = "ROWS") -> List[List[Any]]:
        """Read a range out of the in-memory content.

        Args:
            a1: The range.
            major: ``"ROWS"`` or ``"COLUMNS"``.

        Returns:
            The values, trailing blanks trimmed as the real API trims them.
        """
        title, start_row, start_column, end_row, end_column = self._parse(a1)
        rows = self.tabs.get(title, [])

        last_row = end_row if end_row is not None else len(rows)
        selected = rows[start_row - 1 : last_row]

        cut: List[List[Any]] = []
        for row in selected:
            last_column = end_column + 1 if end_column is not None else len(row)
            piece = [cell for cell in row[start_column:last_column]]
            while piece and (piece[-1] is None or piece[-1] == ""):
                piece.pop()
            cut.append(piece)

        while cut and not cut[-1]:
            cut.pop()

        if major != "COLUMNS":
            return cut

        width = max((len(row) for row in cut), default=0)
        columns: List[List[Any]] = []
        for index in range(width):
            column = [row[index] if index < len(row) else "" for row in cut]
            while column and (column[-1] is None or column[-1] == ""):
                column.pop()
            columns.append(column)
        return columns

    def _do_values_get(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Single-range read."""
        return {"values": self._slice(payload["range"], payload.get("major", "ROWS"))}

    def _do_values_batchGet(self, payload: Dict[str, Any]) -> Dict[str, Any]:  # noqa: N802
        """Multi-range read."""
        return {
            "valueRanges": [
                {"range": a1, "values": self._slice(a1, payload.get("major", "ROWS"))}
                for a1 in payload["ranges"]
            ]
        }

    def _write(self, a1: str, values: Sequence[Sequence[Any]]) -> int:
        """Write values into the in-memory content.

        Args:
            a1: Where to start.
            values: Rows to write.

        Returns:
            How many cells were written.
        """
        title, start_row, start_column, _, _ = self._parse(a1)
        rows = self.tabs.setdefault(title, [])

        written = 0
        for offset, row in enumerate(values):
            index = start_row - 1 + offset
            while len(rows) <= index:
                rows.append([])
            target = rows[index]
            for column_offset, value in enumerate(row):
                position = start_column + column_offset
                while len(target) <= position:
                    target.append("")
                # None means "leave this cell alone", which is how a row is
                # written around a column the crawler does not manage.
                if value is not None:
                    target[position] = value
                    written += 1

        return written

    def _do_values_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Single-range write."""
        written = self._write(payload["range"], payload["body"].get("values", []))
        return {"updatedCells": written}

    def _do_values_batchUpdate(self, payload: Dict[str, Any]) -> Dict[str, Any]:  # noqa: N802
        """Multi-range write."""
        total = 0
        for item in payload["body"].get("data", []):
            total += self._write(item["range"], item.get("values", []))
        return {"totalUpdatedCells": total}

    def _do_values_append(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Append below existing content."""
        title, _, start_column, _, _ = self._parse(payload["range"])
        rows = self.tabs.setdefault(title, [])
        start = len(rows) + 1
        written = self._write(
            f"'{title}'!{chr(ord('A') + start_column)}{start}", payload["body"].get("values", [])
        )
        return {"updates": {"updatedCells": written}}

    def _do_batchUpdate(self, payload: Dict[str, Any]) -> Dict[str, Any]:  # noqa: N802
        """Structural changes."""
        replies: List[Dict[str, Any]] = []

        for request in payload["body"].get("requests", []):
            for kind, body in request.items():
                self.structural_kinds.append(kind)

                if kind == "addSheet":
                    properties = body.get("properties", {})
                    title = properties.get("title", "")
                    grid = properties.get("gridProperties", {})
                    sheet_id = max(self.sheet_ids.values(), default=-1) + 1

                    self.tabs[title] = []
                    self.sheet_ids[title] = sheet_id
                    self.grid[title] = (grid.get("rowCount", 1000), grid.get("columnCount", 26))
                    self.frozen[title] = grid.get("frozenRowCount", 0)
                    replies.append({"addSheet": {"properties": {"sheetId": sheet_id, "title": title}}})

                elif kind == "updateSheetProperties":
                    properties = body.get("properties", {})
                    sheet_id = properties.get("sheetId")
                    name = next(
                        (title for title, ident in self.sheet_ids.items() if ident == sheet_id), None
                    )
                    if name is None:
                        continue

                    if "title" in properties and properties["title"] != name:
                        new_name = properties["title"]
                        # Preserve insertion order, so the fake's tab order
                        # matches what a real rename does.
                        self.tabs = {
                            (new_name if key == name else key): value
                            for key, value in self.tabs.items()
                        }
                        self.sheet_ids[new_name] = self.sheet_ids.pop(name)
                        self.grid[new_name] = self.grid.pop(name)
                        self.frozen[new_name] = self.frozen.pop(name)
                        name = new_name

                    grid = properties.get("gridProperties", {})
                    if "rowCount" in grid or "columnCount" in grid:
                        rows, columns = self.grid[name]
                        wanted_rows = grid.get("rowCount", rows)
                        wanted_columns = grid.get("columnCount", columns)

                        # Applied verbatim, including downwards. This used to
                        # clamp with max(), which made the fake kinder than the
                        # API it stands in for -- and a simulator that refuses
                        # to reproduce a destructive operation cannot be used to
                        # prove the code never asks for one. `ensure_size` sent
                        # a shrinking request against the real spreadsheet for
                        # months while `test_the_grid_only_grows` passed, because
                        # the clamp here was doing the growing-only that the
                        # client was supposed to do.
                        #
                        # A real shrink also discards the rows past the new
                        # boundary, so that is modelled too: a test asserting
                        # "no data was lost" must be able to observe the loss.
                        self.grid[name] = (wanted_rows, wanted_columns)
                        if wanted_rows < rows:
                            del self.tabs[name][wanted_rows:]
                        if wanted_columns < columns:
                            self.tabs[name] = [
                                row[:wanted_columns] for row in self.tabs[name]
                            ]
                    if "frozenRowCount" in grid:
                        self.frozen[name] = grid["frozenRowCount"]

                    replies.append({})

                elif kind == "repeatCell":
                    sheet_id = body.get("range", {}).get("sheetId")
                    name = next(
                        (title for title, ident in self.sheet_ids.items() if ident == sheet_id), ""
                    )
                    if name:
                        self.formatted.append(name)
                    replies.append({})

                else:
                    replies.append({})

        return {"replies": replies}


class FlakyFakeSheetsService(FakeSheetsService):
    """A fake that rate-limits the first few calls, to exercise the backoff.

    Args:
        failures: How many calls to reject before answering normally.
        status: The status to reject them with.
        retry_after: A ``Retry-After`` value to advertise, if any.
        **kwargs: Passed to :class:`FakeSheetsService`.
    """

    def __init__(
        self,
        failures: int = 2,
        status: int = 429,
        retry_after: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.remaining_failures = failures
        self.status = status
        self.retry_after = retry_after

    def _execute(self, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Reject the first few calls, then behave.

        Args:
            kind: Which operation.
            payload: Its arguments.

        Returns:
            The response, once the failures are exhausted.

        Raises:
            FakeHttpError: While failures remain.
        """
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise FakeHttpError(self.status, "rate limited", self.retry_after)
        return super()._execute(kind, payload)
