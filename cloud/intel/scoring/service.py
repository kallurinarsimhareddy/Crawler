"""Explainable scoring: every score is a sum of named factors, with the evidence.

``ScoringService`` ("scoring") scores companies and contacts. Nothing here is a
model or a black box: each score is the sum of the ``points`` of its factors,
and each factor says what it measured (``value``), how much it can contribute
(``weight``) and why it got what it got (``reason``). The records behind a
score are listed as ``evidence`` (type, id, summary, observed_at). Every
computation carries ``computed_at`` and ``model`` (:data:`MODEL`).

=================  =================================================================
account            fit to the workspace's campaigns (technology, industry, country,
                   size, data completeness) — the same rules as the hiring engine
hiring             Σ active hiring signals: weight × strength/100 × confidence
technology         campaign technology match (40), ERP/core platform present (25),
                   evidence confidence (15), recency of the newest observation (20)
opportunity        0.35 × account + 0.50 × hiring + 0.15 × target-contact coverage
buying_stage       hiring intent (25), implementation signals (15), engagement (25),
                   deal progress (25), recent activity (10); the label is the most
                   advanced stage the evidence supports
contact            seniority (30), target function (25), email status (25),
                   source confidence (20)
=================  =================================================================

The account/hiring/opportunity/contact rules live in
:class:`cloud.intel.signals.service.SignalService` and are reused, not copied.
Scores are persisted on the record (``*_score``, ``score_breakdown``,
``scored_at``) and appended to ``score_snapshots`` when they change, which is
the score history.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError, utcnow
from cloud.intel.technology.taxonomy import TAXONOMY

__all__ = ["ScoringService", "run_scoring_task", "MODEL", "COMPANY_KINDS", "BUYING_STAGES", "model_description"]

log = logging.getLogger(__name__)

MODEL = "sana-rules-v2"
COMPANY_KINDS = ("account", "hiring", "technology", "opportunity", "buying_stage")
#: Ordered from least to most advanced.
BUYING_STAGES = ("unaware", "problem_aware", "researching", "engaged", "evaluating", "customer")

_FAMILIES = {t.name.lower(): set(t.families) for t in TAXONOMY}
_CATEGORY = {t.name.lower(): t.category for t in TAXONOMY}
_CORE_FAMILIES = {"ERP", "SAP", "Oracle", "Microsoft Dynamics", "Infor", "Epicor", "NetSuite", "CRM", "HCM"}
_IMPLEMENTATION_SIGNALS = ("PROJECT_IMPLEMENTATION", "SPECIALIZED_TECHNOLOGY")
_ENGAGED_ACTIVITY = ("email_reply", "meeting", "call")


def _factor(name: str, weight: float, points: float, value: Any, reason: str) -> Dict[str, Any]:
    return {"name": name, "weight": weight, "value": value,
            "points": round(max(0.0, min(float(weight), float(points))), 2), "reason": reason}


def _from_component(component: Mapping[str, Any], value: Any = None) -> Dict[str, Any]:
    """The hiring engine's ``{component, weight, value, reason}`` as a factor."""
    return {"name": component["component"], "weight": component["weight"],
            "value": value if value is not None else component["value"],
            "points": component["value"], "reason": component["reason"]}


def _total(factors: Sequence[Mapping[str, Any]]) -> float:
    return round(min(100.0, sum(float(f["points"]) for f in factors)), 1)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _evidence(kind: str, row: Mapping[str, Any], summary: str, observed: Any = None) -> Dict[str, Any]:
    return {"type": kind, "id": row.get("id"), "summary": summary[:300],
            "observed_at": _iso(observed if observed is not None else row.get("updated_at") or row.get("created_at"))}


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def model_description() -> Dict[str, Any]:
    """The rules, stated once, for the UI's "how scores work" panel."""
    return {
        "model": MODEL,
        "kinds": {
            "account": "Fit to campaigns: technology (35), industry (20), country (15), size (15), data completeness (15).",
            "hiring": "Sum over active hiring signals of weight × strength/100 × confidence, capped at 100.",
            "technology": "Campaign technology match (40), ERP/core platform present (25), evidence confidence (15), "
                          "recency of newest observation (20).",
            "opportunity": "0.35 × account + 0.50 × hiring + 0.15 × share of IT/HR/executive functions with contacts.",
            "buying_stage": "Hiring intent (25), implementation signals (15), engagement (25), deal progress (25), "
                            "recent activity (10). Label = most advanced stage the evidence supports: "
                            + " → ".join(BUYING_STAGES) + ".",
            "contact": "Seniority (30), target function (25), email status (25), source confidence (20).",
        },
        "notes": ["Scores are arithmetic over stored records; no AI or hidden model is involved.",
                  "Each factor lists weight, observed value, points and reason; evidence links the records used."],
    }


