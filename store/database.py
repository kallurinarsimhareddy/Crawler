"""The connection, and the one seam another backend would replace.

Everything above this module speaks in rows and dictionaries. This is the only
place that knows the crawler currently stores its state in SQLite, which is
what makes a later PostgreSQL backend an addition rather than a rewrite.

Three SQLite specifics are set deliberately and are worth knowing:

**WAL journalling.** The default rollback journal takes an exclusive lock for
every write, so a pool of workers serialises on it. Write-ahead logging lets
readers continue during a write, which is what makes a twenty-worker claim loop
practical against one file.

**A busy timeout, not an error.** Two workers claiming at the same moment is
the normal case, not an exceptional one. Without a timeout SQLite raises
``database is locked`` immediately; with one it waits, which is what the caller
wanted anyway.

**A connection per thread.** ``sqlite3`` objects are not safe to share across
threads, and the crawler is a thread pool. Each thread lazily gets its own and
releases it when it finishes.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Final, Iterable, Iterator, List, Optional, Sequence

from loguru import logger

__all__ = ["Database", "DEFAULT_DATABASE_PATH"]

#: Where the crawler keeps its state, beside the checkpoint it already writes.
DEFAULT_DATABASE_PATH: Final[Path] = Path("state") / "crawler.db"

#: How long a statement waits for a lock before giving up. Generous: a claim
#: contending with a batch insert is ordinary, and failing fast helps nobody.
_BUSY_TIMEOUT_MS: Final[int] = 30_000


class Database:
    """A thread-safe handle on the crawler's local store.

    Args:
        path: Database file, or ``":memory:"`` for a throwaway one. Parent
            directories are created as needed.
    """

    def __init__(self, path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.path = str(path)
        self._memory = self.path == ":memory:"
        self._local = threading.local()
        self._shared: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

        if not self._memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    # -- connections ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use.

        Returns:
            The connection. An in-memory database returns one shared handle
            instead, because a second connection to ``":memory:"`` would open a
            different, empty database.
        """
        if self._memory:
            with self._lock:
                if self._shared is None:
                    self._shared = self._connect()
                return self._shared

        existing = getattr(self._local, "connection", None)
        if existing is None:
            existing = self._connect()
            self._local.connection = existing
        return existing

    def _connect(self) -> sqlite3.Connection:
        """Open and configure one connection.

        Returns:
            The connection.
        """
        connection = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=self._memory,
            isolation_level=None,  # explicit transactions, not implicit ones
        )
        connection.row_factory = sqlite3.Row

        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        if not self._memory:
            # WAL is what lets readers work during a write. Meaningless for an
            # in-memory database, and SQLite rejects it there.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")

        return connection

    def close(self) -> None:
        """Close this thread's connection, or the shared one."""
        if self._memory:
            with self._lock:
                if self._shared is not None:
                    self._shared.close()
                    self._shared = None
            return

        existing = getattr(self._local, "connection", None)
        if existing is not None:
            existing.close()
            self._local.connection = None

    # -- statements ----------------------------------------------------------

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Run one statement.

        Args:
            sql: The statement.
            parameters: Bound values.

        Returns:
            The cursor, for ``rowcount`` and ``lastrowid``.
        """
        return self.connection.execute(sql, tuple(parameters))

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        """Run one statement over many parameter sets.

        Args:
            sql: The statement.
            rows: One parameter tuple per execution.

        Returns:
            How many rows were affected.
        """
        batch = [tuple(row) for row in rows]
        if not batch:
            return 0
        cursor = self.connection.executemany(sql, batch)
        return cursor.rowcount or 0

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Run a query and return every row.

        Args:
            sql: The query.
            parameters: Bound values.

        Returns:
            The rows as dictionaries.
        """
        cursor = self.connection.execute(sql, tuple(parameters))
        return [dict(row) for row in cursor.fetchall()]

    def stream(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
        batch_size: int = 500,
    ) -> Iterator[Dict[str, Any]]:
        """Run a query and yield rows without materialising the result.

        The reason this exists: at a hundred thousand companies and half a
        million postings, ``fetchall`` is how a run runs out of memory.

        Args:
            sql: The query.
            parameters: Bound values.
            batch_size: Rows fetched per round trip.

        Yields:
            One row at a time, as a dictionary.
        """
        cursor = self.connection.execute(sql, tuple(parameters))
        while True:
            rows = cursor.fetchmany(max(1, batch_size))
            if not rows:
                return
            for row in rows:
                yield dict(row)

    def one(self, sql: str, parameters: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        """Run a query and return its first row.

        Args:
            sql: The query.
            parameters: Bound values.

        Returns:
            The row, or ``None``.
        """
        cursor = self.connection.execute(sql, tuple(parameters))
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    # -- transactions --------------------------------------------------------

    def transaction(self) -> "_Transaction":
        """Begin an immediate transaction.

        ``BEGIN IMMEDIATE`` rather than the default deferred one: a claim reads
        and then writes, and a deferred transaction can find its write blocked
        after its read has already succeeded, which SQLite reports as a
        non-retryable error. Taking the write lock up front turns that race
        into an ordinary wait.

        Returns:
            A context manager that commits on success and rolls back on error.
        """
        return _Transaction(self)


class _Transaction:
    """Commits on a clean exit, rolls back on an exception.

    Args:
        database: The database to transact against.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def __enter__(self) -> Database:
        """Take the write lock.

        Returns:
            The database, for use inside the block.
        """
        self._database.execute("BEGIN IMMEDIATE")
        return self._database

    def __exit__(self, kind, value, traceback) -> bool:
        """Commit or roll back.

        Returns:
            ``False``, so an exception continues to propagate.
        """
        try:
            if kind is None:
                self._database.execute("COMMIT")
            else:
                self._database.execute("ROLLBACK")
        except sqlite3.Error as exc:  # pragma: no cover - only on a lost handle
            logger.debug("Transaction could not be finalised: {}", exc)
        return False
