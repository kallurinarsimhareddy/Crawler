"""Normalising extracted values without changing what they mean.

Rules (a field's ``normalize`` names one; otherwise its type decides):

=============  ==============================================================
``website``    ``https://www.example.com/about`` -> ``https://example.com``
``url``        lower-case host, no fragment, no tracking parameters, no trailing slash
``domain``     registrable domain (``shop.example.co.uk`` -> ``example.co.uk``)
``email``      lower-case, validated shape
``phone``      E.164: ``(918) 555-0142`` -> ``+19185550142`` (US numbers without a
               country code are assumed to be +1; other formats keep their ``+CC``)
``iso_date``   ``Sep 3, 2026`` / ``Posted 2 days ago`` -> ``2026-09-03``; open-ended
               ("30+ days ago") stays empty
``location``   whitespace, and ``United States``/``USA`` -> ``US``
``integer``    ``1,200`` / ``1.2k`` / ``501-1,000`` (lower bound) -> ``1200`` / ``1200`` / ``501``
``decimal``    ``$1.2B`` -> ``1200000000.0``
=============  ==============================================================

A few fields are *derived* from others when requested and missing — ``domain``
from ``website``, ``remote_mode`` from a location/title that says Remote/Hybrid/
On-site, ``seniority`` and ``job_family`` from the job title — marked with method
``derived`` (status ``inferred``) and the evidence they came from.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cloud.intel.core.normalize import domain_of, normalize_email
from cloud.intel.scraper.models import FieldValue
from cloud.intel.scraper.schemas import canonical_type

__all__ = ["canonical_url", "job_family_of", "normalize_date", "normalize_phone", "normalize_record",
           "normalize_value", "normalize_website", "remote_mode_of", "seniority_of"]

_TRACKING = re.compile(r"^(?:utm_|gh_src$|gclid$|fbclid$|mc_|_hs|ref$|source$|src$|trk$|lever-source)", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                        "dec"], start=1)}
_EMPLOYMENT = {"full_time": "Full-time", "fulltime": "Full-time", "full time": "Full-time", "full-time": "Full-time",
               "part_time": "Part-time", "parttime": "Part-time", "part time": "Part-time", "part-time": "Part-time",
               "contractor": "Contract", "contract": "Contract", "temporary": "Temporary", "temp": "Temporary",
               "intern": "Internship", "internship": "Internship", "per_diem": "Per diem", "volunteer": "Volunteer",
               "seasonal": "Seasonal", "other": "Other"}
_COUNTRY_NAMES = re.compile(r"\b(?:United States of America|United States|U\.S\.A\.?|USA)\b", re.I)
_SENIORITY_RULES = (
    ("Intern", r"\bintern(?:ship)?\b|\bco-?op\b"),
    ("Executive", r"\b(?:chief|ceo|cfo|cto|coo|cio|cmo|president|founder)\b"),
    ("VP", r"\b(?:vp|vice president|svp|evp)\b"),
    ("Director", r"\bdirector\b|\bhead of\b"),
    ("Manager", r"\bmanager\b"),
    ("Lead", r"\b(?:lead|principal|staff|architect)\b"),
    ("Senior", r"\b(?:senior|sr\.?|iii|iv)\b"),
    ("Entry", r"\b(?:junior|jr\.?|entry[- ]level|graduate|associate|trainee)\b"),
)
_FAMILY_RULES = (
    ("Data", r"\b(?:data|analytics|machine learning|ml|ai research|scientist|bi)\b"),
    ("Engineering", r"\b(?:engineer|developer|software|devops|sre|programmer|firmware)\b"),
    ("IT", r"\b(?:it|systems administrator|sysadmin|help ?desk|network|erp|sap|oracle|infrastructure)\b"),
    ("Sales", r"\b(?:sales|account executive|business development|account manager|sdr|bdr)\b"),
    ("Marketing", r"\b(?:marketing|brand|content|seo|growth|communications)\b"),
    ("Finance", r"\b(?:finance|accountant|accounting|controller|fp&a|treasury|tax|audit)\b"),
    ("HR", r"\b(?:hr|human resources|recruiter|talent|people partner|people operations)\b"),
    ("Legal", r"\b(?:legal|counsel|attorney|paralegal|compliance)\b"),
    ("Design", r"\b(?:designer|ux|ui|design)\b"),
    ("Product", r"\b(?:product manager|product owner|product)\b"),
    ("Customer Support", r"\b(?:support|customer success|customer service)\b"),
    ("Operations", r"\b(?:operations|logistics|supply chain|warehouse|procurement|buyer)\b"),
    ("Manufacturing", r"\b(?:machinist|technician|production|manufacturing|assembler|welder|maintenance)\b"),
)


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


def normalize_website(value: Any) -> Optional[str]:
    """The site's origin, ``https``-first, without ``www.``: ``https://www.example.com/`` -> ``https://example.com``."""
    text = str(value or "").strip()
    if not text:
        return None
    url = canonical_url(text if "://" in text else "https://" + text)
    if not url:
        return None
    parts = urlsplit(url)
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    return f"{parts.scheme}://{host}"


