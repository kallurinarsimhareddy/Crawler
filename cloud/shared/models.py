"""The job, and the only ways its status may change.

A job is immutable. Every change produces a new :class:`Job`, and a repository
stores it only if the status it replaces is still the one the caller read — see
:meth:`cloud.shared.repository.JobRepository.compare_and_set`. That is what lets
a cancel request and a finishing worker race without either silently undoing
the other, and it maps one-for-one onto ``UPDATE ... WHERE status = :expected``
when the store becomes PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Dict, FrozenSet, List, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CompanyTarget",
    "Job",
    "JobProgress",
    "JobStatus",
    "JobType",
    "TERMINAL_STATUSES",
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
ALLOWED_TRANSITIONS: Dict[JobStatus, FrozenSet[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
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
    """How far a running job has got.

    Deliberately coarse. Phase 5B will fill ``completed``/``total`` from the
    crawler's per-company results; until then only the fake runner reports it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    completed: int = Field(default=0, ge=0)
    total: Optional[int] = Field(default=None, ge=0)
    message: Optional[str] = None


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

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def target_label(self) -> str:
        """What the job is about, in a few words."""
        if self.type is JobType.WEEKLY_CRAWL:
            return "Weekly roster"
        if len(self.targets) == 1:
            return self.targets[0].label()
        if not self.targets:
            return "—"
        return f"{len(self.targets)} companies"
