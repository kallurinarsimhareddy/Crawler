"""Contact intelligence: who is missing at a target company, and finding them lawfully.

**Gap analysis** classifies a company's contacts into the functions a staffing
campaign targets — HR, Recruiting, IT, CIO, CTO, VP, Director, C-level — with
the vendored Seamless title targeting and the zero-credit role vocabulary, and
reports each as:

* ``FOUND`` — at least one matching contact with a verified email;
* ``NEEDS_VERIFICATION`` — matching contacts exist but none is verified;
* ``MISSING`` — nobody on file.

**Finding contacts** follows, per company, and stops as soon as the gaps close::

    internal DB -> existing contacts -> public web (the company's own leadership/
    team pages; zero credits) -> authorised paid enrichment (only allow_paid,
    only against a ledger reservation) -> email validation -> contact confidence

Rules that are not negotiable:

* Nothing is inferred about a person without evidence. An email is recorded
  only when it was **published** next to that person (vendored
  ``zerocredit.extract``) or returned by an authorised provider — addresses are
  never constructed from name patterns.
* Every contact keeps its source, source date and evidence URL (``source_records``).
* A paid step that would be needed without ``allow_paid`` is reported in the
  result (``paid_needed``), not silently skipped.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, NotFoundError, ValidationError
from cloud.intel.core.normalize import domain_of, normalize_email, normalize_website

__all__ = ["FUNCTIONS", "ContactIntelService", "contact_functions", "run_enrichment_task"]

log = logging.getLogger(__name__)

FUNCTIONS = ("hr", "recruiting", "it", "cio", "cto", "vp", "director", "c_level")
_ALIASES = {"executive": "c_level", "c-level": "c_level", "clevel": "c_level", "c_suite": "c_level",
            "human_resources": "hr", "talent": "recruiting"}
_C_BUCKETS = {"CEO", "President", "COO", "CFO", "CHRO", "CIO", "CTO", "CISO", "Other C-Level", "Owner/Founder"}


def _norm_functions(functions: Iterable[str]) -> List[str]:
    out: List[str] = []
    for f in functions:
        key = _ALIASES.get(str(f).strip().lower(), str(f).strip().lower())
        if key not in FUNCTIONS:
            raise ValidationError(f"unknown contact function {f!r}; use {', '.join(FUNCTIONS)}")
        if key not in out:
            out.append(key)
    return out


def contact_functions(title: Optional[str]) -> List[str]:
    """Every gap-analysis function a title belongs to (a CIO is IT, CIO and C-level)."""
    from cloud.intel.vendor import seamless_targeting, zc_extract

    text = f" {re.sub(r'[^a-z0-9]+', ' ', (title or '').lower())} "
    if not text.strip():
        return []
    found: List[str] = []
    function = seamless_targeting.classify(title or "")
    role = zc_extract.role_of(title or "")
    bucket = role[0] if role else ""
    if function == "hr":
        found.append("hr")
    if any(k in text for k in (" recruit", " talent acquisition ", " sourcer ", " staffing ")):
        found.append("recruiting")
    if function == "it" or bucket in ("CIO", "CTO", "CISO", "VP IT", "IT Director"):
        found.append("it")
    if bucket == "CIO" or " chief information officer " in text or " cio " in text:
        found.append("cio")
    if bucket == "CTO" or " chief technology officer " in text or " cto " in text:
        found.append("cto")
    if " vp " in text or " vice president " in text or " svp " in text or " evp " in text:
        found.append("vp")
    if " director " in text:
        found.append("director")
    if bucket in _C_BUCKETS or " chief " in text:
        found.append("c_level")
    return found


class ContactIntelService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store
        #: Injected by tests; defaults to a polite SafeFetcher.
        self.fetcher = None

    def _fetcher(self):
        if self.fetcher is None:
            from cloud.intel.core.http import SafeFetcher

            self.fetcher = SafeFetcher(per_host_delay=1.0)
        return self.fetcher

    # --- gap analysis --------------------------------------------------------------

    def gap_analysis(self, ctx: Ctx, company_id: str, functions: Sequence[str] = ("hr", "it", "executive")
                     ) -> Dict[str, Any]:
        company = self.store.find(ctx, "companies", company_id)
        if company is None:
            raise NotFoundError(f"company {company_id} not found")
        wanted = _norm_functions(functions)
        contacts = self.store.all(ctx, "contacts", {"company_id": company_id}, cap=5000)
        contacts = [c for c in contacts if c.get("status") not in ("left_company", "merged", "archived")]
        result: Dict[str, Any] = {}
        for function in wanted:
            matching = [c for c in contacts if function in contact_functions(c.get("title"))]
            verified = [c for c in matching if c.get("email_status") == "VALID"
                        or c.get("validation_status") == "verified"]
            status = "FOUND" if verified else ("NEEDS_VERIFICATION" if matching else "MISSING")
            result[function] = {
                "status": status,
                "contacts": [{"id": c["id"], "full_name": c["full_name"], "title": c.get("title"),
                              "email": c.get("email"), "email_status": c.get("email_status"),
                              "source": c.get("source"), "confidence": c.get("confidence")} for c in matching],
            }
        summary = {s: sum(1 for v in result.values() if v["status"] == s)
                   for s in ("FOUND", "MISSING", "NEEDS_VERIFICATION")}
        return {"company_id": company_id, "company_name": company["name"], "functions": result, "summary": summary}

    @staticmethod
    def _missing(gap: Mapping[str, Any]) -> List[str]:
        return [f for f, v in gap["functions"].items() if v["status"] == "MISSING"]

    # --- the public web (zero credits) --------------------------------------------------

    def public_web_people(self, website: str, *, company_domain: str = "", max_pages: int = 4
                          ) -> List[Dict[str, Any]]:
        """Senior people named on the company's own site, with the page as evidence."""
        from cloud.intel.vendor import confidence, zc_extract
        from cloud.intel.vendor.html import parse_html

        fetcher = self._fetcher()
        home = fetcher.fetch(website)
        if not home.ok:
            return []
        soup = parse_html(home.text)
        pages = [(home.final_url, "official_other")] + zc_extract.find_leadership_links(soup, home.final_url)
        people: Dict[str, Dict[str, Any]] = {}
        for index, (url, source_type) in enumerate(pages[: max_pages + 1]):
            page_soup = soup if index == 0 else None
            if page_soup is None:
                result = fetcher.fetch(url)
                if not result.ok:
                    continue
                page_soup = parse_html(result.text)
            for person in zc_extract.people_on_page(page_soup, url, source_type, company_domain=company_domain):
                key = person.full_name.strip().lower()
                if not key or key in people:
                    continue
                tier, score = confidence.combine([person.source_type or source_type])
                email = normalize_email(person.email) if person.email else None
                # Link text ("Email Jane", "LinkedIn") can run into the printed title.
                title = re.sub(r"\s+\b(e-?mail|contact|linkedin|phone|bio|read more)\b.*$", "",
                               person.job_title or "", flags=re.I).strip() or person.job_title
                people[key] = {"full_name": person.full_name, "title": title, "email": email,
                               "phone": person.phone or None, "linkedin_url": person.linkedin_url or None,
                               "department": person.department or None, "seniority": person.seniority or None,
                               "role_bucket": person.role_bucket, "source_url": person.source_url or url,
                               "source_type": person.source_type or source_type, "evidence": person.evidence,
                               "confidence": round(score / 100.0, 2), "confidence_tier": tier}
        return list(people.values())

    # --- the workflow ----------------------------------------------------------------------

    def _upsert(self, ctx: Ctx, company: Mapping[str, Any], person: Mapping[str, Any], *, source_kind: str,
                source_name: str, source_ref: Optional[str]) -> Dict[str, Any]:
        values = {k: person.get(k) for k in ("full_name", "title", "email", "phone", "linkedin_url", "department",
                                             "seniority") if person.get(k)}
        values["company_id"] = company["id"]
        values["source"] = source_kind
        functions = contact_functions(person.get("title"))
        if functions:
            values["function"] = functions[0]
        return self.platform.service("crm").upsert_contact(
            ctx, values, source_kind=source_kind, source_name=source_name, source_ref=source_ref,
            original=dict(person), confidence=person.get("confidence"))

    def find_contacts(self, ctx: Ctx, company_ids: Sequence[str], *, functions: Sequence[str] = ("hr", "it", "executive"),
                      allow_paid: bool = False, providers: Optional[Sequence[str]] = None,
                      task_id: Optional[str] = None, validate: bool = True) -> Dict[str, Any]:
        ctx.require_write()
        wanted = _norm_functions(functions)
        registry = self.platform.service("providers")
        providers = list(providers) if providers is not None else [
            p for p in ("seamless", "zoominfo") if registry.configured(ctx, p)]
        report: List[Dict[str, Any]] = []
        for company_id in company_ids:
            report.append(self._find_for(ctx, company_id, wanted, allow_paid=allow_paid, providers=providers,
                                         task_id=task_id, validate=validate))
        audit(self.store, ctx, "contacts.find", summary=f"contact search for {len(company_ids)} company(ies)",
              changes={"allow_paid": allow_paid, "providers": providers, "functions": wanted})
        return {"companies": report, "added": sum(r["added"] for r in report),
                "paid_needed": [r["company_id"] for r in report if r.get("paid_needed")]}

    def _find_for(self, ctx: Ctx, company_id: str, wanted: List[str], *, allow_paid: bool,
                  providers: Sequence[str], task_id: Optional[str], validate: bool) -> Dict[str, Any]:
        company = self.store.find(ctx, "companies", company_id)
        if company is None:
            return {"company_id": company_id, "error": "not found", "added": 0, "steps": []}
        steps: List[Dict[str, Any]] = []
        added: List[Dict[str, Any]] = []
        gap = self.gap_analysis(ctx, company_id, wanted)
        steps.append({"step": "internal", "missing": self._missing(gap)})
        if not self._missing(gap):
            return {"company_id": company_id, "added": 0, "steps": steps, "gap": gap}

        website = normalize_website(company.get("website") or company.get("domain"))
        domain = company.get("domain") or domain_of(website)
        if website:
            try:
                people = self.public_web_people(website, company_domain=domain or "")
            except Exception as error:  # noqa: BLE001 - one site must not stop the batch
                people = []
                steps.append({"step": "public_web", "error": f"{type(error).__name__}: {error}"[:300]})
            for person in people:
                result = self._upsert(ctx, company, person, source_kind="public_web", source_name=domain or website,
                                      source_ref=person["source_url"])
                if result.get("created"):
                    added.append(result["contact"])
            steps.append({"step": "public_web", "people": len(people)})
        else:
            steps.append({"step": "public_web", "skipped": "no website on record"})

        gap = self.gap_analysis(ctx, company_id, wanted)
        missing = self._missing(gap)
        paid_needed = None
        if missing and providers:
            if not allow_paid:
                paid_needed = {"providers": list(providers), "missing": missing,
                               "reason": "public sources left functions missing; paid enrichment needs allow_paid"}
                steps.append({"step": "paid", "refused": paid_needed["reason"]})
            else:
                for provider in providers:
                    try:
                        added += self._paid_enrich(ctx, company, provider, missing, task_id=task_id)
                        steps.append({"step": provider, "ok": True})
                    except Exception as error:  # noqa: BLE001 - report, continue with the next provider
                        steps.append({"step": provider, "error": f"{type(error).__name__}: {error}"[:300]})
                    gap = self.gap_analysis(ctx, company_id, wanted)
                    missing = self._missing(gap)
                    if not missing:
                        break

        emails = [c["email"] for c in added if c.get("email")]
        if validate and emails:
            try:
                self.platform.service("email").validate(ctx, emails, allow_paid=allow_paid, task_id=task_id)
                steps.append({"step": "email_validation", "emails": len(emails)})
            except Exception as error:  # noqa: BLE001
                steps.append({"step": "email_validation", "error": str(error)[:300]})
        for contact in added:
            try:
                self.platform.service("automation").emit(ctx, "new_contact", f"contact:{contact['id']}",
                                                         {"contact_id": contact["id"], "company_id": company_id})
            except Exception:  # noqa: BLE001 - best-effort
                pass
        return {"company_id": company_id, "added": len(added), "steps": steps, "paid_needed": paid_needed,
                "gap": self.gap_analysis(ctx, company_id, wanted)}

    def _paid_enrich(self, ctx: Ctx, company: Mapping[str, Any], provider: str, missing: List[str], *,
                     task_id: Optional[str]) -> List[Dict[str, Any]]:
        registry = self.platform.service("providers")
        ledger = self.platform.service("credits")
        connector = registry.enrichment(ctx, provider)
        domain = company.get("domain") or domain_of(company.get("website"))
        if not domain:
            raise ValidationError("paid enrichment needs a company domain")
        added: List[Dict[str, Any]] = []
        if provider == "seamless":
            estimate = connector.estimate_cost("search_contacts", 10) + connector.estimate_cost(
                "enrich_contacts", len(missing))
            reservation = ledger.reserve(ctx, "seamless", estimate, task_id=task_id, action="contact_enrichment",
                                         reason=f"find {', '.join(missing)} at {domain}")
            try:
                hits = connector.search_contacts({"companyDomain": [domain]}, limit=10, allow_paid=True)
                targets = [h for h in hits if set(contact_functions(h.get("title"))) & set(missing)]
                chosen = connector.select_targets(targets, len(missing))
                enriched = connector.enrich_contacts(chosen, allow_paid=True) if chosen else []
            finally:
                spent = min(float(connector.estimated_spend), float(reservation["amount"]))
                ledger.consume(ctx, reservation["id"], spent) if spent else ledger.release(ctx, reservation["id"])
                if connector.credits_remaining is not None:
                    ledger.sync(ctx, "seamless", remaining=connector.credits_remaining, source="X-PublicAPI-Credits")
            for person in enriched:
                person = {**person, "confidence": 0.75}
                result = self._upsert(ctx, company, person, source_kind="seamless", source_name="Seamless.AI",
                                      source_ref=person.get("seamless_id"))
                if result.get("created"):
                    added.append(result["contact"])
            ledger.record_usage(ctx, "seamless", "contact_enrichment", units=len(enriched), task_id=task_id)
        elif provider == "zoominfo":
            hits = connector.search_contacts({"companyWebsite": domain}, limit=25)
            ledger.record_usage(ctx, "zoominfo", "contact_search", units=len(hits), task_id=task_id)
            targets = [h for h in hits if set(contact_functions(h.get("title"))) & set(missing)][: len(missing)]
            if targets:
                reservation = ledger.reserve(ctx, "zoominfo", connector.estimate_cost("enrich_contacts", len(targets)),
                                             task_id=task_id, action="contact_enrichment",
                                             reason=f"enrich {len(targets)} contact(s) at {domain}")
                enriched: List[Dict[str, Any]] = []
                try:
                    enriched = connector.enrich_contacts([{"personId": t["zoominfo_id"]} for t in targets],
                                                         allow_paid=True)
                finally:
                    ledger.consume(ctx, reservation["id"], min(float(len(enriched)), float(reservation["amount"])))
                for person in enriched:
                    result = self._upsert(ctx, company, {**person, "confidence": 0.75}, source_kind="zoominfo",
                                          source_name="ZoomInfo", source_ref=person.get("zoominfo_id"))
                    if result.get("created"):
                        added.append(result["contact"])
        else:
            raise ValidationError(f"{provider} is not a contact enrichment provider")
        return added


