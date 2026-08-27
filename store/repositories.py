"""Repositories: the vocabulary the crawler uses instead of SQL.

Crawler code asks for companies and records jobs. It does not build queries,
does not know the parameter style, and does not know which engine is underneath
— which is the property that lets a PostgreSQL backend be added later by
reimplementing this file and :mod:`store.database`, and nothing else.

Two rules are enforced here rather than left to callers, because both are ways
to silently lose curated data:

**A blank never clears a stored value.** A sync that omits a field, or a sheet
whose column is empty this week, must not wipe an ``IT Link`` an operator
entered. Only an explicit, non-empty value replaces one.

**Job identity is the crawler's, not the database's.** ``job_key`` is exactly
the uid :func:`crawler.identity.job_identity` derives. The primary key *is* the
dedup rule, so a repeated crawl updates a row rather than inserting a second.
"""

from __future__ import annotations

from typing import Any, Dict, Final, Iterable, Iterator, List, Optional, Sequence
from urllib.parse import urlsplit

from store.database import Database

__all__ = ["CompanyRepository", "JobRepository"]

#: Fields a sync may set on an existing company. ``company_key`` is absent
#: deliberately: it is the identity, not a value, and rewriting it would
#: orphan every job and queue row that references it.
_COMPANY_FIELDS: Final[tuple] = (
    "company_name", "website", "career_url", "it_link",
    "platform", "domain", "status", "sheet_row",
)


def _domain_of(*urls: str) -> str:
    """The registrable-ish host behind the first usable URL.

    Used to group work by site for the rate limiter and the reports, so it
    wants the host, not a precise public-suffix answer.

    Args:
        *urls: Candidate URLs, best first.

    Returns:
        The lowercased hostname without ``www.``, or ``""``.
    """
    for url in urls:
        raw = str(url or "").strip()
        if not raw:
            continue
        if "://" not in raw:
            raw = f"https://{raw}"
        try:
            host = (urlsplit(raw).hostname or "").lower()
        except ValueError:
            continue
        if host:
            return host[4:] if host.startswith("www.") else host
    return ""


class CompanyRepository:
    """The company list, as the crawler needs it.

    Args:
        database: The store to read and write.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_many(self, records: Iterable[Mapping]) -> int:
        """Insert or update companies, keyed on ``company_key``.

        Args:
            records: Company records. Each needs a ``company_key``; every other
                field is optional and a blank one leaves the stored value
                alone.

        Returns:
            How many rows were written.
        """
        from utils.clock import iso

        stamp = iso()
        written = 0

        with self.database.transaction():
            for record in records:
                key = str(record.get("company_key") or "").strip()
                if not key:
                    continue

                values = {
                    name: str(record.get(name, "") or "").strip()
                    for name in _COMPANY_FIELDS
                    if name not in ("domain", "sheet_row")
                }
                values["domain"] = _domain_of(
                    record.get("website", ""),
                    record.get("career_url", ""),
                    record.get("it_link", ""),
                )
                values["sheet_row"] = int(record.get("sheet_row") or 0)
                values.setdefault("status", "")

                existing = self.get(key)
                if existing is None:
                    self.database.execute(
                        """
                        INSERT INTO companies
                            (company_key, company_name, website, career_url, it_link,
                             platform, domain, status, sheet_row, first_seen, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            values.get("company_name", ""),
                            values.get("website", ""),
                            values.get("career_url", ""),
                            values.get("it_link", ""),
                            values.get("platform", ""),
                            values["domain"],
                            values.get("status") or "active",
                            values["sheet_row"],
                            stamp,
                            stamp,
                        ),
                    )
                    written += 1
                    continue

                # A blank incoming value never clears a stored one.
                merged = {
                    name: (values.get(name) or existing.get(name, ""))
                    for name in _COMPANY_FIELDS
                    if name != "sheet_row"
                }
                merged["sheet_row"] = values["sheet_row"] or existing.get("sheet_row", 0)

                if all(str(merged[name]) == str(existing.get(name, "")) for name in merged):
                    continue

                self.database.execute(
                    """
                    UPDATE companies
                       SET company_name = ?, website = ?, career_url = ?, it_link = ?,
                           platform = ?, domain = ?, status = ?, sheet_row = ?,
                           updated_at = ?
                     WHERE company_key = ?
                    """,
                    (
                        merged["company_name"], merged["website"], merged["career_url"],
                        merged["it_link"], merged["platform"], merged["domain"],
                        merged["status"], merged["sheet_row"], stamp, key,
                    ),
                )
                written += 1

        return written

    def get(self, company_key: str) -> Optional[Dict[str, Any]]:
        """One company.

        Args:
            company_key: Its key.

        Returns:
            The row, or ``None``.
        """
        return self.database.one(
            "SELECT * FROM companies WHERE company_key = ?", (company_key,)
        )

    def count(self, status: str = "") -> int:
        """How many companies are stored.

        Args:
            status: Restrict to one lifecycle status, or ``""`` for all.

        Returns:
            The count.
        """
        if status:
            row = self.database.one(
                "SELECT COUNT(*) AS n FROM companies WHERE status = ?", (status,)
            )
        else:
            row = self.database.one("SELECT COUNT(*) AS n FROM companies")
        return int(row["n"]) if row else 0

    def stream(self, batch_size: int = 500) -> Iterator[Dict[str, Any]]:
        """Every company, without loading them all at once.

        Args:
            batch_size: Rows per round trip.

        Yields:
            One company at a time.
        """
        yield from self.database.stream(
            "SELECT * FROM companies ORDER BY company_key", batch_size=batch_size
        )

    def needing_a_board(self, batch_size: int = 500) -> Iterator[Dict[str, Any]]:
        """Companies whose ``it_link`` is empty.

        Args:
            batch_size: Rows per round trip.

        Yields:
            One company at a time.
        """
        yield from self.database.stream(
            "SELECT * FROM companies WHERE it_link = '' ORDER BY company_key",
            batch_size=batch_size,
        )

    def set_board(self, company_key: str, it_link: str, platform: str) -> bool:
        """Record a discovered board, refusing to overwrite a stored one.

        Args:
            company_key: The company.
            it_link: The board URL.
            platform: The vendor label.

        Returns:
            ``True`` when the row was written, ``False`` when it already held a
            board and was therefore left alone.
        """
        from utils.clock import iso

        existing = self.get(company_key)
        if existing is None or (existing.get("it_link") or "").strip():
            return False

        self.database.execute(
            "UPDATE companies SET it_link = ?, platform = ?, updated_at = ? "
            "WHERE company_key = ? AND it_link = ''",
            (it_link, platform, iso(), company_key),
        )
        return True


