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

from cloud.shared.models import CompanyTarget, Job, JobProgress, JobStatus, JobType

__all__ = [
    "BulkCompaniesJobRequest",
    "CompanyInput",
    "DiscoveryJobRequest",
    "HealthResponse",
    "JobCreateRequest",
    "JobCreatedResponse",
    "JobListResponse",
    "JobResponse",
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
    dotted host, or carries credentials is refused with :class:`ValueError`.
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

    @classmethod
    def from_job(cls, job: Job) -> "JobResponse":
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
        )


class JobListResponse(BaseModel):
    jobs: List[JobResponse]
    total: int = Field(description="Jobs matching the filter, before limit/offset.")
    counts: Dict[JobStatus, int] = Field(
        description="Every job in the store, by status, regardless of the filter."
    )


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    runner: str
    storage: str
