"""The HTTP contract: what a client may send, and what it gets back.

Requests are a discriminated union on ``type``, so each job type declares only
the fields it accepts and a mistake is reported against the right one — a
``weekly_crawl`` that carries a ``website`` is refused rather than silently
ignored. Every model forbids unknown fields for the same reason.

Websites are normalised here, once, so the rest of the system never has to ask
whether ``Example.com``, ``https://example.com/`` and ``example.com`` are the
same company.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Dict, List, Literal, Optional, Union
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from cloud.shared.models import (
    CompanyTarget,
    Job,
    JobEvent,
    JobProgress,
    JobStatus,
    JobType,
    ResultFile,
    ResultKind,
    TargetRecord,
    TargetStatus,
)
from cloud.shared.urls import check_public_host

__all__ = [
    "BulkCompaniesJobRequest",
    "CompanyInput",
    "DiscoveryJobRequest",
    "DevSessionRequest",
    "DevSessionResponse",
    "EventListResponse",
    "EventResponse",
    "HealthResponse",
    "JobCreateRequest",
    "JobCreatedResponse",
    "JobListResponse",
    "JobResponse",
    "MeResponse",
    "ResultListResponse",
    "ResultResponse",
    "TargetListResponse",
    "TargetResponse",
    "MAX_BULK_COMPANIES",
    "SingleCompanyJobRequest",
    "WeeklyCrawlJobRequest",
    "normalise_website",
    "parse_job_request",
    "request_targets",
]

#: Upper bound on one bulk request. Large enough for a real roster (the weekly
#: sheet is ~12k companies, which is what ``weekly_crawl`` is for), small enough
#: that one request body cannot exhaust the API's memory.
MAX_BULK_COMPANIES = 5000

_MAX_WEBSITE_LENGTH = 2048
_MAX_NAME_LENGTH = 200


def normalise_website(value: str) -> str:
    """Return ``scheme://host[:port][/path]`` for a user-typed website.

    A missing scheme becomes ``https``. Anything that is not http(s), has no
    dotted host, carries credentials, or can only mean a local or private
    address (see :mod:`cloud.shared.urls`) is refused with :class:`ValueError`.
    Query strings and fragments are dropped: they identify a page, not a
    company.
    """
    text = value.strip()
    if not text:
        raise ValueError("website must not be empty")
    if len(text) > _MAX_WEBSITE_LENGTH:
        raise ValueError(f"website must be at most {_MAX_WEBSITE_LENGTH} characters")
    if any(ch.isspace() for ch in text):
        raise ValueError("website must not contain spaces")
    if "://" not in text:
        text = "https://" + text

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("website must be an http or https address")
    if parts.username or parts.password:
        raise ValueError("website must not contain credentials")
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as error:  # a malformed port, e.g. "example.com:abc"
        raise ValueError("website has an invalid port") from error
    if not host or "." not in host.strip("."):
        raise ValueError("website must include a domain, e.g. example.com")
    check_public_host(host, port)

    netloc = host if port is None else f"{host}:{port}"
    path = parts.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CompanyInput(_Strict):
    """A company as a client names it: a website, a name, or both."""

    website: Optional[str] = Field(default=None, examples=["https://example.com"])
    company_name: Optional[str] = Field(
        default=None, max_length=_MAX_NAME_LENGTH, examples=["Example Inc."]
    )

    @field_validator("website")
    @classmethod
    def _normalise_website(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else normalise_website(value)

    @field_validator("company_name")
    @classmethod
    def _blank_name_is_no_name(cls, value: Optional[str]) -> Optional[str]:
        return value or None

    @model_validator(mode="after")
    def _needs_something_to_crawl(self) -> "CompanyInput":
        if self.website is None and self.company_name is None:
            raise ValueError("provide a website or a company_name")
        return self

    def to_target(self) -> CompanyTarget:
        return CompanyTarget(website=self.website, company_name=self.company_name)


class SingleCompanyJobRequest(CompanyInput):
    """Crawl one company's job board."""

    type: Literal["single_company"]


class DiscoveryJobRequest(CompanyInput):
    """Find a company's careers page and platform, without crawling postings."""

    type: Literal["discovery"]


class BulkCompaniesJobRequest(_Strict):
    """Crawl a list of companies — typed, pasted or read from an uploaded file."""

    type: Literal["bulk_companies"]
    companies: List[CompanyInput] = Field(min_length=1, max_length=MAX_BULK_COMPANIES)


