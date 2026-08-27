"""The schema, and an initialisation that is safe to run twice.

Every statement is ``CREATE ... IF NOT EXISTS``, and each migration is recorded
in ``schema_version`` so a later one can be added without re-running the
earlier. Running :func:`migrate` against a populated database changes nothing
and destroys nothing — a property the tests assert directly, because "run the
initialiser again" is exactly what an operator does when unsure.

Four tables carry the crawler's state:

``companies``
    One row per company key. The sheet's columns, plus a derived ``domain``
    that the rate limiter and the reports both group by.

``crawl_queue``
    One row per company, holding its crawl state, its lease, its attempt count
    and when it may next be tried. This is the durable replacement for the
    JSON checkpoint: a killed process resumes from it rather than restarting.

``discovery_queue``
    The same shape for board discovery, deliberately separate. Discovery is
    expensive, operator-approved and occasional; crawling is cheap and weekly.
    One queue for both would mean either running discovery every week or
    dropping its state.

``jobs``
    One row per job identity, keyed on the uid :mod:`crawler.identity` derives.
    Storage does not invent a second notion of identity — the primary key *is*
    the crawler's rule, which is what makes the database a real defence against
    duplicates rather than a second opinion about them.

``crawl_attempts``
    Append-only forensics: every attempt, its outcome, its HTTP status, whether
    a browser was used. Not needed to run, needed to explain.
"""

from __future__ import annotations

from typing import Final, List, Tuple

from loguru import logger

from store.database import Database

__all__ = ["CURRENT_VERSION", "MIGRATIONS", "migrate"]

#: Schema version this code expects.
CURRENT_VERSION: Final[int] = 1

_VERSION_TABLE: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
)
"""

_MIGRATION_1: Final[Tuple[str, ...]] = (
    # -- companies ---------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS companies (
        company_key   TEXT PRIMARY KEY,
        company_name  TEXT NOT NULL DEFAULT '',
        website       TEXT NOT NULL DEFAULT '',
        career_url    TEXT NOT NULL DEFAULT '',
        it_link       TEXT NOT NULL DEFAULT '',
        platform      TEXT NOT NULL DEFAULT '',
        domain        TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'active',
        sheet_row     INTEGER NOT NULL DEFAULT 0,
        first_seen    TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies (domain)",
    "CREATE INDEX IF NOT EXISTS idx_companies_platform ON companies (platform)",
    "CREATE INDEX IF NOT EXISTS idx_companies_status ON companies (status)",

    # -- the crawl queue ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS crawl_queue (
        company_key     TEXT PRIMARY KEY
                        REFERENCES companies (company_key) ON DELETE CASCADE,
        state           TEXT NOT NULL DEFAULT 'pending',
        attempts        INTEGER NOT NULL DEFAULT 0,
        owner           TEXT NOT NULL DEFAULT '',
        claimed_at      REAL NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        last_attempt_at TEXT NOT NULL DEFAULT '',
        last_success_at TEXT NOT NULL DEFAULT '',
        last_reason     TEXT NOT NULL DEFAULT '',
        jobs_found      INTEGER NOT NULL DEFAULT 0,
        run_id          TEXT NOT NULL DEFAULT '',
        updated_at      TEXT NOT NULL DEFAULT ''
    )
    """,
    # The claim query filters on state and next_attempt_at together, so they
    # are indexed together rather than separately.
    "CREATE INDEX IF NOT EXISTS idx_queue_state ON crawl_queue (state)",
    """
    CREATE INDEX IF NOT EXISTS idx_queue_next_attempt
        ON crawl_queue (state, next_attempt_at)
    """,
    "CREATE INDEX IF NOT EXISTS idx_queue_claimed ON crawl_queue (state, claimed_at)",

    # -- the discovery queue, deliberately its own table -------------------
    """
    CREATE TABLE IF NOT EXISTS discovery_queue (
        company_key     TEXT PRIMARY KEY
                        REFERENCES companies (company_key) ON DELETE CASCADE,
        state           TEXT NOT NULL DEFAULT 'pending',
        attempts        INTEGER NOT NULL DEFAULT 0,
        owner           TEXT NOT NULL DEFAULT '',
        claimed_at      REAL NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        found_url       TEXT NOT NULL DEFAULT '',
        found_platform  TEXT NOT NULL DEFAULT '',
        reason          TEXT NOT NULL DEFAULT '',
        browser_used    INTEGER NOT NULL DEFAULT 0,
        updated_at      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_discovery_state ON discovery_queue (state)",
    """
    CREATE INDEX IF NOT EXISTS idx_discovery_next_attempt
        ON discovery_queue (state, next_attempt_at)
    """,

    # -- jobs ---------------------------------------------------------------
    # job_key is crawler.identity's uid. Making it the primary key is what
    # makes a repeated crawl an update rather than a duplicate.
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_key      TEXT PRIMARY KEY,
        company_key  TEXT NOT NULL
                     REFERENCES companies (company_key) ON DELETE CASCADE,
        job_title    TEXT NOT NULL DEFAULT '',
        job_url      TEXT NOT NULL DEFAULT '',
        url_key      TEXT NOT NULL DEFAULT '',
        content_key  TEXT NOT NULL DEFAULT '',
        location     TEXT NOT NULL DEFAULT '',
        country      TEXT NOT NULL DEFAULT '',
        department   TEXT NOT NULL DEFAULT '',
        platform     TEXT NOT NULL DEFAULT '',
        job_id       TEXT NOT NULL DEFAULT '',
        identity_basis TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'active',
        is_tech      INTEGER NOT NULL DEFAULT 0,
        first_seen   TEXT NOT NULL DEFAULT '',
        last_seen    TEXT NOT NULL DEFAULT '',
        closed_at    TEXT NOT NULL DEFAULT '',
        first_run_id TEXT NOT NULL DEFAULT '',
        last_run_id  TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs (company_key)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_url_key ON jobs (url_key)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_content_key ON jobs (content_key)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_company_status ON jobs (company_key, status)",

    # -- attempts, append-only ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS crawl_attempts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        company_key  TEXT NOT NULL,
        run_id       TEXT NOT NULL DEFAULT '',
        outcome      TEXT NOT NULL DEFAULT '',
        reason       TEXT NOT NULL DEFAULT '',
        http_status  INTEGER NOT NULL DEFAULT 0,
        browser_used INTEGER NOT NULL DEFAULT 0,
        seconds      REAL NOT NULL DEFAULT 0,
        jobs_found   INTEGER NOT NULL DEFAULT 0,
        attempted_at TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempts_company ON crawl_attempts (company_key)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_run ON crawl_attempts (run_id)",
)

#: Version to the statements that produce it.
MIGRATIONS: Final[List[Tuple[int, Tuple[str, ...]]]] = [(1, _MIGRATION_1)]


def migrate(database: Database) -> int:
    """Bring a database up to :data:`CURRENT_VERSION`.

    Safe to call on an empty database, on a current one, and on one part-way
    through. Nothing is dropped, altered destructively, or duplicated.

    Args:
        database: The database to initialise.

    Returns:
        The version now applied.
    """
    from utils.clock import iso

    database.execute(_VERSION_TABLE)

    applied = {
        int(row["version"])
        for row in database.query("SELECT version FROM schema_version")
    }

    for version, statements in MIGRATIONS:
        if version in applied:
            continue

        with database.transaction():
            for statement in statements:
                database.execute(statement)
            database.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, iso()),
            )
        logger.info("Database migrated to version {}", version)

    return CURRENT_VERSION
