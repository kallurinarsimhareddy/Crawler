"""WeAreDevelopers job listings — ported from the standalone Narsimha scraper.

Provenance: ``wearedevelopers_job_scraper/extractors/wearedevelopers.py`` (the
in-browser ``CARD_JS``) and ``generic.py`` (``build_record`` / ``remote_value``),
which collected 321,970 US jobs on 2026-09-04. The same rules, run on server HTML:

* a card is an ``<article>`` holding a ``/jobs/`` link; ``<h3>`` is the title;
* the stacked ``div.truncate`` blocks are company, then the dimmed location
  (a card without a company shows only the dimmed location);
* the outer ``rounded-full`` pills are classified by the site's own colours —
  amber = Remote, secondary = salary, primary = experience, base-200 = skill
  keyword — with a wording fallback for anything the colours do not cover;
* pagination is the "Load more jobs" link in ``turbo-frame#jobs_pagination``.
  Its ``/jobs.turbo_stream?...&page=<cursor>`` target is also served as a normal
  HTML page at ``/jobs?...&page=<cursor>``, so no browser is needed. The cursor
  is a position (date, id), newest first; no link means the listing ended.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from cloud.intel.job_monitor.profiles.base import ListingPage, SiteProfile

__all__ = ["WeAreDevelopersProfile"]

_EXPERIENCE_WORDS = re.compile(r"^(starter|experienced|expert|entry[\s-]?level|junior|mid[\s-]?level|intermediate|"
                               r"senior|lead|principal|intern(ship)?|graduate|student|trainee)$", re.I)
_SALARY_LIKE = re.compile(r"[$€£¥₹]|\b(usd|eur|gbp|chf|pln)\b|\d\s*k\b", re.I)
_REMOTE_BADGE = re.compile(r"^(fully\s+)?remote$|^100%\s*remote$|^remote[\s-]?first$", re.I)
_JOB_ID = re.compile(r"^(/jobs/(?:ext/)?)(\d+)(?:-[^/]*)?$")


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node is not None else ""


def _classes(node: Any) -> str:
    value = node.get("class") or []
    return " ".join(value) if isinstance(value, list) else str(value)


class WeAreDevelopersProfile(SiteProfile):
    name = "wearedevelopers"
    source_name = "WeAreDevelopers"
    hosts = ("wearedevelopers.com",)
    newest_first = True
    # Measured 2026-10-02: the US listing ends ~90 days back (a cursor at today-91 days returns
    # no cards) and every sampled posting older than that answers 410 Gone on its own URL.
    visible_window_days = 90
    gone_statuses = (404, 410)

    @staticmethod
    def cursor_date(url: Optional[str]) -> Optional[date]:
        """The listing date in a ``page=<base64 ["YYYY-MM-DD", id]>`` cursor, or None."""
        if not url:
            return None
        token = (parse_qs(urlsplit(url).query).get("page") or [""])[0]
        if not token:
            return None
        try:
            value = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            return date.fromisoformat(str(value[0]))
        except (ValueError, TypeError, IndexError, json.JSONDecodeError):
            return None

    def canonical_key(self, canonical_url: str) -> Optional[str]:
        """``/jobs/ext/3145489-some-title`` -> ``/jobs/ext/3145489``: the numeric id is the job;
        the slug follows the title and may change."""
        parts = urlsplit(canonical_url)
        match = _JOB_ID.match(parts.path)
        if not match:
            return None
        return urlunsplit((parts.scheme, parts.netloc, match.group(1) + match.group(2), "", ""))

    @staticmethod
    def page_url_for(href: str, base: str) -> str:
        """``/jobs.turbo_stream?country=US&page=C`` -> ``/jobs?country=US&page=C`` (absolute)."""
        absolute = urljoin(base, href)
        parts = urlsplit(absolute)
        path = re.sub(r"\.turbo_stream$", "", parts.path)
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def _card(self, card: Any, page_url: str) -> Optional[Dict[str, Any]]:
        link = card.select_one('a[href*="/jobs/"]') or card.select_one("a[href]")
        href = (link.get("href") or "").strip() if link is not None else ""
        if not href:
            return None
        title_el = card.find("h3") or card.find("h2") or card.find("h4")
        truncs = card.select("div.truncate")
        company = location = None
        dimmed = [d for d in truncs if re.search(r"base-content/|opacity|text-gray|muted", _classes(d))]
        plain = [d for d in truncs if d not in dimmed]
        if plain:
            company = _text(plain[0]) or None
        if dimmed:
            location = _text(dimmed[0]) or None
        elif len(truncs) > 1:
            location = _text(truncs[1]) or None

        pills = [p for p in card.select('span[class*="rounded-full"]')
                 if p.find_parent("span", class_=re.compile("rounded-full")) is None]
        experience = salary = None
        remote = False
        keywords: List[str] = []
        unclassified: List[str] = []
        for pill in pills:
            cls, text = _classes(pill), _text(pill)
            if not text:
                continue
            if re.search(r"amber|remote", cls, re.I) or _REMOTE_BADGE.match(text):
                remote = True
            elif "secondary" in cls and salary is None:
                salary = text
            elif re.search(r"bg-primary|text-primary", cls) and experience is None:
                experience = text
            elif re.search(r"bg-base-200|bg-neutral|bg-gray", cls):
                keywords.append(text)
            else:
                unclassified.append(text)
        for text in unclassified:
            if salary is None and _SALARY_LIKE.search(text):
                salary = text
            elif experience is None and _EXPERIENCE_WORDS.match(text):
                experience = text
            else:
                keywords.append(text)
        return {
            "job_url": urljoin(page_url, href),
            "title": _text(title_el) or None,
            "company_name": company,
            "location": location,
            "experience_level": experience,
            "salary_budget": salary,
            "keywords": keywords,
            "remote": "Remote" if remote else None,
        }

    def parse_listing(self, html: str, page_url: str) -> ListingPage:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "lxml")
        page = ListingPage()
        for card in soup.select("article"):
            if card.select_one('a[href*="/jobs/"]') is None:
                continue
            page.cards += 1
            try:
                record = self._card(card, page_url)
            except Exception as error:  # noqa: BLE001 - one malformed card never loses the page
                page.problems.append(f"a job card could not be read ({type(error).__name__})")
                continue
            if record is None:
                page.problems.append("a job card without a link was skipped")
                continue
            page.records.append(record)
        more = (soup.select_one('turbo-frame#jobs_pagination a[data-turbo-frame="jobs_pagination"]')
                or soup.select_one('a[data-turbo-frame="jobs_pagination"]')
                or soup.select_one('#jobs_pagination a[href*="jobs"]'))
        if more is not None and more.get("href"):
            page.next_url = self.page_url_for(more["href"], page_url)
        # Cards show no date, but the cursors do: the next cursor is the last card's listing
        # date (newest first, so the page's cards are that day or at most a day newer); the
        # last page has no next cursor, so its own cursor bounds it from above.
        listed = self.cursor_date(page.next_url) or self.cursor_date(page_url)
        if listed is not None:
            for record in page.records:
                record["listing_date"] = listed.isoformat()
        return page
