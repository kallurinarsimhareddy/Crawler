"""API configuration, read from the environment and nowhere else.

There are no credentials in this file and there must never be. Values come from
the process environment — locally via ``uvicorn --env-file cloud/api/.env``,
in production from the host's secret store. ``cloud/api/.env.example`` lists
every variable.

**Nothing connects by default.** With an empty environment the API keeps jobs
in memory, runs them with the simulated runner, and refuses every
authenticated request until authentication is configured. PostgreSQL, Redis and
Supabase are used only when explicitly selected, and in ``development`` a
remote database or Redis is refused unless ``CAREERCLOUD_ALLOW_REMOTE_SERVICES=1``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import FrozenSet, List, Mapping, Optional, Tuple

from cloud.shared.config import env_bool, env_choice, env_float, env_int, env_list, env_optional, env_path
from cloud.shared.models import JobType

__all__ = ["Settings", "load_settings"]

log = logging.getLogger(__name__)

_RUNNERS = ("fake", "none")
_ENVIRONMENTS = ("development", "test", "staging", "production")
CLOUD_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_DIR = CLOUD_ROOT / ".localdev" / "results"
DEFAULT_RUNNABLE = frozenset({JobType.SINGLE_COMPANY, JobType.BULK_COMPANIES})


@dataclass(frozen=True)
class Settings:
    """Everything the API decides at startup.

    Attributes:
        environment: ``development``, ``test``, ``staging`` or ``production``.
        cors_origins: Browser origins allowed to call the API.
        runner: With ``queue=inline``: ``fake`` runs jobs in-process with the
            simulated runner; ``none`` leaves them queued. The real crawler
            never runs inside the API process — only in ``python -m cloud.worker``.
        fake_step_seconds: Pause per simulated step, so progress is visible.
        max_concurrent_jobs: In-process jobs run at once with ``queue=inline``.
        storage: ``memory`` or ``postgres``.
        queue: ``inline`` (in-process, development) or ``redis``.
        auth_mode: ``supabase`` (verify Supabase access tokens) or ``dev``
            (locally issued tokens; development only).
    """

    environment: str = "development"
    cors_origins: Tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    runner: str = "fake"
    fake_step_seconds: float = 1.0
    max_concurrent_jobs: int = 2

    # --- Phase 5B ------------------------------------------------------------
    storage: str = "memory"
    queue: str = "inline"
    queue_prefix: str = "careercloud:development"
    auth_mode: str = "supabase"
    allow_remote_services: bool = False
    results_dir: Path = DEFAULT_RESULTS_DIR
    max_attempts: int = 3
    max_active_jobs_per_user: int = 5
    #: Job types the out-of-process worker executes (``queue=redis``). Others are
    #: recorded with an "unsupported" status instead of being enqueued.
    runnable_types: FrozenSet[JobType] = DEFAULT_RUNNABLE
    db_user_role: str = "authenticated"
    db_pool_max: int = 10

    database_url: Optional[str] = field(default=None, repr=False)
    redis_url: Optional[str] = field(default=None, repr=False)
    supabase_url: Optional[str] = None
    supabase_anon_key: Optional[str] = field(default=None, repr=False)
    supabase_service_role_key: Optional[str] = field(default=None, repr=False)
    supabase_jwt_secret: Optional[str] = field(default=None, repr=False)
    supabase_jwt_audience: str = "authenticated"
    dev_jwt_secret: Optional[str] = field(default=None, repr=False)
    dev_token_ttl_seconds: int = 8 * 3600

    # --- Phase 5C: deployment ------------------------------------------------
    #: local (a directory) or s3 (Supabase Storage S3 protocol / Cloudflare R2).
    storage_backend: str = "local"
    s3_endpoint: Optional[str] = None
    s3_region: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_access_key_id: Optional[str] = field(default=None, repr=False)
    s3_secret_access_key: Optional[str] = field(default=None, repr=False)
    #: Top-level prefix for result objects; must contain the environment when deployed.
    results_namespace: Optional[str] = None
    #: This API's public origin, e.g. https://api-staging.example.com.
    api_origin: Optional[str] = None
    #: JSON allowlist of each environment's resources (required when deployed).
    resource_registry: Optional[str] = None
    rate_limit_per_minute: int = 120
    job_create_per_hour: int = 30
    #: none, or cloudflare: trust CF-Connecting-IP from a loopback peer (Cloudflare Tunnel).
    trust_proxy: str = "none"
    log_format: str = "text"
    log_level: str = "INFO"

    @property
    def deployed(self) -> bool:
        return self.environment in ("staging", "production")

    def unused_placeholders(self) -> List[str]:
        """Names of variables that are set but have no effect with this configuration."""
        unused = {
            "CAREERCLOUD_DATABASE_URL": self.database_url if self.storage != "postgres" else None,
            "CAREERCLOUD_REDIS_URL": self.redis_url if self.queue != "redis" else None,
            "CAREERCLOUD_SUPABASE_URL": self.supabase_url if self.auth_mode != "supabase" else None,
            # The browser uses the anon key; the API never does.
            "CAREERCLOUD_SUPABASE_ANON_KEY": self.supabase_anon_key,
            # The API must never hold the service-role key.
            "CAREERCLOUD_SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key,
        }
        return [name for name, value in unused.items() if value]


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build :class:`Settings` from ``env`` (default: ``os.environ``).

    Raises:
        ValueError: A variable is set to something that cannot be used safely.
    """
    env = os.environ if env is None else env
    defaults = Settings()

    environment = env_choice(env, "CAREERCLOUD_ENV", defaults.environment, _ENVIRONMENTS)
    runner = env_choice(env, "CAREERCLOUD_RUNNER", defaults.runner, _RUNNERS)

    origins = env_list(env, "CAREERCLOUD_CORS_ORIGINS", defaults.cors_origins)
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

    storage = env_choice(env, "CAREERCLOUD_STORAGE", defaults.storage, ("memory", "postgres"))
    queue = env_choice(env, "CAREERCLOUD_QUEUE", defaults.queue, ("inline", "redis"))
    auth_mode = env_choice(env, "CAREERCLOUD_AUTH_MODE", defaults.auth_mode, ("supabase", "dev"))
    allow_remote = env_bool(env, "CAREERCLOUD_ALLOW_REMOTE_SERVICES", False)

    runnable_names = env_list(
        env, "CAREERCLOUD_RUNNABLE_TYPES", tuple(sorted(t.value for t in DEFAULT_RUNNABLE))
    )
    try:
        runnable = frozenset(JobType(name) for name in runnable_names)
    except ValueError as error:
        raise ValueError(f"CAREERCLOUD_RUNNABLE_TYPES: {error}") from error

    settings = Settings(
        environment=environment,
        cors_origins=origins,
        runner=runner,
        fake_step_seconds=step,
        max_concurrent_jobs=concurrent,
        storage=storage,
        queue=queue,
        queue_prefix=env.get("CAREERCLOUD_QUEUE_PREFIX", f"careercloud:{environment}").strip(),
        auth_mode=auth_mode,
        allow_remote_services=allow_remote,
        results_dir=env_path(env, "CAREERCLOUD_RESULTS_DIR", DEFAULT_RESULTS_DIR),
        max_attempts=env_int(env, "CAREERCLOUD_MAX_ATTEMPTS", defaults.max_attempts, minimum=1, maximum=10),
        max_active_jobs_per_user=env_int(
            env, "CAREERCLOUD_MAX_ACTIVE_JOBS_PER_USER", defaults.max_active_jobs_per_user, minimum=1, maximum=1000
        ),
        runnable_types=runnable,
        db_user_role=env.get("CAREERCLOUD_DB_USER_ROLE", defaults.db_user_role).strip() or defaults.db_user_role,
        db_pool_max=env_int(env, "CAREERCLOUD_DB_POOL_MAX", defaults.db_pool_max, minimum=1, maximum=100),
        database_url=env_optional(env, "CAREERCLOUD_DATABASE_URL"),
        redis_url=env_optional(env, "CAREERCLOUD_REDIS_URL"),
        supabase_url=(env_optional(env, "CAREERCLOUD_SUPABASE_URL") or "").rstrip("/") or None,
        supabase_anon_key=env_optional(env, "CAREERCLOUD_SUPABASE_ANON_KEY"),
        supabase_service_role_key=env_optional(env, "CAREERCLOUD_SUPABASE_SERVICE_ROLE_KEY"),
        supabase_jwt_secret=env_optional(env, "CAREERCLOUD_SUPABASE_JWT_SECRET"),
        supabase_jwt_audience=env.get("CAREERCLOUD_SUPABASE_JWT_AUDIENCE", "authenticated").strip() or "authenticated",
        dev_jwt_secret=env_optional(env, "CAREERCLOUD_DEV_JWT_SECRET"),
        dev_token_ttl_seconds=int(
            env_float(env, "CAREERCLOUD_DEV_TOKEN_TTL_SECONDS", 8 * 3600, minimum=60, maximum=7 * 86400)
        ),
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
        api_origin=(env_optional(env, "CAREERCLOUD_API_ORIGIN") or "").rstrip("/") or None,
        resource_registry=env_optional(env, "CAREERCLOUD_RESOURCE_REGISTRY"),
        rate_limit_per_minute=env_int(env, "CAREERCLOUD_RATE_LIMIT_PER_MINUTE", 120, minimum=1, maximum=100_000),
        job_create_per_hour=env_int(env, "CAREERCLOUD_JOB_CREATE_PER_HOUR", 30, minimum=1, maximum=100_000),
        trust_proxy=env_choice(env, "CAREERCLOUD_TRUST_PROXY", "none", ("none", "cloudflare")),
        log_format=env_choice(env, "CAREERCLOUD_LOG_FORMAT", "text", ("text", "json")),
        log_level=env_choice(env, "CAREERCLOUD_LOG_LEVEL", "info", ("debug", "info", "warning", "error")).upper(),
    )
    validate_settings(settings)
    for name in settings.unused_placeholders():
        log.warning("%s is set but has no effect with this configuration", name)
    return settings


