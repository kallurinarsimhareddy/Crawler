"""Assemble the API.

``app`` is what uvicorn serves. :func:`create_app` is what tests call, with
their own settings, repository, runner, queue, storage and token verifier, so no
test depends on the environment of the machine running it::

    uvicorn cloud.api.main:app --reload --env-file cloud/api/.env

The backends are chosen by :class:`~cloud.api.settings.Settings` and nothing
else. The API process never imports the crawler: the real CareerCrawler runs
only in ``python -m cloud.worker``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, FrozenSet, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cloud.api.auth import DevTokenIssuer, SupabaseTokenVerifier, TokenVerifier
from cloud.api.deployment import deployment_problems, identity_of
from cloud.api.middleware import AccessLogMiddleware, RateLimiter, RateLimitMiddleware, SecurityHeadersMiddleware
from cloud.shared.environment import EnvironmentIsolationError, enforce_isolation
from cloud.api.routes import API_VERSION, router
from cloud.api.settings import Settings, load_settings
from cloud.db.connection import describe_url, resolve_database_url, resolve_redis_url
from cloud.shared.models import JobType
from cloud.shared.queue import JobQueue
from cloud.shared.repository import InMemoryJobRepository, JobRepository
from cloud.shared.service import JobService, RetryPolicy
from cloud.shared.storage import LocalFileStorage, ObjectStorage
from cloud.worker.dispatcher import InlineDispatcher, JobDispatcher, NullDispatcher, QueueDispatcher
from cloud.worker.executor import JobExecutor
from cloud.worker.fake_runner import FakeRunner
from cloud.worker.results import ResultWriter
from cloud.worker.runner import JobRunner

__all__ = ["app", "create_app"]

log = logging.getLogger(__name__)


def _build_repository(settings: Settings) -> JobRepository:
    if settings.storage == "memory":
        return InMemoryJobRepository()
    url = resolve_database_url(
        settings.database_url, environment=settings.environment, allow_remote=settings.allow_remote_services
    )
    from cloud.db.postgres import PostgresJobRepository

    log.info("jobs stored in PostgreSQL at %s", describe_url(url))
    return PostgresJobRepository.from_url(url, max_size=settings.db_pool_max, user_role=settings.db_user_role)


def _build_verifier(settings: Settings) -> Optional[TokenVerifier]:
    if settings.auth_mode == "dev":
        if not settings.dev_jwt_secret:
            log.warning("CAREERCLOUD_AUTH_MODE=dev without CAREERCLOUD_DEV_JWT_SECRET: every request will be refused")
            return None
        log.warning("DEVELOPMENT AUTH: anyone who can reach this API can sign in as any email")
        return DevTokenIssuer(settings.dev_jwt_secret, ttl_seconds=settings.dev_token_ttl_seconds)
    if not settings.supabase_url:
        log.warning("Supabase auth is not configured: every job request will get 503")
        return None
    return SupabaseTokenVerifier(
        settings.supabase_url,
        jwt_secret=settings.supabase_jwt_secret,
        audience=settings.supabase_jwt_audience,
    )


def _build_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "s3":
        from cloud.shared.s3_storage import S3Storage

        return S3Storage.from_settings(
            endpoint=settings.s3_endpoint or "",
            region=settings.s3_region or "",
            bucket=settings.s3_bucket or "",
            namespace=settings.results_namespace or settings.environment,
            access_key_id=settings.s3_access_key_id or "",
            secret_access_key=settings.s3_secret_access_key or "",
        )
    return LocalFileStorage(settings.results_dir)


def _verify_stamps(settings: Settings, queue: Optional[JobQueue], storage: ObjectStorage) -> None:
    from cloud.ops.stamps import verify_all

    problems = verify_all(
        settings.environment,
        database_url=settings.database_url,
        redis_client=getattr(queue, "_redis", None),
        queue_prefix=settings.queue_prefix,
        storage=storage,
    )
    if problems:
        raise EnvironmentIsolationError(settings.environment, problems)


def create_app(
    settings: Optional[Settings] = None,
    *,
    repository: Optional[JobRepository] = None,
    runner: Optional[JobRunner] = None,
    dispatcher: Optional[JobDispatcher] = None,
    queue: Optional[JobQueue] = None,
    storage: Optional[ObjectStorage] = None,
    token_verifier: Optional[TokenVerifier] = None,
    platform: Optional[object] = None,
) -> FastAPI:
    """Build an API instance.

    Args:
        settings: Defaults to :func:`load_settings` over the environment.
        repository: Defaults to what ``settings.storage`` selects.
        runner: Runs jobs in-process (``queue=inline``). Ignored if ``dispatcher``
            or ``queue`` is given.
        dispatcher: Overrides how created jobs are handed on.
        queue: Enqueue created jobs here for an out-of-process worker.
        storage: Where result files are read from (and, inline, written to).
        token_verifier: Overrides what ``settings.auth_mode`` selects.
        platform: The CareerCrawler platform (``cloud.intel``). Defaults to one
            built from the environment, sharing this API's database settings.
    """
    settings = settings or load_settings()
    if settings.deployed:
        problems = deployment_problems(settings)
        if problems:
            raise EnvironmentIsolationError(settings.environment, problems)
        enforce_isolation(identity_of(settings))
    repository = repository or _build_repository(settings)
    service = JobService(repository)
    storage = storage or _build_storage(settings)
    verifier = token_verifier if token_verifier is not None else _build_verifier(settings)

    runnable_types: FrozenSet[JobType] = frozenset(JobType)
    queue_name = "inline"
    if dispatcher is None:
        if queue is None and settings.queue == "redis":
            from cloud.shared.queue import RedisJobQueue

            url = resolve_redis_url(
                settings.redis_url, environment=settings.environment, allow_remote=settings.allow_remote_services
            )
            log.info("jobs enqueued on Redis at %s", describe_url(url))
            queue = RedisJobQueue.from_url(url, prefix=settings.queue_prefix)
        if queue is not None:
            dispatcher = QueueDispatcher(queue)
            runnable_types = settings.runnable_types
            queue_name = queue.name
        elif runner is None and settings.runner == "none":
            dispatcher = NullDispatcher()
            queue_name = "none"
        else:
            runner = runner or FakeRunner(step_seconds=settings.fake_step_seconds)
            runnable_types = runner.supported_types
            holder: dict = {}
            executor = JobExecutor(
                service,
                runner,
                retry_policy=RetryPolicy(max_attempts=settings.max_attempts, base_delay_seconds=5, max_delay_seconds=60),
                result_writer=ResultWriter(storage),
                runtime_root=settings.results_dir.parent / "runtime",
                on_requeue=lambda job_id, delay: holder["dispatcher"].dispatch_later(job_id, delay),
            )
            dispatcher = InlineDispatcher(executor, max_concurrent=settings.max_concurrent_jobs, name=runner.name)
            holder["dispatcher"] = dispatcher
    elif queue is not None:
        queue_name = queue.name
    else:
        queue_name = dispatcher.name

    if settings.deployed:
        _verify_stamps(settings, queue, storage)

    owns_platform = platform is None
    if platform is None:
        from cloud.intel.bootstrap import build_platform

        platform = build_platform(role="api")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            dispatcher.shutdown()
            repository.close()
            if owns_platform:
                platform.close()

    app = FastAPI(
        title="CareerCloud API",
        version=API_VERSION,
        description="Control plane for CareerCrawler jobs.",
        lifespan=lifespan,
        debug=False,
        # No interactive docs or schema on deployed environments.
        docs_url=None if settings.deployed else "/docs",
        redoc_url=None if settings.deployed else "/redoc",
        openapi_url=None if settings.deployed else "/openapi.json",
    )
    app.state.settings = settings
    app.state.job_service = service
    app.state.dispatcher = dispatcher
    app.state.storage = storage
    app.state.token_verifier = verifier
    app.state.runnable_types = runnable_types
    app.state.queue_name = queue_name
    # `/status` reads queue depth and worker presence straight off the queue.
    app.state.queue = queue

    app.state.job_limiter = RateLimiter(
        capacity=settings.job_create_per_hour, refill_per_second=settings.job_create_per_hour / 3600
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.deployed)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=RateLimiter(
            capacity=settings.rate_limit_per_minute, refill_per_second=settings.rate_limit_per_minute / 60
        ),
        trust_proxy=settings.trust_proxy,
    )
    app.add_middleware(AccessLogMiddleware, trust_proxy=settings.trust_proxy)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
        expose_headers=["Content-Disposition"],
    )
    app.include_router(router)

    from cloud.intel.api import mount as mount_platform

    mount_platform(app, platform)
    return app


def _asgi_app() -> FastAPI:
    """The app uvicorn serves: configure process logging first, then build."""
    settings = load_settings()
    if settings.log_format == "json":
        from cloud.shared.logs import configure_logging

        configure_logging(fmt="json", level=settings.log_level, service="careercloud-api", environment=settings.environment)
    return create_app(settings)


app = _asgi_app()
