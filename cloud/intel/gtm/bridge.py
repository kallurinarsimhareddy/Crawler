"""Scraper and research results → GTM, every step reviewable.

    scrape / research run
      → normalise + de-duplicate (companies by domain/name, contacts by email)
      → company matching (the identity resolver imports use) + contact matching
      → prospect list          (existing CRM records only)
      → email validation job   (a separate job; results never touch the CRM by themselves)
      → campaign preparation   (a DRAFT campaign, sending off, audience = the list)

Plus "Create CRM Proposal" (scraper PROPOSE → REVIEW → APPLY; research runs
already carry proposed actions) and "Research These" (a *planned* research run
that waits for approval).

Nothing here creates a CRM company or contact, sends a message or spends a
credit. Companies not yet in the CRM are reported (``new``) with the way to add
them — a CRM proposal a person approves.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ValidationError
from cloud.intel.core.normalize import company_name_key, domain_of, normalize_email

__all__ = ["GtmBridgeService", "SOURCE_TYPES", "EMAIL_FIELDS"]

log = logging.getLogger(__name__)

SOURCE_TYPES = ("scrape", "research")
#: Record fields that may hold a person's email address.
EMAIL_FIELDS = ("email", "contact_email", "hiring_manager_email", "decision_maker_email", "ceo_email", "work_email")
_PERSON_FIELDS = (("hiring_manager", "Hiring manager"), ("decision_maker", "Decision maker"), ("ceo", "CEO"),
                  ("contact_name", None), ("full_name", None))
_MERGEABLE = ("EXACT", "STRONG")


def _company_key(row: Mapping[str, Any]) -> str:
    return str(domain_of(row.get("domain") or row.get("website") or "") or company_name_key(row.get("company_name")
                                                                                            or row.get("name")) or "")


class GtmBridgeService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- reading a source ----------------------------------------------------------

    def _records(self, ctx: Ctx, source_type: str, source_id: str) -> Tuple[List[Dict[str, Any]], str]:
        """Raw result rows of a run, plus a short label for names."""
        if source_type == "scrape":
            scraper = self.platform.service("scraper")
            run = scraper.get(ctx, source_id)
            rows = scraper.records(ctx, source_id, "all", limit=10000).get("items") or []
            label = str(run.get("name") or run.get("instruction") or source_id)[:60]
            return [dict(r) for r in rows], label
        if source_type == "research":
            run = self.store.get(ctx, "research_runs", source_id)
            rows = []
            for result in self.store.all(ctx, "research_results", {"run_id": source_id}, order="rank", cap=5000):
                data = dict(result.get("data") or {})
                if result.get("company_id"):
                    company = self.store.find(ctx, "companies", result["company_id"]) or {}
                    data.setdefault("company_name", company.get("name"))
                    data.setdefault("website", company.get("website"))
                    data.setdefault("domain", company.get("domain"))
                    data["company_id"] = result["company_id"]
                if result.get("contact_id"):
                    data["contact_id"] = result["contact_id"]
                rows.append(data)
            return rows, str(run.get("question") or source_id)[:60]
        raise ValidationError(f"source_type must be one of {', '.join(SOURCE_TYPES)}")

    def prepare(self, ctx: Ctx, source_type: str, source_id: str) -> Dict[str, Any]:
        """Read-only: normalised, de-duplicated companies and contacts with their CRM matches."""
        rows, label = self._records(ctx, source_type, source_id)
        resolver = self.platform.service("dedupe")
        companies: Dict[str, Dict[str, Any]] = {}
        contacts: Dict[str, Dict[str, Any]] = {}
        duplicates = 0
        for row in rows:
            key = _company_key(row)
            name = row.get("company_name") or row.get("name")
            if key and (name or row.get("website") or row.get("domain")):
                if key in companies:
                    duplicates += 1
                else:
                    entry = {"key": key, "name": name, "website": row.get("website"),
                             "domain": domain_of(row.get("domain") or row.get("website") or "") or None,
                             "source_url": row.get("source_url"), "match": "new", "company_id": None,
                             "reasons": []}
                    if row.get("company_id"):
                        entry.update(match="existing", company_id=row["company_id"], reasons=["linked by the run"])
                    else:
                        values = {"name": name or entry["domain"], "website": row.get("website"),
                                  "domain": entry["domain"]}
                        try:
                            result = resolver.resolve(ctx, {k: v for k, v in values.items() if v})
                        except Exception as error:  # noqa: BLE001 - a bad row must not stop the preview
                            result = {"outcome": "NONE", "reasons": [str(error)[:200]]}
                        outcome = result.get("outcome") or "NONE"
                        if outcome in _MERGEABLE and result.get("company_id"):
                            entry.update(match="existing", company_id=result["company_id"])
                        elif outcome not in ("NONE", None):
                            entry["match"] = "possible_duplicate"
                        entry["reasons"] = list(result.get("reasons") or [])[:3]
                    companies[key] = entry
            for email, person in self._people(row):
                if email in contacts:
                    duplicates += 1
                    continue
                existing = self.store.first(ctx, "contacts", {"email": email})
                contacts[email] = {"email": email, "full_name": person.get("full_name"), "title": person.get("title"),
                                   "company_key": key or None, "match": "existing" if existing else "new",
                                   "contact_id": (existing or {}).get("id") or row.get("contact_id"),
                                   "source_url": row.get("source_url")}
        summary = {"records": len(rows), "duplicates_removed": duplicates,
                   "companies": len(companies), "contacts": len(contacts)}
        for group, items in (("companies", companies), ("contacts", contacts)):
            for state in ("existing", "new", "possible_duplicate"):
                summary[f"{group}_{state}"] = sum(1 for i in items.values() if i["match"] == state)
        return {"source_type": source_type, "source_id": source_id, "label": label,
                "companies": list(companies.values()), "contacts": list(contacts.values()), "summary": summary,
                "next": ["add_to_list", "validate_emails", "create_campaign", "create_crm_proposal",
                         "research_these"],
                "note": "Read-only preview. New companies reach the CRM only through an approved CRM proposal."}

    @staticmethod
    def _people(row: Mapping[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
        seen = set()
        for field in EMAIL_FIELDS:
            values = row.get(field)
            for value in values if isinstance(values, list) else [values]:
                email = normalize_email(str(value)) if value else None
                if not email or email in seen:
                    continue
                seen.add(email)
                person: Dict[str, Any] = {}
                prefix = field[:-len("_email")] if field.endswith("_email") else ""
                for name_field, title in _PERSON_FIELDS:
                    if (not prefix or name_field == prefix) and row.get(name_field):
                        person = {"full_name": row[name_field], "title": row.get("title") or title}
                        break
                yield email, person

    # --- actions -------------------------------------------------------------------------

    def _list(self, ctx: Ctx, entity_type: str, name: Optional[str], list_id: Optional[str], label: str
              ) -> Dict[str, Any]:
        crm = self.platform.service("crm")
        if list_id:
            target = self.store.get(ctx, "lists", list_id)
            if target["entity_type"] != entity_type:
                raise ValidationError(f"list {target['name']} holds {target['entity_type']}, not {entity_type}")
            return target
        base = (name or f"{label} — {entity_type}").strip()[:180]
        for attempt in range(3):
            candidate = base if attempt == 0 else f"{base} ({secrets.token_hex(2)})"
            try:
                return crm.create_list(ctx, candidate, entity_type, source="gtm_bridge",
                                       description="Created from scraper/research results; review before outreach.")
            except ConflictError:
                continue
        raise ConflictError("could not create a uniquely named list")

    def add_to_list(self, ctx: Ctx, source_type: str, source_id: str, *, entity_type: str = "companies",
                    list_id: Optional[str] = None, list_name: Optional[str] = None) -> Dict[str, Any]:
        """Existing CRM companies (or contacts) from the run go into a prospect list."""
        ctx.require_write()
        if entity_type not in ("companies", "contacts"):
            raise ValidationError("entity_type must be companies or contacts")
        preview = self.prepare(ctx, source_type, source_id)
        items = preview["companies"] if entity_type == "companies" else preview["contacts"]
        id_field = "company_id" if entity_type == "companies" else "contact_id"
        ids = [i[id_field] for i in items if i["match"] == "existing" and i.get(id_field)]
        skipped = [i for i in items if not (i["match"] == "existing" and i.get(id_field))]
        target = self._list(ctx, entity_type, list_name, list_id, preview["label"])
        added = self.platform.service("crm").add_to_list(ctx, target["id"], entity_type, ids,
                                                         reason=f"from {source_type} {source_id}") if ids else 0
        audit(self.store, ctx, "gtm_bridge.add_to_list", entity_type="lists", entity_id=target["id"],
              summary=f"{added} {entity_type} from {source_type} {source_id}",
              changes={"added": added, "not_in_crm": len(skipped)})
        return {"list": self.store.get(ctx, "lists", target["id"]), "added": added,
                "not_in_crm": len(skipped),
                "hint": ("records not in the CRM yet were skipped; create a CRM proposal, approve it, then add them"
                         if skipped else None)}

    def validate_emails(self, ctx: Ctx, source_type: str, source_id: str, *, name: Optional[str] = None,
                        start: bool = False) -> Dict[str, Any]:
        """Collect every email in the run into an email validation job (nothing touches the CRM)."""
        ctx.require_write()
        preview = self.prepare(ctx, source_type, source_id)
        rows = [{"email": c["email"], "full_name": c.get("full_name") or "", "title": c.get("title") or "",
                 "company": c.get("company_key") or "", "source_url": c.get("source_url") or ""}
                for c in preview["contacts"]]
        if not rows:
            return {"job": None, "emails": 0, "detail": "the run has no email addresses to validate"}
        job_name = name or f"{preview['label']} — emails"
        jobs = None
        try:
            jobs = self.platform.service("email_jobs")
        except (KeyError, ModuleNotFoundError, ImportError):
            jobs = None
        create = getattr(jobs, "create_from_rows", None)
        if callable(create):
            job = create(ctx, name=job_name, rows=rows, email_field="email",
                         source_type="scrape" if source_type == "scrape" else "manual", source_id=source_id)
            if start and callable(getattr(jobs, "start", None)):
                try:
                    job = jobs.start(ctx, job["id"])
                except Exception as error:  # noqa: BLE001 - the job exists; starting can be retried in the UI
                    log.info("validation job created but not started: %s", error)
            result = {"job": job, "emails": len(rows), "task": None}
        else:
            # Fallback until the email-validation job service is present: the existing
            # validation task (local checks; paid checks never run without allow_paid).
            task = self.platform.tasks.submit(ctx, "validation", {"emails": [r["email"] for r in rows],
                                                                  "allow_paid": False})
            result = {"job": None, "emails": len(rows), "task": task}
        audit(self.store, ctx, "gtm_bridge.validate_emails", summary=f"{len(rows)} email(s) from {source_type} "
                                                                     f"{source_id}")
        return result

    def create_campaign(self, ctx: Ctx, source_type: str, source_id: str, *, name: Optional[str] = None,
                        list_id: Optional[str] = None) -> Dict[str, Any]:
        """A DRAFT campaign (sending off, manual approval) whose audience is the run's prospect list."""
        ctx.require_write()
        if list_id is None:
            listed = self.add_to_list(ctx, source_type, source_id, entity_type="contacts")
            if listed["added"] == 0:
                listed = self.add_to_list(ctx, source_type, source_id, entity_type="companies")
            target = listed["list"]
        else:
            target = self.store.get(ctx, "lists", list_id)
        title = (name or f"{target['name']} campaign").strip()[:200]
        key = f"bridge-{secrets.token_hex(4)}"
        values: Dict[str, Any] = {"key": key, "name": title, "status": "draft", "sending_enabled": False,
                                  "description": f"Prepared from {source_type} {source_id}. Draft: review audience, "
                                                 "senders, templates and schedule before activating."}
        from cloud.intel.store.spec import get_spec

        columns = get_spec("campaigns").columns
        if "audience" in columns:
            values["audience"] = {"list_ids": [target["id"]], "source": {"type": source_type, "id": source_id}}
        if "list_id" in columns:
            values["list_id"] = target["id"]
        if "approval_policy" in columns:
            values["approval_policy"] = "manual"
        campaign = self.store.insert(ctx, "campaigns", values)
        audit(self.store, ctx, "gtm_bridge.create_campaign", entity_type="campaigns", entity_id=campaign["id"],
              summary=f"draft campaign from {source_type} {source_id}")
        return {"campaign": campaign, "list": target,
                "note": "Draft only: nothing is sent. Add senders, templates and a sequence, then activate."}

    def create_crm_proposal(self, ctx: Ctx, source_type: str, source_id: str,
                            actions: Sequence[str] = ("company", "contact")) -> Dict[str, Any]:
        """PROPOSE only; a person reviews and applies (scraper) or applies proposed actions (research)."""
        ctx.require_write()
        if source_type == "scrape":
            result = self.platform.service("scraper").propose(ctx, source_id, actions)
            return {**result, "review": f"/scraper/{source_id}?tab=proposals",
                    "note": "Proposals change nothing until approved and applied."}
        if source_type == "research":
            run = self.store.get(ctx, "research_runs", source_id)
            return {"created": 0, "total": len(run.get("proposed_actions") or []),
                    "proposed_actions": run.get("proposed_actions") or [], "review": f"/research/{source_id}",
                    "note": "Research runs carry their own proposed actions; apply the ones you approve."}
        raise ValidationError(f"source_type must be one of {', '.join(SOURCE_TYPES)}")

    def research_these(self, ctx: Ctx, source_type: str, source_id: str, *, question: Optional[str] = None,
                       limit: int = 25) -> Dict[str, Any]:
        """A PLANNED research run over the run's companies; it runs only after approval."""
        ctx.require_write()
        preview = self.prepare(ctx, source_type, source_id)
        names = [c["name"] or c["domain"] for c in preview["companies"] if c.get("name") or c.get("domain")]
        if not names:
            raise ValidationError("the run has no companies to research")
        text = question or ("Research these companies for hiring activity, technology and decision makers: "
                            + ", ".join(str(n) for n in names[:limit]))
        run = self.platform.service("research").plan(ctx, text[:4000])
        audit(self.store, ctx, "gtm_bridge.research_these", entity_type="research_runs", entity_id=run["id"],
              summary=f"{min(len(names), limit)} companies from {source_type} {source_id}")
        return {"run": run, "companies": min(len(names), limit), "note": "Planned: review and approve to run."}

    def pipeline(self, ctx: Ctx, source_type: str, source_id: str, *, steps: Sequence[str] = (
            "add_to_list", "validate_emails", "create_campaign"), name: Optional[str] = None) -> Dict[str, Any]:
        """Run the reviewable steps in order and report each; stops at the first failure."""
        ctx.require_write()
        allowed = ("add_to_list", "validate_emails", "create_campaign", "create_crm_proposal", "research_these")
        unknown = [s for s in steps if s not in allowed]
        if unknown:
            raise ValidationError(f"unknown step(s): {', '.join(unknown)}")
        out: List[Dict[str, Any]] = []
        list_id: Optional[str] = None
        for step in steps:
            try:
                if step == "add_to_list":
                    result = self.add_to_list(ctx, source_type, source_id, entity_type="contacts", list_name=name)
                    if result["added"] == 0:
                        result = self.add_to_list(ctx, source_type, source_id, entity_type="companies",
                                                  list_name=name)
                    list_id = result["list"]["id"]
                elif step == "validate_emails":
                    result = self.validate_emails(ctx, source_type, source_id)
                elif step == "create_campaign":
                    result = self.create_campaign(ctx, source_type, source_id, list_id=list_id,
                                                  name=f"{name} campaign" if name else None)
                elif step == "create_crm_proposal":
                    result = self.create_crm_proposal(ctx, source_type, source_id)
                else:
                    result = self.research_these(ctx, source_type, source_id)
                out.append({"step": step, "status": "ok", "result": result})
            except Exception as error:  # noqa: BLE001 - report and stop; earlier steps stand
                out.append({"step": step, "status": "failed", "error": f"{type(error).__name__}: {error}"[:500]})
                break
        return {"steps": out}
