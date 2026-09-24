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
                   store: Any = None, queue: Any = None) -> Platform:
    env = dict(os.environ if env is None else env)
    environment = env.get("CAREERCLOUD_ENV", "development")
    allow_remote = env.get("CAREERCLOUD_ALLOW_REMOTE_SERVICES", "") in ("1", "true", "yes")

    if store is None:
        if env.get("CAREERCLOUD_STORAGE", "memory") == "postgres":
            from cloud.db.connection import resolve_database_url
            from cloud.intel.store.postgres import PostgresStore

            url = resolve_database_url(env.get("CAREERCLOUD_DATABASE_URL"), environment=environment,
                                       allow_remote=allow_remote)
            store = PostgresStore.from_url(url, max_size=int(env.get("CAREERCLOUD_DB_POOL_MAX", "10")),
                                           user_role=env.get("CAREERCLOUD_DB_USER_ROLE", "authenticated"))
        else:
            from cloud.intel.store.memory import MemoryStore

            store = MemoryStore()

    inline = None
    if queue is None:
        if env.get("CAREERCLOUD_QUEUE", "inline") == "redis":
            from cloud.db.connection import resolve_redis_url
            from cloud.shared.queue import RedisJobQueue

            url = resolve_redis_url(env.get("CAREERCLOUD_REDIS_URL"), environment=environment,
                                    allow_remote=allow_remote)
            prefix = env.get("CAREERCLOUD_QUEUE_PREFIX", f"careercloud:{environment}")
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
