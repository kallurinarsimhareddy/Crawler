"""Worker configuration, from the environment only. See ``cloud/worker/.env.example``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
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

    # --- Phase 5C: deployment ------------------------------------------------
    storage_backend: str = "local"
    s3_endpoint: Optional[str] = None
    s3_region: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_access_key_id: Optional[str] = field(default=None, repr=False)
    s3_secret_access_key: Optional[str] = field(default=None, repr=False)
    results_namespace: Optional[str] = None
    resource_registry: Optional[str] = None
    #: In-process guard on every crawler HTTP connection (see cloud/worker/egress.py).
    egress_guard: bool = True
    #: Set true only on a host whose nftables egress policy is installed and verified.
    egress_firewall_confirmed: bool = False
    log_format: str = "text"
    log_level: str = "INFO"

    @property
    def deployed(self) -> bool:
        return self.environment in ("staging", "production")


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
    settings = replace(
        settings,
        storage_backend=env_choice(env, "CAREERCLOUD_STORAGE_BACKEND", "local", ("local", "s3")),
        s3_endpoint=env_optional(env, "CAREERCLOUD_S3_ENDPOINT"),
        s3_region=env_optional(env, "CAREERCLOUD_S3_REGION"),
        s3_bucket=env_optional(env, "CAREERCLOUD_S3_BUCKET"),
        s3_access_key_id=env_optional(env, "CAREERCLOUD_S3_ACCESS_KEY_ID"),
        s3_secret_access_key=env_optional(env, "CAREERCLOUD_S3_SECRET_ACCESS_KEY"),
        results_namespace=env_optional(env, "CAREERCLOUD_RESULTS_NAMESPACE"),
        resource_registry=env_optional(env, "CAREERCLOUD_RESOURCE_REGISTRY"),
        egress_guard=env_bool(env, "CAREERCLOUD_EGRESS_GUARD", True),
        egress_firewall_confirmed=env_bool(env, "CAREERCLOUD_EGRESS_FIREWALL_CONFIRMED", False),
        log_format=env_choice(env, "CAREERCLOUD_LOG_FORMAT", "text", ("text", "json")),
        log_level=env_choice(env, "CAREERCLOUD_LOG_LEVEL", "info", ("debug", "info", "warning", "error")).upper(),
    )
    if not settings.database_url:
        raise ValueError("the worker needs CAREERCLOUD_DATABASE_URL (PostgreSQL, or 'localdev' in development)")
    if not settings.redis_url:
        raise ValueError("the worker needs CAREERCLOUD_REDIS_URL")
    if settings.lease_seconds < 3 * 5:
        raise ValueError("CAREERCLOUD_LEASE_SECONDS is too short to heartbeat reliably")
    check_runtime_root(settings.runtime_root)
    check_runtime_root(settings.results_dir)
    problems = worker_deployment_problems(settings)
    if problems:
        raise ValueError("refusing to start the worker:\n  - " + "\n  - ".join(problems))
    return settings


def worker_deployment_problems(settings: WorkerSettings) -> list:
    """What a staging or production worker must satisfy. Empty for development."""
    from urllib.parse import parse_qs, urlsplit

    problems = []
    if settings.browser_fallback and not settings.egress_firewall_confirmed and settings.deployed:
        problems.append(
            "CAREERCLOUD_CRAWLER_BROWSER_FALLBACK needs CAREERCLOUD_EGRESS_FIREWALL_CONFIRMED=true: "
            "a browser bypasses the in-process egress guard"
        )
    if not settings.deployed:
        return problems
    env = settings.environment
    if not settings.egress_guard:
        problems.append(f"{env} must not disable CAREERCLOUD_EGRESS_GUARD")
    if settings.runner != "careercrawler":
        problems.append(f"{env} workers run CAREERCLOUD_WORKER_RUNNER=careercrawler")
    if settings.database_url:
        query = parse_qs(urlsplit(settings.database_url).query)
        if query.get("sslmode", [""])[0] not in ("require", "verify-ca", "verify-full"):
            problems.append("CAREERCLOUD_DATABASE_URL must set sslmode=require (or verify-full)")
        if settings.database_url.strip() == "localdev":
            problems.append("localdev database is development-only")
    if settings.redis_url:
        parts = urlsplit(settings.redis_url)
        if parts.scheme != "rediss":
            problems.append("CAREERCLOUD_REDIS_URL must use TLS (rediss://)")
        if not parts.password:
            problems.append("CAREERCLOUD_REDIS_URL must authenticate (password in the URL)")
    if settings.storage_backend != "s3":
        problems.append(f"{env} requires CAREERCLOUD_STORAGE_BACKEND=s3")
    else:
        if not (settings.s3_endpoint or "").startswith("https://"):
            problems.append("CAREERCLOUD_S3_ENDPOINT must be https://")
        for name in ("s3_bucket", "s3_region", "s3_access_key_id", "s3_secret_access_key"):
            if not getattr(settings, name):
                problems.append(f"CAREERCLOUD_{name.upper()} is required")
    if not settings.results_namespace:
        problems.append("CAREERCLOUD_RESULTS_NAMESPACE is required")
    if not settings.resource_registry:
        problems.append(f"{env} requires CAREERCLOUD_RESOURCE_REGISTRY")
    if settings.log_format != "json":
        problems.append(f"{env} requires CAREERCLOUD_LOG_FORMAT=json")
    return problems
