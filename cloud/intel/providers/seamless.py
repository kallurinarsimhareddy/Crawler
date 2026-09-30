# Transport PORTED from CareerCrawler-seamless seamless/{client,config,models}.py (uncommitted work on
# branch seamless-integration, read 2026-09-24): "Token" header on the session, per-endpoint pacing
# (60 req/min), retry only 429/5xx/transport, X-RateLimit-Reset honoured, X-PublicAPI-Credits observed
# on every response. Cost model and HR/IT/VP targeting come from the vendored seamless_credits.py and
# seamless_targeting.py. Adapted to per-workspace keys and the platform ledger.
"""Seamless.AI as a private, per-workspace enrichment provider.

Every Seamless call costs credits except polling (measured by the original
integration: a search costs ``ceil(limit/10)`` credits, research one credit per
record). So **every method except polling requires** ``allow_paid=True``; the
caller (``ContactIntelService``) reserves credits in the ledger first and
consumes what :meth:`SeamlessConnector.credits_spent` says was used.

The key belongs to one workspace's own Seamless account. It is decrypted only
inside the worker/API process for that workspace's request and never shared.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from cloud.intel.providers.base import EnrichmentProvider, PaidCallRefused, ProviderError, ProviderNotConfigured
from cloud.intel.vendor import seamless_credits, seamless_targeting

__all__ = ["SeamlessConnector", "SeamlessError"]

log = logging.getLogger(__name__)

BASE_URL = "https://api.seamless.ai/api/client/v1"
SEARCH_COMPANIES = "/search/companies"
SEARCH_CONTACTS = "/search/contacts"
RESEARCH_CONTACTS = "/contacts/research"
POLL_CONTACTS = "/contacts/research/poll"
CREDITS_HEADER = "X-PublicAPI-Credits"
MAX_BATCH = 100


class SeamlessError(ProviderError):
    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status, self.retryable = status, retryable


def _text(source: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = source.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def contact_from_search(item: Mapping[str, Any]) -> Dict[str, Any]:
    """A contact search hit (``ContactMatch`` fields in the original models)."""
    title = _text(item, "title")
    return {"search_result_id": _text(item, "searchResultId"), "full_name": _text(item, "name", "fullName"),
            "title": title, "department": _text(item, "department"), "seniority": _text(item, "seniority"),
            "company_name": _text(item, "company"), "domain": _text(item, "domain", "companyDomainAlias"),
            "linkedin_url": _text(item, "liUrl"), "city": _text(item, "city"), "state": _text(item, "state"),
            "country": _text(item, "country"), "function": seamless_targeting.classify(title),
            "target_score": seamless_targeting.score(title, _text(item, "seniority"), _text(item, "department"))}


def contact_from_research(item: Mapping[str, Any]) -> Dict[str, Any]:
    """An enriched contact (``ContactEnrichment`` fields in the original models)."""
    location = item.get("contactLocation") if isinstance(item.get("contactLocation"), Mapping) else {}
    return {"seamless_id": _text(item, "contactId", "apiResearchId"), "full_name": _text(item, "fullName", "name"),
            "first_name": _text(item, "firstName"), "last_name": _text(item, "lastName"),
            "title": _text(item, "title"), "department": _text(item, "department"),
            "seniority": _text(item, "seniority"), "company_name": _text(item, "company", "companyOriginal"),
            "domain": _text(item, "companyDomain", "emailDomain"), "email": _text(item, "email", "email1", "email2"),
            "email_confidence": _text(item, "email1TotalAI", "email1EmailAI"),
            "phone": _text(item, "contactPhone1", "contactPhone2"), "linkedin_url": _text(item, "lIProfileUrl"),
            "city": _text(location, "city"), "state": _text(location, "state"),
            "country": _text(location, "country"), "source": "seamless"}


class SeamlessConnector(EnrichmentProvider):
    name = "seamless"
    access_method = "api"

    def __init__(self, secrets: Mapping[str, str], *, settings: Optional[Mapping[str, Any]] = None,
                 session: Any = None, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time) -> None:
        self._key = (secrets or {}).get("api_key") or ""
        self.settings = dict(settings or {})
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self._sleep, self._monotonic, self._wall_clock = sleep, monotonic, wall_clock
        self._interval = 60.0 / max(1, int(self.settings.get("requests_per_minute", 60)))
        self._next_free: Dict[str, float] = {}
        self.max_attempts = int(self.settings.get("max_attempts", 4))
        self.credits_remaining: Optional[int] = None
        self.estimated_spend = 0
        self.calls = 0

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def health(self) -> Dict[str, Any]:
        if not self.configured:
            return {"status": "not_configured",
                    "detail": "a Seamless.AI API key for this workspace's own Seamless account is not connected"}
        if self.settings.get("verified"):
            return {"status": "ok", "detail": "verified"}
        return {"status": "configured_unverified", "detail": "key stored; not yet verified"}

    def verify(self, *, allow_paid: bool = False) -> Dict[str, Any]:
        """Check the key for free; ``allow_paid`` instead runs a 1-credit search."""
        if not self.configured:
            return self.health()
        if allow_paid:
            self.search_companies({"companyDomain": ["seamless.ai"]}, limit=1, allow_paid=True)
            return {"status": "ok", "detail": "a 1-credit search succeeded", "credits_used": 1,
                    "credits_remaining": self.credits_remaining}
        # Seamless has no account/status endpoint, but polling is free (measured, see
        # seamless_credits.CREDITS_PER_POLL_CALL) and authenticated: a poll for a request id that does
        # not exist is answered 401/403 for a bad key and 2xx/400/404 for a good one.
        try:
            self._request("GET", POLL_CONTACTS, params={"requestIds": "sanagtm-auth-check"}, allow_paid=False)
        except SeamlessError as error:
            if error.status in (401, 403):
                return {"status": "error", "detail": str(error), "credits_used": 0}
            if error.status not in (400, 404):
                raise
        return {"status": "ok", "detail": "key authenticated by a zero-credit poll", "credits_used": 0,
                "credits_remaining": self.credits_remaining}

    def estimate_cost(self, operation: str, n: int) -> float:
        if operation in ("search_contacts", "search_companies"):
            return float(seamless_credits.credit_cost_for(SEARCH_CONTACTS, {"limit": max(1, n)}))
        if operation in ("enrich_contacts", "research_contacts"):
            return float(seamless_credits.credit_cost_for(RESEARCH_CONTACTS, {"searchResultIds": list(range(max(1, n)))}))
        return 0.0

    def credit_balance(self) -> Optional[float]:
        return None if self.credits_remaining is None else float(self.credits_remaining)

    # --- transport ---------------------------------------------------------------------

    def _pace(self, path: str) -> None:
        now = self._monotonic()
        ready = self._next_free.get(path, 0.0)
        self._next_free[path] = max(now, ready) + self._interval
        if ready > now:
            self._sleep(ready - now)

    def _request(self, method: str, path: str, *, body: Any = None, params: Any = None,
                 allow_paid: bool) -> Any:
        if not self.configured:
            raise ProviderNotConfigured("Seamless is not connected for this workspace")
        cost = seamless_credits.credit_cost_for(path, body)
        if cost > 0 and not allow_paid:
            raise PaidCallRefused(f"Seamless {path} costs {cost} credit(s); it needs an explicit action with allow_paid")
        self.estimated_spend += cost
        headers = {"Token": self._key, "Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": "CareerCrawler-Platform/1.0"}
        for attempt in range(1, self.max_attempts + 1):
            self._pace(path)
            self.calls += 1
            try:
                response = self._session.request(method, BASE_URL + path, json=body, params=params, headers=headers,
                                                 timeout=60)
            except Exception as error:  # noqa: BLE001
                error = SeamlessError(f"{method} {path} failed: {type(error).__name__}", retryable=True)
            else:
                raw = response.headers.get(CREDITS_HEADER)
                if raw is not None and str(raw).strip().lstrip("-").isdigit():
                    self.credits_remaining = int(str(raw).strip())
                status = response.status_code
                if 200 <= status < 300:
                    try:
                        return response.json()
                    except ValueError:
                        raise SeamlessError(f"{path} returned a non-JSON body", status=status) from None
                if status in (401, 403):
                    raise SeamlessError(f"Seamless rejected the key (HTTP {status})", status=status)
                if status == 422:
                    raise SeamlessError(f"Seamless refused the request (HTTP 422, often out of credits)", status=status)
                if status in (400, 404):
                    raise SeamlessError(f"Seamless rejected the request to {path} (HTTP {status})", status=status)
                error = SeamlessError(f"HTTP {status} from {path}", status=status, retryable=status == 429 or status >= 500)
                if status == 429:
                    reset = response.headers.get("X-RateLimit-Reset")
                    if reset and str(reset).isdigit() and attempt < self.max_attempts:
                        self._sleep(max(0.0, float(reset) - self._wall_clock()) + 1.0)
                        continue
            if not error.retryable or attempt >= self.max_attempts:
                raise error
            delay = min(60.0, 2.0 * (2 ** (attempt - 1)))
            self._sleep(delay + random.uniform(0, delay * 0.25))
        raise SeamlessError(f"{method} {path} produced no response")

    # --- operations ----------------------------------------------------------------------

    def search_companies(self, filters: Mapping[str, Any], *, limit: int = 25, allow_paid: bool = False
                         ) -> List[Dict[str, Any]]:
        body = {"limit": max(1, int(limit)), **{k: v for k, v in dict(filters).items() if v not in (None, "", [])}}
        payload = self._request("POST", SEARCH_COMPANIES, body=body, allow_paid=allow_paid) or {}
        out = []
        for item in payload.get("data") or []:
            out.append({"name": _text(item, "name"), "domain": _text(item, "domain"),
                        "industry": _text(item, "industries", "industry"), "search_result_id": _text(item, "searchResultId"),
                        "source": "seamless"})
        return out

    def search_contacts(self, filters: Mapping[str, Any], *, limit: int = 25, allow_paid: bool = False
                        ) -> List[Dict[str, Any]]:
        body = {"limit": max(1, int(limit)), **{k: v for k, v in dict(filters).items() if v not in (None, "", [])}}
        payload = self._request("POST", SEARCH_CONTACTS, body=body, allow_paid=allow_paid) or {}
        return [contact_from_search(item) for item in payload.get("data") or []]

    @staticmethod
    def select_targets(matches: Sequence[Dict[str, Any]], wanted: int) -> List[Dict[str, Any]]:
        """Round-robin across HR/IT/executive functions, most senior first (vendored targeting)."""

        class _M:
            def __init__(self, row):
                self.row, self.title = row, row.get("title", "")
                self.seniority, self.department = row.get("seniority", ""), row.get("department", "")

        chosen = seamless_targeting.select([_M(m) for m in matches], wanted)
        return [c.row for c in chosen]

    def enrich_contacts(self, refs: Sequence[Mapping[str, Any]], *, allow_paid: bool = False,
                        max_polls: int = 10, poll_interval: float = 3.0) -> List[Dict[str, Any]]:
        """Research contacts by ``search_result_id`` (1 credit each), then poll (free)."""
        ids = [str(r.get("search_result_id")) for r in refs if r.get("search_result_id")][:MAX_BATCH]
        if not ids:
            return []
        ticket = self._request("POST", RESEARCH_CONTACTS, body={"searchResultIds": ids}, allow_paid=allow_paid) or {}
        request_ids = [str(r) for r in (ticket.get("requestIds") or []) if r]
        done: Dict[str, Dict[str, Any]] = {}
        for _ in range(max_polls):
            pending = [r for r in request_ids if r not in done]
            if not pending:
                break
            payload = self._request("GET", POLL_CONTACTS, params={"requestIds": ",".join(pending)},
                                    allow_paid=allow_paid) or {}
            for item in payload.get("data") or []:
                status = _text(item, "status").lower()
                if status == "done" and isinstance(item.get("contact"), Mapping):
                    done[_text(item, "requestId")] = contact_from_research(item["contact"])
                elif status in ("error", "missing"):
                    done[_text(item, "requestId")] = {}
            if len(done) < len(request_ids):
                self._sleep(poll_interval)
        return [c for c in done.values() if c]
