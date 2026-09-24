"""From an intent to an ordered, reviewable plan.

Each step names one tool from :mod:`cloud.intel.research.tools`, its
parameters, what it reads and writes, whether it can spend provider credits
(with an upper-bound estimate) and whether it changes CRM data.

Execution rules, enforced by :mod:`cloud.intel.research.service`:

* read-only steps run on approval;
* credit-spending steps run in their free mode (internal data, cache, public
  sources) unless the approval explicitly sets ``allow_paid``;
* CRM-mutating steps (create list, create opportunities, assign campaigns,
  enrol in sequences) **never** run inside the research task — they become
  *proposed actions* the user applies one by one.
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["build_plan", "estimate_credits"]

#: Upper-bound credit costs per unit, used only for the preview.
_COST = {"contact_enrichment": 1.0, "email_validation": 1.0}


def _step(n: int, tool: str, description: str, params: Dict[str, Any], *, reads=(), writes=(),
          spends_credits: bool = False, estimate: Dict[str, float] = None, mutates_crm: bool = False) -> Dict[str, Any]:
    return {
        "id": f"s{n}", "tool": tool, "description": description, "params": params,
        "reads": list(reads), "writes": list(writes), "spends_credits": spends_credits,
        "estimated_credits": estimate or {}, "mutates_crm": mutates_crm,
        "requires_approval": True,  # every run is approved as a whole; paid/mutating need more (below)
        "execution": "proposal" if mutates_crm else ("paid_if_approved" if spends_credits else "auto"),
        "status": "planned",
    }


def build_plan(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    n = 0
    target = intent.get("count") or 100
    functions = intent.get("contact_functions") or []
    seniorities = intent.get("contact_seniorities") or []

    def add(*args, **kwargs):
        nonlocal n
        n += 1
        steps.append(_step(n, *args, **kwargs))

    add("query_companies", "Find candidate companies in the workspace's data",
        {"country": intent.get("country"), "industries": intent.get("industries", []),
         "technologies": intent.get("technologies", []), "limit": max(target * 20, 1000)},
        reads=["companies", "company_technologies"])
    if intent.get("match_internal"):
        add("match_internal", "Match candidates against internal company records (identity resolution)", {},
            reads=["companies", "source_records"])
    if intent.get("exclude_existing_crm"):
        add("exclude_existing_crm", "Remove companies already in the CRM (accounts, customers, open opportunities)",
            {"lifecycles": ["account", "customer", "partner"], "with_opportunities": True},
            reads=["companies", "opportunities"])
    hiring = intent.get("hiring") or {}
    if hiring.get("required") or hiring.get("keywords") or hiring.get("signal_types"):
        add("hiring_signals", "Keep companies with matching hiring evidence (signals and job postings)",
            {"keywords": hiring.get("keywords", []), "signal_types": hiring.get("signal_types", []),
             "window_days": hiring.get("window_days", 90), "required": bool(hiring.get("required", True))},
            reads=["hiring_signals", "job_postings"])
    if functions or seniorities:
        estimate = {"contact_enrichment": target * max(1, len(functions) + len(seniorities)) * _COST["contact_enrichment"]}
        add("find_contacts", "Contact gap analysis, then fill missing contacts from authorized sources",
            {"functions": functions, "seniorities": seniorities,
             "missing_only": bool(intent.get("missing_contacts_only", True)),
             "authorized_sources": bool(intent.get("use_authorized_sources"))},
            reads=["contacts"], writes=["contacts", "source_records", "credit_ledger"],
            spends_credits=True, estimate=estimate)
    if intent.get("validate_emails"):
        add("validate_emails", "Validate contact emails (cache and local DNS checks first)", {"max_age_days": 30},
            reads=["contacts", "email_validations"], writes=["email_validations", "credit_ledger"],
            spends_credits=True, estimate={"email_validation": target * 3 * _COST["email_validation"]})
    add("score", "Score and rank companies with explainable account/hiring/opportunity scores",
        {"limit": intent.get("count")}, reads=["hiring_signals", "job_postings", "contacts"])
    if intent.get("assign_campaign"):
        add("assign_campaign", "Match each company to the best GTM campaign (proposal)", {},
            reads=["campaigns"], mutates_crm=True)
    if intent.get("create_list"):
        add("create_list", "Create an outreach list from the results (proposal)",
            {"name": "Research: " + (intent.get("question") or "results")[:150]}, writes=["lists", "list_members"],
            mutates_crm=True)
    if intent.get("create_opportunities") or intent.get("assign_campaign"):
        add("create_opportunities", "Create CRM opportunities for the ranked companies (proposal)", {},
            writes=["opportunities", "activities"], mutates_crm=True)
    if intent.get("export"):
        add("export", "Export the ranked results with evidence", {"format": intent.get("export_format", "xlsx")},
            writes=["exports"])
    return steps


def estimate_credits(plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    total: Dict[str, float] = {}
    for step in plan:
        for kind, amount in step.get("estimated_credits", {}).items():
            total[kind] = total.get(kind, 0) + amount
    return {"upper_bound": total,
            "note": "Upper bound. Paid providers are used only if you approve with allow_paid; "
                    "internal data, cached validations and free sources are always tried first."}
