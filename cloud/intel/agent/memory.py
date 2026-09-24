"""Workspace-scoped AI memory: preferences the agent applies to every request.

    "Whenever I say ERP, include SAP, Oracle, JDE, Infor and Dynamics."
        -> ai_memory(kind="alias", key="erp", value={"expands_to": [...]})
    "Always exclude customers."            -> default_filter
    "Prefer Seamless over ZoomInfo."       -> source_priority
    "Only use internal data and Seamless." -> allowed_providers
    "Show domain, industry and score."     -> preferred_fields

Memory lives in the ``ai_memory`` table (RLS by workspace), so one workspace's
vocabulary never leaks into another's. It **never stores secrets**: anything that
looks like a key, token, password or connection string is refused, and
credentials belong in Settings → Provider connections (encrypted).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ValidationError

__all__ = ["MemoryService", "parse_memory", "looks_secret", "expand_aliases"]

_SECRET = re.compile(
    r"(sk-[a-z0-9_-]{8,}|sk_(live|test)_|api[_ -]?key|secret|passw(or)?d|token\s*[:=]|bearer\s+[a-z0-9._-]{10,}"
    r"|postgres(ql)?://|redis://|://[^/\s:]+:[^@\s]+@|-----begin|aws_(access|secret)|[a-f0-9]{32,}|[A-Za-z0-9+/]{40,}={0,2})",
    re.I)

_ALIAS = re.compile(r"(?:whenever|when|every time|each time)\s+i\s+(?:say|type|mention|write)\s+[\"']?(?P<key>[\w/&.+ -]{1,60}?)[\"']?"
                    r"\s*,?\s*(?:you should\s+|please\s+)?(?:include|also include|it means|mean|means|expand to|use)\s+(?P<values>.+)$", re.I)
_ALIAS2 = re.compile(r"^(?P<key>[\w/&.+ -]{1,60}?)\s+(?:means|=|should include|includes)\s+(?P<values>.+)$", re.I)
_PREFER = re.compile(r"prefer\s+(?P<a>[\w .-]+?)\s+over\s+(?P<b>[\w .-]+)$", re.I)
_ONLY_USE = re.compile(r"only use\s+(?P<values>.+)$", re.I)
_EXCLUDE = re.compile(r"(?:always\s+)?(?:exclude|skip|ignore|remove)\s+(?P<what>customers|partners|accounts|disqualified( companies)?)", re.I)
_DEFAULT_COUNTRY = re.compile(r"(?:default|always)\s+(?:to\s+)?(?:country\s+)?(?:(?:search|look)\s+in\s+)?(?:the\s+)?(?P<country>us|usa|united states|canada|uk|india)\b", re.I)
_FIELDS = re.compile(r"(?:always\s+)?show\s+(?P<values>.+?)(?:\s+in results)?$", re.I)
_SCORING = re.compile(r"(?:weight|weigh|prioriti[sz]e)\s+(?P<what>.+)$", re.I)


def looks_secret(text: str) -> bool:
    return bool(_SECRET.search(text or ""))


def _split(values: str) -> List[str]:
    parts = re.split(r",|\band\b|\bor\b|/|;", values.strip().rstrip("."))
    out = []
    for part in parts:
        item = part.strip().strip("\"'").strip()
        if item and item.lower() not in out and len(item) <= 60:
            out.append(item)
    return out


def parse_memory(text: str) -> Optional[Dict[str, Any]]:
    """Recognise a 'remember this' instruction. Returns kind/key/value/text or None."""
    raw = (text or "").strip()
    if looks_secret(raw):
        raise ValidationError("that looks like a secret; AI memory never stores keys, tokens or passwords. "
                              "Add credentials in Settings → Provider connections instead.")
    body = re.sub(r"^(please\s+)?(remember( that)?|note( that)?|from now on,?)\s*:?\s*", "", raw, flags=re.I).strip()
    for pattern in (_ALIAS, _ALIAS2):
        m = pattern.match(body)
        if m:
            key = m.group("key").strip().lower()
            values = _split(m.group("values"))
            if key and values:
                return {"kind": "alias", "key": key, "value": {"expands_to": values}, "text": raw}
    m = _PREFER.search(body)
    if m:
        return {"kind": "source_priority", "key": "providers", "text": raw,
                "value": {"order": [m.group("a").strip().lower(), m.group("b").strip().lower()]}}
    m = _ONLY_USE.search(body)
    if m:
        return {"kind": "allowed_providers", "key": "providers", "text": raw,
                "value": {"allowed": [v.lower() for v in _split(m.group("values"))]}}
    m = _EXCLUDE.search(body)
    if m:
        what = m.group("what").split()[0].lower()
        lifecycle = {"customers": "customer", "partners": "partner", "accounts": "account"}.get(what, "disqualified")
        return {"kind": "default_filter", "key": f"exclude_{lifecycle}", "text": raw,
                "value": {"exclude_lifecycles": [lifecycle]}}
    m = _DEFAULT_COUNTRY.search(body)
    if m:
        return {"kind": "default_filter", "key": "country", "text": raw, "value": {"country": m.group("country")}}
    m = _FIELDS.match(body)
    if m and len(body) < 200:
        return {"kind": "preferred_fields", "key": "results", "text": raw,
                "value": {"fields": [v.lower().replace(" ", "_") for v in _split(m.group("values"))]}}
    m = _SCORING.match(body)
    if m:
        return {"kind": "scoring_preference", "key": re.sub(r"\W+", "_", m.group("what").lower())[:60], "text": raw,
                "value": {"prioritize": m.group("what").strip()}}
    return None


def expand_aliases(text: str, aliases: Mapping[str, List[str]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Append each matched alias's expansion to the request, so the parser sees the full vocabulary."""
    applied = []
    lowered = f" {text.lower()} "
    extra = []
    for key, values in aliases.items():
        if re.search(rf"(?<![\w]){re.escape(key)}(?![\w])", lowered):
            extra.extend(values)
            applied.append({"alias": key, "expands_to": values})
    if extra:
        text = f"{text} ({', '.join(extra)})"
    return text, applied


