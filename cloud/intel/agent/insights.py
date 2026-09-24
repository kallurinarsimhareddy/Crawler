"""Proactive AI: meaningful changes surfaced without waiting for a question.

    "5 companies just showed hiring spikes."
    "3 accounts have new ERP implementation hiring."
    "14 existing accounts have no IT leadership contact."
    "7 companies changed ATS/platform."

Each insight is a rule over data the platform already holds (signals, change
events, contacts) — deterministic, with evidence and a suggested Control Room
request to investigate it. Rules, thresholds and on/off live in workspace
settings (``settings.ai.proactive``) and can be changed by admins. Insights are
de-duplicated by fingerprint (rule + period + companies), so the worker's
periodic sweep never repeats one. Generating an insight never changes CRM data.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.context import ConflictError, Ctx, utcnow

__all__ = ["DEFAULTS", "InsightService"]

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "window_days": 7,
    "rules": {
        "hiring_spikes": {"enabled": True, "min_companies": 1},
        "erp_implementation": {"enabled": True, "min_companies": 1},
        "missing_it_leadership": {"enabled": True, "min_companies": 1},
        "ats_changes": {"enabled": True, "min_companies": 1},
        "technology_changes": {"enabled": True, "min_companies": 1, "contains": "erp"},
    },
}


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        out[key] = _merge(out[key], value) if isinstance(value, Mapping) and isinstance(out.get(key), Mapping) else value
    return out


class InsightService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    @property
    def store(self):
        return self.platform.store

    def settings(self, ctx: Ctx) -> Dict[str, Any]:
        info = None
        lookup = getattr(self.store, "system_membership", None)
        if callable(lookup):
            info = lookup(ctx.workspace_id)
        custom = ((info or {}).get("settings") or {}).get("ai", {}).get("proactive") or {}
        return _merge(DEFAULTS, custom)

    def configure(self, ctx: Ctx, proactive: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_admin()
        info = self.store.membership(ctx.user_id, ctx.workspace_id) if ctx.user_id else None
        settings = dict((info or {}).get("settings") or {})
        ai = dict(settings.get("ai") or {})
        ai["proactive"] = _merge(ai.get("proactive") or {}, proactive)
        settings["ai"] = ai
        self.store.update_workspace(ctx, settings=settings)
        return self.settings(ctx)

    def generate(self, ctx: Ctx, *, now=None) -> List[Dict[str, Any]]:
        config = self.settings(ctx)
        if not config.get("enabled"):
            return []
        now = now or utcnow()
        since = now - timedelta(days=int(config.get("window_days") or 7))
        period = now.strftime("%G-W%V")
        created = []
        rules = config.get("rules") or {}
        sys = ctx.as_system("agent")

        def emit(kind: str, company_ids: List[str], title: str, detail: str, request: str, severity: str,
                 evidence: List[Dict[str, Any]]) -> None:
            rule = rules.get(kind) or {}
            if not rule.get("enabled", True) or len(company_ids) < int(rule.get("min_companies") or 1):
                return
            digest = hashlib.sha256(",".join(sorted(company_ids)).encode()).hexdigest()[:16]
            fingerprint = f"{kind}:{period}:{digest}"
            if self.store.first(sys, "ai_insights", {"fingerprint": fingerprint}):
                return
            try:
                created.append(self.store.insert(sys, "ai_insights", {
                    "kind": kind, "title": title[:500], "detail": detail[:4000], "severity": severity,
                    "company_ids": company_ids[:500], "evidence": evidence[:50], "fingerprint": fingerprint,
                    "suggested_request": request[:2000]}))
            except ConflictError:
                pass

        signals = self.store.all(sys, "hiring_signals", {"status": "active", "detected_at__gte": since}, cap=50000)
        spikes = [s for s in signals if s["signal_type"] == "HIRING_SPIKE"]
        spike_ids = sorted({s["company_id"] for s in spikes})
        if spike_ids:
            emit("hiring_spikes", spike_ids, f"{len(spike_ids)} companies just showed hiring spikes",
                 "Hiring volume jumped against each company's own baseline in the last "
                 f"{config.get('window_days')} days.",
                 f"Show companies with hiring spikes in the last {config.get('window_days')} days",
                 "high" if len(spike_ids) >= 5 else "notable",
                 [{"company_id": s["company_id"], "signal_id": s["id"], "summary": s.get("summary")} for s in spikes])

        accounts = {c["id"]: c for c in self.store.all(sys, "companies", {"lifecycle": ["account", "customer"],
                                                                          "status": "active"}, cap=50000)}
        erp = [s for s in signals if s["signal_type"] == "PROJECT_IMPLEMENTATION" and s["company_id"] in accounts]
        erp_ids = sorted({s["company_id"] for s in erp})
        if erp_ids:
            emit("erp_implementation", erp_ids, f"{len(erp_ids)} accounts have new ERP implementation hiring",
                 "Existing accounts posted roles tied to an implementation or migration project.",
                 "Show my accounts with new ERP implementation hiring", "high",
                 [{"company_id": s["company_id"], "signal_id": s["id"], "summary": s.get("summary")} for s in erp])

        missing = []
        for cid, company in list(accounts.items())[:5000]:
            contacts = self.store.all(sys, "contacts", {"company_id": cid, "status": "active"}, cap=300)
            if not any((c.get("function") or "") == "it" or any(w in (c.get("title") or "").lower()
                                                                 for w in ("cio", "cto", "it ", "information technology",
                                                                           "technology"))
                       for c in contacts):
                missing.append(cid)
        if missing:
            emit("missing_it_leadership", missing, f"{len(missing)} existing accounts have no IT leadership contact",
                 "No contact with an IT function or CIO/CTO/IT title is recorded for these accounts.",
                 "Which accounts have no IT decision maker? Find missing IT leaders", "notable",
                 [{"company_id": cid, "name": accounts[cid]["name"]} for cid in missing[:50]])

        changes = self.store.all(sys, "change_events", {"detected_at__gte": since}, cap=50000)
        ats = sorted({c["company_id"] for c in changes if c["change_type"] == "ats_changed"})
        if ats:
            emit("ats_changes", ats, f"{len(ats)} companies changed ATS/platform",
                 "The careers platform behind these companies changed; recrawl to keep jobs current.",
                 "Show companies where the ATS changed", "info",
                 [{"company_id": c["company_id"], "summary": c.get("summary")} for c in changes if c["change_type"] == "ats_changed"])
        needle = str((rules.get("technology_changes") or {}).get("contains") or "").lower()
        tech = [c for c in changes if c["change_type"] in ("technology_added", "technology_removed")
                and (not needle or needle in f"{c.get('summary')} {c.get('after')} {c.get('before')}".lower())]
        tech_ids = sorted({c["company_id"] for c in tech})
        if tech_ids:
            emit("technology_changes", tech_ids, f"{len(tech_ids)} companies changed {needle.upper() or 'technology'}",
                 "Technology evidence was added or removed.",
                 f"Show companies where {needle.upper() or 'technology'} technology changed", "notable",
                 [{"company_id": c["company_id"], "summary": c.get("summary")} for c in tech])
        return created

    def list(self, ctx: Ctx, *, status: Optional[str] = None, limit: int = 50):
        filters = {"status": status} if status else {"status__ne": "dismissed"}
        return self.store.list(ctx, "ai_insights", filters, limit=limit)

    def mark(self, ctx: Ctx, insight_id: str, status: str) -> Dict[str, Any]:
        ctx.require_write()
        if status not in ("seen", "dismissed", "acted"):
            raise ConflictError("status must be seen, dismissed or acted")
        return self.store.update(ctx, "ai_insights", insight_id, {"status": status})
