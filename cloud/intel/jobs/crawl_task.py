"""The ``crawl`` task: crawl workspace companies' career boards with CareerCrawler.

Params (one of):

* ``company_ids``: explicit companies;
* ``filters``: a store filter over ``companies`` (e.g. ``{"tags": "erp"}``);
* ``list_id``: every company in a list.

Optional: ``browser_fallback`` (default false), ``detect_signals`` (default true).

For each company, in order: crawl through :mod:`.careercrawler_bridge`, ingest
postings (``source_kind=crawler``; missing postings are closed only for boards
read successfully), record ATS / careers-URL changes, then run hiring-signal
detection. Pause and cancel are honoured between companies; a paused crawl
resumes from where it stopped.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional

from cloud.intel.core.context import Ctx
from cloud.intel.tasks.worker import PermanentTaskError, TaskPaused

__all__ = ["run_crawl", "CRAWL_ENGINE_FACTORY"]

log = logging.getLogger(__name__)

#: Tests (and alternative engines) set this to a zero-argument callable returning an engine.
CRAWL_ENGINE_FACTORY: Optional[Callable[[], Any]] = None
#: Tests set this to a fake ``getaddrinfo`` so no DNS query is made.
CRAWL_RESOLVER: Optional[Callable] = None


def _targets(platform: Any, ctx: Ctx, params: Mapping[str, Any]) -> List[Dict[str, Any]]:
    store = platform.store
    if params.get("company_ids"):
        rows = [store.find(ctx, "companies", cid) for cid in params["company_ids"]]
        return [r for r in rows if r]
    if params.get("list_id"):
        ids = [m["entity_id"] for m in store.all(ctx, "list_members", {"list_id": params["list_id"]}, cap=10000)
               if m["entity_type"] == "companies"]
        return [r for r in (store.find(ctx, "companies", cid) for cid in ids) if r]
    if params.get("filters"):
        return store.all(ctx, "companies", dict(params["filters"]), cap=10000)
    raise PermanentTaskError("a crawl needs company_ids, list_id or filters")


def run_crawl(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.jobs.careercrawler_bridge import crawl_companies

    params = task.get("params") or {}
    companies = [c for c in _targets(platform, ctx, params) if c.get("status") != "merged"]
    start = int(reporter.checkpoint.get("index", 0)) if reporter is not None else 0
    jobs = platform.service("jobs")
    signals = platform.service("signals") if params.get("detect_signals", True) else None
    totals: Dict[str, Any] = {"companies": len(companies), "crawled": 0, "failed": 0, "postings": 0, "new": 0,
                              "closed": 0, "signals": 0, "failures": []}
    remaining = companies[start:]
    crawls = crawl_companies(
        [{"id": c["id"], "name": c["name"], "website": c.get("website") or (f"https://{c['domain']}"
                                                                           if c.get("domain") else None),
          "careers_url": c.get("careers_url")} for c in remaining],
        browser_fallback=bool(params.get("browser_fallback", False)), engine_factory=CRAWL_ENGINE_FACTORY,
        resolver=CRAWL_RESOLVER, is_cancelled=(reporter.is_cancelled if reporter is not None else lambda: False))
    for offset, result in enumerate(crawls):
        index = start + offset
        company = remaining[offset]
        if reporter is not None:
            reporter.progress(f"Crawled {index + 1}/{len(companies)}: {company['name']}", done=index + 1,
                              total=len(companies))
        if result.read_ok:
            totals["crawled"] += 1
        else:
            totals["failed"] += 1
            totals["failures"].append({"company_id": company["id"], "error": (result.error or "")[:300]})
        stats = jobs.ingest_postings(ctx, result.postings, source_kind="crawler", source_name="careercrawler",
                                     company_id=company["id"],
                                     crawled_company_ids={company["id"]} if result.read_ok else set())
        totals["postings"] += len(result.postings)
        totals["new"] += stats["inserted"]
        totals["closed"] += stats["closed"]
        changes: Dict[str, Any] = {}
        if result.read_ok and result.platform and result.platform not in ("Unknown", "Generic HTML") \
                and result.platform != company.get("ats"):
            changes["ats"] = result.platform
        if result.read_ok and result.seed_url and result.discovered and result.seed_url != company.get("careers_url"):
            changes["careers_url"] = result.seed_url
        if changes:
            current = platform.store.get(ctx, "companies", company["id"])
            platform.store.update(ctx, "companies", company["id"], changes)
            monitoring = platform.service("monitoring")
            for field, change_type in (("ats", "ats_changed"), ("careers_url", "careers_url_changed")):
                if field in changes and current.get(field) and current.get(field) != changes[field]:
                    monitoring.record_change(ctx, company["id"], change_type, before={field: current.get(field)},
                                             after={field: changes[field]},
                                             summary=f"{field}: {current.get(field)} → {changes[field]}",
                                             source="careercrawler")
        if signals is not None and result.read_ok:
            try:
                totals["signals"] += len(signals.detect_for_company(ctx, company["id"]))
            except Exception:  # noqa: BLE001 - signals are derived; the crawl result is stored
                log.exception("signal detection failed for %s", company["id"])
        if reporter is not None and reporter.should_pause() and index + 1 < len(companies):
            crawls.close()
            raise TaskPaused({"index": index + 1})
    totals["failures"] = totals["failures"][:50]
    return totals
