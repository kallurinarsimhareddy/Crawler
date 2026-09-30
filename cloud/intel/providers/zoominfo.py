# Transport and auth PORTED from zoominfo-erp-automation/src/zoominfo_erp/api/{auth,client,technology}.py
# (untracked working tree on E:\Crawlers, read 2026-09-24): OAuth2 client_credentials token cache,
# client-side pacing, one re-auth on 401, hard stop on 403, Retry-After honoured on 429 (never
# circumvented), bounded backoff on 5xx/network. Adapted to per-workspace credentials and the
# platform's credit ledger. Credentials come from the workspace connection, falling back to the
# ZOOMINFO_CLIENT_ID / ZOOMINFO_CLIENT_SECRET environment variables; never hardcoded.
"""ZoomInfo as an enrichment and technology provider.

Two access modes exist in this organisation, and only one can run in the cloud:

* **API (this connector).** The ZoomInfo GTM Data API with an OAuth
  ``client_credentials`` application (``client_id`` + ``client_secret``) stored
  encrypted per workspace. Company search and lookup are documented as
  credit-free (per the original client); enrichment consumes credits and needs
  ``allow_paid=True`` plus a ledger reservation made by the caller.
* **Browser login (not here).** ``zoominfo-erp-automation`` drives an
  authorised ZoomInfo session in a persistent Chromium profile that a human
  signs into. The user declined API credentials on 2026-09-08, so that is the
  only mode that currently works — and it is **operator-attended by design**:
  the tool never handles passwords, MFA or CAPTCHA. It is not automated from
  the cloud; :data:`BROWSER_MODE_STATUS` states that plainly.

Request bodies, pagination (``page[size]`` 1-100, ``meta.page.total``) and the
25-record enrichment batch limit follow ZoomInfo's published GTM API reference.
Search is credit-free; enrichment is charged per newly managed record, and only
records the API reports as matched are returned.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from cloud.intel.providers.base import (
    EnrichmentProvider,
    PaidCallRefused,
    ProviderError,
    ProviderNotConfigured,
    TechnologyProvider,
)

__all__ = ["BROWSER_MODE_STATUS", "ZoomInfoConnector", "ZoomInfoError"]

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.zoominfo.com/gtm/oauth/v1/token"
BASE_URL = "https://api.zoominfo.com/gtm"
COMPANY_SEARCH = "/data/v1/companies/search"   # credit-free
LOOKUP = "/data/v1/lookup/{field_name}"         # credit-free
CONTACT_SEARCH = "/data/v1/contacts/search"     # credit-free (match hints only)
COMPANY_ENRICH = "/data/v1/companies/enrich"    # consumes credits
CONTACT_ENRICH = "/data/v1/contacts/enrich"     # consumes credits
#: Request contracts, page size and batch limits follow the published API reference (docs.gtm.ai, 2026-09-29).
PAGE_SIZE_MAX = 100
MAX_SEARCH_RESULTS = 1000
ENRICH_BATCH_MAX = 25
COMPANY_OUTPUT_FIELDS = ("id", "name", "website", "city", "state", "country", "employeeCount", "revenue")
CONTACT_OUTPUT_FIELDS = ("id", "firstName", "lastName", "jobTitle", "email", "phone", "mobilePhone", "companyName",
                         "contactAccuracyScore")
JSON_API = "application/vnd.api+json"
#: Explicit on every request: gateways often reject default library UAs with a misleading 403.
USER_AGENT = "SANA-GTM/1.0 (+https://sanagtm.pages.dev)"

BROWSER_MODE_STATUS = {
    "status": "blocked",
    "detail": ("ZoomInfo browser-login mode requires an operator-attended browser session (a human signs in to a "
               "persistent Chromium profile); it is not runnable unattended in the cloud and login is never automated"),
}

#: ERP-family technologies worth a technology search, with the fuzzy names the
#: lookup API matches on (substring of the product NAME, per the original tool).
ERP_TECH_QUERIES = {
    "sap": "SAP", "oracle": "Oracle E-Business", "jd edwards": "JD Edwards", "infor": "Infor",
    "dynamics": "Dynamics", "as400": "AS/400", "iseries": "iSeries", "rpg": "RPG", "ibm i": "IBM i",
    "epicor": "Epicor", "netsuite": "NetSuite", "sage": "Sage", "syspro": "SYSPRO", "qad": "QAD",
}


class ZoomInfoError(ProviderError):
    pass


class _Token:
    def __init__(self, value: str, expires_at: float) -> None:
        self.value, self.expires_at = value, expires_at

    def valid(self, margin: float) -> bool:
        return time.time() < self.expires_at - margin

    def __repr__(self) -> str:  # never print the token
        return "_Token(***)"


class ZoomInfoConnector(EnrichmentProvider, TechnologyProvider):
    name = "zoominfo"
    access_method = "api"

    def __init__(self, secrets: Mapping[str, str], *, settings: Optional[Mapping[str, Any]] = None,
                 session: Any = None, sleep: Callable[[float], None] = time.sleep) -> None:
        secrets = secrets or {}
        self._client_id = secrets.get("client_id") or os.environ.get("ZOOMINFO_CLIENT_ID", "")
        self._client_secret = secrets.get("client_secret") or os.environ.get("ZOOMINFO_CLIENT_SECRET", "")
        self.settings = dict(settings or {})
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self._sleep = sleep
        self._token: Optional[_Token] = None
        self._lock = threading.Lock()
        self._min_interval = 1.0 / float(self.settings.get("requests_per_second", 2.0))
        self._last_call = 0.0
        self.max_retries = int(self.settings.get("max_retries", 3))
        self.max_retry_after = float(self.settings.get("max_retry_after_seconds", 120))
        self.calls = 0
        self.credit_consuming_calls = 0
        self.last_search: Dict[str, Any] = {}
        self.last_match_statuses: Dict[str, int] = {}

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # --- health -----------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        if not self.configured:
            return {"status": "not_configured",
                    "detail": ("ZoomInfo API credentials (client_id + client_secret for an OAuth client_credentials "
                               "application) are not connected. " + BROWSER_MODE_STATUS["detail"]),
                    "browser_mode": BROWSER_MODE_STATUS}
        if self.settings.get("verified"):
            return {"status": "ok", "detail": "verified"}
        return {"status": "configured_unverified", "detail": "credentials stored; run verify to request a token"}

    def verify(self, *, allow_paid: bool = False) -> Dict[str, Any]:
        """Request an OAuth token — authenticates without searching or spending."""
        if not self.configured:
            return self.health()
        self._get_token(force=True)
        return {"status": "ok", "detail": "OAuth token issued (no search run, no credits used)"}

    def estimate_cost(self, operation: str, n: int) -> float:
        # Search and lookup are credit-free per the original tool. Enrichment is
        # charged per record returned; one credit per record is the conservative
        # planning figure until an authorised run measures it.
        return float(n) if operation in ("enrich_company", "enrich_contacts") else 0.0

    # --- auth (ported TokenProvider) ---------------------------------------------------

    def _get_token(self, force: bool = False) -> str:
        if not self.configured:
            raise ProviderNotConfigured("ZoomInfo API credentials are not connected for this workspace")
        with self._lock:
            if not force and self._token is not None and self._token.valid(60):
                return self._token.value
            body = {"grant_type": "client_credentials", "client_id": self._client_id,
                    "client_secret": self._client_secret}
            if self.settings.get("scopes"):
                body["scope"] = self.settings["scopes"]
            try:
                response = self._session.post(TOKEN_URL, data=body, timeout=30, headers={
                    "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                    "User-Agent": USER_AGENT})
            except Exception as error:  # noqa: BLE001
                raise ZoomInfoError(f"could not reach the ZoomInfo token endpoint: {type(error).__name__}") from None
            if response.status_code in (400, 401):
                raise ZoomInfoError(f"ZoomInfo rejected the client credentials (HTTP {response.status_code})")
            if response.status_code == 403:
                raise ZoomInfoError("the ZoomInfo application is not authorised for the requested scopes (HTTP 403)")
            if response.status_code != 200:
                raise ZoomInfoError(f"unexpected HTTP {response.status_code} from the ZoomInfo token endpoint")
            payload = response.json()
            token = payload.get("access_token")  # opaque: used verbatim, never decoded or trimmed
            if not token:
                raise ZoomInfoError("the token response contained no access_token")
            self._token = _Token(token, time.time() + float(payload.get("expires_in") or 3600))
            return token

    # --- transport (ported ZoomInfoClient._request) ------------------------------------

    def _pace(self) -> None:
        delay = self._min_interval - (time.monotonic() - self._last_call)
        if delay > 0:
            self._sleep(delay)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, *, body: Any = None, params: Optional[Mapping[str, Any]] = None,
                 _reauth: bool = False) -> Dict[str, Any]:
        url = BASE_URL + path
        attempt = 0
        while True:
            attempt += 1
            self._pace()
            headers = {"Accept": JSON_API, "Content-Type": JSON_API, "User-Agent": USER_AGENT,
                       "Authorization": "Bearer " + self._get_token()}
            try:
                self.calls += 1
                response = self._session.request(method, url, headers=headers, json=body,
                                                 params=dict(params) if params else None, timeout=60)
            except Exception as error:  # noqa: BLE001
                if attempt > self.max_retries:
                    raise ZoomInfoError(f"network failure calling {path}: {type(error).__name__}") from None
                self._backoff(attempt)
                continue
            status = response.status_code
            if 200 <= status < 300:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ZoomInfoError(f"{path} returned a non-object body")
                return payload
            if status == 401:
                if _reauth:
                    raise ZoomInfoError("ZoomInfo returned HTTP 401 again after re-authenticating")
                with self._lock:
                    self._token = None
                return self._request(method, path, body=body, params=params, _reauth=True)
            if status == 403:
                raise ZoomInfoError(f"ZoomInfo returned HTTP 403 for {path}: the account is not entitled to this "
                                    "operation (not worked around)")
            if status == 429:
                raw = response.headers.get("Retry-After")
                retry_after = float(raw) if raw and str(raw).replace(".", "", 1).isdigit() else None
                if retry_after is not None and retry_after > self.max_retry_after:
                    raise ZoomInfoError(f"ZoomInfo rate limit on {path}: the server asked for {retry_after:.0f}s; "
                                        "resume later (the limit is never bypassed)")
                if attempt > self.max_retries:
                    raise ZoomInfoError(f"still rate limited on {path} after {self.max_retries} retries")
                self._sleep(retry_after) if retry_after is not None else self._backoff(attempt)
                continue
            if status in (400, 422):
                raise ZoomInfoError(f"ZoomInfo rejected the request to {path} (HTTP {status})")
            if status >= 500 and attempt <= self.max_retries:
                self._backoff(attempt)
                continue
            raise ZoomInfoError(f"unexpected HTTP {status} from {path}")

    def _backoff(self, attempt: int) -> None:
        delay = min(2.0 * (2 ** (attempt - 1)), 60.0)
        self._sleep(delay + random.uniform(0, delay * 0.1))

    # --- search (credit-free) and pagination -----------------------------------------------

    @staticmethod
    def _company(item: Mapping[str, Any], *, verified: bool) -> Dict[str, Any]:
        attrs = item.get("attributes") or item
        return {"name": attrs.get("name"), "website": attrs.get("website"), "city": attrs.get("city"),
                "state": attrs.get("state"), "country": attrs.get("country"),
                "employee_count": attrs.get("employeeCount"), "revenue": attrs.get("revenue"),
                "zoominfo_id": str(item.get("id") or attrs.get("id") or ""),
                "source": "zoominfo", "verified_shape": verified}

    def _search(self, path: str, kind: str, filters: Mapping[str, Any], limit: int) -> List[Dict[str, Any]]:
        """Page through a search: ``page[size]`` 1-100 per the API reference; stop at ``limit``, the last page
        (``meta.page.total``), a short page, or the configured ceiling. Search is credit-free but rate-limited."""
        attributes = {k: v for k, v in dict(filters).items() if v not in (None, "", [])}
        limit = max(1, min(int(limit), int(self.settings.get("max_search_results", MAX_SEARCH_RESULTS))))
        size = min(limit, PAGE_SIZE_MAX)
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            payload = self._request("POST", path, body={"data": {"type": kind, "attributes": attributes}},
                                    params={"page[number]": page, "page[size]": size})
            data = payload.get("data") or []
            items.extend(data)
            meta = payload.get("meta") or {}
            total_pages = (meta.get("page") or {}).get("total")
            self.last_search = {"total_results": meta.get("totalResults"), "total_pages": total_pages,
                                "pages_fetched": page}
            if len(items) >= limit or len(data) < size or (total_pages is not None and page >= int(total_pages)):
                return items[:limit]
            page += 1

    def search_companies(self, filters: Mapping[str, Any], *, limit: int = 25, allow_paid: bool = False
                         ) -> List[Dict[str, Any]]:
        return [self._company(item, verified=True)
                for item in self._search(COMPANY_SEARCH, "CompanySearch", filters, limit)]

    def lookup(self, field_name: str, filters: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        params = {f"filter[{k}]": v for k, v in (filters or {}).items() if v}
        return self._request("GET", LOOKUP.format(field_name=field_name), params=params)

    # --- technology (credit-free lookups + search) -------------------------------------------

    def search_technologies(self, query: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Tech products whose NAME contains ``query``. The lookup API requires a vendor,
        category or parentCategory filter; ERP queries default to parentCategory Operations."""
        filters = {"fuzzyMatch": ERP_TECH_QUERIES.get(query.lower(), query)}
        filters["parentCategory"] = self.settings.get("tech_parent_category", "Operations")
        payload = self.lookup("tech-products", filters)
        items = payload.get("data") or []
        out = []
        for item in items[:limit]:
            attrs = item.get("attributes") or item
            out.append({"technology": attrs.get("name") or attrs.get("product"), "category": attrs.get("category"),
                        "vendor": attrs.get("vendor"), "provider_id": str(item.get("id") or attrs.get("id") or ""),
                        "attribute": attrs.get("attribute")})
        return out

    def companies_using(self, technology_ids: Sequence[str], filters: Mapping[str, Any], *, limit: int = 25,
                        allow_paid: bool = False) -> List[Dict[str, Any]]:
        attributes = {**dict(filters), "techAttributeTagIdList": [str(t) for t in technology_ids]}
        rows = self.search_companies(attributes, limit=limit)
        for row in rows:
            row["technology_ids"] = list(technology_ids)
            row["evidence"] = {"source": "zoominfo", "search": "techAttributeTagIdList",
                               "technology_ids": list(technology_ids)}
        return rows

    # --- contacts and enrichment -------------------------------------------------------------

    def search_contacts(self, filters: Mapping[str, Any], *, limit: int = 25, allow_paid: bool = False
                        ) -> List[Dict[str, Any]]:
        """Credit-free: returns match hints (hasEmail, hasDirectPhone...), never emails or phone numbers."""
        out = []
        for item in self._search(CONTACT_SEARCH, "ContactSearch", filters, limit):
            attrs = item.get("attributes") or item
            company = attrs.get("company") if isinstance(attrs.get("company"), Mapping) else {}
            out.append({"full_name": " ".join(p for p in (attrs.get("firstName"), attrs.get("lastName")) if p),
                        "first_name": attrs.get("firstName"), "last_name": attrs.get("lastName"),
                        "title": attrs.get("jobTitle"), "company_name": company.get("name") or attrs.get("companyName"),
                        "zoominfo_company_id": str(company.get("id") or "") or None,
                        "contact_accuracy_score": attrs.get("contactAccuracyScore"),
                        "has_email": attrs.get("hasEmail"), "has_direct_phone": attrs.get("hasDirectPhone"),
                        "zoominfo_id": str(item.get("id") or ""), "source": "zoominfo", "verified_shape": True})
        return out

    # --- enrichment (consumes credits: one per newly managed record) -------------------------

    def _require_paid(self, allow_paid: bool, what: str) -> None:
        if not allow_paid:
            raise PaidCallRefused(f"ZoomInfo {what} consumes credits; it needs an explicit action with allow_paid")

    @staticmethod
    def _batch(inputs: Sequence[Mapping[str, Any]], what: str) -> List[Dict[str, Any]]:
        if not inputs:
            raise ValueError(f"ZoomInfo {what} needs at least one input")
        if len(inputs) > ENRICH_BATCH_MAX:
            raise ValueError(f"ZoomInfo {what} takes at most {ENRICH_BATCH_MAX} records per call; "
                             f"{len(inputs)} were given (bulk enrichment is never started implicitly)")
        # Numeric identifiers are integers in the API reference.
        return [{k: int(v) if k in ("personId", "companyId") and str(v).isdigit() else v for k, v in dict(i).items()}
                for i in inputs]

    def _matched(self, payload: Mapping[str, Any], accept: Sequence[str]) -> List[Mapping[str, Any]]:
        """Records the API actually matched. Non-matches (type NoMatch, NO_MATCH, OPT_OUT, LIMIT_EXCEEDED...)
        are counted in ``last_match_statuses`` and never returned as data."""
        matched, statuses = [], {}
        for item in payload.get("data") or []:
            status = (item.get("meta") or {}).get("matchStatus") or (
                "NO_MATCH" if item.get("type") == "NoMatch" else "FULL_MATCH")
            statuses[status] = statuses.get(status, 0) + 1
            if status in accept and item.get("type") != "NoMatch" and item.get("attributes"):
                matched.append(item)
        self.last_match_statuses = statuses
        return matched

    def enrich_company(self, identifiers: Mapping[str, Any], *, allow_paid: bool = False) -> Optional[Dict[str, Any]]:
        self._require_paid(allow_paid, "company enrichment")
        inputs = self._batch([identifiers], "company enrichment")
        self.credit_consuming_calls += 1
        payload = self._request("POST", COMPANY_ENRICH, body={"data": {"type": "CompanyEnrich", "attributes": {
            "matchCompanyInput": inputs, "outputFields": list(COMPANY_OUTPUT_FIELDS)}}})
        matched = self._matched(payload, ("FULL_MATCH",))
        return self._company(matched[0], verified=True) if matched else None

    def enrich_contacts(self, refs: Sequence[Mapping[str, Any]], *, allow_paid: bool = False) -> List[Dict[str, Any]]:
        self._require_paid(allow_paid, "contact enrichment")
        inputs = self._batch(refs, "contact enrichment")
        self.credit_consuming_calls += 1
        payload = self._request("POST", CONTACT_ENRICH, body={"data": {"type": "ContactEnrich", "attributes": {
            "matchPersonInput": inputs, "outputFields": list(CONTACT_OUTPUT_FIELDS)}}})
        out = []
        for item in self._matched(payload, ("FULL_MATCH", "CONTACT_ONLY_MATCH")):
            attrs = item["attributes"]
            company = attrs.get("company") if isinstance(attrs.get("company"), Mapping) else {}
            out.append({"full_name": " ".join(p for p in (attrs.get("firstName"), attrs.get("lastName")) if p),
                        "first_name": attrs.get("firstName"), "last_name": attrs.get("lastName"),
                        "title": attrs.get("jobTitle"), "email": attrs.get("email"),
                        "phone": attrs.get("phone") or attrs.get("directPhone"),
                        "mobile_phone": attrs.get("mobilePhone"),
                        "company_name": company.get("name") or attrs.get("companyName"),
                        "contact_accuracy_score": attrs.get("contactAccuracyScore"),
                        "zoominfo_id": str(item.get("id") or ""), "source": "zoominfo", "verified_shape": True})
        return out

if __name__ == "__main__":  # example call: python -m cloud.intel.providers.zoominfo "ZoomInfo"
    import json
    import sys

    connector = ZoomInfoConnector({})
    rows = connector.search_companies({"companyName": sys.argv[1] if len(sys.argv) > 1 else "ZoomInfo"}, limit=5)
    print(json.dumps(rows, indent=2))