def validate_settings(settings: Settings) -> None:
    """Refuse combinations that are unsafe, whatever their source."""
    # Production must be durable. Staging may run lighter, but never with dev auth.
    deployed = settings.environment == "production"
    if settings.auth_mode == "dev" and settings.environment not in ("development", "test"):
        raise ValueError("CAREERCLOUD_AUTH_MODE=dev is only allowed in development or test")
    if settings.auth_mode == "dev" and settings.dev_jwt_secret is not None and len(settings.dev_jwt_secret) < 32:
        raise ValueError("CAREERCLOUD_DEV_JWT_SECRET must be at least 32 characters")
    if settings.supabase_jwt_secret is not None and len(settings.supabase_jwt_secret) < 32:
        raise ValueError("CAREERCLOUD_SUPABASE_JWT_SECRET must be at least 32 characters")
    if settings.supabase_url is not None and not settings.supabase_url.startswith("https://"):
        if not (settings.environment == "development" and settings.supabase_url.startswith("http://127.0.0.1")):
            raise ValueError("CAREERCLOUD_SUPABASE_URL must be an https:// URL")
    if deployed and settings.storage != "postgres":
        raise ValueError(f"{settings.environment} requires CAREERCLOUD_STORAGE=postgres")
    if deployed and settings.queue != "redis":
        raise ValueError(f"{settings.environment} requires CAREERCLOUD_QUEUE=redis")
    if settings.storage == "postgres" and not settings.database_url:
        raise ValueError("CAREERCLOUD_STORAGE=postgres needs CAREERCLOUD_DATABASE_URL")
    if settings.queue == "redis" and not settings.redis_url:
        raise ValueError("CAREERCLOUD_QUEUE=redis needs CAREERCLOUD_REDIS_URL")
    if settings.queue == "redis" and settings.storage != "postgres":
        raise ValueError("CAREERCLOUD_QUEUE=redis needs CAREERCLOUD_STORAGE=postgres (the worker is another process)")
    if not settings.queue_prefix or any(ch.isspace() for ch in settings.queue_prefix):
        raise ValueError("CAREERCLOUD_QUEUE_PREFIX must be non-empty and contain no spaces")
