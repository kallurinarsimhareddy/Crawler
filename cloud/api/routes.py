"""The ``/api/v1`` endpoints. Thin: validate, call the service, shape the reply."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from cloud.api.settings import Settings
from cloud.shared.models import JobStatus
from cloud.shared.schemas import (
    HealthResponse,
    JobCreateRequest,
    JobCreatedResponse,
    JobListResponse,
    JobResponse,
)
from cloud.shared.service import InvalidTransitionError, JobNotFoundError, JobService
from cloud.worker.dispatcher import JobDispatcher

__all__ = ["API_VERSION", "router"]

API_VERSION = "0.1.0"

router = APIRouter(prefix="/api/v1")


def get_service(request: Request) -> JobService:
    return request.app.state.job_service


def get_dispatcher(request: Request) -> JobDispatcher:
    return request.app.state.dispatcher


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(
    settings: Settings = Depends(get_settings),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
) -> HealthResponse:
    return HealthResponse(
        service="careercloud-api",
        version=API_VERSION,
        environment=settings.environment,
        runner=dispatcher.name,
        storage="memory",
    )


@router.post(
    "/jobs",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["jobs"],
)
def create_job(
    payload: JobCreateRequest,
    service: JobService = Depends(get_service),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
) -> JobCreatedResponse:
    job = service.create_job(payload)
    dispatcher.dispatch(job.job_id)
    # Report the status the job was created with. A fast in-process runner may
    # already have moved it on; the client polls GET /jobs/{id} for that.
    return JobCreatedResponse(job_id=job.job_id, status=job.status)


@router.get("/jobs", response_model=JobListResponse, tags=["jobs"])
def list_jobs(
    status_filter: Optional[JobStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: JobService = Depends(get_service),
) -> JobListResponse:
    jobs = service.list_jobs(status=status_filter, limit=limit, offset=offset)
    return JobListResponse(
        jobs=[JobResponse.from_job(job) for job in jobs],
        total=service.count_jobs(status=status_filter),
        counts=service.counts_by_status(),
    )


@router.get("/jobs/{job_id}", response_model=JobResponse, tags=["jobs"])
def get_job(job_id: str, service: JobService = Depends(get_service)) -> JobResponse:
    try:
        return JobResponse.from_job(service.get_job(job_id))
    except JobNotFoundError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from missing


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse, tags=["jobs"])
def cancel_job(job_id: str, service: JobService = Depends(get_service)) -> JobResponse:
    try:
        return JobResponse.from_job(service.cancel_job(job_id))
    except JobNotFoundError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from missing
    except InvalidTransitionError as refused:
        raise HTTPException(status.HTTP_409_CONFLICT, str(refused)) from refused
