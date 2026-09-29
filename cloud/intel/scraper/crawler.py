"""One input URL, crawled deeply but boundedly.

    input page ─► (job request, no jobs here) discovery: ATS board / careers link / common path
               ─► listing page ─► pagination: next · numbered · cursor · load-more link  (≤ max_pages)
                                  load-more button / infinite scroll ─► browser (if enabled for the run)
               ─► still no jobs on a careers-like page ─► browser (if enabled) ─► AI (if allowed)
               ─► job detail pages (if follow_details)  ─► listing + detail merged by precedence
               ─► company fields still missing ─► one AI call on the input page (if allowed)

Every page is recorded (:class:`~cloud.intel.scraper.models.PageVisit`) with its
outcome — including pages refused (BLOCKED, CAPTCHA…), skipped because the run
already visited them, or not fetched because a limit was reached. Nothing is
fetched twice in one run (:class:`VisitedSet` is shared by every input). Limits:
pages per input, records/runtime/browser pages per run (:class:`RunBudget`),
requests and concurrency per domain (:class:`DomainLimiter`).
"""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set

from cloud.intel.scraper.detail import extract_detail, merge_job
from cloud.intel.scraper.discovery import rank_candidates
from cloud.intel.scraper.extractor import (PageFacts, ai_extract_jobs, ai_fill, build_records, extract_page,
                                           keyword_fields, mark_browser)
from cloud.intel.scraper.fetcher import PageFetcher
from cloud.intel.scraper.limits import RunBudget
from cloud.intel.scraper.models import (CrawlOptions, FetchedPage, FieldValue, Outcome, PageExtraction, PageVisit,
                                        merge_value)
from cloud.intel.scraper.normalizer import canonical_url

__all__ = ["Crawl", "VisitedSet", "url_key"]


def url_key(url: str) -> str:
    return hashlib.sha256((canonical_url(url) or url).lower().encode()).hexdigest()


class VisitedSet:
    """Canonical URLs a run has fetched (thread-safe). Pre-loaded on resume."""

    def __init__(self, keys: Iterable[str] = ()) -> None:
        self._keys: Set[str] = set(keys)
        self._lock = threading.Lock()

    def claim(self, url: str) -> bool:
        """``True`` if ``url`` was not visited yet (and now is)."""
        key = url_key(url)
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            return True

    def __contains__(self, url: str) -> bool:
        with self._lock:
            return url_key(url) in self._keys


