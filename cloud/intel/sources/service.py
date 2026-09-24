"""Running external source searches for a workspace.

``SourceService.search(ctx, "ats_public", {"board_url": "https://boards.greenhouse.io/acme"})``
builds the adapter from the workspace's own (encrypted) connection, runs
search -> normalize -> dedupe, records usage, and hands the unified postings to
Track C's ``JobIntelService.ingest_postings`` for classification, company
matching and history. The ``careercrawler`` source instead submits a ``crawl``
task, because the production engine runs only on the worker.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Union

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.providers.base import PaidCallRefused
from cloud.intel.sources.adapters import ADAPTERS
from cloud.intel.sources.base import SourceError, SourceQuery, SourceUnavailable

__all__ = ["SourceService", "run_source_search_task"]

log = logging.getLogger(__name__)


class SourceService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store
        #: Injected by tests: the HTTP fetcher every adapter uses.
        self.fetcher = None

    def adapter(self, ctx: Ctx, name: str):
        return self.platform.service("providers").source_adapter(ctx, name, fetcher=self.fetcher)

    def list_sources(self, ctx: Ctx) -> List[Dict[str, Any]]:
        registry = self.platform.service("providers")
        connections = {c["provider"]: c for c in registry.list_connections(ctx, kind="source")}
        out = []
        for name in ADAPTERS:
            try:
                adapter = self.adapter(ctx, name)
                health = adapter.health()
            except Exception as error:  # noqa: BLE001 - one broken connection must not hide the rest
                adapter, health = ADAPTERS[name](), {"status": "error", "detail": str(error)[:300]}
            conn = connections.get(name, {})
            out.append({**adapter.describe(), "health": health, "connection_status": conn.get("status"),
                        "secret_hint": conn.get("secret_hint"), "last_checked_at": conn.get("last_checked_at")})
        return out

    def search(self, ctx: Ctx, source: str, query: Union[SourceQuery, Mapping[str, Any]], *,
               allow_paid: bool = False, ingest: bool = True, task_id: Optional[str] = None) -> Dict[str, Any]:
        ctx.require_write()
        if source not in ADAPTERS:
            raise ValidationError(f"unknown source {source!r}")
        query = query if isinstance(query, SourceQuery) else SourceQuery.from_mapping(query or {})
        adapter = self.adapter(ctx, source)
        if adapter.paid and not allow_paid:
            raise PaidCallRefused(f"{adapter.label} searches consume paid quota; start it with allow_paid")
        if source == "careercrawler":
            companies = query.extra.get("companies") or ([{"name": query.company, "website": query.domain or
                                                           query.board_url}] if (query.domain or query.board_url) else [])
            if not companies:
                raise ValidationError("the CareerCrawler source needs companies (name + website)")
            task = self.platform.tasks.submit(ctx, "crawl", {"companies": companies, "source": "careercrawler"})
            return {"source": source, "task_id": task["id"], "status": task["status"], "count": 0}

        started = time.monotonic()
        ledger = self.platform.service("credits")
        try:
            rows = adapter.run(query)
        except (SourceUnavailable, SourceError) as error:
            ledger.record_usage(ctx, source, "search", success=False, error=str(error), task_id=task_id,
                                latency_ms=(time.monotonic() - started) * 1000)
            raise
        ledger.record_usage(ctx, source, "search", units=len(rows), task_id=task_id,
                            latency_ms=(time.monotonic() - started) * 1000)
        stats: Optional[Dict[str, Any]] = None
        if ingest and rows:
            # A search scoped to a known company (e.g. its ATS board) links every posting to
            # it; board payloads name the board token, not a resolvable domain.
            company_id = query.extra.get("company_id") or None
            stats = self.platform.service("jobs").ingest_postings(ctx, rows, source_kind="external_source",
                                                                  source_name=source, company_id=company_id)
        audit(self.store, ctx, "source.search", summary=f"{source}: {len(rows)} posting(s)",
              changes={"query": query.as_dict(), "count": len(rows)})
        return {"source": source, "count": len(rows), "rows": rows[:200], "ingest": stats}


def run_source_search_task(platform: Any, ctx: Ctx, task: Dict[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError

    params = task["params"]
    service: SourceService = platform.service("sources")
    queries = params.get("queries") or [params.get("query") or {}]
    total, results = 0, []
    for index, query in enumerate(queries):
        if reporter.is_cancelled():
            break
        try:
            result = service.search(ctx, params["source"], query, allow_paid=bool(params.get("allow_paid")),
                                    ingest=params.get("ingest", True), task_id=task["id"])
        except (SourceUnavailable, PaidCallRefused, ValidationError) as error:
            raise PermanentTaskError(str(error)) from error
        total += result["count"]
        results.append({k: v for k, v in result.items() if k != "rows"})
        reporter.progress(f"{index + 1} of {len(queries)} queries, {total} postings", done=index + 1,
                          total=len(queries))
    return {"source": params.get("source"), "postings": total, "queries": results}
