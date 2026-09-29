"""Normalising extracted values without changing what they mean.

URLs lose tracking parameters and fragments, websites become their origin,
dates become ISO ``YYYY-MM-DD`` (relative dates such as "Posted 2 days ago" are
resolved against the extraction time; open-ended ones such as "30+ days ago"
are left empty), employment types get one spelling, and a few fields are
*derived* from others (``domain`` from ``website``, ``remote_mode`` from a
location/title that says "Remote"/"Hybrid"/"On-site") — marked with method
``derived`` and the evidence they came from.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cloud.intel.core.normalize import domain_of, normalize_email
from cloud.intel.scraper.models import FieldValue

__all__ = ["canonical_url", "normalize_date", "normalize_record", "remote_mode_of"]

_TRACKING = re.compile(r"^(?:utm_|gh_src$|gclid$|fbclid$|mc_|_hs|ref$|source$|src$|trk$|lever-source)", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                        "dec"], start=1)}
_EMPLOYMENT = {"full_time": "Full-time", "fulltime": "Full-time", "full time": "Full-time", "full-time": "Full-time",
               "part_time": "Part-time", "parttime": "Part-time", "part time": "Part-time", "part-time": "Part-time",
               "contractor": "Contract", "contract": "Contract", "temporary": "Temporary", "temp": "Temporary",
               "intern": "Internship", "internship": "Internship", "per_diem": "Per diem", "volunteer": "Volunteer",
               "seasonal": "Seasonal", "other": "Other"}


def canonical_url(value: Any) -> Optional[str]:
    """Lower-case host, no fragment, no tracking parameters, no trailing slash (except the root)."""
    text = str(value or "").strip()
    if not text:
        return None
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = parts.path if parts.path not in ("", "/") else "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def normalize_date(value: Any, *, now: Optional[datetime] = None) -> Optional[str]:
    """ISO date, or ``None`` when the text does not state one date."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().lower()
    today = (now or datetime.now(timezone.utc)).date()
    match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        return _safe(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if re.search(r"\b(?:today|just now|just posted|hours? ago|minutes? ago)\b", text):
        return today.isoformat()
    if re.search(r"\byesterday\b", text):
        return (today - timedelta(days=1)).isoformat()
    match = re.search(r"\b(\d{1,3})\s*(\+)?\s*(day|week|month)s?\s+ago\b", text)
    if match:
        if match.group(2):   # "30+ days ago" is a lower bound, not a date
            return None
        n = int(match.group(1))
        days = n * {"day": 1, "week": 7, "month": 30}[match.group(3)]
        return (today - timedelta(days=days)).isoformat()
    match = re.search(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text)
    if match and match.group(1) in _MONTHS:
        return _safe(int(match.group(3)), _MONTHS[match.group(1)], int(match.group(2)))
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\.?,?\s+(\d{4})\b", text)
    if match and match.group(2) in _MONTHS:
        return _safe(int(match.group(3)), _MONTHS[match.group(2)], int(match.group(1)))
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if match:   # US order, the common one on US careers pages
        return _safe(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    return None


def _safe(year: int, month: int, day: int) -> Optional[str]:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def remote_mode_of(*texts: Any) -> Optional[str]:
    joined = " ".join(str(t) for t in texts if t).lower()
    if re.search(r"\bhybrid\b", joined):
        return "Hybrid"
    if re.search(r"\b(?:remote|work from home|wfh|telecommute|anywhere)\b", joined):
        return "Remote"
    if re.search(r"\b(?:on-?site|in[- ]office|in person)\b", joined):
        return "On-site"
    return None


def _employment(value: Any) -> Any:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    text = str(value).strip()
    parts = [p.strip() for p in re.split(r"[,/;]", text) if p.strip()]
    mapped = [_EMPLOYMENT.get(p.lower().replace("-", "_") if "_" in p else p.lower(), p) for p in parts]
    return ", ".join(dict.fromkeys(mapped)) or None


def _text(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip() or None
    return value


def normalize_value(name: str, kind: str, value: Any, *, now: Optional[datetime] = None) -> Any:
    if value in (None, "", []):
        return None
    if kind == "list":
        items = value if isinstance(value, list) else [value]
        out: List[str] = []
        for item in items:
            text = _text(str(item))
            if text and text not in out:
                out.append(canonical_url(text) or text if text.startswith("http") else text)
        return out or None
    if name == "website":
        url = canonical_url(value if "://" in str(value) else "https://" + str(value).strip())
        if not url:
            return value   # the validator reports it
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"
    if kind == "url":
        return canonical_url(value) or value
    if kind == "email":
        return normalize_email(value) or value
    if kind == "date":
        return normalize_date(value, now=now) or value
    if name == "employment_type":
        return _employment(value)
    if name == "company_name":
        text = _text(str(value))
        return text.strip(" |-–—:·") if text else None
    return _text(value)


def normalize_record(record: Mapping[str, FieldValue], fields: List[Mapping[str, Any]], *,
                     now: Optional[datetime] = None) -> Dict[str, FieldValue]:
    """Normalised copy of ``record``, with derived fields added where requested."""
    kinds = {f["name"]: f.get("type", "string") for f in fields}
    out: Dict[str, FieldValue] = {}
    for name, fv in record.items():
        value = normalize_value(name, kinds.get(name, "string"), fv.value, now=now)
        if value in (None, "", []):
            continue
        out[name] = FieldValue(value, fv.method, fv.confidence, fv.evidence, fv.source_url)
    if "domain" in kinds and "domain" not in out:
        site = out.get("website") or record.get("website")
        base = site.value if site else None
        if base is None:
            source = next((fv for fv in record.values() if fv.source_url), None)
            base = source.source_url if source else None
        domain = domain_of(base) if base else None
        if domain and not _is_ats_host(domain):
            out["domain"] = FieldValue(domain, "derived", 0.8 if site else 0.6, f"from {base}"[:200],
                                       site.source_url if site else base)
    elif "domain" in out:
        out["domain"] = FieldValue(domain_of(out["domain"].value) or out["domain"].value, out["domain"].method,
                                   out["domain"].confidence, out["domain"].evidence, out["domain"].source_url)
    if "remote_mode" in kinds and "remote_mode" not in out:
        clues = [record[n].value for n in ("location", "job_title", "employment_type") if n in record]
        mode = remote_mode_of(*clues)
        if mode:
            origin = next(record[n] for n in ("location", "job_title", "employment_type") if n in record)
            out["remote_mode"] = FieldValue(mode, "derived", 0.7, f"from {', '.join(str(c)[:60] for c in clues)}",
                                            origin.source_url)
    return out


def _is_ats_host(domain: str) -> bool:
    from cloud.intel.vendor import ats_detect

    return ats_detect.detect(f"https://{domain}/x") is not None or domain in {
        "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com", "workable.com",
        "icims.com", "bamboohr.com", "recruitee.com", "breezy.hr", "jobvite.com", "applytojob.com"}
