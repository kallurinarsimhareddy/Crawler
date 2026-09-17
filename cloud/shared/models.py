"""The job, and the only ways its status may change.

A job is immutable. Every change produces a new :class:`Job`, and a repository
stores it only if the job is still in the state the caller expected — see
:meth:`cloud.shared.repository.JobRepository.update_where`. That is what lets a
cancel request, a finishing worker and a stale-job reaper race without any of
them silently undoing another, and it maps one-for-one onto
``UPDATE ... WHERE status = :expected`` in PostgreSQL.

Phase 5B added ownership, leases and richer progress. Every new field has a
default, so a job built the Phase 5A way is still a valid job.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CompanyTarget",
    "Job",
    "JobEvent",
    "JobProgress",
    "JobStatus",
    "JobType",
    "ResultFile",
    "ResultKind",
    "TERMINAL_STATUSES",
    "TargetRecord",
    "TargetStatus",
    "can_transition",
]


class JobType(str, Enum):
    """What a job was asked to do."""

    SINGLE_COMPANY = "single_company"
    BULK_COMPANIES = "bulk_companies"
    WEEKLY_CRAWL = "weekly_crawl"
    DISCOVERY = "discovery"


class JobStatus(str, Enum):
    """Where a job is in its life."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Statuses a job never leaves.
TERMINAL_STATUSES: FrozenSet[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

#: Every legal move. Anything not listed — including a move to the same status,
#: and every move out of a terminal status — is refused.
#:
#: ``running -> queued`` exists for exactly two callers: a worker scheduling a
#: bounded retry, and the reaper returning a job whose worker stopped
#: heartbeating. Both are fenced by attempt count, so neither can loop forever.
ALLOWED_TRANSITIONS: Dict[JobStatus, FrozenSet[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


def can_transition(current: JobStatus, new: JobStatus) -> bool:
    """Whether a job in ``current`` may move to ``new``."""
    return new in ALLOWED_TRANSITIONS[current]


class CompanyTarget(BaseModel):
    """One company to crawl, identified by its website, its name, or both.

    ``website`` is stored normalised (scheme and host, lower-cased host) by the
    request schema; this model trusts what it is given.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    website: Optional[str] = None
    company_name: Optional[str] = None

    def label(self) -> str:
        """The shortest honest description of this target, for a table cell."""
        if self.company_name:
            return self.company_name
        if self.website:
            return self.website.split("://", 1)[-1]
        return "unknown company"


class JobProgress(BaseModel):
    """How far a job has got.

    ``completed`` and ``total`` count companies — the Phase 5A names, kept so
    existing clients keep working. ``failed`` counts the companies among
    ``completed`` that could not be read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    completed: int = Field(default=0, ge=0)
    total: Optional[int] = Field(default=None, ge=0)
    message: Optional[str] = None
    failed: int = Field(default=0, ge=0)
    jobs_found: int = Field(default=0, ge=0)
    current_company: Optional[str] = None
    current_phase: Optional[str] = None


class Job(BaseModel):
    """A request to crawl, and everything known about how it went."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    type: JobType
    status: JobStatus
    targets: List[CompanyTarget] = Field(default_factory=list)
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    progress: JobProgress = Field(default_factory=JobProgress)

    # --- Phase 5B ------------------------------------------------------------
    #: The Supabase user id (``sub``) that owns the job. ``None`` only for jobs
    #: built by Phase 5A code paths that predate ownership.
    owner_id: Optional[str] = None
    updated_at: Optional[datetime] = None
    #: Times a worker has claimed the job. Doubles as the fencing token: a
    #: worker may only write to the attempt it claimed.
    attempts: int = Field(default=0, ge=0)
    #: Claims allowed before the job fails for good. 1 means "never retry".
    max_attempts: int = Field(default=1, ge=1, le=10)
    worker_id: Optional[str] = None
    heartbeat_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    #: Set when someone asks a running job to stop; the worker honours it.
    cancel_requested_at: Optional[datetime] = None
    #: How many companies the job names. A job read in a list may carry only
    #: the first few ``targets``; this is always the full count.
    target_count: Optional[int] = Field(default=None, ge=0)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_requested_at is not None

    def target_label(self) -> str:
        """What the job is about, in a few words."""
        if self.type is JobType.WEEKLY_CRAWL:
            return "Weekly roster"
        count = self.target_count if self.target_count is not None else len(self.targets)
        if count == 1 and self.targets:
            return self.targets[0].label()
        if count == 0:
            return "—"
        return f"{count} companies"


class TargetStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TargetRecord(BaseModel):
    """How one company within a job went."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    position: int = Field(ge=0)
    website: Optional[str] = None
    company_name: Optional[str] = None
    status: TargetStatus = TargetStatus.PENDING
    platform: Optional[str] = None
    outcome: Optional[str] = None
    jobs_found: int = Field(default=0, ge=0)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class JobEvent(BaseModel):
    """Something that happened to a job, for its timeline and for debugging."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: int
    job_id: str
    kind: str
    created_at: datetime
    attempt: Optional[int] = None
    message: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class ResultKind(str, Enum):
    SUMMARY_JSON = "summary_json"
    JOBS_CSV = "jobs_csv"
    JOBS_XLSX = "jobs_xlsx"
    CRAWL_LOG = "crawl_log"


class ResultFile(BaseModel):
    """Metadata for one downloadable result. The bytes live in object storage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: str
    job_id: str
    owner_id: Optional[str] = None
    kind: ResultKind
    filename: str
    content_type: str
    storage_key: str
    size_bytes: int = Field(ge=0)
    sha256: str
    row_count: Optional[int] = Field(default=None, ge=0)
    created_at: datetime
