"""Offline fakes shared by the Track E/G/H tests (no TestCase here).

Nothing in these tests touches the network: HTTP goes through :class:`FakeSession`,
DNS through :func:`public_resolver`, and the CRM/jobs/automation services that
other tracks own are stubbed so these tests do not depend on them.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from cryptography.fernet import Fernet

from cloud.intel.core.audit import provenance
from cloud.intel.core.context import Ctx
from cloud.intel.core.http import SafeFetcher
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore


def public_resolver(host, port, type=None):  # noqa: A002 - mirrors socket.getaddrinfo
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, headers: Optional[Dict[str, str]] = None,
                 text: Optional[str] = None) -> None:
        self.status_code = status
        if text is None:
            text = body if isinstance(body, str) else json.dumps(body if body is not None else {})
        self.text = text
        self.content = text.encode()
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.encoding = "utf-8"
        self.is_redirect = status in (301, 302, 303, 307, 308) and "Location" in self.headers
        self.ok = 200 <= status < 400

    def json(self):
        return json.loads(self.text)

    def iter_content(self, size):
        yield self.content

    def close(self):
        pass


class FakeSession:
    """Routes by URL substring; records every call. Unmatched URLs are 404."""

    def __init__(self, routes: Optional[Dict[str, Any]] = None) -> None:
        self.routes: Dict[str, Any] = dict(routes or {})
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.headers: Dict[str, str] = {}

    def _respond(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for fragment, answer in self.routes.items():
            if fragment in url:
                if callable(answer):
                    answer = answer(method, url, kwargs)
                if isinstance(answer, list) and answer and isinstance(answer[0], FakeResponse):  # consumed in order
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                return answer if isinstance(answer, FakeResponse) else FakeResponse(200, answer)
        return FakeResponse(404, {"error": "not found"})

    def request(self, method, url, **kwargs):
        return self._respond(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)


def fetcher(routes: Dict[str, Any]) -> Tuple[SafeFetcher, FakeSession]:
    session = FakeSession(routes)
    return SafeFetcher(session=session, resolver=public_resolver, per_host_delay=0, respect_robots=False), session


class StubCrm:
    """Minimal stand-in for Track A's CrmService.upsert_contact (dedupe by email, else by name)."""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    def upsert_contact(self, ctx: Ctx, values: Mapping[str, Any], *, source_kind: str, source_name: str,
                       source_ref: Optional[str] = None, original: Optional[Mapping[str, Any]] = None,
                       confidence: Optional[float] = None) -> Dict[str, Any]:
        store = self.platform.store
        existing = None
        if values.get("email"):
            existing = store.first(ctx, "contacts", {"email": values["email"]})
        if existing is None:
            existing = store.first(ctx, "contacts", {"company_id": values.get("company_id"),
                                                     "full_name": values["full_name"]})
        allowed = {k: v for k, v in values.items() if v is not None}
        if confidence is not None:
            allowed["confidence"] = confidence
        if existing is None:
            row, created = store.insert(ctx, "contacts", allowed), True
        else:
            row, created = store.update(ctx, "contacts", existing["id"], {k: v for k, v in allowed.items()
                                                                          if not existing.get(k)}), False
        provenance(store, ctx, "contacts", row["id"], source_kind=source_kind, source_name=source_name,
                   source_ref=source_ref, original=original or {}, confidence=confidence)
        return {"contact": row, "created": created}


class StubJobs:
    def __init__(self) -> None:
        self.batches: List[Dict[str, Any]] = []

    def ingest_postings(self, ctx, postings, *, source_kind, source_name, company_id=None, crawled_company_ids=None):
        self.batches.append({"postings": list(postings), "source_kind": source_kind, "source_name": source_name})
        return {"received": len(postings)}


class StubAutomation:
    def __init__(self) -> None:
        self.events: List[Tuple[str, str, Dict[str, Any]]] = []

    def emit(self, ctx, trigger, event_key, payload):
        self.events.append((trigger, event_key, dict(payload)))
        return []


def make_platform(*, secrets_key: Optional[str] = "generate", max_credits_per_task: float = 500.0):
    store = MemoryStore()
    key = Fernet.generate_key().decode() if secrets_key == "generate" else secrets_key
    platform = Platform(store, config=PlatformConfig(environment="test", secrets_key=key,
                                                     max_credits_per_task=max_credits_per_task))
    platform.override("crm", StubCrm(platform))
    jobs, automation = StubJobs(), StubAutomation()
    platform.override("jobs", jobs)
    platform.override("automation", automation)
    owner = str(uuid.uuid4())
    ws = store.create_workspace(owner, "Acme", f"acme-{uuid.uuid4().hex[:8]}")
    ctx = Ctx(ws["id"], owner, "owner")
    return platform, ctx, jobs, automation


GREENHOUSE = {"jobs": [
    {"id": 101, "title": "SAP FICO Consultant", "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
     "location": {"name": "Remote - US"}, "updated_at": "2026-09-01T10:00:00-04:00",
     "departments": [{"name": "IT"}], "content": "&lt;p&gt;5+ years SAP&lt;/p&gt;"},
    {"id": 102, "title": "Plant Manager", "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
     "location": {"name": "Tulsa, OK"}, "updated_at": "2026-09-02T10:00:00Z", "departments": []},
    {"id": 101, "title": "SAP FICO Consultant", "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
     "location": {"name": "Remote - US"}},
]}

LEVER = [
    {"id": "abc", "text": "ERP Analyst (JD Edwards)", "hostedUrl": "https://jobs.lever.co/acme/abc",
     "categories": {"location": "Dallas, TX", "team": "Information Technology", "commitment": "Full-time"},
     "createdAt": 1756000000000, "workplaceType": "hybrid", "descriptionPlain": "JDE E1 experience"},
]

HOME = """<html><body><nav><a href="/about/leadership">Leadership Team</a>
<a href="https://elsewhere.com/team">Partner team</a></nav><footer>info@acme.com</footer></body></html>"""

LEADERSHIP = """<html><body><h1>Our Leadership</h1>
<div class="person"><h3>Jane Smith</h3><p>Chief Information Officer</p>
<a href="mailto:jane.smith@acme.com">Email Jane</a></div>
<div class="person"><h3>Carol White</h3><p>Director of Information Technology</p></div>
</body></html>"""
