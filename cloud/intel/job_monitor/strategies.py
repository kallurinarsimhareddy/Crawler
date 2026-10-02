"""How a monitor reads one listing page.

Two strategies share one contract (``first_url`` + ``read(url) -> PageResult``):

* ``site_profile`` — a deterministic :class:`~.profiles.SiteProfile` (WeAreDevelopers
  first). Plain HTTP, no browser, no AI.
* ``ai_scraper`` — any other site, read with the AI Scraper's deterministic page
  extractor (JSON-LD JobPosting, microdata, official ATS job-board APIs, job links)
  and its pagination finder. No AI call is made from a monitor run; a page whose
  jobs need AI or a browser yields no records and the run reports it.

Every request goes through the scraper's :class:`~cloud.intel.scraper.fetcher.PageFetcher`
over :class:`~cloud.intel.core.http.SafeFetcher`: SSRF checks on every hop, robots.txt,
per-host pacing, bounded retries of transient failures only. Refusals (CAPTCHA, WAF,
login, robots) are reported, never worked around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.job_monitor.profiles import ListingPage, SiteProfile, get_profile, profile_for_url
from cloud.intel.scraper.models import CrawlOptions, Outcome

__all__ = ["PageResult", "SiteProfileStrategy", "ScraperStrategy", "strategy_for", "make_fetcher"]


@dataclass
class PageResult:
    url: str
    outcome: str                      # scraper Outcome (OK, BLOCKED, CAPTCHA, ROBOTS, TIMEOUT, FAILED…)
    records: List[Dict[str, Any]] = field(default_factory=list)
    next_url: Optional[str] = None
    cards: int = 0
    http_status: int = 0
    reason: Optional[str] = None
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.OK


def make_fetcher(http: Any, *, max_requests: int, max_retries: int = 2, sleep: Any = None) -> Any:
    """A PageFetcher bounded for one monitor run (no browser)."""
    import time

    from cloud.intel.scraper.fetcher import PageFetcher

    options = CrawlOptions(max_retries=max_retries, max_backoff_s=60.0, domain_concurrency=1,
                           max_requests_per_domain=max(10, max_requests), browser=False, use_ai=False)
    return PageFetcher(http, None, options=options, sleep=sleep or time.sleep)


class SiteProfileStrategy:
    name = "site_profile"

    def __init__(self, profile: SiteProfile, fetcher: Any) -> None:
        self.profile = profile
        self.fetcher = fetcher
        self.newest_first = profile.newest_first
        self.source_name = profile.source_name

    def first_url(self, monitor: Mapping[str, Any]) -> str:
        return self.profile.listing_url(monitor["source_url"], monitor.get("filters") or {})

    def read(self, url: str) -> PageResult:
        page = self.fetcher.fetch(url, allow_render=False)
        if page.outcome != Outcome.OK:
            return PageResult(url, page.outcome, http_status=page.http_status, reason=page.reason)
        listing: ListingPage = self.profile.parse_listing(page.html, page.final_url or url)
        return PageResult(url, Outcome.OK, records=listing.records, next_url=listing.next_url, cards=listing.cards,
                          http_status=page.http_status, problems=listing.problems)


def _fv(job: Mapping[str, Any], key: str) -> Any:
    value = job.get(key)
    return getattr(value, "value", value)


class ScraperStrategy:
    """The AI Scraper's deterministic page extractor applied to a listing page."""

    name = "ai_scraper"
    newest_first = False

    def __init__(self, fetcher: Any, source_name: str) -> None:
        self.fetcher = fetcher
        self.source_name = source_name

    def first_url(self, monitor: Mapping[str, Any]) -> str:
        return monitor["source_url"]

    @staticmethod
    def _record(job: Mapping[str, Any]) -> Dict[str, Any]:
        skills = _fv(job, "skills") or []
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",")]
        remote = _fv(job, "remote_mode")
        return {
            "job_url": _fv(job, "job_url"),
            "title": _fv(job, "job_title"),
            "company_name": _fv(job, "company_name") or _fv(job, "hiring_organization"),
            "location": _fv(job, "location"),
            "experience_level": _fv(job, "seniority"),
            "salary_budget": _fv(job, "salary"),
            "keywords": list(skills),
            # Only an explicit statement counts; Hybrid / On-site are kept as stated.
            "remote": remote if remote in ("Remote", "Hybrid", "On-site") else None,
        }

    def read(self, url: str) -> PageResult:
        from cloud.intel.scraper.extractor import extract_page

        page = self.fetcher.fetch(url, allow_render=False)
        if page.outcome != Outcome.OK:
            return PageResult(url, page.outcome, http_status=page.http_status, reason=page.reason)
        facts = extract_page(page.html, page.final_url or url, fetch_json=self.fetcher.fetch_json)
        records = [self._record(job) for job in facts.jobs]
        next_url = None
        pagination = facts.pagination
        for link, kind, _ in getattr(pagination, "links", []) or []:
            if kind in ("next", "cursor", "load_more"):
                next_url = link
                break
        problems = list(facts.notes)
        if getattr(pagination, "needs_browser", False):
            problems.append("more jobs load only in a browser (load-more button / infinite scroll); "
                            "monitor runs stay on plain HTTP")
        return PageResult(url, Outcome.OK, records=records, next_url=next_url, cards=len(records),
                          http_status=page.http_status, problems=problems)


def strategy_for(monitor: Mapping[str, Any], fetcher: Any, platform: Any = None) -> Any:
    if monitor.get("strategy") == "jobspy":
        from cloud.intel.job_monitor.jobspy_source import JobSpyStrategy, enabled_boards

        scrape = platform.config.extra.get("jobspy_scrape") if platform is not None else None
        return JobSpyStrategy(monitor, enabled=enabled_boards(platform) if platform is not None else set(),
                              scrape=scrape)
    if monitor.get("strategy") in ("site_profile", "wad_turbo"):
        name = "wearedevelopers" if monitor.get("strategy") == "wad_turbo" else monitor.get("profile")
        profile = get_profile(name) or profile_for_url(monitor["source_url"])
        if profile is None:
            raise ValueError(f"no site profile {monitor.get('profile')!r} for {monitor['source_url']}")
        return SiteProfileStrategy(profile, fetcher)
    return ScraperStrategy(fetcher, monitor.get("source_name") or "")
