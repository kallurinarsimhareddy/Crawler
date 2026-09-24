"""Deterministic extraction from one fetched page, AI only for what is left.

Order of evidence, strongest first, each recorded per field in
``field_sources``:

1. ``json-ld`` — schema.org ``Organization`` / ``JobPosting`` blocks;
2. ``ats-api`` — for a job page behind a known ATS, that ATS's **official public
   job-board API** (Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
   Recruitee) via :func:`cloud.intel.vendor.ats_detect.api_endpoints`;
3. ``meta`` — OpenGraph / description / canonical / ``<title>``;
4. ``link`` — careers links, LinkedIn company links, ``mailto:``/``tel:``;
5. ``regex`` — emails, phones, "Jane Doe, CEO" patterns in visible text;
6. ``ai`` — only for requested fields still empty, only on this page's text,
   only when the workspace allows external AI (the registry enforces it).

A page that blocks us (401/403/429/451/503, robots.txt) is reported as
``blocked`` and left alone: no CAPTCHA solving, no stealth, no retries through
proxies. A headless browser is used only when explicitly enabled
(:class:`BrowserRenderer`), and never to get past a block.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from cloud.intel.vendor import ats_detect

__all__ = ["BrowserRenderer", "PageExtraction", "extract_from_html", "scrape_url"]

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b")
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b|\+\d{1,3}[\s.-]\d{2,4}[\s.-]\d{3,4}[\s.-]\d{3,4}")
_NAME = r"([A-Z][a-zA-Z'’.-]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-zA-Z'’-]+){1,2})"
_CEO_PATTERNS = (
    re.compile(_NAME + r"\s*[,–—|-]\s*(?:Founder\s*(?:&|and)\s*)?(?:CEO|Chief Executive Officer)\b"),
    re.compile(r"\b(?:CEO|Chief Executive Officer)\s*[,:–—|-]?\s*" + _NAME),
)
_CAREERS_WORDS = ("careers", "career", "jobs", "join us", "join our team", "work with us", "open positions",
                  "we're hiring", "we are hiring", "opportunities")
_JOB_HREF = re.compile(r"/(?:job|jobs|careers?|positions?|openings?|vacanc(?:y|ies))/[^/?#]+", re.I)
_PLACEHOLDER_EMAILS = ("example.com", "domain.com", "email.com", "yourcompany", "sentry.io", "wixpress.com")


@dataclass
class PageExtraction:
    url: str
    final_url: str
    status: str  # ok | blocked | error | empty
    method: str = "http"
    records: List[Dict[str, Any]] = field(default_factory=list)
    field_sources: Dict[str, str] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    fetched_at: Optional[datetime] = None
    page_text: str = ""


class BrowserRenderer:
    """Renders a page with JavaScript. Off by default.

    The CareerCloud egress guard (``cloud/worker/egress.py``) only covers
    ``requests``/urllib3 traffic, **not** a browser, so a renderer must only be
    enabled on a host whose firewall enforces the same public-internet-only
    rule (``cloud/deploy/staging/nftables``). It never interacts with CAPTCHAs
    or logins; a challenge page is returned as-is and reported as blocked.
    """

    def render(self, url: str) -> Optional[str]:  # pragma: no cover - interface
        return None


class PlaywrightRenderer(BrowserRenderer):  # pragma: no cover - needs a browser install
    def __init__(self, timeout_ms: int = 30000) -> None:
        from playwright.sync_api import sync_playwright  # noqa: F401 - availability check

        self.timeout_ms = timeout_ms

    def render(self, url: str) -> Optional[str]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent="CareerCrawlerBot/1.0 (+company research)")
                page.goto(url, timeout=self.timeout_ms, wait_until="networkidle")
                return page.content()
            finally:
                browser.close()


def browser_renderer(enabled: bool) -> Optional[BrowserRenderer]:
    if not enabled:
        return None
    try:
        return PlaywrightRenderer()
    except Exception:  # noqa: BLE001 - playwright not installed
        return None


# --- helpers -------------------------------------------------------------------


def _first(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("@id") or value.get("url")
        if isinstance(value, list):
            value = _first(*value)
        if value not in (None, "") and str(value).strip():
            return str(value).strip()
    return None


def _location_of(job: Dict[str, Any]) -> Optional[str]:
    loc = job.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        address = loc.get("address") if isinstance(loc.get("address"), dict) else {}
        parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
        parts = [(_first(p) if isinstance(p, dict) else p) for p in parts]
        text = ", ".join(str(p) for p in parts if p)
        return text or _first(loc.get("name"))
    if job.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return _first(loc)


def _types(obj: Dict[str, Any]) -> List[str]:
    t = obj.get("@type")
    return [str(x) for x in (t if isinstance(t, list) else [t]) if x]


def _visible_text(soup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _good_email(value: str) -> bool:
    lowered = value.lower()
    return not any(p in lowered for p in _PLACEHOLDER_EMAILS) and not lowered.endswith((".png", ".jpg", ".svg"))


def _iso_date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    text = str(value).strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else None


# --- official ATS job-board APIs -------------------------------------------------------


def _ats_jobs(extractor: str, data: Any, company: Optional[str]) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []

    def add(title, url, location=None, posted=None, employment=None):
        if title:
            jobs.append({"job_title": str(title).strip(), "job_url": url, "location": location,
                         "posted_date": _iso_date(posted), "employment_type": employment, "company_name": company})

    if extractor == "greenhouse":
        for j in (data or {}).get("jobs", []):
            add(j.get("title"), j.get("absolute_url"), (j.get("location") or {}).get("name"),
                j.get("first_published") or j.get("updated_at"))
    elif extractor == "lever":
        for j in data or []:
            add(j.get("text"), j.get("hostedUrl"), (j.get("categories") or {}).get("location"), j.get("createdAt"),
                (j.get("categories") or {}).get("commitment"))
    elif extractor == "ashby":
        for j in (data or {}).get("jobs", []):
            add(j.get("title"), j.get("jobUrl"), j.get("location"), j.get("publishedAt"), j.get("employmentType"))
    elif extractor == "smartrecruiters":
        for j in (data or {}).get("content", []):
            loc = j.get("location") or {}
            add(j.get("name"), j.get("ref"), ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                                                   loc.get("country")) if x), j.get("releasedDate"))
    elif extractor == "workable":
        for j in (data or {}).get("jobs", []):
            add(j.get("title"), j.get("url") or j.get("shortlink"),
                ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x),
                j.get("published_on") or j.get("created_at"), j.get("employment_type"))
    elif extractor == "recruitee":
        for j in (data or {}).get("offers", []):
            add(j.get("title"), j.get("careers_url"), j.get("location"), j.get("published_at"),
                j.get("employment_type_code"))
    return jobs


_SUPPORTED_ATS_API = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee"}


def ats_api_jobs(detection: Dict[str, Any], fetcher: Any, company: Optional[str] = None) -> List[Dict[str, Any]]:
    """Jobs from the ATS's official public board API, or ``[]``."""
    jobs: List[Dict[str, Any]] = []
    for method, url, body, extractor in ats_detect.api_endpoints(detection):
        if extractor not in _SUPPORTED_ATS_API or method != "GET":
            continue
        result = fetcher.fetch(url, accept="application/json")
        if not result.ok:
            continue
        try:
            jobs.extend(_ats_jobs(extractor, result.json(), company))
        except ValueError:
            continue
    return jobs


