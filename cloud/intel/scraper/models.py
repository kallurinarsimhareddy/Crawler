"""Plain data types shared by every scraper stage.

    ScrapeInput ─► crawler (fetcher, pagination, discovery, details) ─► PageExtraction
                   records of FieldValue (value + method + confidence + evidence + source)
                                          │
                   normalizer ─► validator ─► dedupe ─► exports / CRM proposals

``Outcome`` is what happened to one page. Anything other than ``OK``/``EMPTY``
means nothing was extracted from it, and the reason is recorded rather than
worked around: the scraper never solves a CAPTCHA, gets past a WAF, logs in or
pays.

**Precedence.** When two sources give a field different values the stronger
method wins (:data:`METHOD_RANK`): official API > structured data > page
metadata > deterministic extraction > browser-rendered extraction > AI. The
weaker value is kept as an *alternative*, so a conflict is visible, never
silently dropped.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

__all__ = ["BLOCKING", "CrawlOptions", "FieldValue", "FetchedPage", "METHOD_RANK", "NO_RETRY", "Outcome",
           "PageExtraction", "PageVisit", "ScrapeInput", "STAGES", "TRANSIENT", "merge_value", "rank",
           "same_value", "store_status"]


class Outcome:
    OK = "OK"                          # fetched and at least one requested value found
    EMPTY = "EMPTY"                    # fetched, but none of the requested values are on the page
    BLOCKED = "BLOCKED"                # HTTP 403/451 or similar refusal
    CAPTCHA = "CAPTCHA"                # a CAPTCHA / bot challenge page
    WAF = "WAF"                        # a web application firewall answered instead of the site
    LOGIN_REQUIRED = "LOGIN_REQUIRED"  # 401, or redirected to a sign-in form
    ROBOTS = "ROBOTS"                  # robots.txt disallows the URL
    RATE_LIMITED = "RATE_LIMITED"      # HTTP 429 (after the bounded backoff)
    NOT_FOUND = "NOT_FOUND"            # HTTP 404/410
    TIMEOUT = "TIMEOUT"
    UNSAFE = "UNSAFE"                  # private/loopback address, bad port — refused before connecting
    LIMIT = "LIMIT"                    # not fetched: a page/request/runtime limit was reached
    SKIPPED = "SKIPPED"                # not fetched: already visited in this run
    FAILED = "FAILED"                  # anything else (DNS, TLS, 5xx, not HTML)


#: Outcomes where the site refused us. Reported, never bypassed.
BLOCKING = frozenset({Outcome.BLOCKED, Outcome.CAPTCHA, Outcome.WAF, Outcome.LOGIN_REQUIRED, Outcome.ROBOTS,
                      Outcome.RATE_LIMITED})
#: Outcomes a later retry may fix.
TRANSIENT = frozenset({Outcome.TIMEOUT, Outcome.FAILED, Outcome.RATE_LIMITED})
#: Never retried, not even by a run-level retry.
NO_RETRY = frozenset({Outcome.BLOCKED, Outcome.CAPTCHA, Outcome.WAF, Outcome.LOGIN_REQUIRED, Outcome.ROBOTS,
                      Outcome.NOT_FOUND, Outcome.UNSAFE})

#: Progress stages shown in the UI, in order. They are also the run's status while it works.
STAGES = ("queued", "planning", "fetching", "extracting", "paginating", "enriching", "validating", "normalizing",
          "saving", "completed")

#: Evidence strength per method (higher wins). ``browser`` values rank below every
#: HTTP-deterministic method but above AI.
METHOD_RANK: Dict[str, int] = {"ats-api": 60, "json-ld": 50, "meta": 40, "link": 30, "heading": 30, "regex": 30,
                               "url": 30, "derived": 25, "browser": 20, "ai": 10}


def store_status(outcome: str) -> str:
    """Map an outcome onto the four statuses the ``scrape_results`` table allows."""
    if outcome == Outcome.OK:
        return "ok"
    if outcome == Outcome.EMPTY:
        return "empty"
    if outcome in BLOCKING:
        return "blocked"
    return "error"


@dataclass
class ScrapeInput:
    """One URL the user gave, and where it came from."""

    url: str
    row: int                 # 1-based line (pasted) or spreadsheet row (header is row 1)
    batch: str               # import batch id; one per paste or uploaded file
    source: str = "paste"    # "paste", "list" or the uploaded file's name

    def as_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "row": self.row, "batch": self.batch, "source": self.source}


@dataclass
class CrawlOptions:
    """Bounds for one run. Every limit is enforced; nothing crawls without end."""

    pagination: bool = True
    max_pages: int = 25                 # pages per input URL (listing + careers + detail)
    max_listing_pages: int = 25         # extra listing pages followed by pagination, per input URL
    max_records: int = 10_000           # records per run
    max_runtime_s: float = 1800.0       # wall-clock seconds per run
    max_requests_per_domain: int = 300  # per run
    concurrency: int = 4                # input URLs crawled at once
    domain_concurrency: int = 2         # requests in flight to one domain
    follow_details: bool = False        # open each job's detail page
    max_detail_pages: int = 50          # detail pages per input URL (count toward max_pages too)
    browser: bool = False               # browser rendering fallback (off by default)
    browser_concurrency: int = 1
    max_browser_pages: int = 10         # per run
    use_ai: bool = True
    max_ai_calls: Optional[int] = None  # None: the platform default
    ai_concurrency: int = 1
    max_retries: int = 2                # transient failures (408/429/5xx/timeouts), per request
    max_backoff_s: float = 30.0
    discovery: bool = True              # look for careers/ATS pages from a homepage

    LIMITS = {"max_pages": (1, 500), "max_listing_pages": (0, 500), "max_records": (1, 100_000), "max_runtime_s": (10, 6 * 3600),
              "max_requests_per_domain": (1, 5000), "concurrency": (1, 16), "domain_concurrency": (1, 8),
              "max_detail_pages": (0, 1000), "browser_concurrency": (1, 4), "max_browser_pages": (0, 200),
              "ai_concurrency": (1, 4), "max_retries": (0, 5), "max_backoff_s": (0, 300)}

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "CrawlOptions":
        """Options from a request, clamped to safe bounds; unknown keys are ignored."""
        options = cls()
        for key, value in dict(raw or {}).items():
            if key == "max_runtime_minutes" and value is not None:
                key, value = "max_runtime_s", float(value) * 60
            if not hasattr(options, key) or key == "LIMITS" or value is None:
                continue
            default = getattr(cls, key, None)
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
            elif key in cls.LIMITS:
                low, high = cls.LIMITS[key]
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                number = min(max(number, low), high)
                value = number if isinstance(default, float) else int(number)
            elif key == "max_ai_calls":
                try:
                    value = max(0, min(int(value), 500))
                except (TypeError, ValueError):
                    continue
            setattr(options, key, value)
        return options

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FetchedPage:
    url: str
    final_url: str
    outcome: str
    http_status: int = 0
    html: str = ""
    reason: Optional[str] = None
    rendered: bool = False   # True when a browser produced ``html``
    truncated: bool = False
    attempts: int = 1
    browser_reason: Optional[str] = None
    browser_duration_ms: Optional[float] = None
    browser_outcome: Optional[str] = None


@dataclass
class FieldValue:
    """One extracted value and how we know it."""

    value: Any
    method: str              # ats-api | json-ld | meta | link | heading | regex | url | derived | ai
    confidence: float
    evidence: Optional[str] = None   # short snippet or the attribute it came from
    source_url: Optional[str] = None
    browser: bool = False            # found in browser-rendered HTML
    #: Weaker values from other sources that disagree with this one (conflicts).
    alternatives: List["FieldValue"] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        out = {"value": self.value, "method": self.method, "confidence": self.confidence,
               "evidence": self.evidence, "source_url": self.source_url}
        if self.browser:
            out["browser"] = True
        if self.alternatives:
            out["alternatives"] = [a.as_dict() for a in self.alternatives]
        return out


def rank(fv: FieldValue) -> int:
    base = METHOD_RANK.get(fv.method, 30)
    if fv.browser and fv.method != "ai":
        base = min(base, METHOD_RANK["browser"])
    return base


def _norm(value: Any) -> str:
    if isinstance(value, list):
        return "|".join(sorted(_norm(v) for v in value))
    text = str(value).strip().lower().rstrip("/")
    for prefix in ("https://", "http://", "www."):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return " ".join(text.split())


def same_value(a: Any, b: Any) -> bool:
    return _norm(a) == _norm(b)


def merge_value(current: Optional[FieldValue], incoming: Optional[FieldValue]) -> Optional[FieldValue]:
    """Combine two values for one field by precedence, keeping a disagreeing loser as an alternative."""
    if incoming is None or incoming.value in (None, "", []):
        return current
    if current is None or current.value in (None, "", []):
        return incoming
    if same_value(current.value, incoming.value):
        winner, loser = (current, incoming) if rank(current) >= rank(incoming) else (incoming, current)
        winner.confidence = max(winner.confidence, loser.confidence)
        return winner
    stronger = rank(incoming) > rank(current) or (rank(incoming) == rank(current)
                                                 and incoming.confidence > current.confidence)
    winner, loser = (incoming, current) if stronger else (current, incoming)
    alternatives = list(winner.alternatives)
    for alt in [loser] + list(loser.alternatives):
        if not same_value(alt.value, winner.value) and not any(same_value(alt.value, a.value) for a in alternatives):
            alternatives.append(FieldValue(alt.value, alt.method, alt.confidence, alt.evidence, alt.source_url,
                                           alt.browser))
    winner.alternatives = alternatives
    return winner


@dataclass
class PageVisit:
    """One fetched (or refused, or skipped) page, as stored in ``scrape_pages``."""

    url: str
    final_url: str
    outcome: str
    kind: str = "input"       # input | discovery | careers | listing | detail | api
    depth: int = 0
    page_no: Optional[int] = None
    http_status: int = 0
    attempts: int = 1
    records: int = 0
    browser_used: bool = False
    browser_reason: Optional[str] = None
    browser_duration_ms: Optional[float] = None
    browser_outcome: Optional[str] = None
    error: Optional[str] = None
    fetched_at: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["fetched_at"] = self.fetched_at.isoformat() if self.fetched_at else None
        out["rendered"] = self.browser_used   # V1 name
        return out


@dataclass
class PageExtraction:
    """What one input URL produced (including every page it led to)."""

    url: str
    final_url: str
    outcome: str
    records: List[Dict[str, FieldValue]] = field(default_factory=list)
    pages: List[Dict[str, Any]] = field(default_factory=list)   # PageVisit.as_dict() for every page
    problems: List[str] = field(default_factory=list)
    ai_used: bool = False
    fetched_at: Optional[datetime] = None
    page_text: str = ""
    stats: Dict[str, int] = field(default_factory=dict)          # requests, browser_pages, detail_pages…
    started: float = field(default_factory=time.monotonic)

    @property
    def method(self) -> str:
        methods = sorted({fv.method for record in self.records for fv in record.values()})
        return "+".join(methods)[:60] or "none"
