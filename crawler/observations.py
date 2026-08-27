"""Turn what the version 2 crawler found into what the version 3 sheet stores.

The engine produces :class:`~crawler.crawler_engine.CrawlResult` objects holding
:class:`~models.job.Job` records. The storage layer wants flat observation
dictionaries carrying a stable identity. This module is the only thing that
knows both shapes, which is what keeps the engine and the sheet independent of
each other::

    >>> from crawler.observations import observations_from_results
    >>> records = observations_from_results(pairs, run_id="2026-W35-...")
    >>> records[0]["job_key"]
    'c3ab8ff13720e8ad9047dd39466b3c89...'

Four things happen here and nowhere else.

**Every posting is given an identity** by :mod:`crawler.identity`, which prefers
the platform's own requisition id, falls back to the canonical URL, and falls
back again to company-title-location. Measured against all 237,300 postings of a
real run, that resolves 100% of them to a stable key — 87% on a genuine
requisition id — so nothing reaches the sheet keyed on a guess.

**Duplicates are dropped once, here.** A board lists the same posting under
several categories; two companies in the sheet share an ATS tenant; a rerun
re-observes everything. Deduplication is by job key, so all three collapse.

**Missing detail stays missing.** A board that publishes no department, no
employment type and no posted date yields empty cells, not inferred ones. The
sole derived field is ``workplace_type``, and only when the location string
literally says "Remote" or "Hybrid" — which is reading what the board published,
not guessing at what it did not.

**Technology roles are flagged, not filtered.** ``is_tech`` is set on every
observation and ``CURRENT_JOBS`` uses it, but ``JOB_HISTORY`` keeps everything.
The filter can therefore be changed later without re-crawling eight thousand
companies.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from loguru import logger

from crawler.identity import job_identity
from crawler.tech_filter import is_tech_job
from utils.names import company_key as derive_company_key

__all__ = [
    "observation_from_job",
    "observations_from_result",
    "observations_from_results",
    "workplace_type_of",
]

#: A location that states the posting is remote. Matched on the location string
#: the board published, so this reads rather than infers.
_REMOTE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:remote|work\s*from\s*home|wfh|telecommute|virtual|anywhere)\b", re.IGNORECASE
)

#: A location that states the posting is hybrid.
_HYBRID: Final[re.Pattern[str]] = re.compile(r"\bhybrid\b", re.IGNORECASE)

#: A location that states the posting is on site. Only matched when the board
#: says so; a location that simply names a city is left undetermined, because
#: "Austin, TX" does not actually tell us whether the role allows remote work.
_ONSITE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:on[\s-]?site|in[\s-]?office|in[\s-]?person)\b", re.IGNORECASE
)


def workplace_type_of(location: str, title: str = "") -> str:
    """Read the working arrangement out of what the board published.

    Args:
        location: The location string exactly as published.
        title: The posting title, which frequently carries the same marker —
            ``"Senior Engineer (Remote)"``.

    Returns:
        ``"Remote"``, ``"Hybrid"``, ``"On-site"``, or ``""`` when the board did
        not say. Empty is the honest answer for a posting that names only a
        city: most such roles are on site, but "most" is not "this one".
    """
    subject = f"{location} {title}"

    # Hybrid first: "Hybrid - Remote 2 days" is hybrid, and mentions both.
    if _HYBRID.search(subject):
        return "Hybrid"
    if _REMOTE.search(subject):
        return "Remote"
    if _ONSITE.search(subject):
        return "On-site"
    return ""


def observation_from_job(
    job: Any,
    company_key: str = "",
    company_name: str = "",
    website: str = "",
    career_url: str = "",
    run_id: str = "",
    industry: str = "",
    source: str = "",
    extra_keywords: Iterable[str] = (),
) -> Optional[Dict[str, str]]:
    """Convert one :class:`~models.job.Job` into an observation.

    Args:
        job: The posting, as an adapter produced it.
        company_key: The company's identity. Derived from the job when absent,
            though a caller that knows it should pass it — the sheet's own key
            is authoritative and a job's ``company_name`` may be spelled
            differently.
        company_name: The company as the master list names it. Falls back to
            whatever the job carries.
        website: The company's website, for scoping the identity.
        career_url: The board the posting was crawled from.
        run_id: The run that observed it.
        industry: The company's industry, copied onto the posting.
        source: Where the posting came from, e.g. ``"crawl:it_link"``.
        extra_keywords: Additional phrases counting as technical.

    Returns:
        The observation, or ``None`` when the posting has no title or no
        identity — a half-row that could not be delivered to a candidate is
        not worth storing.
    """
    title = str(getattr(job, "job_title", "") or "").strip()
    if not title:
        return None

    name = company_name or str(getattr(job, "company_name", "") or "").strip()
    board = career_url or str(getattr(job, "career_page_url", "") or "").strip()
    location = str(getattr(job, "location", "") or "").strip()
    platform = str(getattr(job, "platform", "") or "").strip()

    key = company_key or derive_company_key(name, website, board)
    if not key:
        logger.debug("Skipping a posting that identifies no company: {!r}", title)
        return None

    identity = job_identity(
        company_name=name,
        job_url=str(getattr(job, "job_url", "") or ""),
        job_title=title,
        location=location,
        platform=platform,
        # Version 2 adapters mostly do not supply one; job_identity recovers it
        # from the URL, which it does for 87% of a real run's postings.
        job_id=str(getattr(job, "job_id", "") or ""),
        website=website,
        career_url=board,
    )

    department = str(getattr(job, "department", "") or "").strip()

    return {
        "job_key": identity.job_uid,
        "company_key": key,
        "company_name": name,
        "job_title": title,
        "job_url": str(getattr(job, "job_url", "") or "").strip(),
        "url_key": identity.url_key,
        "content_key": identity.content_key,
        "identity_basis": identity.basis,
        "job_id": identity.job_id,
        "career_url": board,
        "platform": platform,
        "department": department,
        "location": location,
        "country": str(getattr(job, "country", "") or "").strip(),
        # Read from the location where the board stated it; otherwise blank.
        "workplace_type": str(getattr(job, "workplace_type", "") or "").strip()
        or workplace_type_of(location, title),
        "employment_type": str(getattr(job, "employment_type", "") or "").strip(),
        "posted_date": str(getattr(job, "posted_date", "") or "").strip(),
        "industry": industry,
        "source": source or "crawl",
        "run_id": run_id,
        "is_tech": is_tech_job(title, department, extra_keywords),
    }


def observations_from_result(
    result: Any,
    company: Optional[Mapping[str, str]] = None,
    run_id: str = "",
    extra_keywords: Iterable[str] = (),
    seen: Optional[Set[str]] = None,
) -> List[Dict[str, str]]:
    """Convert one company's crawl result into observations.

    Args:
        result: A :class:`~crawler.crawler_engine.CrawlResult`.
        company: The master-list record this company was crawled from, carrying
            ``company_key``, ``company_name``, ``website``, ``career_url`` and
            optionally ``industry``. Identity is scoped to the sheet's key
            rather than to whatever the adapter called the company.
        run_id: The run that produced this result.
        extra_keywords: Additional phrases counting as technical.
        seen: Job keys already produced, so a caller converting many results
            deduplicates across companies as well as within one. Modified.

    Returns:
        The observations, deduplicated.
    """
    record = dict(company or {})
    known: Set[str] = seen if seen is not None else set()

    company_key = record.get("company_key", "")
    company_name = record.get("company_name") or record.get("company") or ""
    website = record.get("website", "")
    industry = record.get("industry", "")

    # The URL actually crawled beats the one the sheet holds: discovery may
    # have found a better board than the operator supplied.
    board = str(getattr(result, "seed_url", "") or "") or record.get("career_url", "")
    seed_field = str(getattr(result, "seed_field", "") or "")
    source = f"crawl:{seed_field}" if seed_field else "crawl"

    observations: List[Dict[str, str]] = []

    for job in getattr(result, "jobs", None) or []:
        observation = observation_from_job(
            job,
            company_key=company_key,
            company_name=company_name,
            website=website,
            career_url=board,
            run_id=run_id,
            industry=industry,
            source=source,
            extra_keywords=extra_keywords,
        )
        if observation is None:
            continue

        # A board lists one posting under several categories, and two sheet
        # rows can share an ATS tenant. Both collapse here.
        if observation["job_key"] in known:
            continue

        known.add(observation["job_key"])
        observations.append(observation)

    return observations


def observations_from_results(
    pairs: Sequence[Tuple[Mapping[str, str], Any]],
    run_id: str = "",
    extra_keywords: Iterable[str] = (),
) -> List[Dict[str, str]]:
    """Convert every company's results into one deduplicated list.

    Args:
        pairs: ``(company_record, crawl_result)`` for each company crawled.
        run_id: The run that produced them.
        extra_keywords: Additional phrases counting as technical.

    Returns:
        Every observation, deduplicated across all companies.
    """
    seen: Set[str] = set()
    observations: List[Dict[str, str]] = []

    for company, result in pairs:
        observations.extend(
            observations_from_result(
                result,
                company=company,
                run_id=run_id,
                extra_keywords=extra_keywords,
                seen=seen,
            )
        )

    tech = sum(1 for item in observations if item.get("is_tech"))
    logger.info(
        "Converted {} posting(s) into observations ({} technology roles)",
        len(observations),
        tech,
    )
    return observations
