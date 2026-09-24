"""The research agent's structured tools.

Each tool has a JSON schema for its parameters (so an AI planner can call it
through tool use, and every call is validated) and a Python implementation that
works on a shared :class:`ResearchState`. Tools reach other tracks only through
their service contracts (``cloud/intel/CONTRACTS.md``); when a track's service
is unavailable the tool degrades to what the store alone can answer and says so
in its step report — it never pretends.

Tools that would change CRM data (lists, opportunities, campaign assignment)
are *proposal* tools: during a run they only record what they would do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Set

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.core.normalize import normalize_country
from cloud.intel.research.intent import INDUSTRIES, TECHNOLOGIES

__all__ = ["TOOLS", "ResearchState", "tool_schemas"]

log = logging.getLogger(__name__)


@dataclass
class ResearchState:
    platform: Any
    ctx: Ctx
    allow_paid: bool = False
    companies: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    evidence: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    extras: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def keep(self, ids: Set[str]) -> None:
        self.order = [i for i in self.order if i in ids]

    def note(self, company_id: str, step: str, reason: str, **data: Any) -> None:
        self.evidence.setdefault(company_id, []).append({"step": step, "reason": reason, **data})

    def extra(self, company_id: str) -> Dict[str, Any]:
        return self.extras.setdefault(company_id, {})

    def service(self, name: str) -> Optional[Any]:
        try:
            return self.platform.service(name)
        except Exception as error:  # noqa: BLE001 - another track not installed/failing: degrade honestly
            log.warning("research: service %s unavailable: %s", name, error)
            return None


def _aliases(names: List[str], vocab: Dict[str, tuple]) -> Dict[str, tuple]:
    return {n: tuple(a.lower() for a in vocab.get(n, (n.lower(),))) + (n.lower(),) for n in names}


def _matches(values: List[str], aliases: tuple) -> Optional[str]:
    for value in values:
        lowered = str(value).lower()
        for alias in aliases:
            if alias == lowered or f" {alias} " in f" {lowered} ":
                return value
    return None


# --- tools ------------------------------------------------------------------------


def query_companies(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    store, ctx = state.platform.store, state.ctx
    limit = int(params.get("limit") or 2000)
    rows = store.all(ctx, "companies", {"status": "active"}, cap=min(limit, 50_000))
    country = params.get("country")
    industries = _aliases(params.get("industries") or [], INDUSTRIES)
    techs = _aliases(params.get("technologies") or [], TECHNOLOGIES)
    tech_rows: Dict[str, List[Dict[str, Any]]] = {}
    if techs:
        for row in store.all(ctx, "company_technologies", {"status": "active"}, cap=200_000):
            tech_rows.setdefault(row["company_id"], []).append(row)
    kept = 0
    for company in rows:
        reasons = []
        if country:
            if normalize_country(company.get("country")) != country:
                continue
            reasons.append(f"country={country}")
        if industries:
            hit = None
            for name, aliases in industries.items():
                hit = _matches([company.get("industry") or "", company.get("sub_industry") or ""], aliases)
                if hit:
                    reasons.append(f"industry={hit}")
                    break
            if not hit:
                continue
        if techs:
            found = None
            evidence_row = None
            for name, aliases in techs.items():
                found = _matches(company.get("technologies") or [], aliases)
                if found:
                    break
                for row in tech_rows.get(company["id"], []):
                    if _matches([row["technology"]], aliases):
                        found, evidence_row = row["technology"], row
                        break
                if found:
                    break
            if not found:
                continue
            reasons.append(f"technology={found}")
            if evidence_row:
                state.note(company["id"], "query_companies", f"uses {found}", source=evidence_row.get("source"),
                           evidence_url=evidence_row.get("evidence_url"),
                           observed_at=str(evidence_row.get("observed_at")))
        state.companies[company["id"]] = company
        state.order.append(company["id"])
        state.note(company["id"], "query_companies", "; ".join(reasons) or "in workspace data")
        kept += 1
    return {"status": "done", "count_out": kept, "detail": f"{kept} of {len(rows)} companies matched the filters"}


def match_internal(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    """Candidates found in the workspace store are internal records by definition;
    this step confirms identity (and flags possible duplicates) through the resolver."""
    resolver = state.service("dedupe")
    flagged = 0
    for cid in state.order:
        company = state.companies[cid]
        extra = state.extra(cid)
        extra["internal_match"] = {"outcome": "EXACT", "company_id": cid, "reasons": ["internal record"]}
        if resolver is None:
            continue
        try:
            result = resolver.resolve(state.ctx, {"name": company.get("name"), "domain": company.get("domain"),
                                                  "website": company.get("website")})
        except Exception as error:  # noqa: BLE001
            result = {"outcome": "UNKNOWN", "reasons": [f"resolver failed: {error}"]}
        others = [c for c in (result.get("candidates") or []) if c != cid]
        if others:
            flagged += 1
            extra["possible_duplicates"] = others
            state.note(cid, "match_internal", f"possible duplicate records: {', '.join(others[:5])}")
    detail = f"{len(state.order)} matched to internal records"
    if resolver is None:
        detail += " (identity resolver unavailable; duplicate check skipped)"
    elif flagged:
        detail += f"; {flagged} with possible duplicates"
    return {"status": "done", "count_out": len(state.order), "detail": detail}


def exclude_existing_crm(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    lifecycles = set(params.get("lifecycles") or ["account", "customer", "partner"])
    with_opps: Set[str] = set()
    if params.get("with_opportunities", True):
        for opp in state.platform.store.all(state.ctx, "opportunities", {"status": "open"}, cap=200_000):
            with_opps.add(opp["company_id"])
    before = len(state.order)
    keep = set()
    for cid in state.order:
        company = state.companies[cid]
        if company.get("lifecycle") in lifecycles or cid in with_opps:
            continue
        keep.add(cid)
    state.keep(keep)
    return {"status": "done", "count_out": len(state.order),
            "detail": f"removed {before - len(state.order)} already in the CRM"}


def hiring_signals(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    store, ctx = state.platform.store, state.ctx
    keywords = _aliases(params.get("keywords") or [], TECHNOLOGIES)
    types = set(params.get("signal_types") or [])
    since = utcnow() - timedelta(days=int(params.get("window_days") or 90))
    required = bool(params.get("required", True))
    keep = set()
    for cid in state.order:
        signals = store.all(ctx, "hiring_signals", {"company_id": cid, "status": "active"}, cap=500)
        jobs = store.all(ctx, "job_postings", {"company_id": cid, "status": "open"}, cap=2000)
        matched_jobs = []
        for job in jobs:
            if job.get("first_seen_at") and job["first_seen_at"] < since:
                continue
            if keywords:
                haystack = [job.get("title") or ""] + list(job.get("technologies") or []) + list(job.get("skills") or [])
                hit = next((n for n, a in keywords.items() if _matches(haystack, a)), None)
                if not hit:
                    continue
            matched_jobs.append(job)
        matched_signals = [s for s in signals if not types or s["signal_type"] in types]
        if keywords and matched_signals:
            matched_signals = [s for s in matched_signals if not s.get("job_posting_ids")
                               or set(s["job_posting_ids"]) & {j["id"] for j in matched_jobs}] or matched_signals
        extra = state.extra(cid)
        extra["signals"] = matched_signals
        extra["jobs"] = matched_jobs
        if types and not matched_signals:
            if required:
                continue
        elif (keywords or required) and not matched_jobs and not matched_signals:
            if required:
                continue
        keep.add(cid)
        for s in matched_signals[:5]:
            state.note(cid, "hiring_signals", f"{s['signal_type']}: {s.get('summary') or ''}".strip(),
                       signal_id=s["id"], confidence=s.get("confidence"), detected_at=str(s.get("detected_at")))
        for j in matched_jobs[:5]:
            state.note(cid, "hiring_signals", f"open role: {j['title']}", job_posting_id=j["id"],
                       job_url=j.get("job_url"), first_seen_at=str(j.get("first_seen_at")), source=j.get("source_name"))
    before = len(state.order)
    state.keep(keep)
    return {"status": "done", "count_out": len(state.order),
            "detail": f"{len(state.order)} of {before} companies have matching hiring evidence"}


def _classify(title: str) -> str:
    from cloud.intel.vendor.seamless_targeting import classify

    return classify(title or "")


def find_contacts(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    functions = list(params.get("functions") or []) or ["it", "hr", "executive"]
    service = state.service("contacts")
    detail = []
    if service is not None and state.order:
        try:
            outcome = service.find_contacts(state.ctx, list(state.order), functions=tuple(functions),
                                            allow_paid=state.allow_paid)
            detail.append("searched authorized sources" + ("" if state.allow_paid else " (free sources only)"))
            if isinstance(outcome, dict):
                for cid, info in (outcome.get("companies") or {}).items():
                    if cid in state.companies:
                        state.extra(cid)["contact_search"] = info
        except Exception as error:  # noqa: BLE001
            detail.append(f"contact search failed: {error}")
    elif service is None:
        detail.append("contact service unavailable: gap analysis from existing contacts only")
    seniorities = [s.lower() for s in params.get("seniorities") or []]
    missing_total = 0
    for cid in state.order:
        contacts = state.platform.store.all(state.ctx, "contacts", {"company_id": cid, "status": "active"}, cap=500)
        gap: Dict[str, Any] = {}
        for fn in functions:
            people = [c for c in contacts if (c.get("function") or _classify(c.get("title") or "")) == fn]
            if seniorities:
                people = [c for c in people if any(s in (c.get("seniority") or c.get("title") or "").lower()
                                                   for s in seniorities)] or people
            verified = [c for c in people if c.get("email_status") == "VALID"]
            status = "FOUND" if verified else ("NEEDS_VERIFICATION" if people else "MISSING")
            missing_total += status == "MISSING"
            gap[fn] = {"status": status, "contact_ids": [c["id"] for c in people][:10]}
        state.extra(cid)["contact_gap"] = gap
        state.extra(cid)["contacts"] = [{"id": c["id"], "name": c["full_name"], "title": c.get("title"),
                                         "email": c.get("email"), "email_status": c.get("email_status")}
                                        for c in contacts[:25]]
        state.note(cid, "find_contacts", ", ".join(f"{fn}: {g['status']}" for fn, g in gap.items()))
    detail.append(f"{missing_total} function gaps remain")
    return {"status": "done", "count_out": len(state.order), "detail": "; ".join(detail),
            "paid_used": bool(state.allow_paid and service is not None)}


def validate_emails(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    emails: Dict[str, str] = {}
    for cid in state.order:
        for contact in state.extra(cid).get("contacts") or state.platform.store.all(
                state.ctx, "contacts", {"company_id": cid}, cap=500):
            if contact.get("email"):
                emails[contact["email"].lower()] = cid
    if not emails:
        return {"status": "done", "count_out": len(state.order), "detail": "no emails to validate"}
    service = state.service("email")
    if service is None:
        return {"status": "skipped", "count_out": len(state.order), "detail": "email validation service unavailable"}
    try:
        results = service.validate(state.ctx, sorted(emails), allow_paid=state.allow_paid,
                                   max_age_days=int(params.get("max_age_days") or 30))
    except Exception as error:  # noqa: BLE001
        return {"status": "failed", "count_out": len(state.order), "detail": f"validation failed: {error}"}
    counts: Dict[str, int] = {}
    for result in results or []:
        status = result.get("status", "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
        cid = emails.get(str(result.get("email", "")).lower())
        if cid:
            state.extra(cid).setdefault("email_validation", {})[result["email"]] = status
    return {"status": "done", "count_out": len(state.order),
            "detail": ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no results",
            "paid_used": bool(state.allow_paid)}


def score(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    service = state.service("signals")
    fallbacks = 0
    for cid in state.order:
        extra = state.extra(cid)
        result = None
        if service is not None:
            try:
                result = service.score_company(state.ctx, cid)
            except Exception as error:  # noqa: BLE001
                log.warning("score_company failed for %s: %s", cid, error)
        if not isinstance(result, dict):
            fallbacks += 1
            # Transparent fallback: evidence counts, not an opaque number.
            signals, jobs = len(extra.get("signals") or []), len(extra.get("jobs") or [])
            gap = extra.get("contact_gap") or {}
            covered = sum(1 for g in gap.values() if g["status"] != "MISSING")
            opp = min(100.0, signals * 15 + jobs * 8 + covered * 5)
            result = {"opportunity_score": opp, "breakdown": {
                "signals": signals * 15, "relevant_jobs": jobs * 8, "contact_coverage": covered * 5,
                "method": "fallback: 15/signal + 8/relevant job + 5/covered function, capped at 100"}}
        extra["scores"] = result
    ranked = sorted(state.order, key=lambda c: -(state.extra(c)["scores"].get("opportunity_score") or 0))
    limit = params.get("limit")
    state.order = ranked[: int(limit)] if limit else ranked
    return {"status": "done", "count_out": len(state.order),
            "detail": "ranked by opportunity score"
                      + (f" ({fallbacks} with fallback scoring: signal service unavailable)" if fallbacks else "")}


def assign_campaign(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    service = state.service("campaigns")
    if service is None:
        return {"status": "skipped", "count_out": len(state.order), "detail": "campaign service unavailable"}
    assigned = 0
    for cid in state.order:
        extra = state.extra(cid)
        try:
            matches = service.match_campaigns(state.ctx, state.companies[cid], extra.get("signals") or [],
                                              extra.get("jobs") or [])
        except Exception as error:  # noqa: BLE001
            matches = []
            log.warning("match_campaigns failed: %s", error)
        if matches:
            best = matches[0]
            extra["campaign"] = {"id": best["campaign"]["id"], "key": best["campaign"].get("key"),
                                 "name": best["campaign"].get("name"), "score": best.get("score"),
                                 "reasons": best.get("reasons", [])}
            state.note(cid, "assign_campaign", f"best campaign: {best['campaign'].get('name')}",
                       reasons=best.get("reasons", []))
            assigned += 1
    return {"status": "proposed", "count_out": len(state.order),
            "detail": f"{assigned} companies matched to a campaign (not applied)"}


def _deferred(state: ResearchState, params: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": "deferred", "count_out": len(state.order), "detail": "built after ranking"}


Tool = Callable[[ResearchState, Dict[str, Any]], Dict[str, Any]]

_OBJ = {"type": "object"}
TOOLS: Dict[str, Dict[str, Any]] = {
    "query_companies": {"fn": query_companies, "mutates": False, "schema": {**_OBJ, "properties": {
        "country": {"type": ["string", "null"]}, "industries": {"type": "array", "items": {"type": "string"}},
        "technologies": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}}}},
    "match_internal": {"fn": match_internal, "mutates": False, "schema": {**_OBJ, "properties": {}}},
    "exclude_existing_crm": {"fn": exclude_existing_crm, "mutates": False, "schema": {**_OBJ, "properties": {
        "lifecycles": {"type": "array", "items": {"type": "string"}}, "with_opportunities": {"type": "boolean"}}}},
    "hiring_signals": {"fn": hiring_signals, "mutates": False, "schema": {**_OBJ, "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}},
        "signal_types": {"type": "array", "items": {"type": "string"}},
        "window_days": {"type": "integer"}, "required": {"type": "boolean"}}}},
    "find_contacts": {"fn": find_contacts, "mutates": False, "schema": {**_OBJ, "properties": {
        "functions": {"type": "array", "items": {"type": "string"}},
        "seniorities": {"type": "array", "items": {"type": "string"}},
        "missing_only": {"type": "boolean"}, "authorized_sources": {"type": "boolean"}}}},
    "validate_emails": {"fn": validate_emails, "mutates": False, "schema": {**_OBJ, "properties": {
        "max_age_days": {"type": "integer"}}}},
    "score": {"fn": score, "mutates": False, "schema": {**_OBJ, "properties": {"limit": {"type": ["integer", "null"]}}}},
    "assign_campaign": {"fn": assign_campaign, "mutates": True, "schema": {**_OBJ, "properties": {}}},
    "create_list": {"fn": _deferred, "mutates": True, "schema": {**_OBJ, "properties": {"name": {"type": "string"}}}},
    "create_opportunities": {"fn": _deferred, "mutates": True, "schema": {**_OBJ, "properties": {}}},
    "export": {"fn": _deferred, "mutates": False, "schema": {**_OBJ, "properties": {
        "format": {"type": "string", "enum": ["csv", "xlsx", "json"]}}}},
}


def tool_schemas() -> List[Dict[str, Any]]:
    """The tools in Messages-API ``tools`` shape, for an AI planner or tool runner."""
    return [{"name": name, "description": (spec["fn"].__doc__ or name).strip().splitlines()[0],
             "input_schema": spec["schema"]} for name, spec in TOOLS.items()]
