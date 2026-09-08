"""Read and write one tab as a table of records, without disturbing the rest.

Every repository in version 3 is built on :class:`TabStore`, and it exists to
make three awkward properties of a spreadsheet-as-database tractable.

**A spreadsheet has no primary key**, so :meth:`TabStore.upsert` is given the
column that acts as one and maintains the index itself. A record whose key is
already present updates that row *in place*, keeping its position — which
matters because the operator's own filters, notes and conditional formatting
are attached to row positions.

**A spreadsheet has no transactions.** A run that dies half-way through a write
leaves the tab partly updated. Every write here is therefore idempotent: keyed
on a stable identity, and re-running converges rather than duplicating. That is
also why nothing is ever inserted in the middle — appended rows go on the end,
so a failed write leaves a short tab rather than a scrambled one.

**A write covers a rectangle.** Writing the crawler's columns as one block would
also overwrite any column the operator added between them. So a write is split
at each foreign column, via :meth:`sheets.schema.ColumnPlan.contiguous_runs`,
and a ``Notes`` column in the middle of a tab is never touched.

    >>> store = TabStore(client, MASTER_COMPANIES)
    >>> store.upsert([{"company_key": "domain:acme.com", "company_name": "Acme"}])
    UpsertResult(inserted=1, updated=0, unchanged=0)
    >>> store.upsert([{"company_key": "domain:acme.com", "company_name": "Acme"}])
    UpsertResult(inserted=0, updated=0, unchanged=1)

The second call issues no API request at all. That is the property the whole
weekly design rests on: a rerun is free, so an interrupted run can simply be
run again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from loguru import logger

from sheets.client import SheetsClient
from sheets.schema import ColumnPlan, TabSpec, a1_range, match_title, plan_columns

__all__ = [
    "Record",
    "TabStore",
    "UpsertResult",
]

#: The first row holding data. Row 1 is the header.
FIRST_DATA_ROW: int = 2

#: Rows added to the grid beyond what is needed, so a growing tab does not
#: need resizing on every single run.
_GRID_HEADROOM: int = 500


@dataclass
class Record:
    """One row, as field names rather than cell positions.

    Attributes:
        values: Field name to value, for the columns the crawler manages.
        row: The one-based sheet row this came from, or ``0`` for a record not
            yet written.
    """

    values: Dict[str, str] = field(default_factory=dict)
    row: int = 0

    def get(self, field_name: str, default: str = "") -> str:
        """Read one field.

        Args:
            field_name: The field.
            default: Returned when the field is absent or blank.

        Returns:
            The value.
        """
        return self.values.get(field_name) or default


@dataclass
class UpsertResult:
    """What an upsert did.

    Attributes:
        inserted: Records appended as new rows.
        updated: Existing rows whose values changed.
        unchanged: Existing rows that already matched, and so were not written.
        skipped: Incoming records with no usable key.
        cells_written: Cells actually written.
        dry_run: Whether anything was sent.
    """

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    cells_written: int = 0
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        """Whether the upsert had anything to write.

        Returns:
            ``True`` when any row was inserted or updated.
        """
        return bool(self.inserted or self.updated)

    def describe(self) -> str:
        """Render as one line, for a log or a report.

        Returns:
            Human-readable summary.
        """
        parts = [
            f"{self.inserted} inserted",
            f"{self.updated} updated",
            f"{self.unchanged} unchanged",
        ]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.dry_run:
            parts.append("(dry run)")
        return ", ".join(parts)


def _clean(value: object) -> str:
    """Coerce a cell or field to a stripped string.

    Args:
        value: Whatever a caller supplied.

    Returns:
        The stripped string form, or ``""`` for ``None``.
    """
    if value is None:
        return ""
    return str(value).strip()


class TabStore:
    """One tab, read and written as records keyed by a stable identity.

    Args:
        client: The Sheets client.
        spec: What the crawler wants this tab to contain.
        title: The live tab's title, when it differs from the specification's.
            Resolved on first use when omitted.
    """

    def __init__(
        self,
        client: SheetsClient,
        spec: TabSpec,
        title: Optional[str] = None,
    ) -> None:
        self._client = client
        self._spec = spec
        self._title = title
        self._plan: Optional[ColumnPlan] = None

    # -- structure -----------------------------------------------------------

    @property
    def spec(self) -> TabSpec:
        """The specification this store is for.

        Returns:
            The specification.
        """
        return self._spec

    @property
    def title(self) -> str:
        """The live tab's title.

        Returns:
            The title as the spreadsheet spells it.

        Raises:
            KeyError: If the tab does not exist. Run ``python -m sheets.init``.
        """
        if self._title is None:
            titles = self._client.tab_titles()
            found = match_title(self._spec, titles)
            if found is None:
                raise KeyError(
                    f"The spreadsheet has no {self._spec.title!r} tab. "
                    "Run:  python -m sheets.init"
                )
            self._title = found
        return self._title

    @property
    def plan(self) -> ColumnPlan:
        """Where each of the crawler's fields lives on the live tab.

        Returns:
            The column plan, read once and cached for the life of this store.
        """
        if self._plan is None:
            header_rows = self._client.read(a1_range(self.title, 1, 0, 1, None))
            headers = header_rows[0] if header_rows else []
            self._plan = plan_columns(self._spec, headers)

            if not self._plan.is_satisfied:
                logger.warning(
                    "Tab {!r} is missing column(s): {}. Run: python -m sheets.init",
                    self.title,
                    ", ".join(self._plan.appended_headers),
                )
        return self._plan

    def refresh(self) -> None:
        """Forget the cached title and column plan, so the next call re-reads."""
        self._title = None
        self._plan = None

    # -- reading -------------------------------------------------------------

    def read(self) -> List[Record]:
        """Read every data row as a record.

        Returns:
            One record per row that holds anything, in sheet order, each
            carrying the row number it came from.
        """
        plan = self.plan
        if not plan.mapping:
            return []

        last_column = max(plan.mapping.values())
        rows = self._client.read(a1_range(self.title, FIRST_DATA_ROW, 0, None, last_column))

        records: List[Record] = []
        for offset, row in enumerate(rows):
            values = {
                field_name: _clean(row[index]) if index < len(row) else ""
                for field_name, index in plan.mapping.items()
            }
            if not any(values.values()):
                # A wholly blank row is spacing, not a record.
                continue
            records.append(Record(values=values, row=FIRST_DATA_ROW + offset))

        return records

    def read_index(self, key_field: Optional[str] = None) -> Dict[str, Record]:
        """Read the tab keyed by its identity column.

        Args:
            key_field: The identity field. Defaults to the specification's.

        Returns:
            Key to record. A row with a blank key is omitted; where two rows
            share a key the first wins, so a duplicate cannot shadow the
            original.

        Raises:
            ValueError: If the tab has no identity column and none was given.
        """
        key = key_field or self._spec.identity_field
        if not key:
            raise ValueError(f"Tab {self._spec.title!r} has no identity column")

        index: Dict[str, Record] = {}
        duplicates = 0

        for record in self.read():
            identity = record.get(key)
            if not identity:
                continue
            if identity in index:
                duplicates += 1
                continue
            index[identity] = record

        if duplicates:
            logger.warning(
                "Tab {!r} has {} row(s) sharing a {} with an earlier row; "
                "the first of each is authoritative",
                self.title,
                duplicates,
                key,
            )

        return index

    def count(self) -> int:
        """How many data rows the tab holds.

        Returns:
            The count, excluding the header.
        """
        return len(self.read())

    # -- writing -------------------------------------------------------------

    def upsert(
        self,
        records: Iterable[Mapping[str, object]],
        key_field: Optional[str] = None,
        manual_fields: FrozenSet[str] = frozenset(),
        insert_only: FrozenSet[str] = frozenset(),
        clearable: FrozenSet[str] = frozenset(),
        blank_overwrites: bool = False,
        dry_run: bool = False,
    ) -> UpsertResult:
        """Insert or update records, keyed on a stable identity.

        Args:
            records: Incoming records, as field name to value.
            key_field: The identity field. Defaults to the specification's.
            manual_fields: Fields the operator maintains. An existing non-blank
                value in one of these is never replaced, even by a non-blank
                incoming value — so a hand-classified Industry survives every
                import.
            insert_only: Fields written when a record is new and never touched
                again. ``First Seen`` is the reason this exists: it is stamped
                with the moment of writing, so updating it would both destroy
                the fact it records and make every rerun look like a change.
            clearable: Fields where a blank incoming value *does* clear a stored
                one, against the default. ``Closed At`` needs this, so that a
                posting which reopens loses the date it closed.
            blank_overwrites: Whether a blank incoming value should clear an
                existing one, for every field. ``False`` by default, which is
                what stops a re-import from a CSV missing a column erasing that
                column for every row.
            dry_run: Work out what would change, and write nothing.

        Returns:
            What was done, or would be done.

        Raises:
            ValueError: If no identity field is available.
        """
        key = key_field or self._spec.identity_field
        if not key:
            raise ValueError(
                f"Tab {self._spec.title!r} has no identity column, so it cannot be "
                "upserted. Use append() instead."
            )

        plan = self.plan
        existing = self.read()
        by_key: Dict[str, Record] = {}
        for record in existing:
            identity = record.get(key)
            if identity and identity not in by_key:
                by_key[identity] = record

        result = UpsertResult(dry_run=dry_run)
        changed_rows: Dict[int, Dict[str, str]] = {}
        appended: List[Dict[str, str]] = []
        seen: Set[str] = set()

        for incoming in records:
            values = {name: _clean(value) for name, value in incoming.items()}
            identity = values.get(key, "")

            if not identity:
                result.skipped += 1
                continue

            # Two incoming records with one key would otherwise append twice.
            if identity in seen:
                result.skipped += 1
                continue
            seen.add(identity)

            current = by_key.get(identity)

            if current is None:
                appended.append({name: values.get(name, "") for name in plan.mapping})
                result.inserted += 1
                continue

            merged = self._merge(
                current.values, values, manual_fields, insert_only, clearable, blank_overwrites
            )

            if merged == current.values:
                result.unchanged += 1
                continue

            changed_rows[current.row] = merged
            result.updated += 1

        if dry_run or not (changed_rows or appended):
            if not dry_run:
                logger.debug("{}: {}", self.title, result.describe())
            return result

        result.cells_written = self._write_rows(changed_rows, appended, len(existing))
        logger.info("{}: {}", self.title, result.describe())
        return result

    @staticmethod
    def _merge(
        current: Mapping[str, str],
        incoming: Mapping[str, str],
        manual_fields: FrozenSet[str],
        insert_only: FrozenSet[str],
        clearable: FrozenSet[str],
        blank_overwrites: bool,
    ) -> Dict[str, str]:
        """Combine an existing row with an incoming record.

        Args:
            current: The row as stored.
            incoming: The record being written.
            manual_fields: Fields where a stored non-blank value always wins.
            insert_only: Fields never updated once the row exists.
            clearable: Fields where a blank incoming value clears a stored one.
            blank_overwrites: Whether that applies to every field.

        Returns:
            The values the row should hold.
        """
        merged = dict(current)

        for name, value in incoming.items():
            if name not in merged:
                continue

            # Written once, at insert. Touching it again would rewrite history.
            if name in insert_only:
                continue

            stored = merged[name]

            # The operator's own classification is not the crawler's to revise.
            if name in manual_fields and stored:
                continue

            if not value and not (blank_overwrites or name in clearable):
                continue

            merged[name] = value

        return merged

    def _write_rows(
        self,
        changed: Mapping[int, Mapping[str, str]],
        appended: Sequence[Mapping[str, str]],
        existing_count: int,
    ) -> int:
        """Write updated rows in place and new rows on the end.

        Args:
            changed: Row number to the values it should hold.
            appended: New records, in the order they should be added.
            existing_count: How many data rows the tab already had.

        Returns:
            How many cells were written.
        """
        plan = self.plan
        runs = plan.contiguous_runs()
        updates: List[Tuple[str, List[List[Any]]]] = []

        # Updates: group consecutive rows so a run of changes costs one range
        # rather than one per row.
        for first_row, block in self._group_rows(sorted(changed)):
            for start_column, fields in runs:
                matrix = [[changed[row].get(name, "") for name in fields] for row in block]
                updates.append(
                    (
                        a1_range(
                            self.title,
                            first_row,
                            start_column,
                            first_row + len(block) - 1,
                            start_column + len(fields) - 1,
                        ),
                        matrix,
                    )
                )

        # Appends: always at the bottom, so an interrupted write leaves a short
        # tab rather than a scrambled one.
        if appended:
            first_row = FIRST_DATA_ROW + existing_count
            self._ensure_room(first_row + len(appended))
            for start_column, fields in runs:
                matrix = [[record.get(name, "") for name in fields] for record in appended]
                updates.append(
                    (
                        a1_range(
                            self.title,
                            first_row,
                            start_column,
                            first_row + len(appended) - 1,
                            start_column + len(fields) - 1,
                        ),
                        matrix,
                    )
                )

        return self._client.batch_write(updates) if updates else 0

    @staticmethod
    def _group_rows(rows: Sequence[int]) -> List[Tuple[int, List[int]]]:
        """Split sorted row numbers into consecutive stretches.

        Args:
            rows: Row numbers, ascending.

        Returns:
            ``(first_row, rows)`` per stretch.
        """
        groups: List[Tuple[int, List[int]]] = []
        for row in rows:
            if groups and row == groups[-1][1][-1] + 1:
                groups[-1][1].append(row)
            else:
                groups.append((row, [row]))
        return groups

    def _ensure_room(self, last_row: int) -> None:
        """Grow the grid if a write would land past its last row.

        Args:
            last_row: The last row that will be written.
        """
        metadata = self._client.metadata()
        for sheet in metadata.get("sheets", []) or []:
            properties = sheet.get("properties", {}) or {}
            if properties.get("title") != self.title:
                continue

            grid = properties.get("gridProperties", {}) or {}
            rows = int(grid.get("rowCount", 0))
            columns = int(grid.get("columnCount", 0))

            if last_row > rows:
                self._client.ensure_size(
                    int(properties.get("sheetId", 0)),
                    last_row + _GRID_HEADROOM,
                    max(columns, self.plan.width),
                )
            return

    def update_rows(
        self,
        changes: Mapping[int, Mapping[str, object]],
        dry_run: bool = False,
    ) -> UpsertResult:
        """Write specific fields into specific rows, addressed by position.

        The escape hatch from keyed writing, and it exists for one situation:
        a row that has no key yet. A company typed in by hand has no
        ``Company Key``, so :meth:`upsert` cannot find it and would append a
        duplicate instead of filling the cell in. Addressing the row by number
        is the only way to give it one.

        Only the named fields are written; every other cell in the row, managed
        or not, is left exactly as it is.

        Args:
            changes: One-based row number to the fields to set in that row.
            dry_run: Work out what would change, and write nothing.

        Returns:
            What was done, or would be done.
        """
        plan = self.plan
        existing = {record.row: record for record in self.read()}

        wanted: Dict[int, Dict[str, str]] = {}
        result = UpsertResult(dry_run=dry_run)

        for row, fields in changes.items():
            current = existing.get(row)
            if current is None:
                result.skipped += 1
                continue

            merged = dict(current.values)
            for name, value in fields.items():
                if name in merged:
                    merged[name] = _clean(value)

            if merged == current.values:
                result.unchanged += 1
                continue

            wanted[row] = merged
            result.updated += 1

        if dry_run or not wanted:
            return result

        result.cells_written = self._write_rows(wanted, [], len(existing))
        logger.info("{}: updated {} row(s) in place", self.title, len(wanted))
        return result

    def append(
        self,
        records: Iterable[Mapping[str, object]],
        dry_run: bool = False,
    ) -> UpsertResult:
        """Add records to the end of the tab, without checking for duplicates.

        For tabs that are a historical log rather than a table of current
        state. A caller that must not repeat itself on a rerun should filter
        the records first — :mod:`sheets.jobs` does exactly that for the weekly
        changes, keyed on the run.

        Args:
            records: Records to add.
            dry_run: Work out what would be added, and write nothing.

        Returns:
            What was done, or would be done.
        """
        plan = self.plan
        rows = [
            {name: _clean(record.get(name, "")) for name in plan.mapping}
            for record in records
        ]
        rows = [row for row in rows if any(row.values())]

        result = UpsertResult(inserted=len(rows), dry_run=dry_run)
        if dry_run or not rows:
            return result

        existing_count = len(self.read())
        result.cells_written = self._write_rows({}, rows, existing_count)
        logger.info("{}: appended {} row(s)", self.title, len(rows))
        return result

    def remove(
        self,
        keys: Iterable[str],
        key_field: Optional[str] = None,
        dry_run: bool = False,
    ) -> int:
        """Take rows out of the tab by identity, closing the gap behind them.

        The tab is *compacted*, not punched through, and that is not a stylistic
        choice. :meth:`read` treats a wholly blank row as spacing and skips it,
        and :meth:`_write_rows` appends at ``FIRST_DATA_ROW + len(read())`` --
        so a blank left in the middle would make the next append land on top of
        a real row. Rewriting the survivors from the first removal downwards and
        blanking the tail keeps the one invariant the rest of this module rests
        on: **blank rows only ever exist as a contiguous tail.**

        Nothing is deleted at the API level. This writes values, exactly as
        :meth:`replace` does, so :class:`sheets.client.DestructiveRequestError`
        is never provoked and a column the operator added beside the crawler's
        own is left untouched on every row that moves.

        Args:
            keys: Identities to remove. Anything not present is ignored, which
                is what makes calling this twice cost one write and then none.
            key_field: The identity field. Defaults to the specification's.
            dry_run: Work out what would go, and write nothing.

        Returns:
            How many rows were removed, or would be.

        Raises:
            ValueError: If the tab has no identity column and none was given.
        """
        key = key_field or self._spec.identity_field
        if not key:
            raise ValueError(
                f"Tab {self._spec.title!r} has no identity column, so rows "
                "cannot be removed by identity."
            )

        unwanted = {str(item) for item in keys if str(item or "").strip()}
        if not unwanted:
            return 0

        existing = self.read()

        # Every row carrying an unwanted key goes, not merely the first. A tab
        # that somehow holds a posting twice must not keep the second copy.
        survivors = [
            record for record in existing if record.get(key) not in unwanted
        ]
        removed = len(existing) - len(survivors)

        if not removed:
            return 0

        if dry_run:
            logger.info("{}: would remove {} row(s)", self.title, removed)
            return removed

        # Delegated on purpose. `replace` already writes only the rows whose
        # content actually changed -- so everything above the first removal
        # costs nothing -- blanks exactly the tail the shorter list leaves, and
        # never grows the grid, because the survivors can only be fewer.
        self.replace([record.values for record in survivors], dry_run=False)
        logger.info("{}: removed {} row(s)", self.title, removed)
        return removed

    def replace(
        self,
        records: Iterable[Mapping[str, object]],
        dry_run: bool = False,
    ) -> UpsertResult:
        """Make the tab's data area exactly these records.

        Rows are overwritten in place and any surplus is blanked — within the
        crawler's own columns only, so a column the operator added is left
        intact even on the rows that are cleared. No row is ever removed from
        the grid; a shorter result leaves blank rows, not missing ones.

        Used for the tabs that are a snapshot of now rather than a history:
        ``CURRENT_JOBS``, ``FAILURES`` and ``DASHBOARD``.

        Args:
            records: What the tab should hold.
            dry_run: Work out what would change, and write nothing.

        Returns:
            What was done, or would be done.
        """
        plan = self.plan
        wanted = [
            {name: _clean(record.get(name, "")) for name in plan.mapping}
            for record in records
        ]

        existing = self.read()
        result = UpsertResult(dry_run=dry_run)

        changed: Dict[int, Dict[str, str]] = {}
        for offset, values in enumerate(wanted):
            row = FIRST_DATA_ROW + offset
            current = existing[offset].values if offset < len(existing) else None

            if current == values:
                result.unchanged += 1
            elif current is None:
                changed[row] = values
                result.inserted += 1
            else:
                changed[row] = values
                result.updated += 1

        # Blank whatever the previous run left below the new content.
        blank = {name: "" for name in plan.mapping}
        for offset in range(len(wanted), len(existing)):
            row = FIRST_DATA_ROW + offset
            if existing[offset].values != blank:
                changed[row] = dict(blank)
                result.updated += 1

        if dry_run or not changed:
            return result

        self._ensure_room(FIRST_DATA_ROW + len(wanted))
        result.cells_written = self._write_rows(changed, [], len(existing))
        logger.info("{}: replaced with {} row(s)", self.title, len(wanted))
        return result