class ScoringService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    @property
    def _signals(self):
        return self.platform.service("signals")

    # --- company kinds --------------------------------------------------------------

    def _account(self, ctx: Ctx, company: Mapping[str, Any]) -> Dict[str, Any]:
        factors = [_from_component(c) for c in self._signals.account_components(ctx, company)]
        evidence = [_evidence("company", company, "firmographics: " + ", ".join(
            f"{k}={company.get(k)}" for k in ("industry", "country", "employee_range", "employee_count")
            if company.get(k) not in (None, "")) or "no firmographics recorded")]
        return {"factors": factors, "evidence": evidence}

    def _hiring(self, ctx: Ctx, company_id: str) -> Dict[str, Any]:
        signals = self.store.all(ctx, "hiring_signals", {"company_id": company_id, "status": "active"}, cap=200)
        factors = [_from_component(c) for c in self._signals.hiring_components(ctx, company_id)]
        evidence = [_evidence("hiring_signal", s, f"{s['signal_type']}: {s.get('summary') or ''}", s.get("detected_at"))
                    for s in signals]
        open_jobs = self.store.count(ctx, "job_postings", {"company_id": company_id, "status": "open"})
        if open_jobs:
            evidence.append({"type": "job_postings", "id": None, "summary": f"{open_jobs} open job posting(s)",
                             "observed_at": None})
        if not factors:
            factors.append(_factor("active_signals", 100, 0, 0, "no active hiring signals"))
        return {"factors": factors, "evidence": evidence, "signals": signals}

    def _technology(self, ctx: Ctx, company: Mapping[str, Any], now: datetime) -> Dict[str, Any]:
        rows = self.store.all(ctx, "company_technologies", {"company_id": company["id"], "status": "active"}, cap=500)
        names = {r["technology"].lower() for r in rows} | {t.lower() for t in company.get("technologies") or []}
        evidence = [_evidence("company_technology", r, f"{r['technology']} (source {r['source']})"
                              + (f": {r['evidence_text'][:120]}" if r.get("evidence_text") else ""), r.get("observed_at"))
                    for r in rows[:25]]
        if not names:
            return {"factors": [_factor("technologies_observed", 100, 0, 0, "no technologies observed")],
                    "evidence": []}
        wanted = set()
        for campaign in self.store.all(ctx, "campaigns", {"status__in": ["draft", "active"]}, cap=200):
            wanted.update(t.lower() for t in campaign.get("technologies") or [])
        factors: List[Dict[str, Any]] = []
        if wanted:
            hits = sorted(t for t in wanted if any(t == n or t in n for n in names))
            factors.append(_factor("campaign_technology_match", 40, 40 * min(1.0, len(hits) / 2), hits,
                                   f"matches campaign technologies: {', '.join(hits)}" if hits
                                   else "no campaign technology observed"))
        else:
            factors.append(_factor("campaign_technology_match", 40, 20, [],
                                   "no campaign technologies defined; partial credit for observed technologies"))
        core = sorted({n for n in names if (_FAMILIES.get(n, set()) | {_CATEGORY.get(n, "")}) & _CORE_FAMILIES})
        factors.append(_factor("core_platform", 25, 25 if core else 0, core,
                               f"ERP/core platforms: {', '.join(core)}" if core else "no ERP/core platform observed"))
        confidences = [float(r["confidence"]) for r in rows if r.get("confidence") is not None]
        avg = sum(confidences) / len(confidences) if confidences else None
        factors.append(_factor("evidence_confidence", 15, 15 * (avg if avg is not None else 0.5),
                               round(avg, 2) if avg is not None else None,
                               f"average detection confidence {avg:.2f}" if avg is not None
                               else "confidence not recorded (half credit)"))
        newest = max((d for d in (_aware(r.get("observed_at")) for r in rows) if d), default=None)
        if newest is None:
            factors.append(_factor("recency", 20, 5, None, "technologies known only from the company record"))
        else:
            age = (now - newest).days
            points = 20 if age <= 90 else 10 if age <= 180 else 3
            factors.append(_factor("recency", 20, points, age, f"newest observation {age} day(s) ago"))
        return {"factors": factors, "evidence": evidence}

    def _opportunity(self, ctx: Ctx, company_id: str, account: float, hiring: float) -> Dict[str, Any]:
        coverage = self._signals.contact_coverage(ctx, company_id)
        factors = [
            _factor("account_fit", 35, 0.35 * account, account, f"0.35 × account score {account}"),
            _factor("hiring_intent", 50, 0.50 * hiring, hiring, f"0.50 × hiring score {hiring}"),
            _factor("contact_coverage", 15, 15 * coverage["share"], coverage["covered"],
                    f"target functions with contacts: {', '.join(coverage['covered']) or 'none'} of it/hr/executive"),
        ]
        return {"factors": factors, "evidence": [{"type": "contacts", "id": None, "observed_at": None,
                                                  "summary": f"covered functions: {', '.join(coverage['covered']) or 'none'}"}]}

    def _buying_stage(self, ctx: Ctx, company: Mapping[str, Any], hiring_score: float,
                      signals: Sequence[Mapping[str, Any]], now: datetime) -> Dict[str, Any]:
        company_id = company["id"]
        evidence: List[Dict[str, Any]] = []
        implementation = [s for s in signals if s["signal_type"] in _IMPLEMENTATION_SIGNALS]
        contacts = [c["id"] for c in self.store.all(ctx, "contacts", {"company_id": company_id}, cap=2000)]
        replies = sent = 0
        if contacts:
            replies = self.store.count(ctx, "message_events", {"contact_id__in": contacts, "event": "replied"})
            sent = self.store.count(ctx, "message_events", {"contact_id__in": contacts, "event": "sent"})
        activities = self.store.all(ctx, "activities", {"company_id": company_id}, order="-occurred_at", cap=200)
        engaged_acts = [a for a in activities if a["kind"] in _ENGAGED_ACTIVITY]
        opps = self.store.all(ctx, "opportunities", {"company_id": company_id}, cap=200)
        open_opps = [o for o in opps if o["status"] == "open"]
        won = [o for o in opps if o["status"] == "won"]

        factors = [_factor("hiring_intent", 25, 25 * hiring_score / 100, hiring_score,
                           f"hiring score {hiring_score}")]
        factors.append(_factor("implementation_signals", 15, 15 if implementation else 0, len(implementation),
                               f"{len(implementation)} implementation/specialised-technology signal(s)"
                               if implementation else "no implementation signals"))
        evidence += [_evidence("hiring_signal", s, f"{s['signal_type']}: {s.get('summary') or ''}", s.get("detected_at"))
                     for s in implementation[:5]]
        if replies:
            engagement, why = 25, f"{replies} repl(ies) received"
        elif engaged_acts:
            engagement, why = 20, f"{len(engaged_acts)} call/meeting/reply activit(ies) logged"
        elif sent:
            engagement, why = 5, f"{sent} email(s) sent, no reply recorded"
        else:
            engagement, why = 0, "no outreach or engagement recorded"
        factors.append(_factor("engagement", 25, engagement, {"replies": replies, "sent": sent,
                                                              "activities": len(engaged_acts)}, why))
        evidence += [_evidence("activity", a, f"{a['kind']}: {a['summary']}", a.get("occurred_at"))
                     for a in engaged_acts[:5]]
        if won or company.get("lifecycle") == "customer":
            deal, why = 25, (f"{len(won)} won deal(s)" if won else "lifecycle is customer")
        elif open_opps:
            deal, why = 20, f"{len(open_opps)} open deal(s)"
        else:
            deal, why = 0, "no deals"
        factors.append(_factor("deal_progress", 25, deal, {"open": len(open_opps), "won": len(won)}, why))
        evidence += [_evidence("opportunity", o, f"{o['title']} ({o['status']})") for o in (won + open_opps)[:5]]
        last = _aware(activities[0]["occurred_at"]) if activities else None
        if last is not None:
            age = (now - last).days
            factors.append(_factor("recent_activity", 10, 10 if age <= 30 else 5 if age <= 90 else 0, age,
                                   f"last activity {age} day(s) ago"))
        else:
            factors.append(_factor("recent_activity", 10, 0, None, "no activity recorded"))

        if won or company.get("lifecycle") == "customer":
            label = "customer"
        elif open_opps:
            label = "evaluating"
        elif replies or engaged_acts:
            label = "engaged"
        elif implementation:
            label = "researching"
        elif signals:
            label = "problem_aware"
        else:
            label = "unaware"
        return {"factors": factors, "evidence": evidence, "label": label}

    # --- public API ------------------------------------------------------------------

    def score_company(self, ctx: Ctx, company_id: str, *, persist: bool = True,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or utcnow()
        company = self.store.get(ctx, "companies", company_id)
        account = self._account(ctx, company)
        hiring = self._hiring(ctx, company_id)
        technology = self._technology(ctx, company, now)
        account_score = _total(account["factors"])
        hiring_score = _total(hiring["factors"])
        opportunity = self._opportunity(ctx, company_id, account_score, hiring_score)
        buying = self._buying_stage(ctx, company, hiring_score, hiring["signals"], now)
        parts = {"account": account, "hiring": hiring, "technology": technology, "opportunity": opportunity,
                 "buying_stage": buying}
        computed = now.isoformat()
        result: Dict[str, Any] = {}
        for kind, part in parts.items():
            result[kind] = {"score": _total(part["factors"]), "label": part.get("label"), "factors": part["factors"],
                            "evidence": part["evidence"], "computed_at": computed, "model": MODEL}
        if persist:
            ctx.require_write()
            self._persist_company(ctx, company, result, now)
        return {"entity_type": "company", "entity_id": company_id, "model": MODEL, "computed_at": computed,
                "persisted": persist, "scores": result}

    def _persist_company(self, ctx: Ctx, company: Mapping[str, Any], result: Mapping[str, Any], now: datetime) -> None:
        def legacy(kind: str) -> List[Dict[str, Any]]:
            return [{"component": f["name"], "weight": f["weight"], "value": f["points"], "reason": f["reason"]}
                    for f in result[kind]["factors"]]

        breakdown = {**{k: legacy(k) for k in COMPANY_KINDS}, "computed_at": now.isoformat(), "method": MODEL,
                     "buying_stage_label": result["buying_stage"]["label"]}
        self.store.update(ctx, "companies", company["id"], {
            "account_score": result["account"]["score"], "hiring_score": result["hiring"]["score"],
            "opportunity_score": result["opportunity"]["score"], "technology_score": result["technology"]["score"],
            "buying_stage": result["buying_stage"]["label"], "buying_stage_score": result["buying_stage"]["score"],
            "score_breakdown": breakdown, "scored_at": now})
        for kind in COMPANY_KINDS:
            self._snapshot(ctx, "company", company["id"], kind, result[kind], now)

    def score_contact(self, ctx: Ctx, contact_id: str, *, persist: bool = True,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or utcnow()
        contact = self.store.get(ctx, "contacts", contact_id)
        scored = self._signals.score_contact(ctx, contact)
        values = {"seniority": contact.get("seniority"), "function": contact.get("function") or contact.get("department"),
                  "email": contact.get("email_status") if contact.get("email") else None,
                  "confidence": contact.get("confidence")}
        factors = [_from_component(c, values.get(c["component"])) for c in scored["breakdown"]]
        evidence = [_evidence("contact", contact, f"{contact.get('full_name')}: {contact.get('title') or 'no title'}")]
        if contact.get("email"):
            validation = self.store.first(ctx, "email_validations", {"email": contact["email"].lower()})
            if validation is not None:
                evidence.append(_evidence("email_validation", validation,
                                          f"{validation['status']} via {validation['provider']}",
                                          validation.get("validated_at")))
        computed = now.isoformat()
        part = {"score": _total(factors), "label": None, "factors": factors, "evidence": evidence,
                "computed_at": computed, "model": MODEL}
        if persist:
            ctx.require_write()
            self.store.update(ctx, "contacts", contact_id, {
                "contact_score": part["score"], "scored_at": now,
                "score_breakdown": {"contact": scored["breakdown"], "computed_at": computed, "method": MODEL}})
            self._snapshot(ctx, "contact", contact_id, "contact", part, now)
        return {"entity_type": "contact", "entity_id": contact_id, "model": MODEL, "computed_at": computed,
                "persisted": persist, "scores": {"contact": part}}

    def _snapshot(self, ctx: Ctx, entity_type: str, entity_id: str, kind: str, part: Mapping[str, Any],
                  now: datetime) -> Optional[Dict[str, Any]]:
        """Append history only when the score or its factors changed."""
        last = self.store.first(ctx, "score_snapshots", {"entity_type": entity_type, "entity_id": entity_id,
                                                         "kind": kind}, order="-computed_at")
        if last is not None and last["score"] == part["score"] and last["factors"] == _plain(part["factors"]) \
                and last.get("label") == part.get("label"):
            return None
        return self.store.insert(ctx, "score_snapshots", {
            "entity_type": entity_type, "entity_id": entity_id, "kind": kind, "score": part["score"],
            "label": part.get("label"), "factors": _plain(part["factors"]), "evidence": _plain(part["evidence"]),
            "model": MODEL, "computed_at": now})

    def explain(self, ctx: Ctx, entity_type: str, entity_id: str) -> Dict[str, Any]:
        """The current explanation, computed live (read-only: nothing is written)."""
        if entity_type in ("company", "companies"):
            out = self.score_company(ctx, entity_id, persist=False)
        elif entity_type in ("contact", "contacts"):
            out = self.score_contact(ctx, entity_id, persist=False)
        else:
            raise ValidationError("entity_type must be company or contact")
        et = out["entity_type"]
        latest = self.store.first(ctx, "score_snapshots", {"entity_type": et, "entity_id": entity_id},
                                  order="-computed_at")
        out["last_saved_at"] = _iso(latest["computed_at"]) if latest else None
        out["how"] = model_description()["kinds"]
        return out

    def rescore(self, ctx: Ctx, entity_type: str, entity_id: str) -> Dict[str, Any]:
        ctx.require_write()
        if entity_type in ("company", "companies"):
            out = self.score_company(ctx, entity_id)
        elif entity_type in ("contact", "contacts"):
            out = self.score_contact(ctx, entity_id)
        else:
            raise ValidationError("entity_type must be company or contact")
        audit(self.store, ctx, "score.recompute", entity_type=f"{out['entity_type']}s", entity_id=entity_id,
              changes={k: v["score"] for k, v in out["scores"].items()})
        return out

    def history(self, ctx: Ctx, entity_type: str, entity_id: str, *, kind: Optional[str] = None,
                limit: int = 50) -> List[Dict[str, Any]]:
        et = {"companies": "company", "contacts": "contact"}.get(entity_type, entity_type)
        filters: Dict[str, Any] = {"entity_type": et, "entity_id": entity_id}
        if kind:
            filters["kind"] = kind
        return self.store.list(ctx, "score_snapshots", filters, order="-computed_at", limit=limit).rows


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def run_scoring_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Rescore ``params.company_ids`` / ``params.contact_ids``, or every active company (``params.all``)."""
    service: ScoringService = platform.service("scoring")
    params = task.get("params") or {}
    company_ids: List[str] = list(params.get("company_ids") or [])
    contact_ids: List[str] = list(params.get("contact_ids") or [])
    if params.get("all") or (not company_ids and not contact_ids):
        company_ids = [c["id"] for c in platform.store.all(ctx, "companies", {"status": "active"}, cap=100_000)]
    targets = [("company", i) for i in company_ids] + [("contact", i) for i in contact_ids]
    start = int(reporter.checkpoint.get("index", 0)) if reporter is not None else 0
    totals = {"targets": len(targets), "scored": 0, "errors": 0}
    for index in range(start, len(targets)):
        if reporter is not None:
            if reporter.is_cancelled():
                break
            if reporter.should_pause():
                from cloud.intel.tasks.worker import TaskPaused

                raise TaskPaused({"index": index})
            if index % 10 == 0:
                reporter.progress(f"Scoring {index + 1}/{len(targets)}", done=index, total=len(targets))
        entity_type, entity_id = targets[index]
        try:
            if entity_type == "company":
                service.score_company(ctx, entity_id)
            else:
                service.score_contact(ctx, entity_id)
            totals["scored"] += 1
        except Exception:  # noqa: BLE001 - one record must not stop the run
            log.exception("scoring failed for %s %s", entity_type, entity_id)
            totals["errors"] += 1
    return totals