class MemoryService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    @property
    def store(self):
        return self.platform.store

    def remember(self, ctx: Ctx, text: str) -> Dict[str, Any]:
        ctx.require_write()
        parsed = parse_memory(text)
        if parsed is None:
            raise ValidationError("I couldn't tell what to remember. Try: 'Whenever I say ERP, include SAP, Oracle, "
                                  "JDE, Infor and Dynamics.'")
        return self.save(ctx, parsed["kind"], parsed["key"], parsed["value"], text=parsed["text"])

    def save(self, ctx: Ctx, kind: str, key: str, value: Mapping[str, Any], *, text: Optional[str] = None
             ) -> Dict[str, Any]:
        ctx.require_write()
        import json

        if looks_secret(json.dumps(value)) or looks_secret(text or "") or looks_secret(key):
            raise ValidationError("AI memory never stores secrets")
        existing = self.store.first(ctx, "ai_memory", {"kind": kind, "key": key})
        if existing:
            row = self.store.update(ctx, "ai_memory", existing["id"], {"value": dict(value), "text": text, "enabled": True})
        else:
            try:
                row = self.store.insert(ctx, "ai_memory", {"kind": kind, "key": key, "value": dict(value), "text": text})
            except ConflictError:
                existing = self.store.first(ctx, "ai_memory", {"kind": kind, "key": key})
                row = self.store.update(ctx, "ai_memory", existing["id"], {"value": dict(value), "text": text})
        audit(self.store, ctx, "ai_memory.save", entity_type="ai_memory", entity_id=row["id"],
              summary=f"{kind}:{key}", changes={"value": dict(value)})
        return row

    def forget(self, ctx: Ctx, memory_id: str) -> None:
        ctx.require_write()
        self.store.delete(ctx, "ai_memory", memory_id)
        audit(self.store, ctx, "ai_memory.forget", entity_type="ai_memory", entity_id=memory_id)

    def profile(self, ctx: Ctx) -> Dict[str, Any]:
        """Everything the planner applies: aliases, default filters, provider preferences, fields, scoring."""
        rows = self.store.all(ctx, "ai_memory", {"enabled": True}, cap=1000)
        out: Dict[str, Any] = {"aliases": {}, "exclude_lifecycles": [], "country": None, "source_priority": [],
                               "allowed_providers": None, "preferred_fields": [], "scoring": [], "items": rows}
        for row in rows:
            value = row.get("value") or {}
            if row["kind"] == "alias":
                out["aliases"][row["key"]] = list(value.get("expands_to") or [])
            elif row["kind"] == "default_filter":
                out["exclude_lifecycles"] += list(value.get("exclude_lifecycles") or [])
                out["country"] = value.get("country") or out["country"]
            elif row["kind"] == "source_priority":
                out["source_priority"] = list(value.get("order") or [])
            elif row["kind"] == "allowed_providers":
                out["allowed_providers"] = list(value.get("allowed") or [])
            elif row["kind"] == "preferred_fields":
                out["preferred_fields"] = list(value.get("fields") or [])
            elif row["kind"] == "scoring_preference":
                out["scoring"].append(value.get("prioritize"))
        return out
