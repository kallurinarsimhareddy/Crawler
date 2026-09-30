"""Company enrichment behind one facade: routing, source priority, identity checks,
provenance and the credit ledger.

``EnrichmentService.enrich_company(ctx, company_id, allow_paid=False)`` walks the
workspace's source priority (default below) and stops as soon as nothing is left
to fill:

==============  ================================================================
internal        what the CRM already holds — never re-bought
public_web      the company's own home page: schema.org Organization JSON-LD and
                the meta description, fetched with the SSRF-safe fetcher that
                respects robots.txt. A 401/403/429/CAPTCHA/robots answer is
                recorded as ``blocked`` and never worked around
zoominfo        authorised API (OAuth client credentials), paid
seamless        authorised API key, paid (contact data; no company enrichment)
partner_api     any authorised partner/API provider the workspace has a contract
                with (see :class:`PartnerApiConnector` for the JSON contract), paid
==============  ================================================================

Rules:

* A paid source runs only with ``allow_paid``, only when connected **and
  enabled** (verified, where the registry reports it), and only against a
  ledger reservation that is consumed for what was spent or released on error.
* **Identity first.** A provider answer whose domain contradicts the company's
  domain (or, without a domain, whose name does not match) is rejected as
  ``identity_mismatch`` and nothing from it is written.
* **Fill blanks, never overwrite.** A value that disagrees with the stored one
  is reported under ``conflicts`` for review, not applied.
* Every value written carries provenance (source, original payload, confidence)
  and the run is audited. Absent credentials make a source ``not_configured`` —
  visible, never faked.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from cloud.intel.core.audit import audit, provenance
from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.core.normalize import company_name_key, domain_of
from cloud.intel.providers.base import EnrichmentProvider, PaidCallRefused, ProviderError, ProviderNotConfigured

__all__ = ["DEFAULT_PRIORITY", "EnrichmentService", "FIRMOGRAPHIC_FIELDS", "PartnerApiConnector",
           "PublicCompanyResearch", "same_company"]

log = logging.getLogger(__name__)

DEFAULT_PRIORITY = ("internal", "public_web", "zoominfo", "seamless", "partner_api")
PAID_SOURCES = ("zoominfo", "seamless", "partner_api")
FIRMOGRAPHIC_FIELDS = ("website", "industry", "employee_count", "employee_range", "revenue_range", "revenue_usd",
                       "country", "state", "city", "description", "linkedin_url")
_SOURCE_KIND = {"public_web": "public_web", "zoominfo": "zoominfo", "seamless": "seamless", "partner_api": "api"}
_LABELS = {"internal": "Internal CRM data", "public_web": "Company website (public, robots.txt respected)",
           "zoominfo": "ZoomInfo", "seamless": "Seamless.AI", "partner_api": "Authorized partner API"}


def same_company(company: Mapping[str, Any], values: Mapping[str, Any]) -> Optional[str]:
    """None when ``values`` plausibly describe ``company``; otherwise why not."""
    mine = domain_of(company.get("domain") or company.get("website") or "")
    theirs = domain_of(values.get("domain") or values.get("website") or "")
    if mine and theirs:
        return None if mine == theirs else f"domain {theirs} does not match {mine}"
    name_a, name_b = company_name_key(company.get("name") or ""), company_name_key(values.get("name") or "")
    if name_a and name_b and name_a != name_b and name_a not in name_b and name_b not in name_a:
        return f"name {values.get('name')!r} does not match {company.get('name')!r}"
    if not theirs and not name_b:
        return "the answer carries no domain or name to confirm it is the same company"
    return None


# ---------------------------------------------------------------------------
# Public company research (lawful: the company's own published page)
# ---------------------------------------------------------------------------


class PublicCompanyResearch:
    """Reads what a company publishes about itself on its home page."""

    def __init__(self, fetcher: Any = None) -> None:
        self._fetcher = fetcher

    @property
    def fetcher(self):
        if self._fetcher is None:
            from cloud.intel.core.http import SafeFetcher

            self._fetcher = SafeFetcher(per_host_delay=1.0, respect_robots=True)
        return self._fetcher

    def research(self, company: Mapping[str, Any]) -> Dict[str, Any]:
        """``{"status": "ok|blocked|empty|error|skipped", "values", "evidence", "detail"}``."""
        site = company.get("website") or company.get("domain")
        if not site:
            return {"status": "skipped", "values": {}, "evidence": [], "detail": "no website on record"}
        url = site if str(site).startswith(("http://", "https://")) else f"https://{site}"
        try:
            result = self.fetcher.fetch(url)
        except Exception as error:  # noqa: BLE001 - unsafe URL, DNS, network
            return {"status": "error", "values": {}, "evidence": [], "detail": f"{type(error).__name__}: {error}"[:300]}
        if result.blocked:
            return {"status": "blocked", "values": {}, "evidence": [],
                    "detail": f"the site refused automated access ({result.error or result.status}); not bypassed"}
        if not result.ok:
            return {"status": "error", "values": {}, "evidence": [],
                    "detail": result.error or f"HTTP {result.status}"}
        return self.parse(result.final_url or url, result.text or "")

    @staticmethod
    def parse(url: str, markup: str) -> Dict[str, Any]:
        from cloud.intel.vendor.html import json_ld_objects, parse_html

        soup = parse_html(markup)
        values: Dict[str, Any] = {}
        evidence: List[Dict[str, Any]] = []
        for obj in json_ld_objects(soup):
            kinds = obj.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if not any(str(k) in ("Organization", "Corporation", "LocalBusiness", "Company") for k in kinds):
                continue
            if obj.get("name"):
                values.setdefault("name", str(obj["name"])[:300])
            if obj.get("url"):
                values.setdefault("website", str(obj["url"])[:2048])
            if obj.get("description"):
                values.setdefault("description", str(obj["description"])[:4000])
            address = obj.get("address")
            if isinstance(address, list):
                address = address[0] if address else None
            if isinstance(address, Mapping):
                for src, dst in (("addressLocality", "city"), ("addressRegion", "state"),
                                 ("addressCountry", "country")):
                    value = address.get(src)
                    if isinstance(value, Mapping):
                        value = value.get("name")
                    if value:
                        values.setdefault(dst, str(value)[:120])
            employees = obj.get("numberOfEmployees")
            if isinstance(employees, Mapping):
                employees = employees.get("value") or employees.get("maxValue")
            try:
                if employees not in (None, ""):
                    values.setdefault("employee_count", int(float(str(employees).replace(",", ""))))
            except ValueError:
                pass
            for same in obj.get("sameAs") or []:
                if "linkedin.com/company" in str(same):
                    values.setdefault("linkedin_url", str(same)[:500])
            evidence.append({"method": "json-ld", "url": url, "type": kinds[0]})
        if "description" not in values:
            meta = soup.find("meta", attrs={"name": "description"})
            content = meta.get("content") if meta else None
            if content:
                values["description"] = str(content).strip()[:4000]
                evidence.append({"method": "meta description", "url": url})
        if "website" not in values:
            parts = urlsplit(url)
            values["website"] = f"{parts.scheme}://{parts.netloc}"
        return {"status": "ok" if evidence else "empty", "values": values, "evidence": evidence,
                "detail": "read the company's own home page" if evidence else "no structured company data found"}


# ---------------------------------------------------------------------------
# Authorised partner / API provider slot
# ---------------------------------------------------------------------------


class PartnerApiConnector(EnrichmentProvider):
    """A provider the workspace holds a contract with, reached over its API.

    Credentials: ``api_key`` (sent as ``Authorization: Bearer``). Settings:
    ``base_url`` (https), ``company_path`` (default ``/companies/enrich``),
    ``contacts_path`` (default ``/contacts/search``), ``cost_per_company`` and
    ``cost_per_contact`` (credits, default 1).

    Contract: ``POST {base_url}{company_path}`` with ``{"domain", "name"}``
    returns ``{"company": {<company fields>}}``; ``POST {base_url}{contacts_path}``
    with ``{"domain", "name", "titles", "limit"}`` returns ``{"contacts": [...]}``.
    Keys not in the platform's company/contact schema are ignored.
    """

    name = "partner_api"
    access_method = "partner"

    def __init__(self, secrets: Mapping[str, str], *, settings: Optional[Mapping[str, Any]] = None,
                 fetcher: Any = None) -> None:
        self._key = str((secrets or {}).get("api_key") or "")
        self.settings = dict(settings or {})
        self._fetcher = fetcher
        self.calls = 0

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url") or "").rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self._key and self.base_url.startswith("https://"))

    def health(self) -> Dict[str, Any]:
        if not self._key:
            return {"status": "not_configured", "detail": "no partner API key stored"}
        if not self.base_url.startswith("https://"):
            return {"status": "not_configured", "detail": "set an https base_url in the connection settings"}
        return {"status": "configured_unverified", "detail": "stored; the partner contract defines its own check"}

    def verify(self, *, allow_paid: bool = False) -> Dict[str, Any]:
        return self.health()

    def estimate_cost(self, operation: str, n: int) -> float:
        per = {"enrich_company": self.settings.get("cost_per_company", 1),
               "search_contacts": self.settings.get("cost_per_contact", 1)}.get(operation, 0)
        return float(per) * max(0, int(n))

    @property
    def fetcher(self):
        if self._fetcher is None:
            from cloud.intel.core.http import SafeFetcher

            # An authorised API endpoint, not a crawl: robots.txt governs crawlers.
            self._fetcher = SafeFetcher(per_host_delay=0.2, respect_robots=False)
        return self._fetcher

    def _post(self, path: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        if not self.configured:
            raise ProviderNotConfigured("the partner API is not configured for this workspace")
        self.calls += 1
        result = self.fetcher.fetch(self.base_url + path, method="POST", json_body=dict(body),
                                    headers={"Authorization": f"Bearer {self._key}"}, accept="application/json")
        if not result.ok:
            raise ProviderError(f"partner API refused the request ({result.error or result.status})")
        try:
            payload = result.json()
        except ValueError as error:
            raise ProviderError("partner API returned something other than JSON") from error
        if not isinstance(payload, Mapping):
            raise ProviderError("partner API returned an unexpected shape")
        return dict(payload)

    def enrich_company(self, identifiers: Mapping[str, Any], *, allow_paid: bool = False) -> Optional[Dict[str, Any]]:
        if not allow_paid:
            raise PaidCallRefused("partner API enrichment consumes credits; it needs allow_paid")
        payload = self._post(str(self.settings.get("company_path") or "/companies/enrich"),
                             {k: identifiers.get(k) for k in ("domain", "name") if identifiers.get(k)})
        company = payload.get("company")
        return dict(company) if isinstance(company, Mapping) else None

    def search_contacts(self, filters: Mapping[str, Any], *, limit: int = 25, allow_paid: bool = False
                        ) -> List[Dict[str, Any]]:
        if not allow_paid:
            raise PaidCallRefused("partner API contact search consumes credits; it needs allow_paid")
        payload = self._post(str(self.settings.get("contacts_path") or "/contacts/search"),
                             {**dict(filters), "limit": max(1, min(int(limit), 100))})
        return [dict(c) for c in payload.get("contacts") or [] if isinstance(c, Mapping)]


# ---------------------------------------------------------------------------
# The facade
# ---------------------------------------------------------------------------


class EnrichmentService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store
        #: Tests inject these.
        self.fetcher = None
        self.connectors: Dict[str, Any] = {}

    # --- sources and priority ------------------------------------------------------

    def _registry(self):
        return self.platform.service("providers")

    def _enabled(self, ctx: Ctx, name: str) -> bool:
        """Connected and, where the registry can tell, verified: the gate for paid use."""
        if name in self.connectors:
            return True
        registry = self._registry()
        check = getattr(registry, "enabled", None)
        try:
            return bool(check(ctx, name)) if callable(check) else bool(registry.configured(ctx, name))
        except Exception:  # noqa: BLE001 - an unknown provider is simply not enabled
            return False

    def _configured(self, ctx: Ctx, name: str) -> bool:
        if name in self.connectors:
            return True
        try:
            return bool(self._registry().configured(ctx, name))
        except Exception:  # noqa: BLE001
            return False

    def _settings(self, ctx: Ctx) -> Dict[str, Any]:
        lookup = None
        if ctx.user_id and not ctx.system:
            lookup = self.store.membership(ctx.user_id, ctx.workspace_id)
        if lookup is None and callable(getattr(self.store, "system_membership", None)):
            lookup = self.store.system_membership(ctx.workspace_id)
        return dict((lookup or {}).get("settings") or {})

    def priority(self, ctx: Ctx) -> List[str]:
        stored = [s for s in self._settings(ctx).get("enrichment_priority") or [] if s in DEFAULT_PRIORITY]
        return (["internal"] + [s for s in stored if s != "internal"]
                + [s for s in DEFAULT_PRIORITY if s not in stored and s != "internal"]) if stored \
            else list(DEFAULT_PRIORITY)

    def set_priority(self, ctx: Ctx, order: Sequence[str]) -> List[str]:
        ctx.require_admin()
        unknown = [s for s in order if s not in DEFAULT_PRIORITY]
        if unknown:
            raise ValidationError(f"unknown enrichment source(s): {', '.join(unknown)}")
        clean = list(dict.fromkeys(order))
        settings = {**self._settings(ctx), "enrichment_priority": clean}
        self.store.update_workspace(ctx, settings=settings)
        audit(self.store, ctx, "enrichment.priority", changes={"order": clean})
        return self.priority(ctx)

    def sources(self, ctx: Ctx) -> List[Dict[str, Any]]:
        """Every enrichment source in priority order with its honest status."""
        ledger = self.platform.service("credits")
        out = []
        for rank, name in enumerate(self.priority(ctx), start=1):
            entry: Dict[str, Any] = {"name": name, "label": _LABELS[name], "rank": rank,
                                     "paid": name in PAID_SOURCES}
            if name == "internal":
                entry.update(status="available", access_method="internal", configured=True, enabled=True,
                             requirement="")
            elif name == "public_web":
                entry.update(status="available", access_method="public", configured=True, enabled=True,
                             requirement="none: reads each company's own home page; robots.txt respected")
            else:
                configured = self._configured(ctx, name)
                enabled = self._enabled(ctx, name)
                try:
                    info = self._registry().describe(name)
                except Exception:  # noqa: BLE001
                    info = {"requirement": "", "access_method": "api"}
                entry.update(status=("enabled" if enabled else "configured" if configured else "not_configured"),
                             access_method=info.get("access_method"), configured=configured, enabled=enabled,
                             requirement=info.get("requirement", ""))
                try:
                    entry["credits"] = ledger.balance(ctx, name)
                except Exception:  # noqa: BLE001
                    entry["credits"] = None
            out.append(entry)
        return out

    def plan(self, ctx: Ctx, needs: Mapping[str, Any]) -> Dict[str, Any]:
        from cloud.intel.providers.routing import plan_enrichment

        return plan_enrichment(self.platform, ctx, needs)

    def find_contacts(self, ctx: Ctx, company_ids: Sequence[str], **kw: Any) -> Dict[str, Any]:
        return self.platform.service("contacts").find_contacts(ctx, company_ids, **kw)

    # --- company enrichment ------------------------------------------------------------

    def _connector(self, ctx: Ctx, name: str):
        if name in self.connectors:
            return self.connectors[name]
        registry = self._registry()
        if name == "partner_api":
            return PartnerApiConnector(registry.get_secrets(ctx, name), settings=registry._settings(ctx, name),
                                       fetcher=self.fetcher)
        return registry.enrichment(ctx, name)

    def _apply(self, ctx: Ctx, company: Dict[str, Any], values: Mapping[str, Any], wanted: Sequence[str], *,
               source: str, evidence: Any, confidence: float) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        conflicts: List[Dict[str, Any]] = []
        from cloud.intel.store.spec import get_spec

        columns = get_spec("companies").columns
        for field in wanted:
            value = values.get(field)
            if value in (None, "", [], {}) or field not in columns:
                continue
            col = columns[field]
            if col.kind == "text" and col.max_len:
                value = str(value)[:col.max_len]
            elif col.kind == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif col.kind == "float":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            current = company.get(field)
            if current in (None, "", []):
                changes[field] = value
            elif str(current).strip().lower() != str(value).strip().lower():
                conflicts.append({"field": field, "existing": current, "incoming": value, "source": source})
        if changes:
            company.update(self.store.update(ctx, "companies", company["id"], changes))
            provenance(self.store, ctx, "companies", company["id"], source_kind=_SOURCE_KIND[source],
                       source_name=source, original={k: values.get(k) for k in values if k != "raw"},
                       normalized={**changes, "evidence": evidence, "conflicts": conflicts},
                       confidence=confidence, match_rule="enrichment: fill blanks")
        return {"filled": changes, "conflicts": conflicts}

    def enrich_company(self, ctx: Ctx, company_id: str, *, fields: Optional[Sequence[str]] = None,
                       allow_paid: bool = False, providers: Optional[Sequence[str]] = None,
                       task_id: Optional[str] = None) -> Dict[str, Any]:
        ctx.require_write()
        company = dict(self.store.get(ctx, "companies", company_id))
        wanted = [f for f in (fields or FIRMOGRAPHIC_FIELDS) if f in FIRMOGRAPHIC_FIELDS]
        order = [s for s in self.priority(ctx) if providers is None or s in providers or s == "internal"]
        ledger = self.platform.service("credits")
        steps: List[Dict[str, Any]] = []
        filled: Dict[str, Dict[str, Any]] = {}
        conflicts: List[Dict[str, Any]] = []

        def missing() -> List[str]:
            return [f for f in wanted if company.get(f) in (None, "", [])]

        for source in order:
            gap = missing()
            step: Dict[str, Any] = {"source": source, "label": _LABELS[source], "status": "", "filled": [],
                                    "detail": "", "credits": 0.0}
            steps.append(step)
            if source == "internal":
                have = [f for f in wanted if f not in gap]
                step.update(status="used", detail=(f"already on record: {', '.join(have)}" if have
                                                   else "nothing on record yet"))
                continue
            if not gap:
                step.update(status="skipped", detail="nothing left to fill")
                continue
            if source == "public_web":
                result = PublicCompanyResearch(self.fetcher).research(company)
                step["status"] = result["status"]
                step["detail"] = result["detail"]
                if result["status"] == "ok":
                    mismatch = same_company(company, result["values"])
                    if mismatch:
                        step.update(status="identity_mismatch", detail=mismatch)
                        continue
                    applied = self._apply(ctx, company, result["values"], gap, source=source,
                                          evidence=result["evidence"], confidence=0.7)
                    self._record(step, applied, filled, conflicts, source)
                continue
            # paid, authorised providers
            if not self._configured(ctx, source):
                step.update(status="not_configured", detail=f"{_LABELS[source]} is not connected")
                continue
            if not self._enabled(ctx, source):
                step.update(status="not_verified", detail="connected but not verified; verify it before paid use")
                continue
            try:
                connector = self._connector(ctx, source)
            except Exception as error:  # noqa: BLE001
                step.update(status="error", detail=f"{type(error).__name__}: {error}"[:300])
                continue
            cost = float(connector.estimate_cost("enrich_company", 1) or 0.0)
            if not allow_paid:
                step.update(status="skipped_paid", detail=f"needs allow_paid (about {cost:g} credit(s))",
                            credits=cost)
                continue
            reservation = None
            try:
                if cost > 0:
                    reservation = ledger.reserve(ctx, source, cost, reason=f"enrich company {company_id}",
                                                 task_id=task_id, action="enrich_company")
                answer = connector.enrich_company({"domain": domain_of(company.get("domain") or
                                                                       company.get("website") or "") or None,
                                                   "name": company.get("name")}, allow_paid=True)
                if reservation:
                    ledger.consume(ctx, reservation["id"], cost)
                    reservation = None
                ledger.record_usage(ctx, source, "enrich_company", task_id=task_id)
                step["credits"] = cost
            except (ProviderError, PaidCallRefused, ValidationError) as error:
                if reservation:
                    ledger.release(ctx, reservation["id"], reason=f"provider error: {error}"[:300])
                try:
                    ledger.record_usage(ctx, source, "enrich_company", success=False, error=str(error)[:500],
                                        task_id=task_id)
                except Exception:  # noqa: BLE001
                    log.debug("usage record failed", exc_info=True)
                kind = ("not_configured" if isinstance(error, ProviderNotConfigured)
                        else "unsupported" if "does not support" in str(error) else "error")
                step.update(status=kind, detail=str(error)[:300])
                continue
            if not answer:
                step.update(status="empty", detail="the provider returned no match")
                continue
            values = dict(answer)
            if values.get("revenue") and not values.get("revenue_usd"):
                try:
                    values["revenue_usd"] = float(values["revenue"])
                except (TypeError, ValueError):
                    values["revenue_range"] = str(values["revenue"])
            mismatch = same_company(company, values)
            if mismatch:
                step.update(status="identity_mismatch", detail=mismatch)
                continue
            applied = self._apply(ctx, company, values, gap, source=source,
                                  evidence=[{"provider": source}], confidence=0.85)
            step["status"] = "ok"
            self._record(step, applied, filled, conflicts, source)

        audit(self.store, ctx, "enrichment.company", entity_type="companies", entity_id=company_id,
              summary=f"filled {len(filled)} field(s)",
              changes={"filled": {k: v["source"] for k, v in filled.items()}, "conflicts": len(conflicts),
                       "allow_paid": allow_paid, "steps": [(s["source"], s["status"]) for s in steps]})
        return {"company_id": company_id, "filled": filled, "conflicts": conflicts, "steps": steps,
                "missing": missing(), "priority": order,
                "credits_spent": sum(float(s.get("credits") or 0) for s in steps if s["status"] == "ok")}

    @staticmethod
    def _record(step: Dict[str, Any], applied: Mapping[str, Any], filled: Dict[str, Dict[str, Any]],
                conflicts: List[Dict[str, Any]], source: str) -> None:
        step["filled"] = sorted(applied["filled"])
        if applied["conflicts"]:
            step["detail"] = (step.get("detail") or "") + f"; {len(applied['conflicts'])} conflicting value(s) kept"
        for field, value in applied["filled"].items():
            filled[field] = {"value": value, "source": source}
        conflicts.extend(applied["conflicts"])


def run_company_enrichment(platform: Any, ctx: Ctx, company_ids: Sequence[str], *, allow_paid: bool = False,
                           task_id: Optional[str] = None) -> Dict[str, Any]:
    service: EnrichmentService = platform.service("enrichment")
    results = [service.enrich_company(ctx, cid, allow_paid=allow_paid, task_id=task_id) for cid in company_ids]
    return {"companies": len(results), "filled": sum(len(r["filled"]) for r in results), "results": results}
