"""Assemble the API.

``app`` is what uvicorn serves. :func:`create_app` is what tests call, with
their own settings, repository and runner, so no test depends on the
environment of the machine running it::

    uvicorn cloud.api.main:app --reload --env-file cloud/api/.env
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cloud.api.routes import API_VERSION, router
from cloud.api.settings import Settings, load_settings
from cloud.shared.repository import InMemoryJobRepository, JobRepository
from cloud.shared.service import JobService
from cloud.worker.dispatcher import InlineDispatcher, JobDispatcher, NullDispatcher
from cloud.worker.executor import JobExecutor
from cloud.worker.fake_runner import FakeRunner
from cloud.worker.runner import JobRunner

__all__ = ["app", "create_app"]


def _default_dispatcher(settings: Settings, service: JobService, runner: Optional[JobRunner]) -> JobDispatcher:
    if runner is None and settings.runner == "none":
        return NullDispatcher()
    runner = runner or FakeRunner(step_seconds=settings.fake_step_seconds)
    return InlineDispatcher(
        JobExecutor(service, runner),
        max_concurrent=settings.max_concurrent_jobs,
        name=runner.name,
    )


def create_app(
    settings: Optional[Settings] = None,
    *,
    repository: Optional[JobRepository] = None,
    runner: Optional[JobRunner] = None,
    dispatcher: Optional[JobDispatcher] = None,
) -> FastAPI:
    """Build an API instance.

    Args:
        settings: Defaults to :func:`load_settings` over the environment.
        repository: Defaults to a fresh in-memory store.
        runner: Runs jobs in-process. Ignored if ``dispatcher`` is given.
        dispatcher: Overrides how created jobs are handed on.
    """
    settings = settings or load_settings()
    service = JobService(repository or InMemoryJobRepository())
    dispatcher = dispatcher or _default_dispatcher(settings, service, runner)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            dispatcher.shutdown()

    app = FastAPI(
        title="CareerCloud API",
        version=API_VERSION,
        description="Control plane for CareerCrawler jobs. Phase 5A: in-memory, simulated runs.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.job_service = service
    app.state.dispatcher = dispatcher

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )
    app.include_router(router)
    return app


app = create_app()
