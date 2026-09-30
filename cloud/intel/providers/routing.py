"""Credit-aware routing: which source should fill each data gap, cheapest first.

Order, for every (company, need)::

    1. internal data            — already in this workspace's CRM
    2. existing known record    — e.g. an email validated in the last 30 days (the cache)
    3. free / public / permitted — the company's own website, its job postings, local DNS checks
    4. authorised free provider — a connected provider's credit-free operation (ZoomInfo search)
    5. paid provider            — ZoomInfo enrich / Seamless / EmailListVerify, ONLY if the gap
                                  is still open after 1-4, ONLY with allow_paid, ONLY with a reservation

:func:`plan_enrichment` never plans a paid step for data that is already present,
and every step says why it was chosen or skipped, so the plan can be shown to a
person before anything is spent.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cloud.intel.core.context import Ctx, utcnow

__all__ = ["DEFAULT_FUNCTIONS", "NEEDS", "function_of", "plan_enrichment"]

NEEDS = ("contacts", "emails", "technologies", "firmographics")
DEFAULT_FUNCTIONS = ("hr", "it", "executive")
_FIRMOGRAPHICS = ("industry", "employee_range", "revenue_range", "country")


def function_of(title: Optional[str]) -> str:
    from cloud.intel.vendor import seamless_targeting

    return seamless_targeting.classify(title or "")


def _paid_provider_for(need: str, connected: Mapping[str, bool]) -> Optional[str]:
    if need == "contacts":
        return "seamless" if connected.get("seamless") else ("zoominfo" if connected.get("zoominfo") else None)
    if need == "emails":
        return "emaillistverify" if connected.get("emaillistverify") else None
    if need in ("firmographics", "technologies"):
        return "zoominfo" if connected.get("zoominfo") else None
    return None


def plan_enrichment(platform: Any, ctx: Ctx, needs: Mapping[str, Any]) -> Dict[str, Any]:
    store = platform.store
    registry = platform.service("providers")
    company_ids: Sequence[str] = list(needs.get("company_ids") or [])
    wanted: Sequence[str] = [n for n in (needs.get("needs") or NEEDS) if n in NEEDS]
    functions: Sequence[str] = list(needs.get("functions") or DEFAULT_FUNCTIONS)
    connected = {p: registry.configured(ctx, p) for p in ("seamless", "zoominfo")}
    # Paid email validation is offered only once a live check verified the key (as the email service does).
    connected["emaillistverify"] = registry.enabled(ctx, "emaillistverify")
    fresh_after = utcnow() - timedelta(days=int(needs.get("max_age_days") or 30))

    steps: List[Dict[str, Any]] = []
    credits: Dict[str, float] = {}

    def step(company_id: str, need: str, source: str, action: str, reason: str, *, cost: float = 0.0,
             paid: bool = False, provider: Optional[str] = None, conditional: bool = False) -> None:
        steps.append({"company_id": company_id, "need": need, "source": source, "action": action, "reason": reason,
                      "estimated_credits": cost, "paid": paid, "provider": provider, "conditional": conditional})
        if paid and provider:
            credits[provider] = credits.get(provider, 0.0) + cost

    for company_id in company_ids:
        company = store.find(ctx, "companies", company_id)
        if company is None:
            step(company_id, "*", "none", "skip", "company not found in this workspace")
            continue
        contacts = store.all(ctx, "contacts", {"company_id": company_id, "status": "active"}, cap=2000)
        for need in wanted:
            if need == "contacts":
                have = {function_of(c.get("title")) for c in contacts}
                missing = [f for f in functions if f not in have]
                if not missing:
                    step(company_id, need, "internal", "use_existing", f"contacts already cover {', '.join(functions)}")
                    continue
                step(company_id, need, "internal", "use_existing",
                     f"{len(contacts)} contact(s) on file; missing functions: {', '.join(missing)}")
                if company.get("website") or company.get("domain"):
                    step(company_id, need, "public_web", "read_leadership_pages",
                         "free: the company's own leadership/team pages (published names and emails only)")
                else:
                    step(company_id, need, "public_web", "skip", "no website on record to read")
                provider = _paid_provider_for(need, connected)
                if provider == "zoominfo":
                    step(company_id, need, "zoominfo", "contact_search",
                         "authorised provider search (credit-free per the ZoomInfo client)", provider="zoominfo")
                    step(company_id, need, "zoominfo", "enrich_contacts", "paid, only for functions still missing",
                         cost=float(len(missing)), paid=True, provider="zoominfo", conditional=True)
                elif provider == "seamless":
                    from cloud.intel.providers.seamless import SeamlessConnector

                    connector = SeamlessConnector({})
                    cost = connector.estimate_cost("search_contacts", 10) + connector.estimate_cost(
                        "enrich_contacts", len(missing))
                    step(company_id, need, "seamless", "search_and_research",
                         f"paid, only if public sources leave {', '.join(missing)} missing",
                         cost=cost, paid=True, provider="seamless", conditional=True)
                else:
                    step(company_id, need, "paid", "skip", "no paid contact provider connected")
            elif need == "emails":
                with_email = [c for c in contacts if c.get("email")]
                if not with_email:
                    step(company_id, need, "internal", "skip", "no contact emails to validate")
                    continue
                current = [c for c in with_email if c.get("email_status") not in (None, "UNVERIFIED")
                           and c.get("email_validated_at") and c["email_validated_at"] >= fresh_after]
                stale = [c for c in with_email if c not in current]
                if current:
                    step(company_id, need, "cache", "use_existing",
                         f"{len(current)} email(s) validated within the freshness window; not re-validated")
                if not stale:
                    continue
                step(company_id, need, "local_validation", "syntax_mx_disposable_role",
                     f"free: {len(stale)} email(s) checked locally first")
                if connected.get("emaillistverify"):
                    step(company_id, need, "emaillistverify", "verify",
                         "paid, only for emails local checks cannot decide", cost=float(len(stale)), paid=True,
                         provider="emaillistverify", conditional=True)
            elif need == "technologies":
                techs = store.count(ctx, "company_technologies", {"company_id": company_id, "status": "active"})
                if techs:
                    step(company_id, need, "internal", "use_existing", f"{techs} technology record(s) with evidence")
                    continue
                step(company_id, need, "job_postings", "detect_in_postings",
                     "free: technologies named in the company's own job postings")
                if connected.get("zoominfo"):
                    step(company_id, need, "zoominfo", "technology_search",
                         "authorised provider search (credit-free lookup)", provider="zoominfo")
            elif need == "firmographics":
                missing_fields = [f for f in _FIRMOGRAPHICS if not company.get(f)]
                if not missing_fields:
                    step(company_id, need, "internal", "use_existing", "firmographics already on record")
                    continue
                step(company_id, need, "public_web", "read_home_page",
                     f"free: structured data on the company's site for {', '.join(missing_fields)}")
                if connected.get("zoominfo"):
                    step(company_id, need, "zoominfo", "company_search", "authorised provider search (credit-free)",
                         provider="zoominfo")
    return {"steps": steps, "estimated_credits": credits,
            "paid_steps": sum(1 for s in steps if s["paid"]),
            "note": ("paid steps are conditional: they run only if earlier steps leave the gap open, only when the "
                     "action is started with allow_paid, and only against a ledger reservation")}
