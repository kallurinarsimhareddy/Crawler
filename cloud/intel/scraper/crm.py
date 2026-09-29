"""Scraper results ↔ the SANA GTM CRM: matching, then PROPOSE → REVIEW → APPLY.

**Matching** (read-only) compares each company in a run's results with the CRM
through the platform's identity resolver (:meth:`dedupe.resolve`, the same one
imports use):

==================  ==================================================================
``existing``        EXACT/STRONG match and nothing the page says contradicts the CRM
``conflict``        EXACT/STRONG match, but a field disagrees (e.g. a different website)
``possible_duplicate``  PROBABLE/AMBIGUOUS: a person must decide
``new``             no CRM company shares a domain, name or alias
==================  ==================================================================

**Proposals** (:func:`propose`) are rows in ``scrape_proposals`` — companies,
jobs, contacts (a named hiring manager / decision maker / CEO), and, only when
asked, opportunities and follow-up tasks. Creating proposals changes nothing in
the CRM. A person approves or rejects each (:func:`review`); only then does
:func:`apply` write **approved** proposals through the normal CRM services
(provenance ``source_kind="scraper"``, audit, dedupe). A possible duplicate is
never merged automatically: applying it fails with "needs review".
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ValidationError, utcnow
from cloud.intel.core.normalize import company_name_key, domain_of

__all__ = ["ACTIONS", "apply", "match_run", "propose", "review"]

ACTIONS = ("company", "job", "contact", "opportunity", "task")
_COMPANY_FIELDS = {"company_name": "name", "website": "website", "domain": "domain", "careers_url": "careers_url",
                   "linkedin_url": "linkedin_url", "industry": "industry", "employee_count": "employee_count",
                   "description": "description", "headquarters": "hq_city", "phone": "phone"}
_CONFLICT_FIELDS = ("website", "domain", "industry", "linkedin_url", "careers_url")
_MERGEABLE = ("EXACT", "STRONG")


def _company_values(row: Mapping[str, Any]) -> Dict[str, Any]:
    from cloud.intel.store.spec import ENTITIES

    columns = ENTITIES["companies"].columns
    values = {target: row.get(source) for source, target in _COMPANY_FIELDS.items()
              if target in columns and row.get(source) not in (None, "", [])}
    if "name" not in values and row.get("domain"):
        values["name"] = row["domain"]
    return values


def _company_key(row: Mapping[str, Any]) -> str:
    return str(row.get("domain") or (domain_of(row["website"]) if row.get("website") else None)
               or company_name_key(row.get("company_name")) or row.get("source_url") or "")[:200]


def match_run(platform: Any, ctx: Ctx, run_id: str, *, limit: int = 2000) -> List[Dict[str, Any]]:
    """One entry per company in the run's results: CRM match status, company id, reasons, conflicts."""
    rows = platform.service("scraper").records(ctx, run_id, "companies", limit=limit)["items"]
    resolver = platform.service("dedupe")
    out: List[Dict[str, Any]] = []
    for row in rows:
        values = _company_values(row)
        if not values.get("name") and not values.get("website"):
            continue
        result = resolver.resolve(ctx, values)
        outcome = result.get("outcome") or "NONE"
        entry = {"key": _company_key(row), "company_name": row.get("company_name"), "website": row.get("website"),
                 "match": "new", "company_id": result.get("company_id"), "outcome": outcome,
                 "reasons": list(result.get("reasons") or [])[:5], "candidates": list(result.get("candidates") or [])[:5],
                 "conflicts": {}}
        if outcome in _MERGEABLE and result.get("company_id"):
            company = platform.store.get(ctx, "companies", result["company_id"])
            for field in _CONFLICT_FIELDS:
                mine = row.get(field)
                theirs = company.get(field)
                if field == "website" and mine and theirs:
                    mine, theirs = domain_of(mine), domain_of(theirs)
                if mine not in (None, "") and theirs not in (None, "") and str(mine).lower() != str(theirs).lower():
                    entry["conflicts"][field] = {"scraped": row.get(field), "crm": company.get(field)}
            entry["match"] = "conflict" if entry["conflicts"] else "existing"
            entry["crm_name"] = company.get("name")
        elif outcome not in ("NONE", None):
            entry["match"] = "possible_duplicate"
            entry["company_id"] = None
        out.append(entry)
    return out


