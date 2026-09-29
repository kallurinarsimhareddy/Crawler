"""Checking records against their schema, and input URL validation.

Validation never "fixes" meaning: a value that does not fit its field (type,
enum, max length, pattern) is set to ``None``, its field status becomes
``invalid``, and the rejected value is kept with its evidence and the error —
nothing is filled in instead. A missing required field is reported.

Field statuses: ``valid`` · ``invalid`` · ``missing`` · ``inferred`` (derived or
AI-supplied) · ``conflict`` (another source disagreed; both values are kept).

Filters: ``within_days`` and ``contains_any`` are *hard* (a row that fails, or
whose value is unknown, is dropped and counted); ``mode: "soft"`` filters only
annotate each row with pass / fail / unknown.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from cloud.intel.core.normalize import normalize_email
from cloud.intel.scraper.schemas import canonical_type

__all__ = ["apply_filters", "check_input_url", "check_value", "valid_date", "valid_url", "validate_fields",
           "validate_record"]

_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$", re.I)
_DEFAULT_MAX = {"string": 4000, "url": 2048, "email": 320, "phone": 32}


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


def check_value(spec: Mapping[str, Any], value: Any) -> Tuple[Any, Optional[str]]:
    """``(clean, None)`` or ``(None, error)`` for one value of one field."""
    kind = canonical_type(spec.get("type"))
    name = spec.get("name", "value")
    if kind == "url":
        clean: Any = valid_url(value)
    elif kind == "email":
        clean = normalize_email(value)
    elif kind == "phone":
        digits = re.sub(r"\D", "", str(value))
        clean = str(value).strip() if 7 <= len(digits) <= 15 and re.fullmatch(r"[+\d\s().\-x]+", str(value).strip()) \
            else None
    elif kind == "date":
        clean = valid_date(value)
    elif kind == "datetime":
        try:
            clean = datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat() if value else None
        except ValueError:
            clean = None
    elif kind == "integer":
        if isinstance(value, bool):
            clean = None
        elif isinstance(value, int):
            clean = value
        else:
            try:
                number = float(re.sub(r"[,\s]", "", str(value)))
                clean = int(number) if number.is_integer() else None
            except ValueError:
                clean = None
    elif kind == "decimal":
        try:
            clean = None if isinstance(value, bool) else float(re.sub(r"[,$€£\s]", "", str(value)))
        except ValueError:
            clean = None
    elif kind == "boolean":
        clean = value if isinstance(value, bool) else None
    elif kind == "enum":
        allowed = spec.get("enum") or []
        clean = value if (not allowed or value in allowed) else None
        if clean is None:
            return None, f"{name}: {str(value)[:80]!r} is not one of {', '.join(map(str, allowed))[:200]}"
    elif kind == "array":
        items = value if isinstance(value, list) else [value]
        clean = [item if isinstance(item, (dict, int, float)) else re.sub(r"\s+", " ", str(item)).strip()[:500]
                 for item in items if str(item).strip()][:100] or None
    elif kind == "object":
        clean = value if isinstance(value, dict) else None
    else:
        clean = re.sub(r"\s+", " ", str(value)).strip() or None
    if clean is None:
        return None, f"{name}: {str(value)[:80]!r} is not a valid {kind}"
    limit = spec.get("max_length") or _DEFAULT_MAX.get(kind)
    if limit and isinstance(clean, str) and len(clean) > int(limit):
        if kind == "string" and not spec.get("max_length"):
            clean = clean[: int(limit)]   # the default cap trims long page text; an explicit one rejects
        else:
            return None, f"{name}: longer than {limit} characters"
    pattern = spec.get("pattern")
    if pattern and isinstance(clean, str):
        try:
            if not re.fullmatch(pattern, clean):
                return None, f"{name}: {clean[:80]!r} does not match {pattern}"
        except re.error:
            return None, f"{name}: invalid pattern in the schema"
    return clean, None


def validate_fields(record: Mapping[str, Any], fields: Sequence[Mapping[str, Any]], *,
                    methods: Optional[Mapping[str, str]] = None, conflicts: Iterable[str] = ()
                    ) -> Tuple[Dict[str, Any], Dict[str, str], Dict[str, str]]:
    """Returns ``(clean, statuses, errors)``: cleaned values, a status per field, and an
    error per invalid or missing-required field."""
    clean: Dict[str, Any] = {}
    statuses: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    conflicted = set(conflicts)
    for spec in fields:
        name = spec["name"]
        value = record.get(name)
        if value in (None, "", []):
            clean[name] = None
            statuses[name] = "missing"
            if spec.get("required"):
                errors[name] = f"{name}: required but not found"
            continue
        checked, error = check_value(spec, value)
        clean[name] = checked
        if error:
            statuses[name] = "invalid"
            errors[name] = error
        elif name in conflicted:
            statuses[name] = "conflict"
        elif (methods or {}).get(name) in ("ai", "derived"):
            statuses[name] = "inferred"
        else:
            statuses[name] = "valid"
    return clean, statuses, errors


def validate_record(record: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]
                    ) -> Tuple[Dict[str, Any], List[str]]:
    """Type-check every requested field. Returns ``(clean, problems)``; missing values are ``None``."""
    clean, _statuses, errors = validate_fields(record, fields)
    problems = [e if "required" in e else e + "; left empty" for e in errors.values()]
    return clean, problems


def _passes(record: Mapping[str, Any], rule: Mapping[str, Any], now: datetime) -> Optional[bool]:
    """True / False, or ``None`` when the record does not state the value."""
    value = record.get(rule.get("field"))
    op = rule.get("op")
    if op == "within_days":
        day = valid_date(value)
        if day is None:
            return None
        return date.fromisoformat(day) >= (now - timedelta(days=int(rule["value"]))).date()
    if op == "contains_any":
        if value in (None, "", []):
            return None
        text = " ".join(map(str, value)) if isinstance(value, list) else str(value)
        return any(re.search(r"(?<![A-Za-z0-9])" + re.escape(str(k)) + r"(?![A-Za-z0-9])", text, re.I)
                   for k in rule.get("value") or [])
    if op == "equals":
        if value in (None, ""):
            return None
        return str(value).strip().lower() == str(rule.get("value")).strip().lower()
    return True


def apply_filters(records: Iterable[Dict[str, Any]], filters: Sequence[Mapping[str, Any]], *,
                  now: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], int]:
    """Keep records passing every hard filter. Returns ``(kept, dropped)``.

    A hard filter drops a row whose value is unknown too: an undated posting cannot
    be shown to be recent. Soft filters annotate ``_filters`` and never drop.
    """
    now = now or datetime.now(timezone.utc)
    kept, dropped = [], 0
    for record in records:
        ok = True
        notes: Dict[str, str] = {}
        for rule in filters:
            result = _passes(record, rule, now)
            label = f"{rule.get('field')} {rule.get('op')}"
            if rule.get("mode", "hard") == "soft":
                notes[label] = "unknown" if result is None else "pass" if result else "fail"
            elif not result:
                ok = False
        if notes:
            record["_filters"] = notes
        if ok:
            kept.append(record)
        else:
            dropped += 1
    return kept, dropped
