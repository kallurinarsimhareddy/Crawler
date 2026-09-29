"""Plain data types shared by every scraper stage.

    ScrapeInput ─► fetcher ─► FetchedPage (outcome) ─► extractor ─► PageExtraction (records + evidence)
                                                                          │
                              normalizer ─► validator ─► dedupe ─► exports ◄┘

``Outcome`` is what happened to one page. Anything other than ``OK``/``EMPTY``
means nothing was extracted, and the reason is recorded rather than worked
around: the scraper never solves a CAPTCHA, gets past a WAF, logs in or pays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

__all__ = ["BLOCKING", "FieldValue", "FetchedPage", "Outcome", "PageExtraction", "ScrapeInput", "STAGES",
           "TRANSIENT", "store_status"]


class Outcome:
    OK = "OK"                          # fetched and at least one requested value found
    EMPTY = "EMPTY"                    # fetched, but none of the requested values are on the page
    BLOCKED = "BLOCKED"                # HTTP 403/451 or similar refusal
    CAPTCHA = "CAPTCHA"                # a CAPTCHA / bot challenge page
    WAF = "WAF"                        # a web application firewall answered instead of the site
    LOGIN_REQUIRED = "LOGIN_REQUIRED"  # 401, or redirected to a sign-in form
    ROBOTS = "ROBOTS"                  # robots.txt disallows the URL
    RATE_LIMITED = "RATE_LIMITED"      # HTTP 429
    NOT_FOUND = "NOT_FOUND"            # HTTP 404/410
    TIMEOUT = "TIMEOUT"
    UNSAFE = "UNSAFE"                  # private/loopback address, bad port — refused before connecting
    FAILED = "FAILED"                  # anything else (DNS, TLS, 5xx, not HTML)


#: Outcomes where the site refused us. Reported, never bypassed.
BLOCKING = frozenset({Outcome.BLOCKED, Outcome.CAPTCHA, Outcome.WAF, Outcome.LOGIN_REQUIRED, Outcome.ROBOTS,
                      Outcome.RATE_LIMITED})
#: Outcomes a later retry may fix.
TRANSIENT = frozenset({Outcome.TIMEOUT, Outcome.FAILED, Outcome.RATE_LIMITED})

#: Progress stages shown in the UI, in order.
STAGES = ("Queued", "Fetching", "Extracting", "Normalizing", "Saving", "Done")


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
class FetchedPage:
    url: str
    final_url: str
    outcome: str
    http_status: int = 0
    html: str = ""
    reason: Optional[str] = None
    rendered: bool = False   # True when a browser produced ``html``
    truncated: bool = False


@dataclass
class FieldValue:
    """One extracted value and how we know it."""

    value: Any
    method: str              # json-ld | ats-api | meta | link | heading | regex | url | ai
    confidence: float
    evidence: Optional[str] = None   # short snippet or the attribute it came from
    source_url: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "method": self.method, "confidence": self.confidence,
                "evidence": self.evidence, "source_url": self.source_url}


@dataclass
class PageExtraction:
    """What one input URL produced (including any careers page it led to)."""

    url: str
    final_url: str
    outcome: str
    records: List[Dict[str, FieldValue]] = field(default_factory=list)
    pages: List[Dict[str, Any]] = field(default_factory=list)   # every page fetched: url, outcome, http status
    problems: List[str] = field(default_factory=list)
    ai_used: bool = False
    fetched_at: Optional[datetime] = None
    page_text: str = ""

    @property
    def method(self) -> str:
        methods = sorted({fv.method for record in self.records for fv in record.values()})
        return "+".join(methods)[:60] or "none"
