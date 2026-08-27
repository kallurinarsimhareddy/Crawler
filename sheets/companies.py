"""The master company list, held in ``MASTER_COMPANIES``.

Importing the version 2 CSV into the sheet is the one operation that has to be
safe to repeat forever, because it is how the list is maintained: the operator
adds rows to the CSV, re-imports, and expects the sheet to gain those companies
and nothing else to move::

    >>> from sheets.companies import CompanyRepository
    >>> companies = CompanyRepository(client)
    >>> companies.import_csv("input/companies.csv")
    UpsertResult(inserted=8275, updated=0, unchanged=0)
    >>> companies.import_csv("input/companies.csv")
    UpsertResult(inserted=0, updated=0, unchanged=8275)

The second import writes nothing at all.

**Identity is a domain, not a name.** :func:`utils.names.company_key` derives
``domain:acme.com`` from the website or careers URL, falling back to a name slug
only when neither offers one. So ``"Acme Corp."`` in one import and
``"Acme Corporation"`` in the next are one row, and the master list does not
grow a near-duplicate every time somebody retypes a name. A careers URL on a
shared ATS host — ``boards.greenhouse.io/acme`` — yields no domain on purpose,
because that host belongs to the vendor and would merge every tenant on it.

**Two protections for hand-edited cells.** A blank incoming value never clears a
stored one, so re-importing a CSV that lacks the ``Website`` column cannot erase
eight thousand websites. And ``Department`` and ``Industry`` are treated as the
operator's: once either holds a value, no import overwrites it, because those
are the columns a human curates and the CSV usually cannot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sheets.client import SheetsClient
from sheets.schema import MASTER_COMPANIES
from sheets.storage import Record, TabStore, UpsertResult
from utils.clock import iso
from utils.names import company_key as derive_company_key
from utils.urlkey import canonical_url

__all__ = ["MANUAL_FIELDS", "CompanyRepository", "company_record"]

#: Columns the operator curates. Once one of these holds a value, no import
#: replaces it — a hand-assigned Industry is worth more than whatever a CSV
#: export happened to contain.
MANUAL_FIELDS: FrozenSet[str] = frozenset({"department", "industry"})

#: Columns written once, when a company first appears, and never again.
#: ``first_seen`` records the moment of writing, so re-stamping it on every
#: import would both destroy the fact it exists to record and make an otherwise
#: unchanged re-import look like eight thousand modified rows.
INSERT_ONLY_FIELDS: FrozenSet[str] = frozenset({"first_seen"})

#: Where a company came from.
SOURCE_CSV: str = "csv-import"
SOURCE_DISCOVERY: str = "discovery"

#: A company's lifecycle state.
STATUS_ACTIVE: str = "active"


def _tidy_url(value: str) -> str:
    """Normalise a URL for storage, leaving anything else alone.

    Args:
        value: A URL, a bare hostname, or filler such as ``"N/A"``.

    Returns:
        The canonical URL, or the input stripped when it is not one. Filler is
        returned as-is rather than guessed at: the crawler's seed selection
        already knows how to skip it, and rewriting it here would hide from the
        operator that their sheet has a bad cell.
    """
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate and "." not in candidate:
        return candidate
    return canonical_url(candidate if "://" in candidate else f"https://{candidate}")


def company_record(
    source_row: Mapping[str, object],
    source: str = SOURCE_CSV,
    first_seen: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Turn an input row into a ``MASTER_COMPANIES`` record.

    Args:
        source_row: A row from :func:`crawler.csv_reader.read_companies`, or
            anything with the same keys. ``company``/``company_name``,
            ``website``, ``career_url`` and ``it_link`` are recognised.
        source: What to record as the company's origin.
        first_seen: Timestamp for a company being added now. Defaults to now.

    Returns:
        The record, or ``None`` when the row identifies no company at all.
    """
    name = str(
        source_row.get("company") or source_row.get("company_name") or ""
    ).strip()
    if not name:
        return None

    website = _tidy_url(str(source_row.get("website") or ""))
    career_url = _tidy_url(str(source_row.get("career_url") or ""))
    it_link = _tidy_url(str(source_row.get("it_link") or ""))

    key = derive_company_key(name, website, career_url or it_link)
    if not key:
        return None

    return {
        "company_key": key,
        "company_name": name,
        "website": website,
        "career_url": career_url,
        "it_link": it_link,
        "platform": str(source_row.get("platform") or "").strip(),
        "department": str(source_row.get("department") or "").strip(),
        "country": str(source_row.get("country") or "").strip(),
        "location": str(source_row.get("location") or "").strip(),
        "industry": str(source_row.get("industry") or "").strip(),
        "status": str(source_row.get("status") or STATUS_ACTIVE).strip(),
        "source": source,
        "first_seen": first_seen or iso(),
    }


