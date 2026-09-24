"""The platform's one door into the CareerCrawler engine.

Like :mod:`cloud.worker.careercrawler_runner` (whose settings helpers it reuses
rather than copies), this is the only module under ``cloud/intel`` allowed to
import the crawler, and only the engine-facing modules in
:data:`ALLOWED_CRAWLER_MODULES`. It never imports ``store`` (the production
SQLite queue / ``state/crawler.db``), ``sheets``, ``crawler.weekly_run``,
``crawler.checkpoint`` or ``crawler.sync``; ``cloud/tests/test_isolation.py``
enforces that statically.

It is imported **lazily, inside the worker's crawl task only** — importing the
API (``cloud.api.main``) loads no crawler module, which the isolation test also
checks at runtime.

For each company it does what the cloud crawl runner does: a public-DNS check,
then :meth:`CrawlerEngine.crawl_company` with the same record shape the crawler
reads from its sheet (``company``, ``website``, ``career_url``). The engine picks
the seed, discovers the careers page, detects the ATS and runs the adapter;
none of that logic is duplicated here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from cloud.shared.urls import UnsafeTargetError, resolve_public_addresses

__all__ = ["ALLOWED_CRAWLER_MODULES", "CompanyCrawl", "crawl_companies"]

log = logging.getLogger(__name__)

#: Only these crawler modules may be imported by the platform. Checked by tests.
ALLOWED_CRAWLER_MODULES = frozenset({"config.settings", "crawler.crawler_engine", "utils.http"})

_POSTING_FIELDS = ("company_name", "job_title", "job_url", "location", "country", "career_page_url", "platform",
                   "department", "employment_type", "workplace_type", "posted_date", "job_id")


@dataclass
class CompanyCrawl:
    company_id: str
    #: The board was read: jobs found, or a genuinely empty board. Only then may
    #: missing postings be closed (the weekly-diff rule).
    read_ok: bool
    platform: Optional[str] = None
    outcome: Optional[str] = None
    error: Optional[str] = None
    seed_url: Optional[str] = None
    discovered: bool = False
    seconds: float = 0.0
    postings: List[Dict[str, Any]] = field(default_factory=list)


def _posting(job: Any) -> Dict[str, Any]:
    raw = {name: getattr(job, name, "") or "" for name in _POSTING_FIELDS}
    return {"title": raw["job_title"], "job_url": raw["job_url"], "company_name": raw["company_name"],
            "location": raw["location"], "country": raw["country"] or None, "ats": raw["platform"] or None,
            "department": raw["department"] or None, "employment_type": raw["employment_type"] or None,
            "workplace_type": (raw["workplace_type"] or "").lower() or None, "posted_at": raw["posted_date"] or None,
            "external_id": raw["job_id"] or None, "careers_url": raw["career_page_url"] or None}


def _default_engine() -> Any:
    from crawler.crawler_engine import CrawlerEngine

    return CrawlerEngine()


def _default_session() -> Any:
    from utils.http import build_session

    return build_session()


def crawl_companies(companies: Sequence[Mapping[str, Any]], *, runtime_root: Optional[Path] = None,
                    browser_fallback: bool = False, engine_factory: Optional[Callable[[], Any]] = None,
                    session_factory: Optional[Callable[[], Any]] = None, resolver: Optional[Callable] = None,
                    is_cancelled: Callable[[], bool] = lambda: False) -> Iterator[CompanyCrawl]:
    """Crawl companies one by one, yielding a :class:`CompanyCrawl` for each.

    ``companies`` are dicts with ``id``, ``name``, ``website`` and optionally
    ``careers_url``. ``engine_factory``/``session_factory`` are injectable so
    tests never touch the network; in production the crawler's own engine and
    HTTP session policy are used and its process-wide settings are pointed at
    the cloud runtime root (diagnostics off) for the duration of the crawl.
    """
    previous = None
    if engine_factory is None:
        from cloud.worker.careercrawler_runner import apply_crawler_settings
        from cloud.worker.workspace import DEFAULT_RUNTIME_ROOT

        previous = apply_crawler_settings(runtime_root or DEFAULT_RUNTIME_ROOT, browser_fallback=browser_fallback)
    engine = (engine_factory or _default_engine)()
    session = None
    try:
        session = (session_factory or (_default_session if engine_factory is None else (lambda: None)))()
        for company in companies:
            if is_cancelled():
                return
            yield _crawl_one(engine, session, company, resolver)
    finally:
        if session is not None and callable(getattr(session, "close", None)):
            session.close()
        if previous is not None:
            from cloud.worker.careercrawler_runner import restore_crawler_settings

            restore_crawler_settings(previous)


def _crawl_one(engine: Any, session: Any, company: Mapping[str, Any], resolver: Optional[Callable]) -> CompanyCrawl:
    website = company.get("website") or company.get("careers_url")
    if not website:
        return CompanyCrawl(company["id"], False, error="no website or careers URL to crawl")
    parts = urlsplit(website)
    try:
        if resolver is not None:
            resolve_public_addresses(parts.hostname or "", parts.port, resolver=resolver)
        else:
            resolve_public_addresses(parts.hostname or "", parts.port)
    except UnsafeTargetError as refused:
        return CompanyCrawl(company["id"], False, error=str(refused))
    record = {"company": company.get("name") or parts.hostname or "", "website": company.get("website") or ""}
    if company.get("careers_url"):
        record["career_url"] = company["careers_url"]
    started = time.monotonic()
    try:
        result = engine.crawl_company(record, session=session)
    except Exception as error:  # noqa: BLE001 - crawl_company contains adapter errors; belt and braces
        log.exception("engine raised on %s", company.get("name"))
        return CompanyCrawl(company["id"], False, error=f"{type(error).__name__}: {error}",
                            seconds=time.monotonic() - started)
    jobs = list(result.jobs)
    return CompanyCrawl(
        company_id=company["id"],
        read_ok=result.error is None,
        platform=getattr(result.platform, "value", str(result.platform) if result.platform else None),
        outcome=getattr(result.outcome, "value", None),
        error=result.error,
        seed_url=result.seed_url,
        discovered=bool(result.discovered),
        seconds=round(time.monotonic() - started, 2),
        postings=[_posting(job) for job in jobs],
    )