def normalize_phone(value: Any) -> Optional[str]:
    """E.164 where the number allows it."""
    text = str(value or "").strip()
    if not text:
        return None
    text = re.split(r"\s*(?:x|ext\.?|extension)\s*\d+$", text, flags=re.I)[0]
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if text.startswith("00") and 10 <= len(digits) <= 17:
        return "+" + digits[2:]
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


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


def normalize_datetime(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value.tzinfo else value.isoformat()
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        day = normalize_date(text)
        return f"{day}T00:00:00" if day else None


def _safe(year: int, month: int, day: int) -> Optional[str]:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(k|m|mm|b|bn|thousand|million|billion)?\b", text)
    if not match:
        return None
    number = float(match.group(1))
    scale = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9,
             "billion": 1e9}.get(match.group(2) or "", 1)
    return number * scale


def remote_mode_of(*texts: Any) -> Optional[str]:
    joined = " ".join(str(t) for t in texts if t).lower()
    if re.search(r"\bhybrid\b", joined):
        return "Hybrid"
    if re.search(r"\b(?:remote|work from home|wfh|telecommute|anywhere)\b", joined):
        return "Remote"
    if re.search(r"\b(?:on-?site|in[- ]office|in person)\b", joined):
        return "On-site"
    return None


def seniority_of(title: Any) -> Optional[str]:
    text = str(title or "").lower()
    for level, pattern in _SENIORITY_RULES:
        if re.search(pattern, text):
            return level
    return None


def job_family_of(title: Any) -> Optional[str]:
    text = str(title or "").lower()
    for family, pattern in _FAMILY_RULES:
        if re.search(pattern, text):
            return family
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


def _enum(value: Any, allowed: Optional[List[str]]) -> Any:
    if not allowed:
        return _text(value)
    text = str(value).strip().lower()
    synonyms = {"onsite": "on-site", "on site": "on-site", "in office": "on-site", "in-office": "on-site",
                "work from home": "remote", "telecommute": "remote", "wfh": "remote"}
    text = synonyms.get(text, text)
    for option in allowed:
        if option.lower() == text:
            return option
    return value   # the validator reports it


