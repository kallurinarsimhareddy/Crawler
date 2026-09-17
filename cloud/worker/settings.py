"""Worker configuration, from the environment only. See ``cloud/worker/.env.example``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from cloud.shared.config import env_bool, env_choice, env_float, env_int, env_optional, env_path
from cloud.worker.workspace import DEFAULT_RUNTIME_ROOT, check_runtime_root

__all__ = ["WorkerSettings", "load_worker_settings"]

CLOUD_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_DIR = CLOUD_ROOT / ".localdev" / "results"


@dataclass(frozen=True)
class WorkerSettings:
    environment: str = "development"
    allow_remote_services: bool = False
    database_url: Optional[str] = field(default=None, repr=False)
    redis_url: Optional[str] = field(default=None, repr=False)
    queue_prefix: str = "careercloud:development"
    db_pool_max: int = 5

    runner: str = "careercrawler"
    concurrency: int = 1
    lease_seconds: float = 120.0
    poll_interval: float = 2.0
    visibility_timeout: float = 600.0
    reap_interval: float = 30.0
    orphan_after_seconds: float = 600.0

    max_attempts: int = 3
    retry_base_delay: float = 30.0
    retry_max_delay: float = 900.0

    runtime_root: Path = DEFAULT_RUNTIME_ROOT
    results_dir: Path = DEFAULT_RESULTS_DIR
    keep_workspaces: bool = False

    # Crawler knobs for cloud jobs. Independent of the production weekly run,
    # whose own defaults (including its worker count) are never changed here.
    company_concurrency: int = 4
    browser_fallback: bool = False
    max_runtime_seconds: float = 3600.0
    fake_step_seconds: float = 1.0


def load_worker_settings(env: Optional[Mapping[str, str]] = None) -> WorkerSettings:
    env = os.environ if env is None else env
    d = WorkerSettings()
    environment = env_choice(env, "CAREERCLOUD_ENV", d.environment, ("development", "test", "staging", "production"))
    settings = WorkerSettings(
        environment=environment,
        allow_remote_services=env_bool(env, "CAREERCLOUD_ALLOW_REMOTE_SERVICES", False),
        database_url=env_optional(env, "CAREERCLOUD_DATABASE_URL"),
        redis_url=env_optional(env, "CAREERCLOUD_REDIS_URL"),
        queue_prefix=env.get("CAREERCLOUD_QUEUE_PREFIX", f"careercloud:{environment}").strip(),
        db_pool_max=env_int(env, "CAREERCLOUD_DB_POOL_MAX", d.db_pool_max, minimum=1, maximum=50),
        runner=env_choice(env, "CAREERCLOUD_WORKER_RUNNER", d.runner, ("careercrawler", "fake")),
        concurrency=env_int(env, "CAREERCLOUD_WORKER_CONCURRENCY", d.concurrency, minimum=1, maximum=32),
        lease_seconds=env_float(env, "CAREERCLOUD_LEASE_SECONDS", d.lease_seconds, minimum=10, maximum=3600),
        poll_interval=env_float(env, "CAREERCLOUD_POLL_INTERVAL", d.poll_interval, minimum=0.1, maximum=60),
        visibility_timeout=env_float(env, "CAREERCLOUD_VISIBILITY_TIMEOUT", d.visibility_timeout, minimum=10, maximum=86400),
        reap_interval=env_float(env, "CAREERCLOUD_REAP_INTERVAL", d.reap_interval, minimum=1, maximum=3600),
        orphan_after_seconds=env_float(env, "CAREERCLOUD_ORPHAN_AFTER_SECONDS", d.orphan_after_seconds, minimum=10, maximum=86400),
        max_attempts=env_int(env, "CAREERCLOUD_MAX_ATTEMPTS", d.max_attempts, minimum=1, maximum=10),
        retry_base_delay=env_float(env, "CAREERCLOUD_RETRY_BASE_DELAY", d.retry_base_delay, minimum=0, maximum=3600),
        retry_max_delay=env_float(env, "CAREERCLOUD_RETRY_MAX_DELAY", d.retry_max_delay, minimum=0, maximum=86400),
        runtime_root=env_path(env, "CAREERCLOUD_WORKER_RUNTIME_DIR", DEFAULT_RUNTIME_ROOT),
        results_dir=env_path(env, "CAREERCLOUD_RESULTS_DIR", DEFAULT_RESULTS_DIR),
        keep_workspaces=env_bool(env, "CAREERCLOUD_KEEP_WORKSPACES", False),
        company_concurrency=env_int(env, "CAREERCLOUD_CRAWLER_COMPANY_CONCURRENCY", d.company_concurrency, minimum=1, maximum=32),
        browser_fallback=env_bool(env, "CAREERCLOUD_CRAWLER_BROWSER_FALLBACK", False),
        max_runtime_seconds=env_float(env, "CAREERCLOUD_CRAWLER_MAX_RUNTIME_SECONDS", d.max_runtime_seconds, minimum=10, maximum=86400),
        fake_step_seconds=env_float(env, "CAREERCLOUD_FAKE_STEP_SECONDS", d.fake_step_seconds, minimum=0, maximum=60),
    )
    if not settings.database_url:
        raise ValueError("the worker needs CAREERCLOUD_DATABASE_URL (PostgreSQL, or 'localdev' in development)")
    if not settings.redis_url:
        raise ValueError("the worker needs CAREERCLOUD_REDIS_URL")
    if settings.lease_seconds < 3 * 5:
        raise ValueError("CAREERCLOUD_LEASE_SECONDS is too short to heartbeat reliably")
    check_runtime_root(settings.runtime_root)
    check_runtime_root(settings.results_dir)
    return settings
