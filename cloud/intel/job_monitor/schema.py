"""The 14 mandatory job fields, their normalisation and the stable content hash.

Every job record carries all 14 fields. A value the source does not show stays
``None`` — nothing is guessed: no salary, location, experience, keywords, company
or URL is ever invented. Keyword 1-5 are the first five distinct skills the
source itself shows; fewer than five leaves the rest empty.

The content hash covers the *meaningful* fields only (title, company, location,
experience, salary, keywords, remote). The Job URL is the identity, and Source /
Scraped Date are bookkeeping, so re-scraping an unchanged job on a later day does
not count as a change.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from cloud.intel.jobs.service import canonical_job_url

__all__ = ["JOB_FIELDS", "FIELD_COLUMNS", "HASH_COLUMNS", "KEYWORD_COLUMNS", "clean_text", "keywords_of",
           "remote_of", "parse_date", "content_hash", "job_url_key", "normalize_job", "to_row", "field_values"]

#: The mandatory schema, in the exact column order used by imports and exports.
JOB_FIELDS: Tuple[str, ...] = ("Job URL", "Job Title", "Company Name", "Location", "Experience Level",
                               "Salary Budget", "Keyword 1", "Keyword 2", "Keyword 3", "Keyword 4", "Keyword 5",
                               "Remote", "Source", "Scraped Date")
KEYWORD_COLUMNS: Tuple[str, ...] = ("keyword_1", "keyword_2", "keyword_3", "keyword_4", "keyword_5")
#: field label -> job_postings column
FIELD_COLUMNS: Dict[str, str] = {
    "Job URL": "job_url", "Job Title": "title", "Company Name": "company_name", "Location": "location",
    "Experience Level": "experience_level", "Salary Budget": "salary_budget",
    **{f"Keyword {i}": f"keyword_{i}" for i in range(1, 6)},
    "Remote": "remote", "Source": "source", "Scraped Date": "scraped_date",
}
#: Columns whose change makes a posting CHANGED.
HASH_COLUMNS: Tuple[str, ...] = ("title", "company_name", "location", "experience_level", "salary_budget",
                                 *KEYWORD_COLUMNS, "remote")

_LIMITS = {"job_url": 2048, "title": 500, "company_name": 300, "location": 500, "experience_level": 100,
           "salary_budget": 200, "remote": 40, "source": 200, **{k: 200 for k in KEYWORD_COLUMNS}}
_BLANKS = {"", "-", "--", "n/a", "na", "none", "null", "nan", "undefined"}


def clean_text(value: Any, limit: int = 500) -> Optional[str]:
    """Collapse whitespace, normalise Unicode (NFC), drop replacement characters; blank -> None."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN from a spreadsheet
        return None
    text = unicodedata.normalize("NFC", str(value)).replace("�", "")
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in _BLANKS:
        return None
    return text[:limit] or None


def keywords_of(values: Iterable[Any], limit: int = 5) -> List[Optional[str]]:
    """The first ``limit`` distinct keywords in the order the source shows them, padded with None."""
    seen, ordered = set(), []
    for value in values:
        text = clean_text(value, 200)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            ordered.append(text)
        if len(ordered) == limit:
            break
    return ordered + [None] * (limit - len(ordered))


_REMOTE_TRUE = {"remote", "yes", "y", "true", "1", "fully remote", "100% remote", "remote only", "work from home",
                "wfh", "remote-first", "remote first"}
_ONSITE = {"no", "n", "false", "0", "onsite", "on-site", "on site", "office", "in office", "in-office"}


