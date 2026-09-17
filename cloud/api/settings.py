"""API configuration, read from the environment and nowhere else.

There are no credentials in this file and there must never be. Values come from
the process environment — locally via ``uvicorn --env-file cloud/api/.env``,
in production from the host's secret store. ``cloud/api/.env.example`` lists
every variable.

The database, Supabase and Redis settings are read but **not used** in Phase 5A:
jobs are kept in memory. They are parsed now so that a deployment which sets
them is told plainly that they have no effect yet, rather than assuming its data
is being persisted.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Tuple

__all__ = ["Settings", "load_settings"]

log = logging.getLogger(__name__)

_RUNNERS = ("fake", "none")


@dataclass(frozen=True)
class Settings:
    """Everything the API decides at startup.

    Attributes:
        environment: ``development``, ``staging`` or ``production``; reported by
            the health endpoint.
        cors_origins: Browser origins allowed to call the API.
        runner: ``fake`` runs jobs in-process with the simulated runner;
            ``none`` leaves them queued for an external worker.
        fake_step_seconds: Pause per simulated step, so progress is visible.
        max_concurrent_jobs: In-process jobs run at once with ``runner=fake``.
    """

    environment: str = "development"
    cors_origins: Tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    runner: str = "fake"
    fake_step_seconds: float = 1.0
    max_concurrent_jobs: int = 2

    # Phase 5B placeholders — parsed, reported as unused, never connected to.
    database_url: Optional[str] = field(default=None, repr=False)
    supabase_url: Optional[str] = None
    supabase_anon_key: Optional[str] = field(default=None, repr=False)
    supabase_service_role_key: Optional[str] = field(default=None, repr=False)
    redis_url: Optional[str] = field(default=None, repr=False)

    def unused_placeholders(self) -> List[str]:
        """Names of Phase 5B variables that are set but have no effect yet."""
        names = {
            "CAREERCLOUD_DATABASE_URL": self.database_url,
            "CAREERCLOUD_SUPABASE_URL": self.supabase_url,
            "CAREERCLOUD_SUPABASE_ANON_KEY": self.supabase_anon_key,
            "CAREERCLOUD_SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key,
            "CAREERCLOUD_REDIS_URL": self.redis_url,
        }
        return [name for name, value in names.items() if value]


def _optional(env: Mapping[str, str], name: str) -> Optional[str]:
    value = env.get(name, "").strip()
    return value or None


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build :class:`Settings` from ``env`` (default: ``os.environ``).

    Raises:
        ValueError: A variable is set to something that cannot be used.
    """
    env = os.environ if env is None else env
    defaults = Settings()

    runner = env.get("CAREERCLOUD_RUNNER", defaults.runner).strip().lower()
    if runner not in _RUNNERS:
        raise ValueError(f"CAREERCLOUD_RUNNER must be one of {', '.join(_RUNNERS)}, not {runner!r}")

    origins_raw = env.get("CAREERCLOUD_CORS_ORIGINS")
    origins = (
        tuple(origin.strip() for origin in origins_raw.split(",") if origin.strip())
        if origins_raw is not None
        else defaults.cors_origins
    )
    if "*" in origins:
        raise ValueError("CAREERCLOUD_CORS_ORIGINS must list origins explicitly, not '*'")

    try:
        step = float(env.get("CAREERCLOUD_FAKE_STEP_SECONDS", defaults.fake_step_seconds))
        concurrent = int(env.get("CAREERCLOUD_MAX_CONCURRENT_JOBS", defaults.max_concurrent_jobs))
    except ValueError as error:
        raise ValueError(f"invalid numeric setting: {error}") from error
    if step < 0:
        raise ValueError("CAREERCLOUD_FAKE_STEP_SECONDS must not be negative")
    if concurrent < 1:
        raise ValueError("CAREERCLOUD_MAX_CONCURRENT_JOBS must be at least 1")

    settings = Settings(
        environment=env.get("CAREERCLOUD_ENV", defaults.environment).strip() or defaults.environment,
        cors_origins=origins,
        runner=runner,
        fake_step_seconds=step,
        max_concurrent_jobs=concurrent,
        database_url=_optional(env, "CAREERCLOUD_DATABASE_URL"),
        supabase_url=_optional(env, "CAREERCLOUD_SUPABASE_URL"),
        supabase_anon_key=_optional(env, "CAREERCLOUD_SUPABASE_ANON_KEY"),
        supabase_service_role_key=_optional(env, "CAREERCLOUD_SUPABASE_SERVICE_ROLE_KEY"),
        redis_url=_optional(env, "CAREERCLOUD_REDIS_URL"),
    )
    for name in settings.unused_placeholders():
        log.warning("%s is set but Phase 5A keeps jobs in memory; it has no effect yet", name)
    return settings
