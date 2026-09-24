"""Checking, filtering and de-duplicating extracted records.

Validation never "fixes" meaning: a value that does not fit its declared type
is dropped from the record and reported as a problem, and the original page is
still linked from the result row.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from cloud.intel.core.normalize import normalize_email

__all__ = ["apply_filters", "dedupe_records", "validate_record"]


def _valid_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    parts = urlsplit(text)
    return text if parts.scheme in ("http", "https") and parts.netloc else None


def _valid_date(value: Any) -> Optional[str]:
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value or "").strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def validate_record(record: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    clean: Dict[str, Any] = {}
    problems: List[str] = []
    for spec in fields:
        name, kind = spec["name"], spec.get("type", "string")
        value = record.get(name)
        if value in (None, "", []):
            clean[name] = None
            continue
        if kind == "url":
            checked = _valid_url(value)
        elif kind == "email":
            checked = normalize_email(value)
        elif kind == "date":
            checked = _valid_date(value)
        elif kind == "number":
            try:
                checked = float(str(value).replace(",", ""))
            except ValueError:
                checked = None
        elif kind == "boolean":
            checked = value if isinstance(value, bool) else None
        else:
            checked = re.sub(r"\s+", " ", str(value)).strip()[:4000] or None
        if checked is None:
            problems.append(f"{name}: {str(value)[:80]!r} is not a valid {kind}; dropped")
        clean[name] = checked
    return clean, problems


def apply_filters(records: Iterable[Dict[str, Any]], filters: Sequence[Mapping[str, Any]], *,
                  now: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], int]:
    """Keep records passing every filter. Returns ``(kept, dropped)``.

    ``within_days`` keeps only records whose date is known and recent enough —
    an undated posting cannot be shown to be recent, so it is dropped (and
    counted), never assumed.
    """
    now = now or datetime.now(timezone.utc)
    kept, dropped = [], 0
    for record in records:
        ok = True
        for rule in filters:
            if rule.get("op") == "within_days":
                value = _valid_date(record.get(rule["field"]))
                if value is None or date.fromisoformat(value) < (now - timedelta(days=int(rule["value"]))).date():
                    ok = False
        if ok:
            kept.append(record)
        else:
            dropped += 1
    return kept, dropped


def _key(record: Mapping[str, Any]) -> Optional[Tuple]:
    if record.get("job_url"):
        return ("job_url", str(record["job_url"]).split("#")[0].rstrip("/").lower())
    if record.get("job_title"):
        return ("job", str(record.get("company_name") or "").lower(), str(record["job_title"]).lower(),
                str(record.get("location") or "").lower())
    if record.get("website"):
        host = (urlsplit(str(record["website"])).hostname or "").lower()
        return ("site", host[4:] if host.startswith("www.") else host)
    if record.get("company_name"):
        return ("company", str(record["company_name"]).strip().lower())
    return None


def dedupe_records(records: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    seen, out, dupes = set(), [], 0
    for record in records:
        key = _key(record)
        if key is not None and key in seen:
            dupes += 1
            continue
        if key is not None:
            seen.add(key)
        out.append(record)
    return out, dupes