class WeeklyCrawlJobRequest(_Strict):
    """Run the scheduled roster crawl. Takes no target: the roster is the target."""

    type: Literal["weekly_crawl"]


JobCreateRequest = Annotated[
    Union[
        SingleCompanyJobRequest,
        BulkCompaniesJobRequest,
        WeeklyCrawlJobRequest,
        DiscoveryJobRequest,
    ],
    Field(discriminator="type"),
]

_REQUEST_ADAPTER: TypeAdapter[JobCreateRequest] = TypeAdapter(JobCreateRequest)


def parse_job_request(payload: object) -> JobCreateRequest:
    """Validate a raw payload outside FastAPI, e.g. from a queue message or a test."""
    return _REQUEST_ADAPTER.validate_python(payload)


def request_targets(request: JobCreateRequest) -> List[CompanyTarget]:
    """The companies a validated request names, in the order given."""
    if isinstance(request, BulkCompaniesJobRequest):
        return [company.to_target() for company in request.companies]
    if isinstance(request, WeeklyCrawlJobRequest):
        return []
    return [request.to_target()]


# --- responses ---------------------------------------------------------------


class JobCreatedResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    job_id: str
    type: JobType
    status: JobStatus
    target: str = Field(description="A short human-readable description of the target.")
    targets: List[CompanyTarget]
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    progress: JobProgress
    cancel_requested: bool = False
    attempts: int = 0
    max_attempts: int = 1
    elapsed_seconds: Optional[float] = Field(
        default=None, description="Seconds since started_at, up to completed_at if finished."
    )
    runnable: bool = Field(
        default=True,
        description="False when no runner in this deployment executes this job type yet.",
    )

    @classmethod
    def from_job(
        cls, job: Job, *, runnable: bool = True, now: Optional[datetime] = None
    ) -> "JobResponse":
        elapsed = None
        if job.started_at is not None:
            end = job.completed_at or now
            if end is not None:
                elapsed = max(0.0, (end - job.started_at).total_seconds())
        return cls(
            job_id=job.job_id,
            type=job.type,
            status=job.status,
            target=job.target_label(),
            targets=job.targets,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            error=job.error,
            progress=job.progress,
            cancel_requested=job.cancel_requested,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            elapsed_seconds=elapsed,
            runnable=runnable,
        )


class JobListResponse(BaseModel):
    jobs: List[JobResponse]
    total: int = Field(description="Jobs matching the filter, before limit/offset.")
    counts: Dict[JobStatus, int] = Field(
        description="Every job visible to the caller, by status, regardless of the filter."
    )


class TargetResponse(BaseModel):
    position: int
    website: Optional[str]
    company_name: Optional[str]
    status: TargetStatus
    platform: Optional[str]
    outcome: Optional[str]
    jobs_found: int
    error: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    @classmethod
    def from_record(cls, record: TargetRecord) -> "TargetResponse":
        return cls(**record.model_dump(exclude={"job_id"}))


class TargetListResponse(BaseModel):
    targets: List[TargetResponse]


class EventResponse(BaseModel):
    kind: str
    created_at: datetime
    attempt: Optional[int]
    message: Optional[str]

    @classmethod
    def from_event(cls, event: JobEvent) -> "EventResponse":
        return cls(
            kind=event.kind,
            created_at=event.created_at,
            attempt=event.attempt,
            message=event.message,
        )


class EventListResponse(BaseModel):
    events: List[EventResponse]


class ResultResponse(BaseModel):
    result_id: str
    kind: ResultKind
    filename: str
    content_type: str
    size_bytes: int
    row_count: Optional[int]
    created_at: datetime
    download_url: str

    @classmethod
    def from_result(cls, result: ResultFile) -> "ResultResponse":
        return cls(
            result_id=result.result_id,
            kind=result.kind,
            filename=result.filename,
            content_type=result.content_type,
            size_bytes=result.size_bytes,
            row_count=result.row_count,
            created_at=result.created_at,
            download_url=f"/api/v1/jobs/{result.job_id}/results/{result.result_id}/download",
        )


class ResultListResponse(BaseModel):
    results: List[ResultResponse]


class MeResponse(BaseModel):
    user_id: str
    email: Optional[str] = None
    auth_mode: str


class DevSessionRequest(_Strict):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")


class DevSessionResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user_id: str
    email: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    runner: str
    storage: str
    queue: Optional[str] = None
    auth: Optional[str] = None
