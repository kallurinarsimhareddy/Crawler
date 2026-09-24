"""Hiring intelligence: persist signals, aggregate companies, compute explainable scores.

**Scores are arithmetic, not AI.** Every score is a sum of named components;
the breakdown stored on the company (``score_breakdown``) lists each
component's weight, the value it got and why. The research agent and the UI
show that breakdown verbatim.

=================  ================================================================
account_score      fit to the workspace's campaigns: technology overlap (35),
                   industry (20), country (15), size (15), data completeness (15)
hiring_score       Σ over active signals of weight × strength/100 × confidence (cap 100)
opportunity_score  0.35 × account + 0.50 × hiring + 0.15 × target-contact coverage
contact_score      seniority (30) + target function (25) + email status (25) +
                   source confidence (20)
=================  ================================================================

Signals are idempotent: a signal's fingerprint is its type, company and the set
of postings that produced it, so re-running detection refreshes it rather than
duplicating it. A signal whose postings no longer support it becomes
``expired``; one a user ``dismissed`` is never reactivated for the same evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, utcnow
from cloud.intel.signals.engine import SIGNAL_TYPES, aggregate, detect_signals
from cloud.intel.technology.service import emit_best_effort
from cloud.intel.technology.taxonomy import TAXONOMY

__all__ = ["SignalService", "run_signals_task", "SIGNAL_WEIGHTS", "DEFAULT_FIT"]

log = logging.getLogger(__name__)

SIGNAL_WEIGHTS = {
    "NEW_ROLE": 15, "MULTIPLE_RELEVANT_ROLES": 15, "HIRING_SPIKE": 20, "HIRING_VELOCITY": 15, "LONG_OPEN_ROLE": 10,
    "HARD_TO_FILL": 10, "SPECIALIZED_TECHNOLOGY": 15, "PROJECT_IMPLEMENTATION": 20, "EXPANSION_HIRING": 10,
    "BACKFILL_REPLACEMENT": 5, "LEADERSHIP_HIRING": 15,
}

#: Used when no campaign states its own fit rules (campaign.rules.{industries,countries,min_employees,max_employees}).
DEFAULT_FIT = {"industries": ["manufacturing", "distribution", "industrial", "automotive", "food", "chemical",
                              "aerospace", "consumer goods", "retail", "logistics", "healthcare", "technology"],
               "countries": ["United States"], "min_employees": 50, "max_employees": 20000}

_FAMILIES = {t.name: t.families for t in TAXONOMY}
_SENIORITY_POINTS = {"c_level": 30, "c-level": 30, "vp": 27, "director": 24, "manager": 15, "lead": 12,
                     "senior": 10}
_EMAIL_POINTS = {"VALID": 25, "RISKY": 12, "ROLE": 8, "UNKNOWN": 8, "UNVERIFIED": 8, "FREE_PROVIDER": 4,
                 "DISPOSABLE": 0, "INVALID": 0}
_TARGET_FUNCTIONS = {"it", "hr", "executive", "erp", "engineering", "recruiting"}


def _component(name: str, weight: float, value: float, reason: str) -> Dict[str, Any]:
    return {"component": name, "weight": weight, "value": round(max(0.0, min(weight, value)), 2), "reason": reason}


def _employees(company: Mapping[str, Any]) -> Optional[int]:
    if company.get("employee_count"):
        return int(company["employee_count"])
    text = str(company.get("employee_range") or "")
    import re

    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text)]
    if not numbers:
        return None
    return int(sum(numbers[:2]) / len(numbers[:2]))


class SignalService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- detection ------------------------------------------------------------

    def _has_campaigns(self, ctx: Ctx) -> bool:
        return self.store.count(ctx, "campaigns", {"status__in": ["draft", "active"]}) > 0

    def detect_for_company(self, ctx: Ctx, company_id: str, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Run every rule for one company; upsert active signals, expire stale ones, rescore."""
        now = now or utcnow()
        company = self.store.get(ctx, "companies", company_id)
        jobs = self.store.all(ctx, "job_postings", {"company_id": company_id}, cap=5000)
        detected = detect_signals(jobs, now=now, has_campaigns=self._has_campaigns(ctx),
                                  family_of=lambda t: _FAMILIES.get(t, ()))
        kept: List[Dict[str, Any]] = []
        fingerprints = set()
        for sig in detected:
            fp = sig.fingerprint(company_id, "all")
            fingerprints.add(fp)
            values = {"company_id": company_id, "signal_type": sig.signal_type, "detected_at": now,
                      "window_start": sig.window_start, "window_end": sig.window_end,
                      "confidence": round(sig.confidence, 3), "strength": round(sig.strength, 1),
                      "reason_codes": sig.reason_codes[:30], "summary": sig.summary[:1000],
                      "evidence": sig.evidence, "job_posting_ids": sig.job_posting_ids[:200],
                      "source": "hiring_engine", "fingerprint": fp}
            existing = self.store.first(ctx, "hiring_signals", {"fingerprint": fp})
            if existing is not None:
                if existing["status"] == "dismissed":
                    continue
                refresh = {k: v for k, v in values.items() if k not in ("detected_at", "fingerprint", "company_id")}
                kept.append(self.store.update(ctx, "hiring_signals", existing["id"], {**refresh, "status": "active"}))
                continue
            try:
                row = self.store.insert(ctx, "hiring_signals", {**values, "status": "active"})
            except ConflictError:
                row = self.store.first(ctx, "hiring_signals", {"fingerprint": fp})
            kept.append(row)
            if sig.signal_type == "HIRING_SPIKE":
                emit_best_effort(self.platform, ctx, "hiring_spike", fp,
                                 {"company_id": company_id, "signal_id": row["id"], "summary": sig.summary})
                try:
                    self.platform.service("monitoring").record_change(
                        ctx, company_id, "hiring_spike", after={"signal_id": row["id"]}, summary=sig.summary,
                        source="hiring_engine")
                except Exception:  # noqa: BLE001
                    log.debug("change event skipped", exc_info=True)
        for old in self.store.all(ctx, "hiring_signals", {"company_id": company_id, "status": "active"}):
            if old["fingerprint"] not in fingerprints:
                self.store.update(ctx, "hiring_signals", old["id"], {"status": "expired"})
        active_types = sorted({s["signal_type"] for s in kept if s and s["status"] == "active"})
        agg = aggregate(jobs, now=now, has_campaigns=self._has_campaigns(ctx))
        self.store.update(ctx, "companies", company_id, {"hiring_signals": active_types,
                                                         "hiring_velocity": float(agg["velocity_30d"])})
        self.score_company(ctx, company_id)
        return [s for s in kept if s]

    def dismiss(self, ctx: Ctx, signal_id: str) -> Dict[str, Any]:
        row = self.store.update(ctx, "hiring_signals", signal_id, {"status": "dismissed"})
        audit(self.store, ctx, "signal.dismiss", entity_type="hiring_signals", entity_id=signal_id)
        return row

    def aggregate_company(self, ctx: Ctx, company_id: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        jobs = self.store.all(ctx, "job_postings", {"company_id": company_id}, cap=5000)
        return aggregate(jobs, now=now or utcnow(), has_campaigns=self._has_campaigns(ctx))

    # --- scoring --------------------------------------------------------------------

    def _fit_rules(self, ctx: Ctx) -> Dict[str, Any]:
        rules = {"industries": set(), "countries": set(), "technologies": set(), "min": None, "max": None}
        for c in self.store.all(ctx, "campaigns", {"status__in": ["draft", "active"]}, cap=200):
            r = c.get("rules") or {}
            rules["industries"].update(i.lower() for i in r.get("industries") or [])
            rules["countries"].update(r.get("countries") or [])
            rules["technologies"].update(t.lower() for t in c.get("technologies") or [])
            if r.get("min_employees") is not None:
                rules["min"] = min(rules["min"] or r["min_employees"], r["min_employees"])
            if r.get("max_employees") is not None:
                rules["max"] = max(rules["max"] or r["max_employees"], r["max_employees"])
        rules["industries"] = rules["industries"] or set(DEFAULT_FIT["industries"])
        rules["countries"] = rules["countries"] or set(DEFAULT_FIT["countries"])
        rules["min"] = rules["min"] if rules["min"] is not None else DEFAULT_FIT["min_employees"]
        rules["max"] = rules["max"] if rules["max"] is not None else DEFAULT_FIT["max_employees"]
        return rules

    def account_components(self, ctx: Ctx, company: Mapping[str, Any]) -> List[Dict[str, Any]]:
        rules = self._fit_rules(ctx)
        comps = []
        techs = {t.lower() for t in company.get("technologies") or []}
        if rules["technologies"]:
            hits = sorted(t for t in rules["technologies"] if any(t == x or t in x for x in techs))
            value = min(35, 35 * len(hits) / 2) if hits else 0
            comps.append(_component("technology_fit", 35, value,
                                    f"matches campaign technologies: {', '.join(hits)}" if hits
                                    else "no campaign technology observed"))
        else:
            value = 20 if techs else 0
            comps.append(_component("technology_fit", 35, value, "no campaign technologies defined; "
                                    + ("technologies observed" if techs else "no technologies observed")))
        industry = str(company.get("industry") or "").lower()
        hit = next((i for i in rules["industries"] if i and i in industry), None)
        comps.append(_component("industry_fit", 20, 20 if hit else 0,
                                f"industry '{company.get('industry')}' matches '{hit}'" if hit
                                else f"industry '{company.get('industry') or 'unknown'}' not in target industries"))
        country = company.get("country")
        comps.append(_component("country_fit", 15, 15 if country in rules["countries"] else 0,
                                f"country {country or 'unknown'}" + (" is targeted" if country in rules["countries"]
                                                                    else " is not targeted")))
        size = _employees(company)
        if size is None:
            comps.append(_component("size_fit", 15, 5, "employee count unknown (partial credit)"))
        else:
            fits = rules["min"] <= size <= rules["max"]
            comps.append(_component("size_fit", 15, 15 if fits else 0,
                                    f"~{size} employees {'within' if fits else 'outside'} "
                                    f"{rules['min']}-{rules['max']}"))
        present = [f for f in ("domain", "website", "careers_url", "industry", "country") if company.get(f)]
        comps.append(_component("data_completeness", 15, 3 * len(present),
                                f"{len(present)}/5 key fields known ({', '.join(present) or 'none'})"))
        return comps

    def hiring_components(self, ctx: Ctx, company_id: str) -> List[Dict[str, Any]]:
        comps = []
        for sig in self.store.all(ctx, "hiring_signals", {"company_id": company_id, "status": "active"}):
            weight = SIGNAL_WEIGHTS[sig["signal_type"]]
            value = weight * (sig.get("strength") or 0) / 100 * (sig.get("confidence") or 0)
            comps.append(_component(sig["signal_type"], weight, value,
                                    f"{sig.get('summary')} (strength {sig.get('strength')}, "
                                    f"confidence {sig.get('confidence')})"))
        return comps

    def contact_coverage(self, ctx: Ctx, company_id: str) -> Dict[str, Any]:
        contacts = self.store.all(ctx, "contacts", {"company_id": company_id, "status": "active"}, cap=2000)
        functions = {str(c.get("function") or "").lower() for c in contacts} & {"it", "hr", "executive"}
        return {"covered": sorted(functions), "share": len(functions) / 3}

    def score_company(self, ctx: Ctx, company_id: str) -> Dict[str, Any]:
        company = self.store.get(ctx, "companies", company_id)
        account = self.account_components(ctx, company)
        hiring = self.hiring_components(ctx, company_id)
        account_score = round(min(100.0, sum(c["value"] for c in account)), 1)
        hiring_score = round(min(100.0, sum(c["value"] for c in hiring)), 1)
        coverage = self.contact_coverage(ctx, company_id)
        opportunity = [
            _component("account_fit", 35, 0.35 * account_score, f"0.35 × account score {account_score}"),
            _component("hiring_intent", 50, 0.50 * hiring_score, f"0.50 × hiring score {hiring_score}"),
            _component("contact_coverage", 15, 15 * coverage["share"],
                       f"target functions with contacts: {', '.join(coverage['covered']) or 'none'} of it/hr/executive"),
        ]
        opportunity_score = round(min(100.0, sum(c["value"] for c in opportunity)), 1)
        breakdown = {"account": account, "hiring": hiring, "opportunity": opportunity, "computed_at": utcnow().isoformat(),
                     "method": "rules-v1"}
        self.store.update(ctx, "companies", company_id, {"account_score": account_score, "hiring_score": hiring_score,
                                                         "opportunity_score": opportunity_score,
                                                         "score_breakdown": breakdown})
        return {"account_score": account_score, "hiring_score": hiring_score, "opportunity_score": opportunity_score,
                "breakdown": breakdown}

    def score_contact(self, ctx: Ctx, contact: Mapping[str, Any], company: Optional[Mapping[str, Any]] = None
                      ) -> Dict[str, Any]:
        seniority = str(contact.get("seniority") or "").lower()
        function = str(contact.get("function") or contact.get("department") or "").lower()
        status = str(contact.get("email_status") or "UNVERIFIED").upper()
        confidence = contact.get("confidence")
        comps = [
            _component("seniority", 30, _SENIORITY_POINTS.get(seniority, 6), f"seniority '{seniority or 'unknown'}'"),
            _component("function", 25, 25 if function in _TARGET_FUNCTIONS else 8,
                       f"function '{function or 'unknown'}'" + (" is a target function" if function in _TARGET_FUNCTIONS
                                                               else "")),
            _component("email", 25, _EMAIL_POINTS.get(status, 8) if contact.get("email") else 0,
                       f"email {status.lower()}" if contact.get("email") else "no email"),
            _component("confidence", 20, 20 * (confidence if confidence is not None else 0.5),
                       f"source confidence {confidence if confidence is not None else 'unknown (0.5)'}"),
        ]
        return {"contact_score": round(min(100.0, sum(c["value"] for c in comps)), 1), "breakdown": comps}


def run_signals_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Detect signals and rescore: ``params.company_ids`` or every active company."""
    service: SignalService = platform.service("signals")
    params = task.get("params") or {}
    ids: Sequence[str] = params.get("company_ids") or [
        c["id"] for c in platform.store.all(ctx, "companies", {"status": "active"}, cap=100_000)]
    start = int(reporter.checkpoint.get("index", 0)) if reporter is not None else 0
    totals = {"companies": len(ids), "signals": 0, "errors": 0}
    for index in range(start, len(ids)):
        if reporter is not None:
            if reporter.is_cancelled():
                break
            if reporter.should_pause():
                from cloud.intel.tasks.worker import TaskPaused

                raise TaskPaused({"index": index})
            if index % 10 == 0:
                reporter.progress(f"Scoring {index + 1}/{len(ids)}", done=index, total=len(ids))
        try:
            totals["signals"] += len(service.detect_for_company(ctx, ids[index]))
        except Exception:  # noqa: BLE001 - one company must not stop the run
            log.exception("signal detection failed for %s", ids[index])
            totals["errors"] += 1
    return totals
