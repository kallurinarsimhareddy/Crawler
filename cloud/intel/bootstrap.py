"""Build a :class:`~cloud.intel.platform.Platform` from ``CAREERCLOUD_*`` settings.

The platform reuses CareerCloud's configuration and its safety rules:

==============================  ==================================================
CAREERCLOUD_ENV                 development | test | staging | production
CAREERCLOUD_STORAGE             memory | postgres (same database as CareerCloud)
CAREERCLOUD_DATABASE_URL        via :func:`cloud.db.connection.resolve_database_url`
                                (postgresql:// only — never SQLite, never crawler.db)
CAREERCLOUD_QUEUE               inline | redis
CAREERCLOUD_REDIS_URL           via :func:`cloud.db.connection.resolve_redis_url`
CAREERCLOUD_QUEUE_PREFIX        platform tasks use ``<prefix>:platform``
CAREERCLOUD_PLATFORM_SECRETS_KEY  Fernet key for provider credentials at rest
CAREERCLOUD_AI_PROVIDER         rules | claude | gemini | openai_compatible
CAREERCLOUD_AI_MODEL            model id for that provider (optional)
CAREERCLOUD_ALLOW_EMAIL_SENDING only honoured in production; default false
==============================  ==================================================
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from cloud.intel.platform import Platform, PlatformConfig

__all__ = ["InlineTaskQueue", "build_platform"]

log = logging.getLogger(__name__)


def _describe(url: str) -> str:
    from cloud.db.connection import describe_url

    return describe_url(url)


class InlineTaskQueue:
    """Development without Redis: run each task on a background thread in-process.

    Implements just enough of :class:`cloud.shared.queue.JobQueue` for
    :class:`~cloud.intel.tasks.service.TaskService`.
    """

    name = "inline"

    def __init__(self) -> None:
        self.platform: Optional[Platform] = None
        self._timers: list = []

    def enqueue(self, key: str, *, delay_seconds: float = 0.0) -> bool:
        from cloud.intel.tasks.service import parse_queue_key
        from cloud.intel.tasks.worker import run_task_inline

        if self.platform is None:
            return False
        workspace_id, task_id = parse_queue_key(key)
        timer = threading.Timer(max(0.0, delay_seconds), run_task_inline, args=(self.platform, workspace_id, task_id))
        timer.daemon = True
        timer.start()
        self._timers.append(timer)
        return True

    def heartbeat_worker(self, worker_id: str) -> None:  # pragma: no cover - interface filler
        pass

    def forget_worker(self, worker_id: str) -> None:  # pragma: no cover
        pass


def build_platform(role: str = "api", env: Optional[Mapping[str, str]] = None, *,
                   store: Any = None, queue: Any = None, settings: Any = None) -> Platform:
    """Build the platform.

    Persistence: PostgreSQL is the default — the embedded local server
    (``CAREERCLOUD_DATABASE_URL=localdev``) in development. The in-memory store is
    an explicit fallback (``CAREERCLOUD_STORAGE=memory``) for tests and quick UI
    work; its data disappears when the process stops.

    Given the API's ``settings``, the platform uses exactly the API's storage
    choice and database, so crawl jobs and platform records live in one place.
    Before a PostgreSQL platform is returned, :func:`require_schema` checks that
    every migration and table is present, and fails clearly if not.
    """
    env = dict(os.environ if env is None else env)
    environment = env.get("CAREERCLOUD_ENV", "development")
    allow_remote = env.get("CAREERCLOUD_ALLOW_REMOTE_SERVICES", "") in ("1", "true", "yes")
    storage = env.get("CAREERCLOUD_STORAGE") or "postgres"
    database_url = env.get("CAREERCLOUD_DATABASE_URL") or ("localdev" if environment == "development" else None)
    pool_max = int(env.get("CAREERCLOUD_DB_POOL_MAX", "10"))
    user_role = env.get("CAREERCLOUD_DB_USER_ROLE", "authenticated")
    if settings is not None:
        environment, allow_remote = settings.environment, settings.allow_remote_services
        storage, database_url = settings.storage, settings.database_url
        pool_max, user_role = settings.db_pool_max, settings.db_user_role

    if store is None:
        if storage == "postgres":
            from cloud.db.connection import resolve_database_url
            from cloud.intel.store.postgres import PostgresStore
            from cloud.intel.store.schema_check import require_schema

            url = resolve_database_url(database_url, environment=environment, allow_remote=allow_remote)
            if not url:
                from cloud.db.connection import ConfigurationError

                raise ConfigurationError(
                    "the platform persists to PostgreSQL but CAREERCLOUD_DATABASE_URL is not set — use "
                    "CAREERCLOUD_DATABASE_URL=localdev in development (after `python -m cloud.devtools.localpg "
                    "start`), a postgresql:// URL elsewhere, or CAREERCLOUD_STORAGE=memory for a throwaway store")
            store = PostgresStore.from_url(url, max_size=pool_max, user_role=user_role)
            try:
                report = require_schema(store._pool)  # noqa: SLF001 - the store owns the pool
            except Exception:
                store.close()
                raise
            log.info("platform persistence: PostgreSQL at %s (%d/%d tables verified)", _describe(url),
                     report["tables_verified"], report["tables_expected"])
        elif storage == "memory":
            from cloud.intel.store.memory import MemoryStore

            log.warning("platform persistence: IN MEMORY (CAREERCLOUD_STORAGE=memory) — data is lost on restart")
            store = MemoryStore()
        else:
            raise ValueError(f"CAREERCLOUD_STORAGE must be postgres or memory, not {storage!r}")

    inline = None
    if queue is None:
        queue_kind = settings.queue if settings is not None else env.get("CAREERCLOUD_QUEUE", "inline")
        if queue_kind == "redis":
            from cloud.db.connection import resolve_redis_url
            from cloud.shared.queue import RedisJobQueue

            redis_url = settings.redis_url if settings is not None else env.get("CAREERCLOUD_REDIS_URL")
            url = resolve_redis_url(redis_url, environment=environment, allow_remote=allow_remote)
            prefix = (settings.queue_prefix if settings is not None else None) or env.get(
                "CAREERCLOUD_QUEUE_PREFIX", f"careercloud:{environment}")
            queue = RedisJobQueue.from_url(url, prefix=f"{prefix}:platform")
        elif role == "api":
            inline = queue = InlineTaskQueue()

    files_dir = env.get("CAREERCLOUD_PLATFORM_FILES_DIR")
    config = PlatformConfig(
        environment=environment,
        secrets_key=env.get("CAREERCLOUD_PLATFORM_SECRETS_KEY") or None,
        allow_email_sending=(environment == "production"
                             and env.get("CAREERCLOUD_ALLOW_EMAIL_SENDING", "") in ("1", "true", "yes")),
        ai_provider=env.get("CAREERCLOUD_AI_PROVIDER", "rules"),
        ai_model=env.get("CAREERCLOUD_AI_MODEL") or None,
    )
    if files_dir:
        config.files_dir = Path(files_dir)
    storage = None
    if env.get("CAREERCLOUD_STORAGE_BACKEND") == "s3":
        from cloud.shared.s3_storage import S3Storage

        storage = S3Storage.from_settings(
            endpoint=env.get("CAREERCLOUD_S3_ENDPOINT", ""), region=env.get("CAREERCLOUD_S3_REGION", ""),
            bucket=env.get("CAREERCLOUD_S3_BUCKET", ""),
            namespace=(env.get("CAREERCLOUD_RESULTS_NAMESPACE") or environment) + "/platform",
            access_key_id=env.get("CAREERCLOUD_S3_ACCESS_KEY_ID", ""),
            secret_access_key=env.get("CAREERCLOUD_S3_SECRET_ACCESS_KEY", ""))
    platform = Platform(store, queue=queue, storage=storage, config=config)
    if inline is not None:
        inline.platform = platform
    return platform
