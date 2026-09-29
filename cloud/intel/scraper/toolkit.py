"""The scraper as a clean internal tool interface.

Used by the Research Agent (:mod:`cloud.intel.research.tools`), the AI Control
Room (:mod:`cloud.intel.agent.tools`) and anything else in SANA GTM that needs
scraping. Every function takes the platform and the caller's workspace context,
so RLS, audit, limits, robots.txt and free-only AI all apply unchanged.

    plan = plan_scrape(platform, ctx, urls, "Get company name and open SAP jobs")
    run = create_scrape_run(platform, ctx, urls, instruction, options={...})   # saved, not started
    run = start_scrape(platform, ctx, run["id"])                               # queued for the worker
    pause_scrape / resume_scrape / cancel_scrape / retry_scrape / restart_scrape
    get_scrape_run · get_scrape_pages · get_scrape_results · export_scrape_results
    match_scrape_crm · propose_scrape_crm   (proposals only — applying needs a person's approval)
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Union

from cloud.intel.core.context import Ctx

__all__ = ["cancel_scrape", "create_scrape_run", "export_scrape_results", "get_scrape_pages", "get_scrape_results",
           "get_scrape_run", "match_scrape_crm", "pause_scrape", "plan_scrape", "preview_schema", "propose_scrape_crm",
           "restart_scrape", "resume_scrape", "retry_scrape", "run_scrape_now", "start_scrape"]

Urls = Union[str, Sequence[str], None]


def _svc(platform: Any) -> Any:
    return platform.service("scraper")


def preview_schema(platform: Any, ctx: Ctx, instruction: str) -> Dict[str, Any]:
    return _svc(platform).preview_schema(ctx, instruction)


def plan_scrape(platform: Any, ctx: Ctx, urls: Urls, instruction: str = "", **kw: Any) -> Dict[str, Any]:
    return _svc(platform).plan(ctx, urls, instruction, **kw)


def create_scrape_run(platform: Any, ctx: Ctx, urls: Urls, instruction: str = "", **kw: Any) -> Dict[str, Any]:
    """Save a run (inputs, schema, options) without starting it."""
    return _svc(platform).start(ctx, urls, instruction, enqueue=False, **kw)


def start_scrape(platform: Any, ctx: Ctx, run_or_urls: Any, instruction: str = "", **kw: Any) -> Dict[str, Any]:
    """Queue a saved run (by id), or create and queue a new one (from URLs)."""
    if isinstance(run_or_urls, str) and run_or_urls.startswith("sc_"):
        return _svc(platform).enqueue(ctx, run_or_urls)
    return _svc(platform).start(ctx, run_or_urls, instruction, **kw)


def run_scrape_now(platform: Any, ctx: Ctx, urls: Urls, instruction: str = "", **kw: Any) -> Dict[str, Any]:
    """Create a run and execute it in the calling thread (for callers already inside a
    background task, such as the research agent). Returns the finished run."""
    from cloud.intel.scraper.runner import run_scrape_inline

    run = create_scrape_run(platform, ctx, urls, instruction, **kw)
    run_scrape_inline(platform, ctx, run["id"])
    return _svc(platform).get(ctx, run["id"])


def pause_scrape(platform: Any, ctx: Ctx, run_id: str) -> Dict[str, Any]:
    return _svc(platform).pause(ctx, run_id)


def resume_scrape(platform: Any, ctx: Ctx, run_id: str) -> Dict[str, Any]:
    return _svc(platform).resume(ctx, run_id)


def cancel_scrape(platform: Any, ctx: Ctx, run_id: str) -> Dict[str, Any]:
    return _svc(platform).cancel(ctx, run_id)


def retry_scrape(platform: Any, ctx: Ctx, run_id: str) -> Dict[str, Any]:
    return _svc(platform).retry(ctx, run_id)


def restart_scrape(platform: Any, ctx: Ctx, run_id: str) -> Dict[str, Any]:
    return _svc(platform).restart(ctx, run_id)


def get_scrape_run(platform: Any, ctx: Ctx, run_id: str) -> Dict[str, Any]:
    """Status, progress and statistics (no bulky inputs)."""
    run = _svc(platform).get(ctx, run_id)
    stats = run["stats"]
    return {"id": run["id"], "status": run["status"], "instruction": run["instruction"], "schema": run["schema"],
            "error": run.get("error"), "progress": stats.get("progress") or {},
            "observability": stats.get("observability") or {}, "outcomes": stats.get("outcomes") or {},
            "records": stats.get("records"), "ai_note": stats.get("ai_note"),
            "files": sorted((stats.get("files") or {}).keys())}


def get_scrape_pages(platform: Any, ctx: Ctx, run_id: str, **kw: Any) -> Dict[str, Any]:
    return _svc(platform).pages(ctx, run_id, **kw)


def get_scrape_results(platform: Any, ctx: Ctx, run_id: str, view: str = "all", **kw: Any) -> Dict[str, Any]:
    return _svc(platform).records(ctx, run_id, view, **kw)


def export_scrape_results(platform: Any, ctx: Ctx, run_id: str, fmt: str = "xlsx", view: str = "all"
                          ) -> Optional[Dict[str, Any]]:
    """The stored export file's info (storage key, content type, file name)."""
    return _svc(platform).file(ctx, run_id, fmt, view)


def match_scrape_crm(platform: Any, ctx: Ctx, run_id: str) -> Dict[str, Any]:
    return _svc(platform).match_crm(ctx, run_id)


def propose_scrape_crm(platform: Any, ctx: Ctx, run_id: str, actions: Sequence[str] = ("company", "job"),
                       **kw: Any) -> Dict[str, Any]:
    return _svc(platform).propose(ctx, run_id, actions, **kw)


def summarize(run: Mapping[str, Any]) -> str:
    stats = run.get("stats") or {}
    return (f"{run.get('status')}: {stats.get('records', 0)} records from {stats.get('url_count', 0)} URLs"
            + (f" — AI: {stats['ai_note']}" if stats.get("ai_note") else ""))
