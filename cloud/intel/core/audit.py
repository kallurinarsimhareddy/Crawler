"""Audit trail and provenance: the two records every mutation leaves behind.

* :func:`audit` writes one ``audit_log`` row: who did what to which record.
  It never raises — an audit failure is logged, not allowed to undo the work
  (the work is already committed) — but tests assert rows exist.
* :func:`provenance` writes one ``source_records`` row: where a value came from,
  the original values, the normalised values, and when it was observed.
"""

from __future__ import annotations

import contextvars
import logging
import re
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.store.base import Store

__all__ = ["audit", "provenance", "record_activity", "redact", "actor_label", "set_actor_label"]

log = logging.getLogger(__name__)

#: Who is acting, for display (an email). Set per request by the API layer; audit
#: rows carry it so the log is readable without a user directory lookup.
_ACTOR_LABEL: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("audit_actor_label", default=None)

#: Keys whose values never reach the audit log, at any depth.
_SECRET_KEY = re.compile(r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|credential|"
                         r"private[_-]?key|cookie|session[_-]?id|ciphertext|signature|smtp_pass)", re.I)
REDACTED = "[redacted]"


def set_actor_label(label: Optional[str]) -> contextvars.Token:
    return _ACTOR_LABEL.set((label or None) and str(label)[:320])


def actor_label() -> Optional[str]:
    return _ACTOR_LABEL.get()


def redact(value: Any) -> Any:
    """Replace the value of every secret-looking key (recursively) with ``[redacted]``.
    Keys that only *name* fields (``fields``, ``secret_hint``) stay readable."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            if name in ("fields", "secret_hint", "hint") or not _SECRET_KEY.search(name):
                out[name] = redact(item)
            else:
                out[name] = REDACTED if item not in (None, "") else item
        return out
    if isinstance(value, (list, tuple, set)):
        return [redact(v) for v in value]
    if isinstance(value, str) and re.match(r"(?i)^bearer\s+\S+", value):
        return REDACTED
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def audit(store: Store, ctx: Ctx, action: str, *, entity_type: Optional[str] = None,
          entity_id: Optional[str] = None, summary: Optional[str] = None,
          changes: Optional[Mapping[str, Any]] = None) -> None:
    try:
        store.insert(ctx, "audit_log", {
            "actor_id": ctx.user_id,
            "actor_kind": ctx.actor_kind if ctx.actor_kind in ("user", "system", "agent", "workflow") else "system",
            "action": action[:100],
            "entity_type": entity_type,
            "entity_id": entity_id,
            "summary": (summary or "")[:1000] or None,
            "changes": redact(_jsonable(dict(changes or {}))),
            "request_id": ctx.request_id,
            "actor_label": getattr(ctx, "actor_label", None) or actor_label()
                           or (None if ctx.actor_kind == "user" else ctx.actor_kind),
        })
    except Exception:  # noqa: BLE001 - the audited work is already committed
        log.exception("could not write audit row for %s %s", action, entity_id)


def provenance(store: Store, ctx: Ctx, entity_type: str, entity_id: str, *, source_kind: str,
               source_name: str, original: Optional[Mapping[str, Any]] = None,
               normalized: Optional[Mapping[str, Any]] = None, source_ref: Optional[str] = None,
               import_batch_id: Optional[str] = None, import_file_id: Optional[str] = None,
               row_number: Optional[int] = None, confidence: Optional[float] = None,
               match_rule: Optional[str] = None, observed_at: Optional[datetime] = None) -> Dict[str, Any]:
    return store.insert(ctx, "source_records", {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_kind": source_kind,
        "source_name": source_name[:200],
        "source_ref": (source_ref or None) and source_ref[:2048],
        "import_batch_id": import_batch_id,
        "import_file_id": import_file_id,
        "row_number": row_number,
        "original": _jsonable(dict(original or {})),
        "normalized": _jsonable(dict(normalized or {})),
        "observed_at": observed_at or utcnow(),
        "confidence": confidence,
        "match_rule": match_rule,
    })


def record_activity(store: Store, ctx: Ctx, kind: str, summary: str, **links: Any) -> Dict[str, Any]:
    """An entry on a record's activity timeline (``company_id``, ``contact_id``…)."""
    data = links.pop("data", None) or {}
    return store.insert(ctx, "activities", {
        "kind": kind, "summary": summary[:1000], "occurred_at": links.pop("occurred_at", None) or utcnow(),
        "actor_id": ctx.user_id, "data": _jsonable(data), **links,
    })