def propose(platform: Any, ctx: Ctx, run_id: str, actions: Sequence[str] = ("company", "job"), *,
            task_title: Optional[str] = None) -> Dict[str, Any]:
    """Create proposals for a run (idempotent: an existing proposal is not duplicated). Changes no CRM data."""
    ctx.require_write()
    unknown = [a for a in actions if a not in ACTIONS]
    if unknown:
        raise ValidationError(f"unknown proposal actions: {', '.join(unknown)}")
    store = platform.store
    service = platform.service("scraper")
    run = service.get(ctx, run_id)
    if not (run["stats"].get("files") or {}).get("json"):
        raise ValidationError("the run has no results yet")
    matches = {m["key"]: m for m in match_run(platform, ctx, run_id)}
    existing = {(p["record_key"], p["action"]) for p in store.all(ctx, "scrape_proposals", {"run_id": run_id})}
    created = 0

    def add(record_key: str, action: str, payload: Dict[str, Any], match: Optional[Mapping[str, Any]]) -> None:
        nonlocal created
        record_key = record_key[:200]
        if (record_key, action) in existing:
            return
        existing.add((record_key, action))
        store.insert(ctx, "scrape_proposals", {
            "run_id": run_id, "record_key": record_key, "action": action, "status": "proposed",
            "match": (match or {}).get("match", "new"), "match_company_id": (match or {}).get("company_id"),
            "match_reasons": list((match or {}).get("reasons") or [])[:5]
            + ([f"conflicts: {', '.join(match['conflicts'])}"] if match and match.get("conflicts") else []),
            "payload": payload})
        created += 1

    companies = service.records(ctx, run_id, "companies", limit=5000)["items"]
    for row in companies:
        key = _company_key(row)
        match = matches.get(key)
        values = _company_values(row)
        if not values.get("name"):
            continue
        if "company" in actions:
            add(key, "company", {"values": values, "source_url": row.get("source_url")}, match)
        for field, title in (("hiring_manager", "Hiring manager"), ("decision_maker", "Decision maker"),
                             ("ceo", "CEO")):
            if "contact" in actions and row.get(field):
                add(f"{key}:{field}", "contact", {"values": {"full_name": row[field], "title": title},
                                                  "company_key": key, "source_url": row.get("source_url")}, match)
        if "opportunity" in actions:
            add(f"{key}:opportunity", "opportunity",
                {"title": f"{values['name']} — from scrape {run_id}", "company_key": key}, match)
        if "task" in actions:
            add(f"{key}:task", "task", {"title": (task_title or f"Review {values['name']} (scraped)")[:300],
                                        "company_key": key}, match)
    if "job" in actions:
        for row in service.records(ctx, run_id, "jobs", limit=10000)["items"]:
            if not row.get("job_url") or not row.get("job_title"):
                continue
            key = _company_key(row)
            add(f"job:{row['job_url']}", "job", {
                "posting": {k: row.get(k) for k in ("job_title", "job_url", "location", "posted_date", "department",
                                                    "employment_type", "description", "company_name", "website")
                            if row.get(k) not in (None, "", [])}, "company_key": key}, matches.get(key))
    audit(store, ctx, "scraper.propose", entity_type="scrape_runs", entity_id=run_id,
          summary=f"{created} CRM proposals ({', '.join(actions)}); nothing applied")
    return {"created": created, "total": len(existing)}


def review(platform: Any, ctx: Ctx, proposal_ids: Iterable[str], decision: str) -> List[Dict[str, Any]]:
    """Approve or reject proposals. Approving still changes nothing until :func:`apply`."""
    ctx.require_write()
    if decision not in ("approved", "rejected", "proposed"):
        raise ValidationError("decision must be approved, rejected or proposed")
    out = []
    for pid in proposal_ids:
        proposal = platform.store.get(ctx, "scrape_proposals", pid)
        if proposal["status"] in ("applied",):
            raise ConflictError(f"proposal {pid} was already applied")
        out.append(platform.store.update(ctx, "scrape_proposals", pid, {"status": decision, "reviewed_at": utcnow()}))
    audit(platform.store, ctx, f"scraper.proposals.{decision}", entity_type="scrape_proposals",
          summary=f"{len(out)} proposals {decision}")
    return out


