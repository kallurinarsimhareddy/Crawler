"""A real PostgreSQL for the tests.

By default the tests start one embedded PostgreSQL 16 (``pgserver``) for the
whole run, under ``cloud/.localdev/test-postgres-<pid>``, and delete it at exit.
Each test class gets a fresh database with the migrations applied, so classes
cannot see each other's rows.

Set ``CAREERCLOUD_TEST_DATABASE_URL`` to use another server instead — it must be
a disposable server: the tests create and drop databases on it. If neither is
available the PostgreSQL tests are skipped, loudly.
"""

from __future__ import annotations

import atexit
import os
import shutil
import threading
import unittest
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

CLOUD = Path(__file__).resolve().parent.parent
_lock = threading.Lock()
_server_url: Optional[str] = None
_unavailable: Optional[str] = None


def _start() -> str:
    global _server_url, _unavailable
    with _lock:
        if _server_url is not None:
            return _server_url
        if _unavailable is not None:
            raise unittest.SkipTest(_unavailable)

        external = os.environ.get("CAREERCLOUD_TEST_DATABASE_URL")
        if external:
            _server_url = external
            return external
        try:
            import pgserver
        except ImportError:
            _unavailable = "PostgreSQL tests skipped: install pgserver or set CAREERCLOUD_TEST_DATABASE_URL"
            raise unittest.SkipTest(_unavailable)

        data = CLOUD / ".localdev" / f"test-postgres-{os.getpid()}"
        if data.exists():
            shutil.rmtree(data, ignore_errors=True)
        data.parent.mkdir(parents=True, exist_ok=True)
        server = pgserver.get_server(data, cleanup_mode="stop")

        def _cleanup() -> None:
            try:
                server.cleanup()
            finally:
                shutil.rmtree(data, ignore_errors=True)

        atexit.register(_cleanup)
        _server_url = server.get_uri()
        return _server_url


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def fresh_database(*, migrate: bool = True) -> str:
    """Create an empty database (optionally migrated) and return its URL."""
    import psycopg

    from cloud.db.migrate import apply_migrations

    server = _start()
    name = f"cc_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'create database "{name}"')
    url = _with_database(server, name)
    if migrate:
        apply_migrations(url)
    return url


def drop_database(url: str) -> None:
    import psycopg

    server = _start()
    name = urlsplit(url).path.lstrip("/")
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{name}" with (force)')


class PostgresTestCase(unittest.TestCase):
    """A class-scoped migrated database and a repository on it."""

    database_url: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from cloud.db.postgres import PostgresJobRepository

        cls.database_url = fresh_database()
        cls.pg_repository = PostgresJobRepository.from_url(cls.database_url, max_size=8)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.pg_repository.close()
        drop_database(cls.database_url)
        super().tearDownClass()

    def truncate(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute(
                "truncate careercloud.job_results, careercloud.job_events, "
                "careercloud.crawl_targets, careercloud.jobs cascade"
            )
