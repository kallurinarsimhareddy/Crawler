"""Checking records against their schema, and URL input validation.

Validation never "fixes" meaning: a value that does not fit its declared type is
set to ``None`` and reported as a problem, a missing required field is reported
(and a job row without a title is dropped — it is not a job), and nothing is
filled in.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from cloud.intel.core.normalize import normalize_email

__all__ = ["apply_filters", "check_input_url", "validate_record", "valid_date", "valid_url"]

_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$", re.I)


def valid_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    parts = urlsplit(text)
    return text if parts.scheme in ("http", "https") and parts.netloc and " " not in text else None


def valid_date(value: Any) -> Optional[str]:
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(value or "").strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def check_input_url(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(url, None)`` for a usable input URL, ``(None, reason)`` otherwise.

    Adds ``https://`` to bare domains. Refuses other protocols, spaces, hosts that
    are not domain names or IP literals, and credentials. The SSRF check (private
    addresses) runs after this, in the service, and again on every fetch.
    """
    text = str(raw or "").strip().strip("<>\"'")
    if not text:
        return None, "empty"
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):(?!\d)", text, re.I)   # "host:8080" is not a scheme
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        return None, f"unsupported protocol {scheme.group(1).lower()}:"
    if not scheme:
        text = "https://" + text.lstrip("/")
    if re.search(r"\s", text):
        return None, "URL contains spaces"
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if not host:
        return None, "no host"
    if parts.username or parts.password:
        return None, "URLs with credentials are refused"
    if not (_HOST.match(host) or re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", host) or host.startswith("[")):
        return None, f"{host!r} is not a valid host name"
    try:
        parts.port
    except ValueError:
        return None, "invalid port"
    return parts.geturl(), None


def validate_record(record: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]
                    ) -> Tuple[Dict[str, Any], List[str]]:
    """Type-check every requested field. Returns ``(clean, problems)``; missing values are ``None``."""
    clean: Dict[str, Any] = {}
    problems: List[str] = []
    for spec in fields:
        name, kind = spec["name"], spec.get("type", "string")
        value = record.get(name)
        if value in (None, "", []):
            clean[name] = None
            if spec.get("required"):
                problems.append(f"{name}: required but not found")
            continue
        if kind == "url":
            checked: Any = valid_url(value)
        elif kind == "email":
            checked = normalize_email(value)
        elif kind == "date":
            checked = valid_date(value)
        elif kind == "number":
            try:
                checked = float(re.sub(r"[,$€£\s]", "", str(value)))
                checked = int(checked) if checked.is_integer() else checked
            except ValueError:
                checked = None
        elif kind == "boolean":
            checked = value if isinstance(value, bool) else {"true": True, "yes": True, "false": False,
                                                              "no": False}.get(str(value).strip().lower())
        elif kind == "list":
            items = value if isinstance(value, list) else [value]
            checked = [re.sub(r"\s+", " ", str(v)).strip()[:500] for v in items if str(v).strip()][:50] or None
        else:
            checked = re.sub(r"\s+", " ", str(value)).strip()[:4000] or None
        if checked is None:
            problems.append(f"{name}: {str(value)[:80]!r} is not a valid {kind}; left empty")
        clean[name] = checked
    return clean, problems


def apply_filters(records: Iterable[Dict[str, Any]], filters: Sequence[Mapping[str, Any]], *,
                  now: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], int]:
    """Keep records passing every filter. Returns ``(kept, dropped)``.

    ``within_days`` keeps only records whose date is known and recent enough: an
    undated posting cannot be shown to be recent, so it is dropped (and counted).
    """
    now = now or datetime.now(timezone.utc)
    kept, dropped = [], 0
    for record in records:
        ok = True
        for rule in filters:
            if rule.get("op") == "within_days":
                value = valid_date(record.get(rule["field"]))
                if value is None or date.fromisoformat(value) < (now - timedelta(days=int(rule["value"]))).date():
                    ok = False
        if ok:
            kept.append(record)
        else:
            dropped += 1
    return kept, dropped
