"""Explainable scores with reason codes: Account, Contact, Hiring, Opportunity, Intent.

Account, hiring, opportunity and contact scores come from the existing rules
engine (:class:`cloud.intel.signals.service.SignalService`); this module only
restates their components as signed reason codes (``+20 hiring spike``), and
adds the **Intent Score**: how strongly a company's recent evidence matches what
*this request* asked for (its technologies, hiring keywords and signal types).

No score here is produced by an LLM. Every point is traceable to a component.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

__all__ = ["intent_score", "reason_codes", "score_card"]

_SIGNAL_POINTS = {
    "HIRING_SPIKE": 20, "PROJECT_IMPLEMENTATION": 15, "SPECIALIZED_TECHNOLOGY": 15, "LEADERSHIP_HIRING": 15,
    "MULTIPLE_RELEVANT_ROLES": 12, "LONG_OPEN_ROLE": 8, "HARD_TO_FILL": 8, "HIRING_VELOCITY": 10,
    "EXPANSION_HIRING": 10, "NEW_ROLE": 6, "BACKFILL_REPLACEMENT": 4,
}


def _code(points: float, label: str, **evidence: Any) -> Dict[str, Any]:
    return {"points": round(points, 1), "label": label, "code": f"{'+' if points >= 0 else ''}{round(points):d} {label}",
            **({"evidence": evidence} if evidence else {})}


def reason_codes(components: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Turn rules-engine components (``name``, ``value``, ``reason``) into signed reason codes."""
    out = []
    for comp in components or []:
        value = float(comp.get("value") or 0)
        if abs(value) < 0.05:
            continue
        label = str(comp.get("reason") or comp.get("name") or "").strip()
        out.append(_code(value, label or str(comp.get("name"))))
    return sorted(out, key=lambda c: -abs(c["points"]))


def intent_score(company: Mapping[str, Any], *, signals: Iterable[Mapping[str, Any]] = (),
                 jobs: Iterable[Mapping[str, Any]] = (), technologies: Iterable[str] = (),
                 keywords: Iterable[str] = (), signal_types: Iterable[str] = ()) -> Dict[str, Any]:
    """0-100: how well this company's evidence matches the request. Returns score and reason codes."""
    wanted_tech = {t.lower() for t in technologies}
    wanted_kw = [k.lower() for k in keywords]
    wanted_types = set(signal_types)
    codes: List[Dict[str, Any]] = []
    have_tech = {t.lower() for t in company.get("technologies") or []}
    tech_hits = sorted(t for t in wanted_tech if any(t in h for h in have_tech))
    if tech_hits:
        codes.append(_code(min(20, 10 * len(tech_hits)), f"uses {', '.join(tech_hits)}"))
    jobs = list(jobs)
    relevant = [j for j in jobs if not wanted_kw or any(k in (j.get("title") or "").lower() for k in wanted_kw)
                or any(t in " ".join(j.get("technologies") or []).lower() for t in wanted_tech)]
    if relevant:
        codes.append(_code(min(25, 6 * len(relevant)), f"{len(relevant)} matching open role(s)",
                           job_ids=[j.get("id") for j in relevant[:10]]))
    seen = set()
    for signal in signals:
        kind = signal.get("signal_type")
        if not kind or kind in seen:
            continue
        seen.add(kind)
        points = _SIGNAL_POINTS.get(kind, 5) * (1.3 if kind in wanted_types else 1.0)
        codes.append(_code(points, kind.replace("_", " ").lower(), signal_id=signal.get("id")))
    total = round(min(100.0, sum(c["points"] for c in codes)), 1)
    return {"score": total, "reasons": sorted(codes, key=lambda c: -c["points"]), "method": "intent-rules-v1"}


def score_card(scores: Optional[Mapping[str, Any]], intent: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Every score for one company, each with its reason codes."""
    scores = scores or {}
    breakdown = scores.get("breakdown") or {}
    card = {
        "account": {"score": scores.get("account_score"), "reasons": reason_codes(breakdown.get("account"))},
        "hiring": {"score": scores.get("hiring_score"), "reasons": reason_codes(breakdown.get("hiring"))},
        "opportunity": {"score": scores.get("opportunity_score"), "reasons": reason_codes(breakdown.get("opportunity"))},
    }
    if intent:
        card["intent"] = {"score": intent.get("score"), "reasons": intent.get("reasons", [])}
    return card
