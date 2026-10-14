"""Job intelligence: one normalised ``job_postings`` table fed by every source.

Sources (the CareerCrawler crawl task, external job sources, imports, manual
JSON) hand :meth:`JobIntelService.ingest_postings` a list of dicts. Each posting
is validated, keyed by its canonical URL, classified (:mod:`.classify`), linked
to a company, flagged relevant to the workspace's campaigns, and upserted with
``first_seen_at`` / ``last_seen_at``.

**Closing jobs follows CareerCrawler's weekly-diff rule.** A posting is marked
closed only when its company is in ``crawled_company_ids`` — i.e. that
company's board was *read successfully* in this run (an empty board counts) —
and the posting was not seen. A failed crawl never closes anything: "we could
not read the board" is not "the jobs are gone".
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ValidationError, utcnow
from cloud.intel.core.normalize import blank, domain_of, normalize_website
from cloud.intel.jobs.classify import classify as _classify
from cloud.intel.technology.service import emit_best_effort

__all__ = ["JobIntelService", "canonical_job_url", "LONG_OPEN_DAYS"]

log = logging.getLogger(__name__)

#: A posting open at least this long is "long open" (and can feed LONG_OPEN_ROLE).
LONG_OPEN_DAYS = 45

_TRACKING = re.compile(r"^(utm_\w+|gh_src|gh_jid_src|source|src|ref|refid|referrer|trk|trackingid|lever-source|"
                       r"lever-origin|iis|iisn|mode|fbclid|gclid|mc_cid|mc_eid|_hsenc|_hsmi|codes|jobpipeline)$", re.I)


def canonical_job_url(url: str) -> Optional[str]:
    """Lower-case scheme/host, drop fragments, tracking parameters and trailing slashes.

    Parameters that identify the job (``gh_jid``, ``id``, ``jobId``…) are kept and sorted.
    """
    if blank(url):
        return None
    text = str(url).strip()
    if not re.match(r"^https?://", text, re.I):
        return None
    parts = urlsplit(text)
    if not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host + (f":{parts.port}" if parts.port and parts.port not in (80, 443) else "")
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not _TRACKING.match(k))
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit(("https", netloc, path, urlencode(query), ""))[:2048]


_REMOTE_LABELS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}
_STATUS_LABELS = {"open": "ACTIVE", "stale": "STALE"}
_CAMPAIGN_FIELDS = ("is_relevant", "relevance_reasons", "campaign_keys")


def _text(value: Any, limit: int) -> Optional[str]:
    if blank(value):
        return None
    return str(value).strip()[:limit]


class JobIntelService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    classify = staticmethod(_classify)

    # --- helpers ----------------------------------------------------------------

    def _campaign_rules(self, ctx: Ctx) -> List[Dict[str, Any]]:
        rules = []
        for c in self.store.all(ctx, "campaigns", {"status__in": ["draft", "active"]}, cap=200):
            rules.append({
                "key": c["key"],
                "keywords": [k.lower() for k in c.get("focus_keywords") or [] if k],
                "technologies": [t.lower() for t in c.get("technologies") or [] if t],
                "departments": [d.lower() for d in c.get("departments") or [] if d],
            })
        return rules

    @staticmethod
    def relevance(rules: List[Dict[str, Any]], title: str, description: str, classified: Mapping[str, Any]
                  ) -> Dict[str, List[str]]:
        """Which campaigns a posting is relevant to, and why (keyword / technology / department)."""
        haystack = f"{title}\n{description}".lower()
        techs = {t.lower() for t in classified.get("technologies") or []}
        keys: List[str] = []
        reasons: List[str] = []
        for rule in rules:
            why = []
            for kw in rule["keywords"]:
                if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", haystack):
                    why.append(f"keyword '{kw}'")
                    break
            for tech in rule["technologies"]:
                if any(tech == t or tech in t for t in techs):
                    why.append(f"technology '{tech}'")
                    break
            if classified.get("department", "").lower() in rule["departments"]:
                why.append(f"department '{classified['department']}'")
            if why:
                keys.append(rule["key"])
                reasons.append(f"{rule['key']}: " + ", ".join(why))
        return {"campaign_keys": keys, "reasons": reasons}

    def _resolve_company(self, ctx: Ctx, posting: Mapping[str, Any], cache: Dict[str, Optional[str]]
                         ) -> Optional[str]:
        domain = domain_of(posting.get("domain") or posting.get("website") or posting.get("company_website"))
        name = _text(posting.get("company_name") or posting.get("company"), 300)
        key = f"{domain}|{(name or '').lower()}"
        if key in cache:
            return cache[key]
        company_id = None
        if domain:
            row = self.store.first(ctx, "companies", {"domain": domain, "status__ne": "merged"})
            company_id = row["id"] if row else None
        if company_id is None and (domain or name):
            try:
                result = self.platform.service("dedupe").resolve(ctx, {"name": name, "domain": domain,
                                                                       "website": posting.get("website")})
                if result.get("outcome") in ("EXACT", "STRONG"):
                    company_id = result.get("company_id")
            except Exception:  # noqa: BLE001 - the resolver is another track's; linking can happen later
                log.debug("company resolver unavailable", exc_info=True)
        cache[key] = company_id
        return company_id

    # --- ingest -------------------------------------------------------------------

    def ingest_postings(self, ctx: Ctx, postings: List[Dict[str, Any]], *, source_kind: str, source_name: str,
                        company_id: Optional[str] = None, crawled_company_ids: Optional[Set[str]] = None
                        ) -> Dict[str, Any]:
        """Store postings from a crawl, an external source, the scraper or the API.

        Persistence is :meth:`JobMonitorService.upsert_batch` — the one write path of
        ``job_postings`` — as a live observation: the same URL identity (``job_url_key``,
        site-profile aware), NEW / CHANGED / REOPENED rules, ``job_posting_changes`` history,
        keyword-set relevance and the first source's attribution. This method adds what only
        these sources have: company linking by explicit id or domain, campaign relevance,
        classifier extras, technology evidence, company change events, automation events and
        closing a successfully crawled company's missing postings."""
        from cloud.intel.job_monitor.schema import normalize_job, parse_date

        ctx.require_write()
        now = utcnow()
        stats = {"received": len(postings), "inserted": 0, "updated": 0, "reopened": 0, "closed": 0,
                 "rejected": 0, "linked": 0, "unlinked": 0, "relevant": 0, "problems": []}
        rules = self._campaign_rules(ctx)
        cache: Dict[str, Optional[str]] = {}
        seen_by_company: Dict[str, Set[str]] = {}
        touched_companies: Set[str] = set()
        technology = self.platform.service("technology")
        monitors = self.platform.service("job_monitors")
        source_name = source_name[:200]

        records: List[Dict[str, Any]] = []
        extras: Dict[str, Dict[str, Any]] = {}
        texts: Dict[str, str] = {}
        for index, raw in enumerate(postings):
            title = _text(raw.get("title") or raw.get("job_title"), 500)
            url = raw.get("job_url") or raw.get("url")
            posted = raw.get("posted_at") or raw.get("posted_date")
            values, problems = normalize_job({
                "job_url": url, "title": title,
                "company_name": raw.get("company_name") or raw.get("company"),
                "location": raw.get("location"), "description": raw.get("description"),
                "experience_level": raw.get("experience_level"),
                "salary_budget": raw.get("salary_budget") or raw.get("salary"),
                "keywords": raw.get("keywords"),
                "remote": _REMOTE_LABELS.get(str(raw.get("workplace_type") or "").lower()),
                "date_posted": posted, "listing_date": raw.get("listing_date"),
                "search_term": raw.get("search_term"), "source_board": raw.get("source_board"),
            }, source=source_name, scraped_date=now.date())
            if values is None or not canonical_job_url(url or ""):
                stats["rejected"] += 1
                stats["problems"].append({"index": index, "problem": "missing title or a valid http(s) job URL"})
                continue
            if posted and parse_date(posted) is None:
                stats["problems"].append({"index": index, "problem": f"unreadable posted date {posted!r}"})
            cid = raw.get("company_id") or company_id or self._resolve_company(ctx, raw, cache)
            if cid:
                values["company_id"] = cid
                if not values.get("company_name"):
                    from cloud.intel.job_monitor.schema import content_hash

                    values["company_name"] = self.store.get(ctx, "companies", cid)["name"][:300]
                    values["content_hash"] = content_hash(values)
            if not values.get("company_name"):
                values["company_name"] = "(unknown company)"
            description = values.get("description") or ""
            c = _classify(title, description, values.get("location") or "")
            rel = self.relevance(rules, title, description, c)
            extra = {
                "domain": domain_of(raw.get("domain") or raw.get("website")),
                "external_id": _text(raw.get("external_id") or raw.get("job_id"), 200),
                "country": _text(raw.get("country"), 100),
                "employment_type": _text(raw.get("employment_type"), 60) or c["employment_type"],
                "department": _text(raw.get("department"), 100),
                "years_experience_min": c["years_experience_min"],
                "certifications": c["certifications"] or None,
                "industry": _text(raw.get("industry"), 200),
                "ats": _text(raw.get("ats") or raw.get("platform"), 100),
                "is_relevant": bool(rel["campaign_keys"]),
                "relevance_reasons": rel["reasons"],
                "campaign_keys": rel["campaign_keys"],
            }
            if c["skills"]:
                extra["skills"] = c["skills"][:50]
            records.append(values)
            extras[values["url_key"]] = {k: v for k, v in extra.items() if v is not None}
            texts[values["url_key"]] = f"{title}\n{description}"

        keys = list(extras)
        before = {row["url_key"]: row for row in self._rows_by_key(ctx, keys)}
        result = monitors.upsert_batch(ctx, records, observed_at=now, source_kind=source_kind,
                                       source_name=source_name, observed=True) if records else {"new_ids": []}
        new_ids = set(result.get("new_ids") or [])
        rows = {row["url_key"]: row for row in self._rows_by_key(ctx, keys)}
        patches = []
        for key, row in rows.items():
            # A job another source found first keeps its attribution and values: this source
            # only fills its blanks (campaign relevance is the workspace's and always updates).
            owner = row.get("source_name") == source_name
            patch = {k: v for k, v in extras[key].items() if row.get(k) != v and
                     (owner or k in _CAMPAIGN_FIELDS or row.get(k) in (None, "", []))}
            if patch:
                patches.append((row["id"], patch))
                row.update(patch)
        if patches:
            self.store.update_many(ctx, "job_postings", patches)
        stats["inserted"] = len(new_ids)
        stats["updated"] = sum(1 for key in rows if key in before)
        stats["reopened"] = int(result.get("reopened") or 0)

        for key, row in rows.items():
            title = row["title"]
            if row.get("is_relevant"):
                stats["relevant"] += 1
            if not row.get("company_id"):
                stats["unlinked"] += 1
                continue
            stats["linked"] += 1
            cid = row["company_id"]
            seen_by_company.setdefault(cid, set()).add(row["id"])
            touched_companies.add(cid)
            previous = before.get(key)
            if previous is not None and previous.get("status") in ("closed", "expired") and row["status"] == "open":
                self._change(ctx, cid, "new_job", {"job_posting_id": row["id"], "title": title, "reopened": True},
                             f"Reopened: {title}", source_name)
            if row["id"] in new_ids:
                self._change(ctx, cid, "new_job", {"job_posting_id": row["id"], "title": title,
                                                   "url": row["job_url"]}, f"New job: {title}", source_name)
                emit_best_effort(self.platform, ctx, "job_posted", row["id"],
                                 {"company_id": cid, "job_posting_id": row["id"], "title": title,
                                  "is_relevant": row["is_relevant"], "campaign_keys": row["campaign_keys"]})
                title_text, _, desc_text = texts[key].partition("\n")
                for tech in self._detected(title_text, desc_text):
                    try:
                        technology.record(ctx, cid, tech["technology"], source="job_posting",
                                          category=tech["category"], families=tech["families"],
                                          vendor=tech["vendor"], evidence_url=row["job_url"],
                                          evidence_text=tech["evidence_text"])
                    except Exception:  # noqa: BLE001 - evidence is best-effort, the posting is stored
                        log.exception("technology evidence failed for %s", row["id"])
            opened = row["first_seen_at"]
            if opened and now - opened >= timedelta(days=LONG_OPEN_DAYS) and row["is_relevant"]:
                emit_best_effort(self.platform, ctx, "long_open_job", f"{row['id']}:{LONG_OPEN_DAYS}d",
                                 {"company_id": cid, "job_posting_id": row["id"], "title": title,
                                  "days_open": (now - opened).days})

        # Close what a successful crawl no longer shows (never for failed crawls). Only this
        # source's own jobs: another source's job is that source's to close.
        for cid in crawled_company_ids or set():
            seen = seen_by_company.get(cid, set())
            closing = [job for job in self.store.all(ctx, "job_postings", {"company_id": cid,
                                                                           "status": ["open", "stale"],
                                                                           "source_name": source_name})
                       if job["id"] not in seen]
            if closing:
                self.store.update_many(ctx, "job_postings", [
                    (job["id"], {"status": "closed", "closed_at": now})
                    for job in closing])
                self.store.insert_many(ctx, "job_posting_changes", [
                    {"job_posting_id": job["id"], "monitor_id": None, "run_id": None, "change": "closed",
                     "changed_fields": ["status"], "before": {"status": _STATUS_LABELS.get(job["status"])},
                     "after": {"status": "CLOSED", "reason": "missing_from_board", "source": source_name},
                     "detected_at": now} for job in closing])
            for job in closing:
                stats["closed"] += 1
                self._change(ctx, cid, "job_closed", {"job_posting_id": job["id"], "title": job["title"]},
                             f"Closed: {job['title']}", source_name)
            touched_companies.add(cid)

        for cid in touched_companies:
            self.refresh_company(ctx, cid, crawled=cid in (crawled_company_ids or set()) and source_kind == "crawler")
        if stats["inserted"] or stats["closed"]:
            audit(self.store, ctx, "jobs.ingest", entity_type="job_postings",
                  summary=f"{source_name}: +{stats['inserted']} new, {stats['updated']} updated, "
                          f"{stats['closed']} closed")
        stats["problems"] = stats["problems"][:100]
        return stats

    def _rows_by_key(self, ctx: Ctx, keys: List[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            rows.extend(self.store.all(ctx, "job_postings", {"url_key": chunk}, cap=len(chunk) + 5))
        return rows

    @staticmethod
    def _detected(title: str, description: str) -> List[Dict[str, Any]]:
        from cloud.intel.technology.service import TechnologyService

        skip = {"ERP (generic)", "WMS (generic)", "SQL"}
        return [t for t in TechnologyService.detect_in_text(f"{title}\n{description}") if t["technology"] not in skip]

    def refresh_company(self, ctx: Ctx, company_id: str, *, crawled: bool = False) -> Dict[str, Any]:
        """Recompute the company's denormalised hiring summary from its open postings."""
        company = self.store.find(ctx, "companies", company_id)
        if company is None:
            return {}
        # open + stale: a STALE posting is still listed by its source (only old)
        open_jobs = self.store.all(ctx, "job_postings", {"company_id": company_id, "status": ["open", "stale"]},
                                   cap=5000)
        changes: Dict[str, Any] = {"hiring_count": len(open_jobs), "last_seen_at": utcnow()}
        platforms = [j["ats"] for j in open_jobs if j.get("ats")]
        if platforms:
            top = max(set(platforms), key=platforms.count)
            if top != company.get("ats"):
                changes["ats"] = top
                if company.get("ats"):
                    self._change(ctx, company_id, "ats_changed", {"ats": top}, f"ATS changed to {top}",
                                 "job_postings", before={"ats": company.get("ats")})
        if crawled:
            changes["last_crawled_at"] = utcnow()
        if company.get("first_seen_at") is None:
            changes["first_seen_at"] = utcnow()
        return self.store.update(ctx, "companies", company_id, changes)

    def _change(self, ctx: Ctx, company_id: str, change_type: str, after: Dict[str, Any], summary: str,
                source: str, before: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.platform.service("monitoring").record_change(ctx, company_id, change_type, before=before,
                                                              after=after, summary=summary, source=source)
        except Exception:  # noqa: BLE001 - change events are derived data
            log.exception("could not record %s for %s", change_type, company_id)

    # --- queries -------------------------------------------------------------------

    def jobs_for_company(self, ctx: Ctx, company_id: str, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        filters: Dict[str, Any] = {"company_id": company_id}
        if status:
            filters["status"] = status
        return self.store.all(ctx, "job_postings", filters, order="-first_seen_at", cap=5000)