def remote_of(value: Any) -> Optional[str]:
    """``Remote`` / ``Hybrid`` / ``On-site`` when the source says so; anything else is kept as
    written (trimmed). Blank stays None: an absent badge means *not stated*, not *not remote*."""
    text = clean_text(value, 40)
    if text is None:
        return None
    if isinstance(value, bool):
        return "Remote" if value else "On-site"
    low = text.casefold()
    if low in _REMOTE_TRUE:
        return "Remote"
    if low in _ONSITE:
        return "On-site"
    if low in ("hybrid", "partially remote", "partly remote"):
        return "Hybrid"
    return text


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%d-%m-%Y", "%b %d, %Y",
                 "%d %b %Y", "%B %d, %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parse_date(value: Any) -> Optional[date]:
    """A calendar date from a date, datetime, ISO/US/EU string or Excel serial; else None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 < value < 80000:  # Excel serial day number (1954-2119)
            from datetime import timedelta

            return date(1899, 12, 30) + timedelta(days=int(value))
        return None
    text = clean_text(value, 40)
    if not text:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", text):  # an Excel serial that arrived as text
        return parse_date(float(text))
    text = re.sub(r"(\.\d+)?(Z|[+-]\d{2}:?\d{2})$", "", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _hash_value(value: Any) -> str:
    text = clean_text(value, 2000) or ""
    return text.casefold()


def content_hash(values: Mapping[str, Any]) -> str:
    """SHA-256 over the meaningful fields, case- and whitespace-insensitive."""
    payload = json.dumps([_hash_value(values.get(c)) for c in HASH_COLUMNS], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def job_url_key(url: Any) -> Optional[str]:
    """The dedupe identity of a Job URL: a site profile's stable key when one knows the host
    (e.g. WeAreDevelopers' numeric job id), else the canonical URL."""
    from cloud.intel.job_monitor.profiles import profile_for_url

    text = clean_text(url, 2048)
    if not text:
        return None
    base = canonical_job_url(text)
    if base is None:
        return None
    profile = profile_for_url(text)
    if profile is not None:
        return profile.canonical_key(base) or base
    return base


def normalize_job(raw: Mapping[str, Any], *, source: Optional[str] = None,
                  scraped_date: Optional[date] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """``raw`` uses column names (``job_url``, ``title``…, ``keywords`` list allowed).

    Returns ``(values, problems)``; ``values`` is None when the record cannot be stored
    (no usable Job URL or no title). ``source`` / ``scraped_date`` fill those fields only
    when the record has none of its own."""
    problems: List[str] = []
    url = clean_text(raw.get("job_url"), 2048)
    key = job_url_key(url) if url else None
    title = clean_text(raw.get("title"), _LIMITS["title"])
    if not key:
        problems.append("missing or invalid Job URL" if not url else f"Job URL is not an http(s) link: {url[:80]}")
    if not title:
        problems.append("missing Job Title")
    if problems:
        return None, problems
    if raw.get("keywords") is not None:
        kws = keywords_of(raw.get("keywords") or [])
    else:
        kws = keywords_of(raw.get(k) for k in KEYWORD_COLUMNS)
    raw_date = raw.get("scraped_date")
    parsed = parse_date(raw_date)
    if raw_date not in (None, "") and parsed is None:
        problems.append(f"unreadable Scraped Date {str(raw_date)[:40]!r} (left blank)")
    values: Dict[str, Any] = {
        "job_url": url,
        "url_key": key,
        "title": title,
        "company_name": clean_text(raw.get("company_name"), _LIMITS["company_name"]),
        "location": clean_text(raw.get("location"), _LIMITS["location"]),
        "experience_level": clean_text(raw.get("experience_level"), _LIMITS["experience_level"]),
        "salary_budget": clean_text(raw.get("salary_budget"), _LIMITS["salary_budget"]),
        **dict(zip(KEYWORD_COLUMNS, kws)),
        "remote": remote_of(raw.get("remote")),
        "source": clean_text(raw.get("source"), _LIMITS["source"]) or clean_text(source, _LIMITS["source"]),
        "scraped_date": parsed or (scraped_date if raw_date in (None, "") else None),
    }
    values["content_hash"] = content_hash(values)
    # Context kept with the job but outside the 14 fields and the hash (a reworded
    # description is not a CHANGED job): used by relevance scoring and the detail page.
    for column, limit in (("description", 50000), ("search_term", 300), ("source_board", 100)):
        text = clean_text(raw.get(column), limit)
        if text:
            values[column] = text
    listed = parse_date(raw.get("listing_date"))
    if listed:
        values["listing_date"] = listed
    posted = parse_date(raw.get("date_posted"))
    if posted:
        values["posted_at"] = datetime.combine(posted, time(0, 0), tzinfo=timezone.utc)
    return values, problems


def field_values(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A job row as the 14 labelled fields (for exports, the API and notifications)."""
    out: Dict[str, Any] = {}
    for label, column in FIELD_COLUMNS.items():
        value = row.get(column)
        out[label] = value.isoformat() if isinstance(value, (date, datetime)) else value
    return out


def to_row(values: Mapping[str, Any]) -> List[Any]:
    """The 14 fields of ``values`` in :data:`JOB_FIELDS` order (an export row)."""
    labelled = field_values(values)
    return [labelled[label] for label in JOB_FIELDS]