class Crawl:
    def __init__(self, schema: Mapping[str, Any], fetcher: PageFetcher, *, options: Optional[CrawlOptions] = None,
                 ai: Any = None, budget: Optional[RunBudget] = None, visited: Optional[VisitedSet] = None,
                 on_stage: Callable[[str], Any] = lambda stage: None) -> None:
        self.schema = schema
        self.fields = list(schema["fields"])
        self.requested = {f["name"] for f in self.fields}
        self.want_jobs = schema.get("entity") == "job"
        self.fetcher = fetcher
        self.options = options or CrawlOptions()
        self.ai = ai
        self.budget = budget
        self.visited = visited or VisitedSet()
        self.on_stage = on_stage
        self.pages: List[PageVisit] = []
        self.problems: List[str] = []
        self.interrupted: Optional[str] = None
        self._limit_noted = False

    # --- fetching ---------------------------------------------------------------------------

    def _stop_reason(self) -> Optional[str]:
        if self.budget is not None:
            reason = self.budget.exhausted()
            if reason:
                return reason
        if len([p for p in self.pages if p.outcome not in (Outcome.SKIPPED, Outcome.LIMIT)]) >= self.options.max_pages:
            return f"the page limit for this URL ({self.options.max_pages}) was reached"
        return None

    def _visit(self, url: str, kind: str, *, depth: int = 0, page_no: Optional[int] = None,
               first: bool = False) -> Optional[FetchedPage]:
        """Fetch ``url`` once per run, within limits, recording the visit. ``None`` when not fetched."""
        now = datetime.now(timezone.utc)
        reason = self._stop_reason()
        if reason:
            if self.budget is not None and self.budget.stop.is_set():
                self.interrupted = reason
            if not self._limit_noted:
                self.problems.append(f"stopped: {reason}")
                self._limit_noted = True
            self.pages.append(PageVisit(url, url, Outcome.LIMIT, kind, depth, page_no, error=reason, attempts=0,
                                        fetched_at=now))
            return None
        if not self.visited.claim(url) and not first:
            self.pages.append(PageVisit(url, url, Outcome.SKIPPED, kind, depth, page_no, attempts=0,
                                        error="already visited in this run", fetched_at=now))
            return None
        self.on_stage("fetching" if kind in ("input", "discovery", "careers") else
                      "paginating" if kind == "listing" else "enriching")
        page = self.fetcher.fetch(url)
        self.pages.append(PageVisit(url, page.final_url or url, page.outcome, kind, depth, page_no, page.http_status,
                                    page.attempts, 0, page.rendered, page.browser_reason, page.browser_duration_ms,
                                    page.browser_outcome, page.reason, now))
        return page

    def _render(self, url: str, reason: str, *, kind: str, interact: bool = False, depth: int = 0
                ) -> Optional[FetchedPage]:
        if not self.options.browser or self._stop_reason():
            return None
        page = self.fetcher.render(url, reason=reason, interact=interact)
        if page is None:
            return None
        self.pages.append(PageVisit(url, page.final_url or url, page.outcome, kind, depth, None, page.http_status, 1,
                                    0, True, page.browser_reason, page.browser_duration_ms, page.browser_outcome,
                                    page.reason, datetime.now(timezone.utc)))
        return page

    def _extract(self, page: FetchedPage) -> PageFacts:
        self.on_stage("extracting")
        facts = extract_page(page.html, page.final_url, fetch_json=self.fetcher.fetch_json, want_jobs=self.want_jobs)
        if page.rendered:
            mark_browser(facts)
        self.problems.extend(n for n in facts.notes if n not in self.problems)
        if page.truncated:
            self.problems.append("page was larger than the size limit and was cut")
        return facts

    def _record_count(self, url: str, count: int) -> None:
        for visit in reversed(self.pages):
            if visit.final_url == url or visit.url == url:
                visit.records = count
                return

    # --- the crawl --------------------------------------------------------------------------

    def run(self, url: str) -> PageExtraction:
        started = datetime.now(timezone.utc)
        self.on_stage("planning")
        page = self._visit(url, "input", first=True)
        if page is None or page.outcome != Outcome.OK:
            outcome = page.outcome if page is not None else Outcome.LIMIT
            reason = (page.reason if page is not None else None) or "refused"
            suffix = "" if outcome in (Outcome.TIMEOUT, Outcome.FAILED, Outcome.NOT_FOUND, Outcome.LIMIT) \
                else "; not bypassed"
            return PageExtraction(url, page.final_url if page else url, outcome, pages=self._pages(),
                                  problems=[f"{outcome}: {reason}{suffix}"] + self.problems, fetched_at=started,
                                  stats=self._stats())
        facts = self._extract(page)
        company = dict(facts.company)
        job_facts: Optional[PageFacts] = facts if facts.jobs else None
        careers_like: Optional[PageFacts] = facts if facts.is_careers_page else None

        # 1. discovery: from a homepage to its careers page / ATS board
        need_ats = not self.want_jobs and "ats" in self.requested and "ats" not in company
        if (self.want_jobs and job_facts is None) or need_ats:
            checked = 0
            for candidate, _score, why in rank_candidates(page.final_url, facts.links, facts.detection, limit=3,
                                                          guess=self.options.discovery and self.want_jobs):
                sub = self._visit(candidate, "discovery", depth=1)
                if sub is None:
                    continue
                checked += 1
                if sub.outcome != Outcome.OK:
                    self.problems.append(f"careers page {candidate[:120]} ({why}): {sub.outcome} "
                                         f"({sub.reason or 'refused'})")
                    continue
                sub_facts = self._extract(sub)
                self._merge_company(company, sub_facts)
                if sub_facts.is_careers_page and careers_like is None:
                    careers_like = sub_facts
                if need_ats and "ats" in company:
                    break
                if sub_facts.jobs:
                    job_facts = sub_facts
                    break
            if self.want_jobs and checked > 1 and job_facts is None:
                self.problems.append(f"also checked {checked} candidate careers pages")

        # 2. pagination on the listing (an official ATS API already returned the whole board)
        if self.want_jobs and job_facts is not None and self.options.pagination and job_facts.job_method != "ats-api":
            self._paginate(job_facts)

        # 3. browser fallback when the listing clearly needs JavaScript
        if self.want_jobs and job_facts is None and careers_like is not None:
            rendered = self._render(careers_like.url, "required content missing: no job postings in the HTML",
                                    kind="careers", interact=True, depth=1)
            if rendered is not None and rendered.outcome == Outcome.OK:
                rendered_facts = self._extract(rendered)
                if rendered_facts.jobs:
                    job_facts = rendered_facts
                    self._paginate(job_facts)

        # 4. AI reads a careers page the rules could not
        ai_used = False
        if self.want_jobs and job_facts is None and self.ai is not None and self.ai.available:
            target = careers_like or facts
            ai_used = self.ai.call(lambda provider: ai_extract_jobs(target, self.fields, provider, self.problems),
                                   self.problems) or ai_used
            if target.jobs:
                job_facts = target
                if "careers_url" in self.requested and target is not facts:
                    company["careers_url"] = FieldValue(target.url, "url", 0.85, "job postings are listed on this page",
                                                        target.url)

        # 5. job detail pages
        if self.want_jobs and job_facts is not None and self.options.follow_details:
            self._details(job_facts)

        # 6. company fields still missing: one AI call on the input page
        page_fields = [f for f in self.fields if f.get("level") != "job" and f["name"] not in company
                       and f["name"] not in ("domain",) and not (f.get("type") == "boolean" and f.get("hint"))]
        keyword_fields(facts, self.fields, company)
        if page_fields and self.ai is not None and self.ai.available:
            record: Dict[str, FieldValue] = {}
            ai_used = self.ai.call(lambda provider: ai_fill(facts, record, page_fields, provider, self.problems),
                                   self.problems) or ai_used
            for name, fv in record.items():
                company[name] = merge_value(company.get(name), fv)

        if self.want_jobs and job_facts is None:
            self.problems.append("no job postings found on the page")
        records = build_records(job_facts or PageFacts(url=facts.url), self.schema, company=company)
        if self.want_jobs and not records:
            records = [{name: fv for name, fv in company.items() if name in self.requested}]
        if self.budget is not None and records:
            room = self.budget.add_records(len(records))
            if room < len(records):
                self.problems.append(f"the run's record limit was reached: {len(records) - room} records not kept")
                records = records[:room]
        has_value = any(record for record in records)
        text = (job_facts or facts).text
        return PageExtraction(url, page.final_url, Outcome.OK if has_value else Outcome.EMPTY, records=records,
                              pages=self._pages(), problems=self.problems, ai_used=ai_used, fetched_at=started,
                              page_text=text, stats=self._stats())

    # --- steps ------------------------------------------------------------------------------

    @staticmethod
    def _merge_company(base: Dict[str, FieldValue], sub: PageFacts) -> None:
        prefer = ("careers_url", "ats") if sub.is_careers_page else ()
        for name, fv in sub.company.items():
            if name == "website":
                base.setdefault(name, fv)
            elif name in prefer:
                base[name] = fv
            else:
                base[name] = merge_value(base.get(name), fv)

    def _paginate(self, listing: PageFacts) -> None:
        """Follow the listing's next pages; stop at the limits or when pages stop adding jobs."""
        known = {job["job_url"].value for job in listing.jobs if "job_url" in job}
        self._record_count(listing.url, len(listing.jobs))
        queue = list(listing.pagination.links) if listing.pagination else []
        needs_browser = bool(listing.pagination and listing.pagination.needs_browser)
        page_no, dry = 1, 0
        followed = 0
        while queue and dry < 2:
            if followed >= self.options.max_listing_pages:
                self.problems.append(f"listing page limit ({self.options.max_listing_pages}) reached; "
                                     f"{len(queue)} more listing pages not followed")
                break
            next_url, kind, number = queue.pop(0)
            followed += 1
            page_no += 1
            fetched = self._visit(next_url, "listing", depth=2, page_no=number or page_no)
            if fetched is None:
                if self._stop_reason():
                    break
                continue
            if fetched.outcome != Outcome.OK:
                self.problems.append(f"listing page {next_url[:120]}: {fetched.outcome}")
                continue
            facts = self._extract(fetched)
            added = 0
            for job in facts.jobs:
                key = job["job_url"].value if "job_url" in job else None
                if key and key in known:
                    continue
                if key:
                    known.add(key)
                listing.jobs.append(job)
                added += 1
            self._record_count(fetched.final_url, added)
            dry = dry + 1 if added == 0 else 0
            for link in (facts.pagination.links if facts.pagination else []):
                if link[0] not in self.visited and all(link[0] != q[0] for q in queue):
                    queue.append(link)
        if needs_browser and self.options.browser:
            why = "a load-more button" if listing.pagination.load_more_button else "an infinite-scroll listing"
            rendered = self._render(listing.url, f"pagination needs interaction: {why}", kind="listing",
                                    interact=True, depth=2)
            if rendered is not None and rendered.outcome == Outcome.OK:
                facts = self._extract(rendered)
                added = 0
                for job in facts.jobs:
                    key = job["job_url"].value if "job_url" in job else None
                    if key and key in known:
                        continue
                    if key:
                        known.add(key)
                    listing.jobs.append(job)
                    added += 1
                self._record_count(rendered.final_url, added)
        elif needs_browser:
            self.problems.append("the listing loads more jobs with a button or on scroll; enable browser "
                                 "rendering for this run to follow it")

    def _details(self, listing: PageFacts) -> None:
        done = 0
        for index, job in enumerate(listing.jobs):
            if done >= self.options.max_detail_pages:
                self.problems.append(f"detail page limit ({self.options.max_detail_pages}) reached; "
                                     f"{len(listing.jobs) - index} jobs not enriched")
                break
            if "job_url" not in job:
                continue
            target = str(job["job_url"].value)
            if target.rstrip("/") == listing.url.rstrip("/"):
                continue
            fetched = self._visit(target, "detail", depth=3)
            if fetched is None:
                if self._stop_reason():
                    break
                continue
            done += 1
            if fetched.outcome != Outcome.OK:
                continue
            self.on_stage("enriching")
            detail = extract_detail(fetched.html, fetched.final_url)
            if fetched.rendered:
                for fv in detail.values():
                    fv.browser = True
            listing.jobs[index] = merge_job(job, detail)
            self._record_count(fetched.final_url, 1)

    def _pages(self) -> List[Dict[str, Any]]:
        return [p.as_dict() for p in self.pages]

    def _stats(self) -> Dict[str, int]:
        fetched = [p for p in self.pages if p.outcome not in (Outcome.SKIPPED, Outcome.LIMIT)]
        return {"pages": len(fetched), "browser_pages": sum(1 for p in self.pages if p.browser_used),
                "detail_pages": sum(1 for p in fetched if p.kind == "detail"),
                "listing_pages": sum(1 for p in fetched if p.kind == "listing"),
                "requests": sum(p.attempts for p in fetched), "skipped": len(self.pages) - len(fetched)}
