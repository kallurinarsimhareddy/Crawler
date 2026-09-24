"""GTM campaign mapping: which private workspace campaign a hiring signal belongs to.

Flow, and what each step leaves behind::

    hiring signal ─► relevant company / jobs ─► evidence ─► campaign (scored, with reasons)
                 ─► opportunity (PROPOSED unless create=True) ─► target contacts ─► sequence suggestion

Nothing here sends a message. Mapping produces a *proposal*; creating the
opportunity is an explicit call (``create=True``); enrolling contacts in a
sequence is a separate explicit action that lands in ``pending_approval``
(see :mod:`cloud.intel.gtm.sequences`).

Scores are explainable: every point added to a campaign's score is recorded
as a reason, so a user can see *why* COX-LITTLE beat ITECH US for a company.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError

__all__ = ["CampaignService", "DEFAULT_CAMPAIGNS"]

log = logging.getLogger(__name__)

#: The three private workspace campaigns. Seeded per workspace, never shared.
DEFAULT_CAMPAIGNS: List[Dict[str, Any]] = [
    {
        "key": "cox-little",
        "name": "COX-LITTLE & COMPANY",
        "brand": "COX-LITTLE & COMPANY",
        "description": "ERP consulting: implementation, modernization and ERP leadership.",
        "focus_keywords": ["ERP", "SAP", "Oracle", "JD Edwards", "Infor", "Microsoft Dynamics", "iSeries", "RPG",
                           "AS400", "AS/400", "IBM i", "WMS", "ERP implementation", "modernization",
                           "ERP leadership", "S/4HANA", "NetSuite", "Epicor", "Syteline"],
        "technologies": ["SAP", "Oracle", "JD Edwards", "Infor", "Microsoft Dynamics", "iSeries", "RPG", "AS400",
                         "WMS", "NetSuite", "Epicor"],
        "departments": ["IT", "Information Technology", "Finance", "Operations", "Supply Chain"],
        "signal_types": ["PROJECT_IMPLEMENTATION", "SPECIALIZED_TECHNOLOGY", "LEADERSHIP_HIRING",
                         "LONG_OPEN_ROLE", "HARD_TO_FILL"],
        "target_titles": ["CIO", "VP IT", "VP of Information Technology", "ERP Director", "IT Director", "CFO"],
    },
    {
        "key": "riseit",
        "name": "RISEIT",
        "brand": "RISEIT",
        "description": "Engineering, IT, digital transformation and technology expansion.",
        "focus_keywords": ["engineering", "software engineer", "IT", "digital transformation", "technology",
                           "platform", "DevOps", "cloud", "architecture", "expansion"],
        "technologies": ["AWS", "Azure", "GCP", "Kubernetes", "Java", "Python", ".NET", "React"],
        "departments": ["Engineering", "IT", "Information Technology", "Product", "Technology"],
        "signal_types": ["EXPANSION_HIRING", "HIRING_SPIKE", "HIRING_VELOCITY", "MULTIPLE_RELEVANT_ROLES"],
        "target_titles": ["CTO", "CIO", "VP Engineering", "Director of Engineering", "IT Director"],
    },
    {
        "key": "itech-us",
        "name": "ITECH US",
        "brand": "ITECH US",
        "description": "IT staffing, staff augmentation and project teams.",
        "focus_keywords": ["IT staffing", "staff augmentation", "contract", "project team", "cloud", "data",
                           "developer", "development", "QA", "quality assurance", "SAP", "infrastructure",
                           "migration", "implementation", "data engineer", "analyst"],
        "technologies": ["SAP", "AWS", "Azure", "Snowflake", "Databricks", "Java", "Python", "Selenium",
                         "ServiceNow", "Salesforce"],
        "departments": ["IT", "Information Technology", "Engineering", "Data", "QA"],
        "signal_types": ["MULTIPLE_RELEVANT_ROLES", "HARD_TO_FILL", "LONG_OPEN_ROLE", "HIRING_SPIKE"],
        "target_titles": ["HR Director", "Talent Acquisition", "Recruiting Manager", "Head of Recruiting",
                          "IT Manager", "IT Director"],
    },
]

# Points per kind of overlap. Kept small and explicit so a reason list adds up.
_W_SIGNAL = 12.0
_W_TECH = 8.0
_W_KEYWORD = 4.0
_W_DEPARTMENT = 3.0
_CAP = 100.0


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def _contains_term(haystack: str, term: str) -> bool:
    """Word-boundary match, so "IT" does not match "with" and "RPG" not "rpgx"."""
    term = _norm(term)
    if not term or not haystack:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", haystack) is not None


class CampaignService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- defaults -------------------------------------------------------------

    def ensure_defaults(self, ctx: Ctx) -> None:
        """Seed COX-LITTLE, RISEIT and ITECH US for this workspace. Idempotent by key."""
        for template in DEFAULT_CAMPAIGNS:
            if self.store.first(ctx, "campaigns", {"key": template["key"]}) is not None:
                continue
            values = {**template, "status": "draft", "sending_enabled": False,
                      "rules": {"seeded": True, "min_score": 20}}
            try:
                row = self.store.insert(ctx, "campaigns", values)
            except ConflictError:
                continue  # a concurrent seed won
            audit(self.store, ctx, "campaign.seed", entity_type="campaigns", entity_id=row["id"],
                  summary=row["name"])

    # --- matching ---------------------------------------------------------------

    def match_campaigns(self, ctx: Ctx, company: Mapping[str, Any], signals: Sequence[Mapping[str, Any]],
                        jobs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Score every non-archived campaign for this company, highest first.

        Returns ``[{"campaign": row, "score": float, "reasons": [str]}]``.
        Every point is explained; a campaign with no overlap scores 0.
        """
        campaigns = [c for c in self.store.all(ctx, "campaigns", cap=200) if c["status"] != "archived"]
        tech_text = " | ".join(_norm(t) for t in (company.get("technologies") or []))
        job_text = " | ".join(
            _norm(" ".join([str(j.get("title") or ""), str(j.get("department") or ""),
                            " ".join(j.get("technologies") or []), " ".join(j.get("skills") or [])]))
            for j in jobs)
        company_text = _norm(" ".join([str(company.get("industry") or ""), str(company.get("description") or ""),
                                       " ".join(company.get("tags") or [])]))
        departments = {_norm(j.get("department")) for j in jobs if j.get("department")}
        signal_types = {}
        for signal in signals:
            signal_types.setdefault(signal.get("signal_type"), signal)

        results = []
        for campaign in campaigns:
            score, reasons = 0.0, []
            for signal_type in campaign.get("signal_types") or []:
                if signal_type in signal_types:
                    score += _W_SIGNAL
                    summary = signal_types[signal_type].get("summary") or ""
                    reasons.append(f"signal {signal_type}" + (f": {summary}" if summary else ""))
            for tech in campaign.get("technologies") or []:
                where = []
                if _contains_term(tech_text, tech):
                    where.append("company technologies")
                if _contains_term(job_text, tech):
                    where.append("job postings")
                if where:
                    score += _W_TECH * len(where)
                    reasons.append(f"technology {tech} in {' and '.join(where)}")
            for keyword in campaign.get("focus_keywords") or []:
                if keyword in (campaign.get("technologies") or []):
                    continue  # already counted as a technology
                if _contains_term(job_text, keyword) or _contains_term(company_text, keyword):
                    score += _W_KEYWORD
                    reasons.append(f"keyword '{keyword}'")
            for dept in campaign.get("departments") or []:
                if _norm(dept) in departments:
                    score += _W_DEPARTMENT
                    reasons.append(f"hiring in {dept}")
            results.append({"campaign": campaign, "score": round(min(score, _CAP), 1), "reasons": reasons})
        results.sort(key=lambda r: (-r["score"], r["campaign"]["key"]))
        return results

    # --- signal -> opportunity mapping ------------------------------------------

    def _company_context(self, ctx: Ctx, company_id: str):
        company = self.store.get(ctx, "companies", company_id)
        signals = self.store.all(ctx, "hiring_signals", {"company_id": company_id, "status": "active"}, cap=200)
        jobs = self.store.all(ctx, "job_postings", {"company_id": company_id, "status": "open"}, cap=500)
        return company, signals, jobs

    def target_contacts(self, ctx: Ctx, company_id: str, campaign: Mapping[str, Any]) -> Dict[str, Any]:
        """Contacts at the company whose titles match the campaign's target titles,
        plus the gap analysis from the contact track when it is available."""
        titles = [_norm(t) for t in campaign.get("target_titles") or []]
        found = []
        for contact in self.store.all(ctx, "contacts", {"company_id": company_id, "status": "active"}, cap=500):
            title = _norm(contact.get("title"))
            matched = [t for t in titles if t and (_contains_term(title, t) or t in title)]
            if matched:
                found.append({"contact_id": contact["id"], "full_name": contact["full_name"],
                              "title": contact.get("title"), "email_status": contact.get("email_status"),
                              "matched_titles": matched})
        gap = None
        try:
            gap = self.platform.service("contacts").gap_analysis(ctx, company_id)
        except Exception:  # noqa: BLE001 - optional track; mapping works without it
            log.debug("contact gap analysis unavailable", exc_info=True)
        missing = [t for t in campaign.get("target_titles") or []
                   if not any(t in c["matched_titles"] or _norm(t) in c["matched_titles"] for c in found)]
        return {"found": found, "missing_titles": missing, "gap_analysis": gap}

    def map_signal_to_opportunity(self, ctx: Ctx, company_id: str, *, create: bool = False,
                                  campaign_id: Optional[str] = None, min_score: float = 20.0) -> Dict[str, Any]:
        """Hiring signal -> company/jobs -> evidence -> campaign -> opportunity -> contacts -> sequence.

        With ``create=False`` (the default) this is a *proposal*: nothing is written.
        With ``create=True`` it creates one opportunity through the CRM track.
        It never enrolls anyone and never sends anything.
        """
        company, signals, jobs = self._company_context(ctx, company_id)
        matches = self.match_campaigns(ctx, company, signals, jobs)
        if campaign_id is not None:
            matches = [m for m in matches if m["campaign"]["id"] == campaign_id]
            if not matches:
                raise NotFoundError(f"campaign {campaign_id} not found")
        best = matches[0] if matches else None
        relevant_jobs = [j for j in jobs if j.get("is_relevant")] or jobs
        evidence = [
            {"kind": "signal", "signal_id": s["id"], "signal_type": s["signal_type"], "summary": s.get("summary"),
             "confidence": s.get("confidence"), "detected_at": s.get("detected_at")}
            for s in signals
        ] + [
            {"kind": "job", "job_posting_id": j["id"], "title": j["title"], "url": j.get("job_url"),
             "first_seen_at": j.get("first_seen_at")}
            for j in relevant_jobs[:20]
        ]
        proposal: Dict[str, Any] = {
            "company_id": company_id,
            "company_name": company["name"],
            "campaign_scores": [{"campaign_id": m["campaign"]["id"], "key": m["campaign"]["key"],
                                 "name": m["campaign"]["name"], "score": m["score"], "reasons": m["reasons"]}
                                for m in matches],
            "evidence": evidence,
            "created": False,
            "opportunity": None,
        }
        if best is None or (campaign_id is None and best["score"] < min_score):
            proposal["status"] = "no_campaign_match"
            proposal["reason"] = (f"best campaign score {best['score'] if best else 0} is below {min_score}"
                                  if best else "no campaigns in this workspace")
            return proposal
        campaign = best["campaign"]
        signal_types = sorted({s["signal_type"] for s in signals})
        title = f"{company['name']} – {campaign['name']}"
        if signal_types:
            title += f" ({', '.join(signal_types[:3])})"
        reason = "; ".join(best["reasons"][:8]) or "campaign selected explicitly"
        proposal.update({
            "status": "proposed",
            "campaign": {"id": campaign["id"], "key": campaign["key"], "name": campaign["name"]},
            "opportunity_draft": {"title": title[:300], "score": best["score"], "reason": reason[:2000],
                                  "signal_ids": [s["id"] for s in signals], "signal_types": signal_types},
            "target_contacts": self.target_contacts(ctx, company_id, campaign),
            "sequence_suggestion": ({"sequence_id": campaign.get("default_sequence_id"),
                                     "note": "enrolling is a separate explicit action; enrollments start "
                                             "in pending_approval"}
                                    if campaign.get("default_sequence_id") else
                                    {"sequence_id": None, "note": "campaign has no default sequence"}),
        })
        if create:
            ctx.require_write()
            opportunity = self.platform.service("crm").create_opportunity(
                ctx, company_id, title[:300], signal_ids=[s["id"] for s in signals], signal_types=signal_types,
                score=best["score"], score_breakdown={"campaign_match": best["reasons"]}, reason=reason[:2000],
                campaign_id=campaign["id"], evidence=evidence, source="campaign_mapping")
            proposal.update({"created": True, "opportunity": opportunity, "status": "created"})
            audit(self.store, ctx, "campaign.map_opportunity", entity_type="opportunities",
                  entity_id=opportunity.get("id"), summary=title[:200],
                  changes={"campaign_id": campaign["id"], "score": best["score"]})
        return proposal

    def assign_campaign(self, ctx: Ctx, opportunity_id: str, campaign_id: str) -> Dict[str, Any]:
        self.store.get(ctx, "campaigns", campaign_id)
        row = self.store.update(ctx, "opportunities", opportunity_id, {"campaign_id": campaign_id})
        audit(self.store, ctx, "opportunity.assign_campaign", entity_type="opportunities", entity_id=opportunity_id,
              changes={"campaign_id": campaign_id})
        return row
