"""Deterministic clocks and ids, so lifecycle assertions can compare timestamps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Callable

from cloud.shared.repository import InMemoryJobRepository
from cloud.shared.schemas import parse_job_request
from cloud.shared.service import JobService

START = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


class TickingClock:
    """Each call returns one second later than the last."""

    def __init__(self, start: datetime = START) -> None:
        self._next = start

    def __call__(self) -> datetime:
        now = self._next
        self._next = now + timedelta(seconds=1)
        return now


def sequential_ids(prefix: str = "job_") -> Callable[[], str]:
    numbers = count(1)
    return lambda: f"{prefix}{next(numbers):04d}"


def make_service() -> JobService:
    return JobService(
        InMemoryJobRepository(), clock=TickingClock(), id_factory=sequential_ids()
    )


def single(website: str = "https://example.com"):
    return parse_job_request({"type": "single_company", "website": website})
