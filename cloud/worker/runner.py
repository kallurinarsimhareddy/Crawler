"""The interface between a job and whatever actually runs it.

A runner knows how to *do* a job and nothing about where jobs are stored. It is
handed the job and a :class:`RunContext`, reports progress through the context,
checks the context for cancellation between units of work, and returns a
:class:`RunResult`. :class:`~cloud.worker.executor.JobExecutor` turns that
result into status changes.

The Phase 5B runner will adapt the existing engine — ``crawler.crawler_engine``
and its adapters — behind this interface. It will call the engine, not copy it:
the cloud never grows a second crawler.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Protocol

from cloud.shared.models import Job

__all__ = ["JobRunner", "RunContext", "RunOutcome", "RunResult"]


class RunOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunResult:
    """How a run ended.

    Attributes:
        outcome: Whether the work finished, failed, or stopped on request.
        error: Why it failed. Required when ``outcome`` is ``FAILED``.
        summary: Free-form counters for the job record, e.g. postings found.
    """

    outcome: RunOutcome
    error: Optional[str] = None
    summary: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome is RunOutcome.FAILED and not self.error:
            raise ValueError("a failed RunResult must say why")

    @classmethod
    def completed(cls, **summary: Any) -> "RunResult":
        return cls(RunOutcome.COMPLETED, summary=dict(summary))

    @classmethod
    def failed(cls, error: str) -> "RunResult":
        return cls(RunOutcome.FAILED, error=error)

    @classmethod
    def cancelled(cls) -> "RunResult":
        return cls(RunOutcome.CANCELLED)


class RunContext(Protocol):
    """What a runner may ask of the system running it."""

    def report_progress(
        self, completed: int, total: Optional[int] = None, message: Optional[str] = None
    ) -> None:
        """Record how far the run has got. Never raises."""

    def is_cancelled(self) -> bool:
        """Whether someone has asked this job to stop. Check between units of work."""


class JobRunner(ABC):
    """Does the work a job describes."""

    #: Short identifier reported by the health endpoint.
    name: str = "runner"

    @abstractmethod
    def run(self, job: Job, context: RunContext) -> RunResult:
        """Run ``job`` to the end, or until ``context.is_cancelled()``.

        May raise; the executor records an unexpected exception as a failure.
        """
