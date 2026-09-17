"""Turning configuration into a database connection string — carefully.

The rules, all enforced here so the API, the worker and the migrator agree:

* Only ``postgresql://`` / ``postgres://`` URLs are accepted. A SQLite path, a
  file path or anything else is refused, so a misconfigured variable can never
  point CareerCloud at ``state/crawler.db``.
* ``localdev`` means the embedded development PostgreSQL under
  ``cloud/.localdev/postgres`` (see :mod:`cloud.devtools.localpg`). It is only
  accepted when the environment is ``development``.
* In ``development``, a URL whose host is not this machine is refused unless
  ``CAREERCLOUD_ALLOW_REMOTE_SERVICES=1`` says so. Local development never
  reaches a hosted database by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

__all__ = [
    "LOCALDEV",
    "LOCALDEV_PGDATA",
    "ConfigurationError",
    "describe_url",
    "is_local_host",
    "resolve_database_url",
    "resolve_redis_url",
]

LOCALDEV = "localdev"
CLOUD_ROOT = Path(__file__).resolve().parent.parent
LOCALDEV_PGDATA = CLOUD_ROOT / ".localdev" / "postgres"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


class ConfigurationError(ValueError):
    """A setting that would be unsafe or cannot work."""


def is_local_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in _LOCAL_HOSTS


def describe_url(url: Optional[str]) -> str:
    """``scheme://host:port/db`` with credentials removed, for logs and health."""
    if not url:
        return "unset"
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def _check_remote(url: str, *, environment: str, allow_remote: bool, what: str) -> None:
    if environment == "development" and not is_local_host(url) and not allow_remote:
        raise ConfigurationError(
            f"{what} points at {describe_url(url)}, which is not this machine. "
            "Local development refuses remote services unless "
            "CAREERCLOUD_ALLOW_REMOTE_SERVICES=1 is set deliberately."
        )


def resolve_database_url(
    value: Optional[str], *, environment: str, allow_remote: bool = False
) -> Optional[str]:
    """Validate ``CAREERCLOUD_DATABASE_URL`` and return a usable URL, or ``None`` if unset."""
    text = (value or "").strip()
    if not text:
        return None

    if text == LOCALDEV:
        if environment != "development":
            raise ConfigurationError("CAREERCLOUD_DATABASE_URL=localdev is only allowed in development")
        return localdev_database_url()

    scheme = urlsplit(text).scheme.lower()
    if scheme not in ("postgresql", "postgres"):
        raise ConfigurationError(
            "CAREERCLOUD_DATABASE_URL must be a postgresql:// URL "
            "(CareerCloud never uses SQLite or a file path)"
        )
    _check_remote(text, environment=environment, allow_remote=allow_remote, what="CAREERCLOUD_DATABASE_URL")
    return text


def resolve_redis_url(
    value: Optional[str], *, environment: str, allow_remote: bool = False
) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None
    scheme = urlsplit(text).scheme.lower()
    if scheme not in ("redis", "rediss"):
        raise ConfigurationError("CAREERCLOUD_REDIS_URL must be a redis:// or rediss:// URL")
    _check_remote(text, environment=environment, allow_remote=allow_remote, what="CAREERCLOUD_REDIS_URL")
    return text


def localdev_database_url() -> str:
    """Start (or find) the embedded development PostgreSQL and return its URL."""
    try:
        import pgserver  # development-only dependency
    except ImportError as error:  # pragma: no cover - depends on the venv
        raise ConfigurationError(
            "CAREERCLOUD_DATABASE_URL=localdev needs the pgserver package: "
            "pip install -r cloud/requirements-dev.txt"
        ) from error
    LOCALDEV_PGDATA.parent.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(LOCALDEV_PGDATA, cleanup_mode=None)
    return server.get_uri()