def apply(platform: Any, ctx: Ctx, proposal_ids: Iterable[str]) -> Dict[str, Any]:
    """Write **approved** proposals to the CRM. Anything else is refused."""
    ctx.require_write()
    store = platform.store
    crm = platform.service("crm")
    proposals = [store.get(ctx, "scrape_proposals", pid) for pid in proposal_ids]
    not_approved = [p["id"] for p in proposals if p["status"] != "approved"]
    if not_approved:
        raise ValidationError(f"only approved proposals can be applied ({len(not_approved)} are not approved)")
    order = {"company": 0, "contact": 1, "job": 2, "opportunity": 3, "task": 4}
    company_ids: Dict[str, str] = {}
    for p in store.all(ctx, "scrape_proposals", {"action": "company", "status": "applied"}):
        company_ids[p["record_key"]] = p["applied_entity_id"]
    results = {"applied": 0, "failed": 0}
    for proposal in sorted(proposals, key=lambda p: order[p["action"]]):
        payload = proposal["payload"]
        try:
            entity_type, entity_id = _apply_one(platform, ctx, crm, proposal, payload, company_ids)
            store.update(ctx, "scrape_proposals", proposal["id"], {"status": "applied", "applied_entity_type":
                                                                   entity_type, "applied_entity_id": entity_id,
                                                                   "error": None})
            if proposal["action"] == "company":
                company_ids[proposal["record_key"]] = entity_id
            results["applied"] += 1
        except Exception as error:  # noqa: BLE001 - one proposal failing must not stop the others
            store.update(ctx, "scrape_proposals", proposal["id"], {"status": "failed",
                                                                   "error": f"{type(error).__name__}: {error}"[:1000]})
            results["failed"] += 1
    audit(store, ctx, "scraper.proposals.apply", entity_type="scrape_proposals",
          summary=f"{results['applied']} applied, {results['failed']} failed")
    return results


def _apply_one(platform: Any, ctx: Ctx, crm: Any, proposal: Mapping[str, Any], payload: Mapping[str, Any],
               company_ids: Dict[str, str]) -> tuple:
    run_id = proposal["run_id"]
    source = dict(source_kind="scraper", source_name=f"scrape {run_id}")
    company_id = company_ids.get(payload.get("company_key") or proposal["record_key"]) or proposal.get("match_company_id")
    action = proposal["action"]
    if action == "company":
        if proposal["match"] == "possible_duplicate":
            raise ConflictError("needs review: this may duplicate an existing company; resolve it in the CRM")
        result = crm.upsert_company(ctx, payload["values"], source_ref=payload.get("source_url"),
                                    auto_merge=proposal["match"] in ("existing", "conflict"), **source)
        if result.get("needs_review") or not result.get("company"):
            raise ConflictError("needs review: the CRM found a possible duplicate")
        return "companies", result["company"]["id"]
    if action == "contact":
        values = dict(payload["values"])
        if company_id:
            values["company_id"] = company_id
        result = crm.upsert_contact(ctx, values, source_ref=payload.get("source_url"), **source)
        contact = result.get("contact") if isinstance(result, dict) else None
        return "contacts", (contact or result).get("id")
    if action == "job":
        posting = dict(payload["posting"])
        if company_id:
            posting["company_id"] = company_id
        stats = platform.service("jobs").ingest_postings(ctx, [posting], **source)
        if stats.get("rejected"):
            raise ValidationError("; ".join(p["problem"] for p in stats.get("problems", [])[:3]) or "rejected")
        return "job_postings", posting.get("job_url")
    if not company_id:
        raise ValidationError("apply the company proposal first (or match it to a CRM company)")
    if action == "opportunity":
        opportunity = crm.create_opportunity(ctx, company_id, payload["title"], source="scraper",
                                             reason=f"proposed from scrape {run_id}")
        return "opportunities", opportunity["id"]
    task = crm.create_task(ctx, {"title": payload["title"], "company_id": company_id, "source": "scraper"})
    return "crm_tasks", task["id"]
