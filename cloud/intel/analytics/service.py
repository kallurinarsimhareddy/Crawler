"""Workspace analytics: the numbers behind the dashboard.

Everything is computed through the store with the caller's context, so the
same workspace isolation (and, in PostgreSQL, the same RLS) applies as for any
other read — a dashboard can never count another workspace's rows.

Counts use ``count``/``group_count`` (a single aggregate query each in
PostgreSQL); the only row scans are capped (pipeline value, daily series).
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.context import Ctx, ValidationError, utcnow

__all__ = ["AnalyticsService", "run_analytics_task", "TIMESERIES_ENTITIES"]

#: entity -> the timestamp column its daily series is bucketed on.
TIMESERIES_ENTITIES = {
    "companies": "created_at",
    "contacts": "created_at",
    "job_postings": "first_seen_at",
    "hiring_signals": "detected_at",
    "opportunities": "created_at",
    "discovery_candidates": "created_at",
    "message_events": "occurred_at",
    "research_runs": "created_at",
}

_SCAN_CAP = 20_000


def _clean(counts: Mapping[Any, int]) -> Dict[str, int]:
    return {("none" if k is None else str(k)): v for k, v in sorted(counts.items(), key=lambda kv: -kv[1])}


class AnalyticsService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    def _count(self, ctx: Ctx, entity: str, filters: Optional[Mapping[str, Any]] = None) -> int:
        return self.store.count(ctx, entity, filters)

    def _group(self, ctx: Ctx, entity: str, column: str, filters: Optional[Mapping[str, Any]] = None
               ) -> Dict[str, int]:
        return _clean(self.store.group_count(ctx, entity, column, filters))

    # --- sections -------------------------------------------------------------------

    def companies(self, ctx: Ctx) -> Dict[str, Any]:
        return {
            "total": self._count(ctx, "companies", {"status__ne": "merged"}),
            "by_lifecycle": self._group(ctx, "companies", "lifecycle"),
            "by_source_kind": self._group(ctx, "source_records", "source_kind", {"entity_type": "companies"}),
            "discovered": self._count(ctx, "discovery_candidates"),
            "discovery_by_status": self._group(ctx, "discovery_candidates", "status"),
            "matched": self._count(ctx, "discovery_candidates", {"status": "DUPLICATE"})
            + self._count(ctx, "import_rows", {"status": "duplicate"}),
            "merged_duplicates": self._count(ctx, "companies", {"status": "merged"}),
            "with_open_jobs": self._count(ctx, "companies", {"hiring_count__gt": 0}),
        }

    def contacts(self, ctx: Ctx) -> Dict[str, Any]:
        return {
            "total": self._count(ctx, "contacts"),
            "by_function": self._group(ctx, "contacts", "function"),
            "by_seniority": self._group(ctx, "contacts", "seniority"),
            "by_email_status": self._group(ctx, "contacts", "email_status"),
            "verified_emails": self._count(ctx, "contacts", {"email_status": "VALID"}),
            "unsubscribed": self._count(ctx, "contacts", {"unsubscribed": True}),
        }

    def jobs(self, ctx: Ctx) -> Dict[str, Any]:
        return {
            "discovered": self._count(ctx, "job_postings"),
            # ACTIVE + STALE: a stale job is still listed by its source (only old)
            "open": self._count(ctx, "job_postings", {"status": ["open", "stale"]}),
            "stale": self._count(ctx, "job_postings", {"status": "stale"}),
            # the relevance engine's HIGH / REVIEW jobs, or the older is_relevant flag
            "relevant": self._count(ctx, "job_postings", {"any_of": [
                {"relevance_class": ["HIGH", "REVIEW"]}, {"is_relevant": True}]}),
            "by_source": self._group(ctx, "job_postings", "source_name"),
            "by_workplace_type": self._group(ctx, "job_postings", "workplace_type"),
            "top_technologies": dict(list(self._group(ctx, "job_postings", "technologies",
                                                      {"status": ["open", "stale"]}).items())[:20]),
        }

    def signals(self, ctx: Ctx) -> Dict[str, Any]:
        return {
            "total": self._count(ctx, "hiring_signals"),
            "active": self._count(ctx, "hiring_signals", {"status": "active"}),
            "by_type": self._group(ctx, "hiring_signals", "signal_type", {"status": "active"}),
        }

    def pipeline(self, ctx: Ctx) -> Dict[str, Any]:
        stages = {s["id"]: s for s in self.store.all(ctx, "pipeline_stages", cap=500)}
        by_stage: Dict[str, Dict[str, Any]] = OrderedDict()
        for stage in sorted(stages.values(), key=lambda s: (s["pipeline_id"], s["position"])):
            by_stage[stage["id"]] = {"stage": stage["name"], "pipeline_id": stage["pipeline_id"], "count": 0,
                                     "value": 0.0}
        open_value = won_value = 0.0
        for opp in self.store.all(ctx, "opportunities", cap=_SCAN_CAP):
            amount = float(opp.get("amount") or 0)
            bucket = by_stage.setdefault(opp["stage_id"], {"stage": "(unknown)", "pipeline_id": opp["pipeline_id"],
                                                           "count": 0, "value": 0.0})
            bucket["count"] += 1
            bucket["value"] += amount
            if opp["status"] == "open":
                open_value += amount
            elif opp["status"] == "won":
                won_value += amount
        return {
            "opportunities": self._count(ctx, "opportunities"),
            "by_status": self._group(ctx, "opportunities", "status"),
            "by_stage": list(by_stage.values()),
            "open_pipeline_value": round(open_value, 2),
            "won_value": round(won_value, 2),
            "by_campaign": self._group(ctx, "opportunities", "campaign_id"),
        }

    def campaigns(self, ctx: Ctx) -> Dict[str, Any]:
        campaigns = self.store.all(ctx, "campaigns", cap=500)
        per_campaign = []
        for campaign in campaigns:
            events = self._group(ctx, "message_events", "event", {"campaign_id": campaign["id"]})
            sent = events.get("sent", 0)
            per_campaign.append({
                "campaign_id": campaign["id"], "key": campaign["key"], "name": campaign["name"],
                "status": campaign["status"], "sending_enabled": campaign["sending_enabled"],
                "opportunities": self._count(ctx, "opportunities", {"campaign_id": campaign["id"]}),
                "enrollments": self._count(ctx, "sequence_enrollments", {"campaign_id": campaign["id"]}),
                "events": events,
                "reply_rate": round(events.get("replied", 0) / sent, 4) if sent else None,
                "bounce_rate": round(events.get("bounced", 0) / sent, 4) if sent else None,
            })
        return {"total": len(campaigns), "campaigns": per_campaign,
                "message_events": self._group(ctx, "message_events", "event"),
                "enrollments_by_status": self._group(ctx, "sequence_enrollments", "status"),
                "suppressions": self._count(ctx, "suppressions")}

    def sources(self, ctx: Ctx) -> Dict[str, Any]:
        by_kind: Dict[str, Dict[str, Any]] = {}
        for kind in ("crawl", "discovery", "scraper", "enrichment", "validation", "source_search", "research"):
            statuses = self._group(ctx, "platform_tasks", "status", {"kind": kind})
            finished = statuses.get("completed", 0) + statuses.get("failed", 0)
            by_kind[kind] = {"statuses": statuses,
                             "success_rate": round(statuses.get("completed", 0) / finished, 4) if finished else None}
        providers: Dict[str, Dict[str, Any]] = {}
        for provider, total in self.store.group_count(ctx, "usage_events", "provider").items():
            ok = self._count(ctx, "usage_events", {"provider": provider, "success": True})
            providers[str(provider)] = {"calls": total, "succeeded": ok,
                                        "success_rate": round(ok / total, 4) if total else None}
        return {"tasks_by_kind": by_kind, "providers": providers,
                "job_sources": self._group(ctx, "job_postings", "source_name")}

    def credits(self, ctx: Ctx) -> Dict[str, Any]:
        accounts = []
        for account in self.store.all(ctx, "credit_accounts", cap=100):
            total = float(account["total_credits"] or 0)
            reserved = float(account["reserved_credits"] or 0)
            consumed = float(account["consumed_credits"] or 0)
            accounts.append({"provider": account["provider"], "total": total, "reserved": reserved,
                             "consumed": consumed, "remaining": round(total - reserved - consumed, 4),
                             "last_sync_at": account.get("last_sync_at")})
        return {"accounts": accounts, "ledger_entries": self._group(ctx, "credit_ledger", "entry_type")}

    def validation(self, ctx: Ctx) -> Dict[str, Any]:
        return {"cached_results": self._count(ctx, "email_validations"),
                "by_status": self._group(ctx, "email_validations", "status"),
                "by_provider": self._group(ctx, "email_validations", "provider")}

    def research(self, ctx: Ctx) -> Dict[str, Any]:
        return {"runs": self._count(ctx, "research_runs"), "by_status": self._group(ctx, "research_runs", "status"),
                "scrape_runs": self._group(ctx, "scrape_runs", "status")}

    def tasks(self, ctx: Ctx) -> Dict[str, Any]:
        return {"by_status": self._group(ctx, "platform_tasks", "status"),
                "by_kind": self._group(ctx, "platform_tasks", "kind")}

    # --- time series ---------------------------------------------------------------------

    def timeseries(self, ctx: Ctx, entity: str, days: int = 30, *, now: Optional[datetime] = None
                   ) -> Dict[str, Any]:
        if entity not in TIMESERIES_ENTITIES:
            raise ValidationError(f"timeseries entity must be one of {', '.join(TIMESERIES_ENTITIES)}")
        days = max(1, min(int(days), 366))
        column = TIMESERIES_ENTITIES[entity]
        now = now or utcnow()
        start_day = (now - timedelta(days=days - 1)).date()
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
        buckets: Dict[str, int] = OrderedDict((str(start_day + timedelta(days=i)), 0) for i in range(days))
        # Counted per day by the store (one GROUP BY), never by loading the rows.
        for key, count in self.store.count_by_day(ctx, entity, column, {f"{column}__gte": start}).items():
            if key in buckets:
                buckets[key] += count
        return {"entity": entity, "days": days, "column": column,
                "points": [{"date": d, "count": c} for d, c in buckets.items()],
                "truncated": False}

    # --- everything ---------------------------------------------------------------------------

    def dashboard(self, ctx: Ctx) -> Dict[str, Any]:
        return {
            "generated_at": utcnow().isoformat(),
            "workspace_id": ctx.workspace_id,
            "companies": self.companies(ctx),
            "contacts": self.contacts(ctx),
            "jobs": self.jobs(ctx),
            "signals": self.signals(ctx),
            "pipeline": self.pipeline(ctx),
            "campaigns": self.campaigns(ctx),
            "sources": self.sources(ctx),
            "credits": self.credits(ctx),
            "validation": self.validation(ctx),
            "research": self.research(ctx),
            "tasks": self.tasks(ctx),
            "series": {name: self.timeseries(ctx, name, 30)["points"]
                       for name in ("companies", "job_postings", "hiring_signals")},
        }


def run_analytics_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Precompute the dashboard into the task's result (a snapshot)."""
    reporter.progress("Computing analytics")
    from cloud.intel.core.audit import _jsonable

    return {"dashboard": _jsonable(platform.service("analytics").dashboard(ctx))}
