"""Helpers shared by the adapters when assembling :class:`~models.job.Job` records.

Two things every adapter needs and none should reimplement: turning whatever the
platform returned into a normalised record, and removing the duplicates that
boards emit when one posting is filed under several categories or locations.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from loguru import logger

from models.job import Job
from utils.location import derive_country

__all__ = ["build_job", "dedupe"]


def build_job(
    company_name: str,
    title: str,
    job_url: str,
    location: str = "",
    country: str = "",
    career_page_url: str = "",
    platform: str = "",
) -> Optional[Job]:
    """Assemble one posting, or reject it as unusable.

    A posting with no title or no URL cannot be delivered to a candidate, so it
    is dropped rather than exported as a half-row.

    Args:
        company_name: Company as named in the input sheet.
        title: Posting title.
        job_url: Absolute URL of the posting.
        location: Location as the platform published it.
        country: Country, when the platform states one. Derived from
            ``location`` when left empty.
        career_page_url: Board the posting was found on.
        platform: Label of the ATS.

    Returns:
        The job, or ``None`` if it has no title or no URL.
    """
    clean_title = (title or "").strip()
    clean_url = (job_url or "").strip()

    if not clean_title or not clean_url:
        return None

    clean_location = (location or "").strip()

    return Job(
        company_name=company_name,
        job_title=clean_title,
        job_url=clean_url,
        location=clean_location,
        country=(country or "").strip() or derive_country(clean_location),
        career_page_url=career_page_url,
        platform=platform,
    )


def dedupe(jobs: Iterable[Optional[Job]]) -> List[Job]:
    """Drop repeated postings, keeping the first of each.

    Args:
        jobs: Jobs to filter. ``None`` entries — as returned by
            :func:`build_job` for unusable postings — are ignored.

    Returns:
        The distinct jobs, in the order they were seen.
    """
    seen: set = set()
    unique: List[Job] = []
    duplicates = 0

    for job in jobs:
        if job is None:
            continue
        if job.key in seen:
            duplicates += 1
            continue
        seen.add(job.key)
        unique.append(job)

    if duplicates:
        logger.debug("Dropped {} duplicate posting(s)", duplicates)

    return unique