class CompanyRepository:
    """Reads and writes ``MASTER_COMPANIES``.

    Args:
        client: The Sheets client.
        title: The live tab's title, when it differs from the default.
    """

    def __init__(self, client: SheetsClient, title: Optional[str] = None) -> None:
        self.store = TabStore(client, MASTER_COMPANIES, title)

    # -- reading -------------------------------------------------------------

    def all(self) -> List[Record]:
        """Every company on the list.

        Returns:
            The records, in sheet order.
        """
        return self.store.read()

    def keys(self) -> set:
        """Every company key on the list.

        Used to deduplicate discovered companies against the master list.

        Returns:
            The keys.
        """
        return set(self.store.read_index("company_key"))

    def count(self) -> int:
        """How many companies are on the list.

        Returns:
            The count.
        """
        return self.store.count()

    def backfill_keys(self, dry_run: bool = False) -> Tuple[UpsertResult, int]:
        """Give every row a ``Company Key``, deriving one where it is missing.

        A company typed straight into the sheet has no key, and the key is what
        every other tab joins on: without one, an upsert cannot find the row and
        appends a duplicate beside it instead. So the first thing a run does is
        fill the blanks in, addressing each row by position because there is no
        key yet to address it by.

        The key is derived exactly as an import would derive it — from the
        website, else the careers URL, else the name — so a row typed by hand and
        the same company imported from a CSV land on one identity.

        Args:
            dry_run: Work out what would change, and write nothing.

        Returns:
            ``(result, duplicates)`` where ``duplicates`` counts rows whose
            derived key another row already holds. Those are genuinely the same
            company entered twice; they are keyed identically and reported, and
            nothing is deleted.
        """
        changes: Dict[int, Dict[str, str]] = {}
        seen: Dict[str, int] = {}
        duplicates = 0

        for record in self.store.read():
            existing = record.get("company_key")
            derived = existing or derive_company_key(
                record.get("company_name"),
                record.get("website"),
                record.get("career_url") or record.get("it_link"),
            )

            if not derived:
                continue

            if derived in seen:
                duplicates += 1
            else:
                seen[derived] = record.row

            if not existing:
                changes[record.row] = {"company_key": derived}

        if duplicates:
            logger.warning(
                "{} row(s) name a company another row already names; "
                "they share a Company Key and will be crawled once",
                duplicates,
            )

        if not changes:
            return UpsertResult(dry_run=dry_run), duplicates

        logger.info("Filling in Company Key for {} hand-added row(s)", len(changes))
        return self.store.update_rows(changes, dry_run=dry_run), duplicates

    def prepare_roster(self, dry_run: bool = False) -> Tuple[List[Dict[str, str]], int]:
        """Read the list once, key the unkeyed rows, and return what to crawl.

        Backfilling and reading the roster both need every row, and the Sheets
        read quota is sixty calls a minute, so they share one read rather than
        taking one each.

        Args:
            dry_run: Derive the keys but write none of them back.

        Returns:
            ``(records, duplicates)`` — one record per distinct company, and how
            many rows named a company another row already named.
        """
        rows = self.store.read()

        changes: Dict[int, Dict[str, str]] = {}
        records: List[Dict[str, str]] = []
        seen: set = set()
        duplicates = 0

        for company in rows:
            key = company.get("company_key") or derive_company_key(
                company.get("company_name"),
                company.get("website"),
                company.get("career_url") or company.get("it_link"),
            )
            if not key:
                continue

            if not company.get("company_key"):
                changes[company.row] = {"company_key": key}

            if key in seen:
                duplicates += 1
                continue
            seen.add(key)

            if company.get("status", STATUS_ACTIVE).lower() not in ("", STATUS_ACTIVE):
                continue

            records.append(
                {
                    "company": company.get("company_name"),
                    "website": company.get("website"),
                    "career_url": company.get("career_url"),
                    "it_link": company.get("it_link"),
                    "company_key": key,
                }
            )

        if duplicates:
            logger.warning(
                "{} row(s) name a company another row already names; each is crawled once",
                duplicates,
            )

        if changes and not dry_run:
            logger.info("Filling in Company Key for {} hand-added row(s)", len(changes))
            self.store.update_rows(changes, dry_run=False)

        return records, duplicates

    def crawl_records(self) -> List[Dict[str, str]]:
        """Project the list onto the shape the version 2 engine consumes.

        The engine takes ``{"company", "website", "career_url", "it_link"}``
        dictionaries, exactly as :func:`crawler.csv_reader.read_companies`
        produces them. Projecting here means the existing crawler runs against
        the sheet without knowing the sheet exists.

        A row whose ``Company Key`` is blank -- a company typed in by hand --
        has one derived on the spot, so it is crawlable before
        :meth:`backfill_keys` has ever run.

        Two rows naming one company yield one record. The reference sheet's
        109 hand-added rows cover 63 companies; crawling the repeats would
        multiply the work and produce identical postings, which the observation
        layer would then deduplicate anyway.

        Returns:
            One record per distinct active company, in sheet order.
        """
        records: List[Dict[str, str]] = []
        seen: set = set()

        for company in self.all():
            if company.get("status", STATUS_ACTIVE).lower() not in ("", STATUS_ACTIVE):
                continue

            key = company.get("company_key") or derive_company_key(
                company.get("company_name"),
                company.get("website"),
                company.get("career_url") or company.get("it_link"),
            )

            if not key or key in seen:
                continue
            seen.add(key)

            records.append(
                {
                    "company": company.get("company_name"),
                    "website": company.get("website"),
                    "career_url": company.get("career_url"),
                    "it_link": company.get("it_link"),
                    # Carried alongside so a caller can attribute results back
                    # to the row they came from without re-deriving the key.
                    "company_key": key,
                }
            )

        return records

    # -- writing -------------------------------------------------------------

    def upsert(
        self,
        records: Iterable[Mapping[str, object]],
        dry_run: bool = False,
    ) -> UpsertResult:
        """Insert or update companies, keyed on their company key.

        Args:
            records: Company records, as :func:`company_record` produces.
            dry_run: Work out what would change, and write nothing.

        Returns:
            What was done, or would be done.
        """
        return self.store.upsert(
            records,
            key_field="company_key",
            manual_fields=MANUAL_FIELDS,
            insert_only=INSERT_ONLY_FIELDS,
            dry_run=dry_run,
        )

    def import_rows(
        self,
        rows: Sequence[Mapping[str, object]],
        source: str = SOURCE_CSV,
        dry_run: bool = False,
    ) -> Tuple[UpsertResult, int]:
        """Import company rows, normalising and deduplicating them first.

        Two rows that resolve to one company — the same firm listed under two
        spellings, or with and without ``www.`` — are collapsed before the
        write, so the sheet never sees the duplicate at all.

        Args:
            rows: Input rows.
            source: What to record as their origin.
            dry_run: Work out what would change, and write nothing.

        Returns:
            ``(result, collapsed)`` where ``collapsed`` counts input rows that
            merged into an earlier one.
        """
        stamp = iso()
        prepared: Dict[str, Dict[str, str]] = {}
        unusable = 0
        collapsed = 0

        for row in rows:
            record = company_record(row, source=source, first_seen=stamp)
            if record is None:
                unusable += 1
                continue

            key = record["company_key"]
            if key in prepared:
                collapsed += 1
                # Keep the fuller of the two: a later row may carry the careers
                # URL the earlier one lacked.
                for name, value in record.items():
                    if value and not prepared[key].get(name):
                        prepared[key][name] = value
                continue

            prepared[key] = record

        if unusable:
            logger.warning("{} input row(s) identified no company and were skipped", unusable)
        if collapsed:
            logger.info("{} input row(s) collapsed onto a company already in the batch", collapsed)

        result = self.upsert(prepared.values(), dry_run=dry_run)
        return result, collapsed

    def import_csv(
        self,
        csv_path: Path | str,
        dry_run: bool = False,
    ) -> Tuple[UpsertResult, int, str]:
        """Import the version 2 company CSV.

        Reading goes through :func:`crawler.csv_reader.read_companies_with_encoding`,
        so a sheet that is Windows-1252 rather than UTF-8 — the recurring
        ``0xA0`` problem — is recovered rather than fatal, and the encoding
        actually used is reported.

        Args:
            csv_path: The CSV to read.
            dry_run: Work out what would change, and write nothing.

        Returns:
            ``(result, collapsed, encoding)``.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If it is not parsable, or lacks a required column.
        """
        from crawler.csv_reader import read_companies_with_encoding

        rows, decoded = read_companies_with_encoding(csv_path)
        logger.info("Read {} row(s) from {} as {}", len(rows), csv_path, decoded.encoding)

        result, collapsed = self.import_rows(rows, source=SOURCE_CSV, dry_run=dry_run)
        return result, collapsed, decoded.encoding

    def record_crawl(
        self,
        outcomes: Mapping[str, Mapping[str, object]],
        dry_run: bool = False,
    ) -> UpsertResult:
        """Note what a crawl learned about each company.

        Args:
            outcomes: Company key to the fields to refresh — any of
                ``platform``, ``career_url``, ``last_outcome``, ``active_jobs``.
                ``last_checked`` is stamped automatically.
            dry_run: Work out what would change, and write nothing.

        Returns:
            What was done, or would be done.
        """
        stamp = iso()
        records = [
            {
                "company_key": key,
                "last_checked": stamp,
                **{name: value for name, value in fields.items()},
            }
            for key, fields in outcomes.items()
        ]

        # last_checked and last_outcome are the crawler's own columns, so a
        # blank there really does mean "clear it" -- a company crawled with no
        # outcome should not keep last week's.
        return self.store.upsert(
            records,
            key_field="company_key",
            manual_fields=MANUAL_FIELDS,
            insert_only=INSERT_ONLY_FIELDS,
            dry_run=dry_run,
        )