def run_enrichment_task(platform: Any, ctx: Ctx, task: Dict[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError, TaskPaused

    params = task["params"]
    service: ContactIntelService = platform.service("contacts")
    company_ids = list(params.get("company_ids") or [])
    if params.get("list_id"):
        company_ids += [m["entity_id"] for m in platform.store.all(ctx, "list_members", {"list_id": params["list_id"]})]
    if not company_ids:
        raise PermanentTaskError("no companies to enrich")
    done = int(reporter.checkpoint.get("done", 0))
    added = int(reporter.checkpoint.get("added", 0))
    paid_needed: List[str] = list(reporter.checkpoint.get("paid_needed") or [])
    for index in range(done, len(company_ids)):
        if reporter.is_cancelled():
            break
        if reporter.should_pause():
            raise TaskPaused({"done": index, "added": added, "paid_needed": paid_needed})
        result = service.find_contacts(ctx, [company_ids[index]], functions=params.get("functions") or
                                       ("hr", "it", "executive"), allow_paid=bool(params.get("allow_paid")),
                                       providers=params.get("providers"), task_id=task["id"])
        added += result["added"]
        paid_needed += result["paid_needed"]
        reporter.progress(f"{index + 1} of {len(company_ids)} companies", done=index + 1, total=len(company_ids))
    return {"companies": len(company_ids), "added": added, "paid_needed": paid_needed}
