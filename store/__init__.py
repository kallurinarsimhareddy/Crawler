"""Durable local storage for the crawler.

Google Sheets stays the human-facing configuration layer: an operator curates
companies, board URLs and platform labels there, and reads the results there.
What it is not is an operational datastore. A sheet has a ten-million-cell
ceiling, a sixty-reads-per-minute quota, no transactions and no way to claim a
row, and at twelve thousand companies — let alone a hundred thousand — those
stop being inconveniences and start being the design.

So the crawler's own state lives here instead: which companies exist, what is
queued, what is running, what failed and why, when to try again, and every
posting ever seen. Sheets is synchronised to and from this, deliberately and
under an operator's hand, by :mod:`crawler.sync`.

    >>> from store import Database, migrate
    >>> database = Database("state/crawler.db")
    >>> migrate(database)

**The backend is not hardcoded.** Crawler code talks to
:class:`~store.repositories.CompanyRepository`,
:class:`~store.repositories.JobRepository` and
:class:`~store.queue.CrawlQueue`, never to SQL directly. :class:`Database`
owns the connection, the parameter style and the transaction, which is the
single seam a PostgreSQL backend would replace. Nothing above it would change.
"""

from __future__ import annotations

from store.database import Database
from store.schema import CURRENT_VERSION, migrate

__all__ = ["CURRENT_VERSION", "Database", "migrate"]
