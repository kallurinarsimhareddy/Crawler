"""Environment stamps on the database, the Redis queue and object storage.

Each shared resource records the one environment it belongs to:

=============  =========================================  ==============================
Resource       Stamp                                      Written by
=============  =========================================  ==============================
PostgreSQL     ``careercloud.deployment`` (one row)       ``migrate stamp`` (explicitly)
Redis          ``<queue prefix>:environment``             first deployed process (SET NX)
Storage        object ``meta/environment``                first deployed process
=============  =========================================  ==============================

The database stamp is never written implicitly: pointing a new process at an
unstamped database is an operator mistake worth stopping for. Redis and storage
are stamped on first use, because they are created empty by the provider and
the queue prefix and bucket name are already checked against the registry.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional

__all__ = [
    "StampMismatchError",
    "check_database_stamp",
    "ensure_redis_stamp",
    "ensure_storage_stamp",
    "read_database_stamp",
    "stamp_database",
]

STORAGE_STAMP_KEY = "meta/environment"


class StampMismatchError(RuntimeError):
    pass


def read_database_stamp(database_url: str) -> Optional[str]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        exists = conn.execute("select to_regclass('careercloud.deployment') is not null").fetchone()[0]
        if not exists:
            return None
        row = conn.execute("select environment from careercloud.deployment").fetchone()
    return row[0] if row else None


def stamp_database(database_url: str, environment: str) -> str:
    """Stamp an unstamped database. Refuses to restamp as anything else."""
    import psycopg

    current = read_database_stamp(database_url)
    if current == environment:
        return current
    if current is not None:
        raise StampMismatchError(f"database is stamped {current!r}; refusing to stamp it {environment!r}")
    with psycopg.connect(database_url) as conn:
        conn.execute("insert into careercloud.deployment (environment) values (%s)", [environment])
    return environment


def check_database_stamp(database_url: str, environment: str) -> None:
    current = read_database_stamp(database_url)
    if current is None:
        raise StampMismatchError(
            f"database is not stamped; run `python -m cloud.db.migrate stamp --environment {environment}` "
            "after checking it is the right database"
        )
    if current != environment:
        raise StampMismatchError(f"database is stamped {current!r}, but this process is {environment!r}")


def ensure_redis_stamp(client: object, prefix: str, environment: str) -> None:
    key = f"{prefix}:environment"
    client.set(key, environment, nx=True)  # type: ignore[attr-defined]
    current = client.get(key)  # type: ignore[attr-defined]
    if isinstance(current, bytes):
        current = current.decode()
    if current != environment:
        raise StampMismatchError(f"Redis prefix {prefix!r} is stamped {current!r}, but this process is {environment!r}")


def ensure_storage_stamp(storage: object, environment: str) -> None:
    if storage.exists(STORAGE_STAMP_KEY):  # type: ignore[attr-defined]
        with storage.open(STORAGE_STAMP_KEY) as handle:  # type: ignore[attr-defined]
            current = handle.read().decode("utf-8").strip()
        if current != environment:
            raise StampMismatchError(f"storage is stamped {current!r}, but this process is {environment!r}")
        return
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "environment"
        source.write_text(environment, encoding="utf-8")
        storage.put_file(STORAGE_STAMP_KEY, source, content_type="text/plain")  # type: ignore[attr-defined]


def verify_all(
    environment: str,
    *,
    database_url: Optional[str],
    redis_client: object = None,
    queue_prefix: Optional[str] = None,
    storage: object = None,
) -> List[str]:
    """Check every stamp this process can see. Returns problems instead of raising."""
    problems: List[str] = []
    checks = []
    if database_url:
        checks.append(lambda: check_database_stamp(database_url, environment))
    if redis_client is not None and queue_prefix:
        checks.append(lambda: ensure_redis_stamp(redis_client, queue_prefix, environment))
    if storage is not None:
        checks.append(lambda: ensure_storage_stamp(storage, environment))
    for check in checks:
        try:
            check()
        except StampMismatchError as error:
            problems.append(str(error))
    return problems