# --- the page -----------------------------------------------------------------------------


def extract_from_html(html: str, url: str, fields: Sequence[str], *, fetcher: Any = None,
                      entity: str = "company") -> PageExtraction:
    """Deterministic extraction. ``fields`` are schema field names."""
    from bs4 import BeautifulSoup

    from cloud.intel.vendor.html import json_ld_objects

    out = PageExtraction(url=url, final_url=url, status="ok")
    soup = BeautifulSoup(html or "", "lxml")
    wanted = set(fields)
    company: Dict[str, Any] = {}
    sources: Dict[str, str] = {}

    def put(name: str, value: Any, source: str) -> None:
        if name in wanted and value not in (None, "", []) and company.get(name) in (None, "", []):
            company[name] = value
            sources[name] = source

    try:
        objects = json_ld_objects(soup)
    except Exception:  # noqa: BLE001 - malformed JSON-LD is common
        objects = []
    ld_jobs: List[Dict[str, Any]] = []
    for obj in objects:
        types = _types(obj)
        if any(t in ("Organization", "Corporation", "LocalBusiness", "EmployerAggregateRating") for t in types):
            put("company_name", _first(obj.get("legalName"), obj.get("name")), "json-ld")
            put("website", _first(obj.get("url")), "json-ld")
            put("description", _first(obj.get("description")), "json-ld")
            put("industry", _first(obj.get("industry")), "json-ld")
            same = obj.get("sameAs") or []
            for link in (same if isinstance(same, list) else [same]):
                if "linkedin.com/company" in str(link):
                    put("linkedin_url", str(link), "json-ld")
            address = obj.get("address")
            if isinstance(address, dict):
                put("location", ", ".join(str(address.get(k)) for k in ("addressLocality", "addressRegion",
                                                                        "addressCountry") if address.get(k)), "json-ld")
            put("email", _first(obj.get("email")), "json-ld")
            put("phone", _first(obj.get("telephone")), "json-ld")
            people = []
            for key in ("employee", "founder", "member"):
                value = obj.get(key)
                people.extend(value if isinstance(value, list) else [value] if value else [])
            for person in people:
                if isinstance(person, dict) and re.search(r"\b(CEO|chief executive)", str(person.get("jobTitle", "")), re.I):
                    put("ceo", _first(person.get("name")), "json-ld")
        if "JobPosting" in types:
            org = obj.get("hiringOrganization") or {}
            ld_jobs.append({
                "job_title": _first(obj.get("title")),
                "company_name": _first(org.get("name") if isinstance(org, dict) else org),
                "location": _location_of(obj),
                "posted_date": _iso_date(obj.get("datePosted")),
                "job_url": _first(obj.get("url")) or url,
                "employment_type": _first(obj.get("employmentType")),
                "description": (BeautifulSoup(str(obj.get("description") or ""), "lxml").get_text(" ")[:2000]
                                if "description" in wanted else None),
            })

    # meta
    def meta(*names: str) -> Optional[str]:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    put("company_name", meta("og:site_name", "application-name"), "meta")
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        parts = urlsplit(urljoin(url, canonical["href"]))
        put("website", f"{parts.scheme}://{parts.netloc}", "meta")
    put("description", meta("og:description", "description", "twitter:description"), "meta")

    # links
    careers_links: List[str] = []
    job_links: List[Dict[str, Any]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        absolute = urljoin(url, href)
        lowered = (text + " " + href).lower()
        if href.lower().startswith("mailto:"):
            address = href[7:].split("?")[0]
            if _EMAIL.fullmatch(address) and _good_email(address):
                put("email", address, "link")
            continue
        if href.lower().startswith("tel:"):
            put("phone", href[4:].strip(), "link")
            continue
        if "linkedin.com/company/" in href:
            put("linkedin_url", absolute.split("?")[0], "link")
        if any(word in lowered for word in _CAREERS_WORDS) and absolute.startswith("http"):
            careers_links.append(absolute)
        if _JOB_HREF.search(absolute) and 3 <= len(text) <= 120 and not any(w == text.lower() for w in _CAREERS_WORDS):
            job_links.append({"job_title": text, "job_url": absolute})
    if careers_links:
        put("careers_url", careers_links[0], "link")

    # ATS
    detection = ats_detect.detect(url)
    if not detection:
        for link in careers_links:
            detection = ats_detect.detect(link)
            if detection:
                break
    if not detection:
        found = ats_detect.find_ats_in_text(html or "", limit=4000)
        detection = found[0] if found else None
    if detection:
        put("ats", detection.get("platform"), "link")
        if "careers_url" in wanted and not company.get("careers_url"):
            put("careers_url", detection.get("url"), "link")

    # visible text
    text = _visible_text(soup)
    title = soup.title.get_text(" ").strip() if soup.title else None
    if "company_name" in wanted and not company.get("company_name") and title:
        put("company_name", re.split(r"\s[|\-–—:]\s", title)[-1].strip() if " | " in title or " - " in title else title,
            "meta")
    if "email" in wanted and not company.get("email"):
        emails = [e for e in _EMAIL.findall(text) if _good_email(e)]
        if emails:
            put("email", emails[0], "regex")
    if "phone" in wanted and not company.get("phone"):
        phones = _PHONE.findall(text)
        if phones:
            put("phone", phones[0].strip(), "regex")
    if "ceo" in wanted and not company.get("ceo"):
        for pattern in _CEO_PATTERNS:
            match = pattern.search(text)
            if match:
                put("ceo", match.group(1).strip(), "regex")
                break
    if "location" in wanted and not company.get("location"):
        pass  # free-text addresses are too ambiguous for a regex; AI may fill it when allowed
    if "title" in wanted:
        put("title", title, "meta")
    if "website" in wanted and not company.get("website"):
        parts = urlsplit(url)
        put("website", f"{parts.scheme}://{parts.netloc}", "link")

    # records
    if entity == "job":
        jobs = [j for j in ld_jobs if j.get("job_title")]
        method = "json-ld"
        if not jobs and detection and fetcher is not None:
            jobs = ats_api_jobs(detection, fetcher, company.get("company_name"))
            method = "ats-api"
        if not jobs:
            jobs = job_links
            method = "link"
        records = []
        for job in jobs:
            record = {name: job.get(name) for name in fields if job.get(name) not in (None, "")}
            for name in fields:
                if name not in record and company.get(name) not in (None, ""):
                    record[name] = company[name]
            records.append(record)
        for name in fields:
            if any(name in r for r in records):
                sources.setdefault(name, method)
        out.records = records
        out.method = method
    else:
        out.records = [{name: company.get(name) for name in fields}]
    out.field_sources = sources
    if not any(any(v not in (None, "", []) for v in r.values()) for r in out.records):
        out.status = "empty"
    out.page_text = text
    return out


def ai_fill(extraction: PageExtraction, schema_fields: Sequence[Dict[str, Any]], ai: Any) -> None:
    """Ask the model for requested fields that are still empty, from this page's text only."""
    from cloud.intel.ai.base import AIError

    if ai is None or not extraction.records or len(extraction.records) != 1:
        return
    record = extraction.records[0]
    missing = [f for f in schema_fields if record.get(f["name"]) in (None, "", [])]
    text = extraction.page_text
    if not missing or not text:
        return
    types = {"number": "number", "boolean": "boolean"}
    schema = {"type": "object", "additionalProperties": False, "required": [f["name"] for f in missing],
              "properties": {f["name"]: {"type": [types.get(f["type"], "string"), "null"],
                                         "description": f["description"]} for f in missing}}
    prompt = ("Extract these fields from the web page text below. Use null when the page does not state the "
              "value; never guess.\n\nFields: " + json.dumps([{k: f[k] for k in ("name", "description")} for f in missing])
              + "\n\nPage URL: " + extraction.final_url + "\n\nPage text:\n" + text[:12000])
    try:
        answer = ai.complete_json("You extract facts from web pages. Only report what the text states.",
                                  prompt, schema, max_tokens=2000)
    except AIError as error:
        extraction.problems.append(f"AI extraction skipped: {error}")
        return
    for f in missing:
        value = answer.get(f["name"])
        if value not in (None, ""):
            record[f["name"]] = value
            extraction.field_sources[f["name"]] = "ai"


def scrape_url(url: str, schema: Dict[str, Any], *, fetcher: Any, ai: Any = None,
               renderer: Optional[BrowserRenderer] = None) -> PageExtraction:
    """Fetch one URL safely and extract the schema's fields."""
    fields = [f["name"] for f in schema["fields"]]
    result = fetcher.fetch(url)
    now = datetime.now(timezone.utc)
    if result.error and result.error.startswith("unsafe target"):
        return PageExtraction(url, result.final_url, "error", problems=[result.error], fetched_at=now)
    if result.blocked:
        reason = result.error or f"HTTP {result.status}"
        return PageExtraction(url, result.final_url, "blocked",
                              problems=[f"blocked ({reason}); not bypassed"], fetched_at=now)
    if not result.ok:
        return PageExtraction(url, result.final_url, "error",
                              problems=[result.error or f"HTTP {result.status}"], fetched_at=now)
    html = result.text
    method = "http"
    extraction = extract_from_html(html, result.final_url, fields, fetcher=fetcher, entity=schema.get("entity", "company"))
    if extraction.status == "empty" and renderer is not None:
        rendered = renderer.render(result.final_url)
        if rendered:
            extraction = extract_from_html(rendered, result.final_url, fields, fetcher=fetcher,
                                           entity=schema.get("entity", "company"))
            method = "browser"
    extraction.url = url
    extraction.final_url = result.final_url
    extraction.fetched_at = now
    extraction.method = f"{method}:{extraction.method}" if extraction.method != "http" else method
    if result.truncated:
        extraction.problems.append("page was larger than the size limit and was cut")
    if schema.get("entity") != "job":
        ai_fill(extraction, schema["fields"], ai)
    if extraction.status == "empty" and any(
            v not in (None, "") for r in extraction.records for v in r.values()):
        extraction.status = "ok"
    return extraction