class JobRepository:
    """Every posting the crawler has ever seen.

    Args:
        database: The store to read and write.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def record_many(
        self,
        postings: Iterable[Mapping],
        run_id: str = "",
    ) -> Dict[str, int]:
        """Store postings, updating rather than duplicating what is known.

        Identity comes from :func:`crawler.identity.job_identity`, so the same
        posting seen twice — on two pages, in two runs, or through a URL
        carrying tracking parameters — collapses onto one row.

        A record that already carries a ``job_key`` keeps it. Observations from
        :mod:`crawler.observations` do, and theirs is derived from the board the
        crawl actually used rather than from the company's stored careers URL —
        two things that disagree exactly when discovery found a better board.
        Re-deriving would give one posting two primary keys.

        Args:
            postings: Records carrying at least ``company_key``, ``job_title``
                and ``job_url``. A ``job_key`` — with its ``url_key``,
                ``content_key`` and ``identity_basis`` — is honoured when
                present and derived when not.
            run_id: The run these were seen on.

        Returns:
            ``{"inserted": n, "updated": n, "duplicates": n}``.
        """
        from crawler.identity import JobIdentity, job_identity
        from utils.clock import iso

        stamp = iso()
        counts = {"inserted": 0, "updated": 0, "duplicates": 0}
        seen: set = set()

        # job_identity derives the company scope from name, website and
        # careers URL -- exactly as crawler.observations calls it. Passing the
        # company_key instead would produce a different uid for the same
        # posting, and the database's job_key would stop matching the one in
        # JOB_HISTORY. One lookup per company per batch keeps them identical.
        companies: Dict[str, Dict[str, Any]] = {}

        def scope(company_key: str) -> Dict[str, Any]:
            """The name, website and careers URL identity is derived from."""
            if company_key not in companies:
                companies[company_key] = (
                    CompanyRepository(self.database).get(company_key) or {}
                )
            return companies[company_key]

        with self.database.transaction():
            for posting in postings:
                company_key = str(posting.get("company_key") or "").strip()
                title = str(posting.get("job_title") or "").strip()
                url = str(posting.get("job_url") or "").strip()
                if not company_key or not title or not url:
                    continue

                supplied = str(posting.get("job_key") or "").strip()
                if supplied:
                    # The crawler has already decided what this posting is, and
                    # it decided using the board it *actually crawled*. Deriving
                    # a second uid here from the company's stored careers URL
                    # produces a different answer precisely when discovery found
                    # a better board than the sheet holds -- which is when it
                    # matters -- and the same posting would then carry one key
                    # in JOB_HISTORY and another here. Storage records identity;
                    # it does not hold a second opinion about it.
                    identity = JobIdentity(
                        job_uid=supplied,
                        basis=str(posting.get("identity_basis") or ""),
                        company_key=company_key,
                        job_id=str(posting.get("job_id") or ""),
                        url_key=str(posting.get("url_key") or ""),
                        content_key=str(posting.get("content_key") or ""),
                    )
                else:
                    # No uid supplied, so derive one exactly as before. The
                    # company scope comes from name, website and careers URL --
                    # as crawler.observations calls it -- rather than from the
                    # company_key, which would produce a different uid again.
                    # One lookup per company per batch keeps them identical.
                    company = scope(company_key)
                    identity = job_identity(
                        company_name=str(company.get("company_name") or company_key),
                        job_url=url,
                        job_title=title,
                        location=str(posting.get("location") or ""),
                        platform=str(posting.get("platform") or ""),
                        job_id=str(posting.get("job_id") or ""),
                        website=str(company.get("website") or ""),
                        career_url=str(company.get("career_url") or ""),
                    )

                if identity.job_uid in seen:
                    counts["duplicates"] += 1
                    continue
                seen.add(identity.job_uid)

                existing = self.database.one(
                    "SELECT job_key FROM jobs WHERE job_key = ?", (identity.job_uid,)
                )

                if existing is None:
                    self.database.execute(
                        """
                        INSERT INTO jobs
                            (job_key, company_key, job_title, job_url, url_key,
                             content_key, location, country, department, platform,
                             job_id, identity_basis, status, is_tech,
                             first_seen, last_seen, first_run_id, last_run_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                        """,
                        (
                            identity.job_uid, company_key, title, url,
                            identity.url_key, identity.content_key,
                            str(posting.get("location") or ""),
                            str(posting.get("country") or ""),
                            str(posting.get("department") or ""),
                            str(posting.get("platform") or ""),
                            str(posting.get("job_id") or ""),
                            identity.basis,
                            1 if posting.get("is_tech") else 0,
                            stamp, stamp, run_id, run_id,
                        ),
                    )
                    counts["inserted"] += 1
                    continue

                # Seen before: refresh what can change, keep what cannot.
                # first_seen and first_run_id are history and are never revised.
                self.database.execute(
                    """
                    UPDATE jobs
                       SET job_title = ?, job_url = ?, location = ?, country = ?,
                           department = ?, platform = ?, status = 'active',
                           closed_at = '', last_seen = ?, last_run_id = ?
                     WHERE job_key = ?
                    """,
                    (
                        title, url,
                        str(posting.get("location") or ""),
                        str(posting.get("country") or ""),
                        str(posting.get("department") or ""),
                        str(posting.get("platform") or ""),
                        stamp, run_id, identity.job_uid,
                    ),
                )
                counts["updated"] += 1

        return counts

    def close_missing(self, company_key: str, seen_keys: Sequence[str], run_id: str = "") -> int:
        """Close this company's active jobs that were not seen this time.

        Scoped to one company on purpose. A caller must only invoke it for a
        company it actually read — the same rule
        :mod:`crawler.weekly_diff` applies, for the same reason: a blocked
        crawl proves nothing about what the board still advertises.

        Args:
            company_key: The company just read.
            seen_keys: Job uids observed on it.
            run_id: The run doing the closing.

        Returns:
            How many jobs were closed.
        """
        from utils.clock import iso

        keep = set(seen_keys)
        rows = self.database.query(
            "SELECT job_key FROM jobs WHERE company_key = ? AND status = 'active'",
            (company_key,),
        )
        gone = [row["job_key"] for row in rows if row["job_key"] not in keep]
        if not gone:
            return 0

        stamp = iso()
        self.database.execute_many(
            "UPDATE jobs SET status = 'closed', closed_at = ?, last_run_id = ? "
            "WHERE job_key = ?",
            [(stamp, run_id, key) for key in gone],
        )
        return len(gone)

    def count(self, status: str = "") -> int:
        """How many postings are stored.

        Args:
            status: Restrict to ``active`` or ``closed``, or ``""`` for all.

        Returns:
            The count.
        """
        if status:
            row = self.database.one(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = ?", (status,)
            )
        else:
            row = self.database.one("SELECT COUNT(*) AS n FROM jobs")
        return int(row["n"]) if row else 0

    def all(self) -> List[Dict[str, Any]]:
        """Every posting. For tests and small reports only.

        Returns:
            The rows.
        """
        return self.database.query("SELECT * FROM jobs ORDER BY job_key")

    def stream(self, status: str = "", batch_size: int = 500) -> Iterator[Dict[str, Any]]:
        """Postings, without loading them all.

        Args:
            status: Restrict to one status, or ``""`` for all.
            batch_size: Rows per round trip.

        Yields:
            One posting at a time.
        """
        if status:
            yield from self.database.stream(
                "SELECT * FROM jobs WHERE status = ? ORDER BY company_key",
                (status,), batch_size=batch_size,
            )
        else:
            yield from self.database.stream(
                "SELECT * FROM jobs ORDER BY company_key", batch_size=batch_size
            )


# Imported late so the module's public names read first.
from typing import Mapping  # noqa: E402
