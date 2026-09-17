"""The ``/api/v1`` endpoints. Thin: authenticate, call the service, shape the reply.

Every job endpoint depends on :func:`current_user` and passes the caller's id
as ``owner_id``. A job that belongs to someone else is indistinguishable from
one that does not exist: both are 404, so ids cannot be probed.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cloud.api.auth import AuthError, AuthUnavailableError, DevTokenIssuer, Principal, TokenVerifier
from cloud.api.settings import Settings
from cloud.shared.models import JobStatus, JobType
from cloud.shared.schemas import (
    DevSessionRequest,
    DevSessionResponse,
    EventListResponse,
    EventResponse,
    HealthResponse,
    JobCreateRequest,
    JobCreatedResponse,
    JobListResponse,
    JobResponse,
    MeResponse,
    ResultListResponse,
    ResultResponse,
    TargetListResponse,
    TargetResponse,
)
from cloud.shared.service import InvalidTransitionError, JobNotFoundError, JobService
from cloud.shared.storage import ObjectStorage
from cloud.worker.dispatcher import JobDispatcher

__all__ = ["API_VERSION", "router"]

API_VERSION = "0.2.0"

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")
_bearer = HTTPBearer(auto_error=False)


# --- dependencies ------------------------------------------------------------


def get_service(request: Request) -> JobService:
    return request.app.state.job_service


def get_dispatcher(request: Request) -> JobDispatcher:
    return request.app.state.dispatcher


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(request: Request) -> ObjectStorage:
    return request.app.state.storage


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": 'Bearer realm="careercloud"'}
    )


def current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Principal:
    verifier: Optional[TokenVerifier] = request.app.state.token_verifier
    if verifier is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is not configured")
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _unauthorized("sign in required")
    try:
        principal = verifier.verify(credentials.credentials)
        request.state.user_id = principal.user_id
        return principal
    except AuthError as error:
        raise _unauthorized(str(error)) from error
    except AuthUnavailableError as error:
        log.error("authentication unavailable: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is unavailable") from error


def _runnable(request: Request, job_type: JobType) -> bool:
    return job_type in request.app.state.runnable_types


def _not_found(error: JobNotFoundError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(error))


# --- system ------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(
    request: Request,
    settings: Settings = Depends(get_settings),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
) -> HealthResponse:
    verifier = request.app.state.token_verifier
    return HealthResponse(
        service="careercloud-api",
        version=API_VERSION,
        environment=settings.environment,
        runner=dispatcher.name,
        storage=request.app.state.job_service.repository.name,
        queue=request.app.state.queue_name,
        auth=verifier.mode if verifier is not None else "unconfigured",
    )


@router.get("/me", response_model=MeResponse, tags=["auth"])
def me(request: Request, user: Principal = Depends(current_user)) -> MeResponse:
    return MeResponse(user_id=user.user_id, email=user.email, auth_mode=request.app.state.token_verifier.mode)


@router.post("/auth/dev-session", response_model=DevSessionResponse, tags=["auth"], include_in_schema=False)
def dev_session(payload: DevSessionRequest, request: Request) -> DevSessionResponse:
    """Development only: sign in as any email. 404 unless ``CAREERCLOUD_AUTH_MODE=dev``."""
    issuer = request.app.state.token_verifier
    if not isinstance(issuer, DevTokenIssuer):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return DevSessionResponse(**issuer.issue(payload.email))


# --- jobs --------------------------------------------------------------------


@router.post(
    "/jobs",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["jobs"],
)
def create_job(
    payload: JobCreateRequest,
    request: Request,
    user: Principal = Depends(current_user),
    service: JobService = Depends(get_service),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
    settings: Settings = Depends(get_settings),
) -> JobCreatedResponse:
    allowed, wait = request.app.state.job_limiter.allow(f"user:{user.user_id}")
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "job creation limit reached; try again later",
            headers={"Retry-After": str(max(1, int(wait + 0.999)))},
        )
    if service.active_job_count(owner_id=user.user_id) >= settings.max_active_jobs_per_user:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"you already have {settings.max_active_jobs_per_user} queued or running jobs; "
            "wait for one to finish or cancel one",
        )
    job_type = JobType(payload.type)
    runnable = _runnable(request, job_type)
    job = service.create_job(
        payload,
        owner_id=user.user_id,
        max_attempts=settings.max_attempts,
        unrunnable_reason=None
        if runnable
        else f"{job_type.value.replace('_', ' ')} jobs are not supported by the cloud runner yet; this job will not start",
    )
    if runnable:
        dispatcher.dispatch(job.job_id)
    # Report the status the job was created with; the client polls for the rest.
    return JobCreatedResponse(job_id=job.job_id, status=job.status)


@router.get("/jobs", response_model=JobListResponse, tags=["jobs"])
def list_jobs(
    request: Request,
    status_filter: Optional[JobStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: Principal = Depends(current_user),
    service: JobService = Depends(get_service),
) -> JobListResponse:
    jobs = service.list_jobs(status=status_filter, limit=limit, offset=offset, owner_id=user.user_id)
    now = service.now()
    return JobListResponse(
        jobs=[JobResponse.from_job(job, runnable=_runnable(request, job.type), now=now) for job in jobs],
        total=service.count_jobs(status=status_filter, owner_id=user.user_id),
        counts=service.counts_by_status(owner_id=user.user_id),
    )


@router.get("/jobs/{job_id}", response_model=JobResponse, tags=["jobs"])
def get_job(
    job_id: str,
    request: Request,
    user: Principal = Depends(current_user),
    service: JobService = Depends(get_service),
) -> JobResponse:
    try:
        job = service.get_job(job_id, owner_id=user.user_id)
    except JobNotFoundError as missing:
        raise _not_found(missing) from missing
    return JobResponse.from_job(job, runnable=_runnable(request, job.type), now=service.now())


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse, tags=["jobs"])
def cancel_job(
    job_id: str,
    request: Request,
    user: Principal = Depends(current_user),
    service: JobService = Depends(get_service),
) -> JobResponse:
    try:
        job = service.request_cancel(job_id, owner_id=user.user_id)
    except JobNotFoundError as missing:
        raise _not_found(missing) from missing
    except InvalidTransitionError as refused:
        raise HTTPException(status.HTTP_409_CONFLICT, str(refused)) from refused
    return JobResponse.from_job(job, runnable=_runnable(request, job.type), now=service.now())


@router.get("/jobs/{job_id}/targets", response_model=TargetListResponse, tags=["jobs"])
def list_targets(
    job_id: str,
    user: Principal = Depends(current_user),
    service: JobService = Depends(get_service),
) -> TargetListResponse:
    try:
        targets = service.list_targets(job_id, owner_id=user.user_id)
    except JobNotFoundError as missing:
        raise _not_found(missing) from missing
    return TargetListResponse(targets=[TargetResponse.from_record(t) for t in targets])


@router.get("/jobs/{job_id}/events", response_model=EventListResponse, tags=["jobs"])
def list_events(
    job_id: str,
    user: Principal = Depends(current_user),
    service: JobService = Depends(get_service),
) -> EventListResponse:
    try:
        events = service.list_events(job_id, owner_id=user.user_id)
    except JobNotFoundError as missing:
        raise _not_found(missing) from missing
    return EventListResponse(events=[EventResponse.from_event(e) for e in events])


# --- results -----------------------------------------------------------------


@router.get("/jobs/{job_id}/results", response_model=ResultListResponse, tags=["results"])
def list_results(
    job_id: str,
    user: Principal = Depends(current_user),
    service: JobService = Depends(get_service),
) -> ResultListResponse:
    try:
        results = service.list_results(job_id, owner_id=user.user_id)
    except JobNotFoundError as missing:
        raise _not_found(missing) from missing
    return ResultListResponse(results=[ResultResponse.from_result(r) for r in results])


@router.get("/jobs/{job_id}/results/{result_id}/download", tags=["results"])
def download_result(
    job_id: str,
    result_id: str,
    user: Principal = Depends(current_user),
    service: JobService = Depends(get_service),
    storage: ObjectStorage = Depends(get_storage),
) -> StreamingResponse:
    try:
        result = service.get_result(job_id, result_id, owner_id=user.user_id)
    except JobNotFoundError as missing:
        raise _not_found(missing) from missing
    if result is None or (result.owner_id is not None and result.owner_id != user.user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "result not found")
    try:
        handle = storage.open(result.storage_key)
    except FileNotFoundError as missing:
        log.error("result %s is recorded but its file %s is missing", result.result_id, result.storage_key)
        raise HTTPException(status.HTTP_410_GONE, "result file is no longer available") from missing

    def chunks() -> Iterator[bytes]:
        with handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    return
                yield chunk

    return StreamingResponse(
        chunks(),
        media_type=result.content_type,
        headers={
            # filename is constrained to [A-Za-z0-9._-] by the schema and the database.
            "Content-Disposition": f'attachment; filename="{job_id}-{result.filename}"',
            "Content-Length": str(result.size_bytes),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
