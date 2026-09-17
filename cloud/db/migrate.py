"""Apply CareerCloud's SQL migrations, in order, exactly once.

    python -m cloud.db.migrate status
    python -m cloud.db.migrate apply
    python -m cloud.db.migrate apply --database-url postgresql://...

The URL defaults to ``CAREERCLOUD_DATABASE_URL`` and goes through the same
safety checks as the API (see :mod:`cloud.db.connection`).

Each file in ``cloud/db/migrations`` runs in its own transaction and is
recorded in ``careercloud.schema_migrations`` with a SHA-256 of its contents.
Editing a migration that has already been applied is refused rather than
silently ignored — write a new one. A session advisory lock stops two
migrators running at once.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from cloud.db.connection import ConfigurationError, describe_url, resolve_database_url

__all__ = ["Migration", "MigrationError", "apply_migrations", "load_migrations", "pending_migrations"]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_LOCK_KEY = 0x6361726565  # "caree", an arbitrary constant shared by every migrator


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str
    checksum: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> List[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=path.stem,
                path=path,
                sql=text,
                checksum=hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest(),
            )
        )
    return migrations


_BOOKKEEPING = """
create schema if not exists careercloud;
create table if not exists careercloud.schema_migrations (
  version     text primary key,
  checksum    text not null,
  applied_at  timestamptz not null default now()
);
"""


def _applied(conn) -> dict:
    rows = conn.execute("select version, checksum from careercloud.schema_migrations").fetchall()
    return {version: checksum for version, checksum in rows}


def pending_migrations(conn, migrations: Sequence[Migration]) -> List[Migration]:
    applied = _applied(conn)
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise MigrationError(
                f"migration {migration.version} was edited after being applied; "
                "restore it and add a new migration instead"
            )
    return [m for m in migrations if m.version not in applied]


def apply_migrations(database_url: str, migrations: Optional[Sequence[Migration]] = None) -> List[str]:
    """Apply everything pending. Returns the versions applied."""
    import psycopg

    migrations = list(migrations if migrations is not None else load_migrations())
    applied_now: List[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(%s)", [_LOCK_KEY])
        try:
            conn.execute(_BOOKKEEPING)
            for migration in pending_migrations(conn, migrations):
                with conn.transaction():
                    conn.execute(migration.sql)
                    conn.execute(
                        "insert into careercloud.schema_migrations (version, checksum) values (%s, %s)",
                        [migration.version, migration.checksum],
                    )
                applied_now.append(migration.version)
        finally:
            conn.execute("select pg_advisory_unlock(%s)", [_LOCK_KEY])
    return applied_now


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.db.migrate", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["status", "apply"])
    parser.add_argument("--database-url", default=os.environ.get("CAREERCLOUD_DATABASE_URL"))
    parser.add_argument("--environment", default=os.environ.get("CAREERCLOUD_ENV", "development"))
    args = parser.parse_args(argv)

    try:
        url = resolve_database_url(
            args.database_url,
            environment=args.environment,
            allow_remote=os.environ.get("CAREERCLOUD_ALLOW_REMOTE_SERVICES") == "1",
        )
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if url is None:
        print("error: set CAREERCLOUD_DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2

    print(f"database: {describe_url(url)}")
    migrations = load_migrations()
    if args.command == "status":
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(_BOOKKEEPING)
            pending = pending_migrations(conn, migrations)
        for migration in migrations:
            state = "pending" if migration in pending else "applied"
            print(f"  {migration.version:<40} {state}")
        return 0

    applied = apply_migrations(url, migrations)
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
