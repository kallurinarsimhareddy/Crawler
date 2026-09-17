"""The interface between a job and whatever actually runs it.

A runner knows how to *do* a job and nothing about where jobs are stored. It is
handed the job and a :class:`RunContext`, reports progress through the context,
checks the context for cancellation between units of work, and returns a
:class:`RunResult`. :class:`~cloud.worker.executor.JobExecutor` turns that
result into status changes and result files.

:class:`~cloud.worker.careercrawler_runner.CareerCrawlerRunner` adapts the
existing crawler engine behind this interface. It calls the engine, it does not
copy it: the cloud never grows a second crawler.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Protocol

from cloud.shared.models import Job, JobType, ResultKind

if TYPE_CHECKING:  # pragma: no cover
    from cloud.worker.workspace import JobWorkspace

__all__ = ["Artifact", "JobRunner", "RunContext", "RunOutcome", "RunResult"]


class RunOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Artifact:
    """A file the runner produced inside its workspace, to be kept as a result."""

    kind: ResultKind
    path: Path
    content_type: str
    row_count: Optional[int] = None


@dataclass(frozen=True)
class RunResult:
    """How a run ended.

    Attributes:
        outcome: Whether the work finished, failed, or stopped on request.
        error: Why it failed. Required when ``outcome`` is ``FAILED``.
        summary: Counters for the job record, e.g. postings found.
        retryable: Whether a failure is worth another attempt. A runner that
            *returns* a failure is saying it knows why (bad input, unsupported
            type) and defaults to ``False``; an exception is always retryable.
        postings: Normalised job postings, one dict per posting.
        posting_fields: Column order for ``postings``, so an empty result still
            exports a header.
        companies: One dict per company describing how it went.
        artifacts: Extra files to keep (e.g. an XLSX the crawler's exporter wrote).
    """

    outcome: RunOutcome
    error: Optional[str] = None
    summary: Dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    postings: List[Dict[str, Any]] = field(default_factory=list)
    posting_fields: List[str] = field(default_factory=list)
    companies: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.outcome is RunOutcome.FAILED and not self.error:
            raise ValueError("a failed RunResult must say why")

    @classmethod
    def completed(cls, **summary: Any) -> "RunResult":
        return cls(RunOutcome.COMPLETED, summary=dict(summary))

    @classmethod
    def failed(cls, error: str, *, retryable: bool = False) -> "RunResult":
        return cls(RunOutcome.FAILED, error=error, retryable=retryable)

    @classmethod
    def cancelled(cls) -> "RunResult":
        return cls(RunOutcome.CANCELLED)


class RunContext(Protocol):
    """What a runner may ask of the system running it.

    Only ``report_progress`` and ``is_cancelled`` existed in Phase 5A; a runner
    that uses nothing else still works. The rest are optional to *use* but
    always provided by :class:`~cloud.worker.executor.JobExecutor`.
    """

    #: Private scratch directories for this attempt, or ``None`` when the
    #: executor was built without a runtime root (Phase 5A behaviour).
    workspace: "Optional[JobWorkspace]"

    def report_progress(
        self, completed: int, total: Optional[int] = None, message: Optional[str] = None
    ) -> None:
        """Record how far the run has got. Never raises."""

    def is_cancelled(self) -> bool:
        """Whether the job should stop now: cancelled by its owner, reassigned
        after a lost lease, or its worker is shutting down."""

    def update(self, **progress: Any) -> None:
        """Merge fields into the job's progress (``failed``, ``jobs_found``,
        ``current_company``, ``current_phase``, ``message``…). Never raises."""

    def target_started(self, position: int) -> None:
        """Mark one company as running. Never raises."""

    def target_finished(self, position: int, **outcome: Any) -> None:
        """Record how one company went (``status``, ``platform``, ``outcome``,
        ``jobs_found``, ``error``). Never raises."""


class JobRunner(ABC):
    """Does the work a job describes."""

    #: Short identifier reported by the health endpoint.
    name: str = "runner"

    #: Job types this runner executes. Others are refused before they start.
    supported_types: FrozenSet[JobType] = frozenset(JobType)

    def supports(self, job_type: JobType) -> bool:
        return job_type in self.supported_types

    @abstractmethod
    def run(self, job: Job, context: RunContext) -> RunResult:
        """Run ``job`` to the end, or until ``context.is_cancelled()``.

        May raise; the executor records an unexpected exception as a retryable
        failure.
        """
