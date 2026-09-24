"""The provider-neutral external source layer.

A :class:`SourceAdapter` is one place jobs or companies can come from. It is
honest about how it gets them (``access_method``) and what it needs
(``requires``): an adapter whose credentials are missing reports
``not_configured`` from :meth:`health` and refuses :meth:`search` with
:class:`SourceUnavailable` — it never falls back to scraping a site that
requires authorisation, and never works around a CAPTCHA, WAF, login wall,
paywall or rate limit.

Unified output: :meth:`normalize` turns a raw record into a dict whose keys are
``job_postings`` columns (``company_name``, ``title``, ``job_url``,
``location``, ``posted_at``, ``description``, ``department``,
``employment_type``, ``workplace_type``, ``external_id``, ``ats``,
``source_name``, …) so Track C's ``JobIntelService.ingest_postings`` can take
them from any source.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = ["SourceAdapter", "SourceQuery", "SourceUnavailable", "SourceError", "dedupe_postings"]


class SourceError(RuntimeError):
    """A configured source failed (network, bad response, provider error)."""


class SourceUnavailable(SourceError):
    """The source cannot be used by this workspace (credentials/authorisation missing)."""


@dataclass
class SourceQuery:
    keywords: str = ""
    location: str = ""
    company: str = ""
    domain: str = ""
    #: A careers/ATS board URL (``https://boards.greenhouse.io/acme``), for ATS sources.
    board_url: str = ""
    limit: int = 50
    posted_within_days: Optional[int] = None
    page: int = 1
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SourceQuery":
        known = {k: data[k] for k in ("keywords", "location", "company", "domain", "board_url", "limit",
                                      "posted_within_days", "page") if k in data and data[k] is not None}
        extra = {k: v for k, v in data.items() if k not in known}
        query = cls(**known, extra=extra)
        query.limit = max(1, min(int(query.limit), 500))
        return query

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _key(row: Mapping[str, Any]) -> str:
    url = str(row.get("job_url") or "").strip().lower().rstrip("/")
    if url:
        return "u:" + url.split("#", 1)[0]
    text = "|".join(str(row.get(k) or "").strip().lower() for k in ("company_name", "title", "location"))
    return "c:" + hashlib.sha256(text.encode()).hexdigest()


def dedupe_postings(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """First occurrence wins, by job URL, else by (company, title, location) hash —
    the same two layers the external job-board crawler used."""
    seen, out = set(), []
    for row in rows:
        key = _key(row)
        if key not in seen:
            seen.add(key)
            out.append(dict(row))
    return out


class SourceAdapter(ABC):
    name: str = "source"
    label: str = "Source"
    #: "job_board" | "ats" | "company"
    kind: str = "job_board"
    #: "official_api" | "public_api" | "partner" | "authorized_account"
    access_method: str = "official_api"
    #: Credential names this source needs from the workspace connection.
    requires: Sequence[str] = ()
    #: Whether a call may consume paid credits/quota the workspace pays for.
    paid: bool = False
    #: What the operator must obtain for this source to work, shown when not configured.
    requirement: str = ""

    def __init__(self, *, credentials: Optional[Mapping[str, str]] = None,
                 settings: Optional[Mapping[str, Any]] = None, fetcher: Any = None) -> None:
        self.credentials = dict(credentials or {})
        self.settings = dict(settings or {})
        self._fetcher = fetcher
        self.calls = 0
        self.errors = 0
        self.verified = bool(self.settings.get("verified"))

    @property
    def fetcher(self):
        if self._fetcher is None:
            from cloud.intel.core.http import SafeFetcher

            self._fetcher = SafeFetcher(per_host_delay=0.5)
        return self._fetcher

    @property
    def configured(self) -> bool:
        return all(self.credentials.get(name) for name in self.requires)

    def missing(self) -> List[str]:
        return [name for name in self.requires if not self.credentials.get(name)]

    def require_configured(self) -> None:
        if not self.configured:
            raise SourceUnavailable(
                f"{self.label} is not configured for this workspace: {self.requirement or 'credentials required'}"
                + (f" (missing: {', '.join(self.missing())})" if self.missing() else ""))

    def health(self) -> Dict[str, Any]:
        if not self.configured:
            return {"status": "not_configured", "detail": self.requirement, "missing": self.missing()}
        if not self.verified:
            return {"status": "configured_unverified",
                    "detail": "credentials stored; not yet verified against the live provider"}
        return {"status": "ok", "detail": "verified"}

    @abstractmethod
    def search(self, query: SourceQuery) -> List[Dict[str, Any]]:
        """Raw records for a query. Raises :class:`SourceUnavailable` when not configured."""

    def collect(self, ref: str) -> Optional[Dict[str, Any]]:
        """One raw record by reference (URL or provider id); optional."""
        return None

    @abstractmethod
    def normalize(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        """A raw record as a unified ``job_postings``-shaped dict."""

    def dedupe(self, rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        return dedupe_postings(rows)

    def usage(self) -> Dict[str, Any]:
        return {"calls": self.calls, "errors": self.errors}

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "label": self.label, "kind": self.kind, "access_method": self.access_method,
                "requires": list(self.requires), "paid": self.paid, "requirement": self.requirement}

    def run(self, query: SourceQuery) -> List[Dict[str, Any]]:
        """search -> normalize -> dedupe, the path every caller should use."""
        raws = self.search(query)
        rows = [self.normalize(raw) for raw in raws]
        return self.dedupe([r for r in rows if r.get("title") and r.get("job_url")])