def normalize_value(name: str, kind: str, value: Any, *, now: Optional[datetime] = None,
                    rule: Optional[str] = None, enum: Optional[List[str]] = None) -> Any:
    kind = canonical_type(kind)
    if value in (None, "", []):
        return None
    rule = rule or {"url": "url", "email": "email", "phone": "phone", "date": "iso_date",
                    "datetime": "iso_datetime", "integer": "integer", "decimal": "decimal"}.get(kind)
    if name == "website" or rule == "website":
        return normalize_website(value) or value
    if kind == "array":
        items = value if isinstance(value, list) else re.split(r"\s*[;|,]\s*", str(value))
        out: List[Any] = []
        for item in items:
            text = _text(str(item)) if not isinstance(item, dict) else item
            if isinstance(text, str) and text.startswith("http"):
                text = canonical_url(text) or text
            if text and text not in out:
                out.append(text)
        return out or None
    if kind == "object":
        return value if isinstance(value, dict) else value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        return {"true": True, "yes": True, "y": True, "1": True, "false": False, "no": False, "n": False,
                "0": False}.get(str(value).strip().lower(), value)
    if kind == "enum":
        return _enum(value, enum)
    if rule == "url":
        return canonical_url(value) or value
    if rule == "domain" or name == "domain":
        return domain_of(value) or value
    if rule == "email":
        return normalize_email(value) or value
    if rule == "phone":
        return normalize_phone(value) or value
    if rule == "iso_date":
        return normalize_date(value, now=now) or value
    if rule == "iso_datetime":
        return normalize_datetime(value) or value
    if rule == "integer":
        number = _number(value)
        return int(number) if number is not None else value
    if rule == "decimal":
        number = _number(value)
        return number if number is not None else value
    if name == "employment_type":
        return _employment(value)
    if rule == "company_name" or name == "company_name":
        text = _text(str(value))
        return text.strip(" |-–—:·") if text else None
    if rule == "location":
        text = _text(str(value))
        return _COUNTRY_NAMES.sub("US", text) if text else None
    if rule == "lower":
        return _text(str(value)).lower()
    if rule == "upper":
        return _text(str(value)).upper()
    return _text(value) if isinstance(value, str) else value


def _derived(value: Any, origin: FieldValue, evidence: str) -> FieldValue:
    return FieldValue(value, "derived", 0.7, evidence[:200], origin.source_url, origin.browser)


def normalize_record(record: Mapping[str, FieldValue], fields: List[Mapping[str, Any]], *,
                     now: Optional[datetime] = None) -> Dict[str, FieldValue]:
    """Normalised copy of ``record``, with derived fields added where requested."""
    specs = {f["name"]: f for f in fields}
    out: Dict[str, FieldValue] = {}
    for name, fv in record.items():
        spec = specs.get(name, {})
        value = normalize_value(name, spec.get("type", "string"), fv.value, now=now, rule=spec.get("normalize"),
                                enum=spec.get("enum"))
        if value in (None, "", []):
            continue
        alternatives = [FieldValue(normalize_value(name, spec.get("type", "string"), a.value, now=now,
                                                   rule=spec.get("normalize"), enum=spec.get("enum")) or a.value,
                                   a.method, a.confidence, a.evidence, a.source_url, a.browser)
                        for a in fv.alternatives]
        out[name] = FieldValue(value, fv.method, fv.confidence, fv.evidence, fv.source_url, fv.browser, alternatives)
    if "domain" in specs and "domain" not in out:
        site = out.get("website") or record.get("website")
        base = site.value if site else None
        if base is None:
            source = next((fv for fv in record.values() if fv.source_url), None)
            base = source.source_url if source else None
        domain = domain_of(base) if base else None
        if domain and not _is_ats_host(domain):
            out["domain"] = FieldValue(domain, "derived", 0.8 if site else 0.6, f"from {base}"[:200],
                                       site.source_url if site else base)
    title = record.get("job_title")
    if "remote_mode" in specs and "remote_mode" not in out:
        present = [record[n] for n in ("location", "job_title", "employment_type") if n in record]
        mode = remote_mode_of(*[fv.value for fv in present])
        if mode:
            out["remote_mode"] = _derived(mode, present[0], "from " + ", ".join(str(fv.value)[:60] for fv in present))
    if "seniority" in specs and "seniority" not in out and title is not None:
        level = seniority_of(title.value)
        if level:
            out["seniority"] = _derived(level, title, f"from the title {str(title.value)[:120]!r}")
    if "job_family" in specs and "job_family" not in out and title is not None:
        family = job_family_of(title.value)
        if family:
            out["job_family"] = _derived(family, title, f"from the title {str(title.value)[:120]!r}")
    return out


def _is_ats_host(domain: str) -> bool:
    from cloud.intel.vendor import ats_detect

    return ats_detect.detect(f"https://{domain}/x") is not None or domain in {
        "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com", "workable.com",
        "icims.com", "bamboohr.com", "recruitee.com", "breezy.hr", "jobvite.com", "applytojob.com"}
