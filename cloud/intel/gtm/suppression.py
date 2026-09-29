"""Suppression: who must never be emailed, checked before every send.

Three scopes, checked in this order by :meth:`SuppressionService.check`:

* **global** — the operator's platform-wide list: the file named by
  ``CAREERCLOUD_GLOBAL_SUPPRESSIONS_FILE`` (default ``gtm/data/global_suppressions.txt``)
  plus ``CAREERCLOUD_GLOBAL_SUPPRESSIONS`` (comma separated). Read-only in the
  product: it is not workspace data, so no workspace user can edit or remove it.
* **workspace** — ``suppressions`` rows with ``scope='workspace'``: apply to
  every campaign in the workspace.
* **campaign** — ``suppressions`` rows with ``scope='campaign'`` and a
  ``campaign_id``: apply only when that campaign sends.

Each scope holds addresses (``kind='email'``) and domains (``kind='domain'``);
a domain entry also covers its subdomains' registrable domain. Rows past
``expires_at`` no longer suppress. Removing an ``unsubscribe``, ``complaint``
or ``hard_bounce`` entry needs workspace admin rights (it re-enables mail to
someone who asked to stop or whose mailbox does not exist).
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, ValidationError, utcnow
from cloud.intel.core.normalize import domain_of, normalize_email

__all__ = ["SuppressionService", "PROTECTED_REASONS", "REASONS", "global_suppressions"]

REASONS = ("manual", "unsubscribe", "bounce", "hard_bounce", "complaint", "legal", "customer", "invalid",
           "blocked", "role_policy")
#: Removing one of these re-enables mail to someone who opted out or cannot receive mail.
PROTECTED_REASONS = frozenset({"unsubscribe", "complaint", "hard_bounce", "bounce", "legal"})
_DEFAULT_GLOBAL = Path(__file__).resolve().parent / "data" / "global_suppressions.txt"


def _parse_value(raw: str, kind: Optional[str] = None) -> Tuple[str, str]:
    """``(kind, normalized value)``; kind is inferred from '@' when not given."""
    text = (raw or "").strip().lower()
    if text.startswith("@"):
        text = text[1:]
        kind = kind or "domain"
    kind = kind or ("email" if "@" in text else "domain")
    if kind not in ("email", "domain"):
        raise ValidationError("kind must be email or domain")
    value = (normalize_email(text) if kind == "email" else domain_of(text)) or ""
    if not value:
        raise ValidationError(f"not a valid {kind}: {raw!r}")
    return kind, value


def global_suppressions(env: Optional[Mapping[str, str]] = None) -> Dict[str, FrozenSet[str]]:
    """The operator's platform-wide list as ``{"email": {...}, "domain": {...}}``."""
    env = env if env is not None else os.environ
    entries: List[str] = []
    path = Path(env.get("CAREERCLOUD_GLOBAL_SUPPRESSIONS_FILE") or _DEFAULT_GLOBAL)
    if path.is_file():
        entries += [line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()]
    entries += (env.get("CAREERCLOUD_GLOBAL_SUPPRESSIONS") or "").split(",")
    emails, domains = set(), set()
    for entry in entries:
        if not entry.strip():
            continue
        try:
            kind, value = _parse_value(entry)
        except ValidationError:
            continue
        (emails if kind == "email" else domains).add(value)
    return {"email": frozenset(emails), "domain": frozenset(domains)}


def _domains_for(email: str) -> List[str]:
    domain = email.rpartition("@")[2]
    out = [domain]
    registrable = domain_of(domain)
    if registrable and registrable != domain:
        out.append(registrable)
    return out


class SuppressionService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- the check ----------------------------------------------------------------

    @staticmethod
    def _active(row: Mapping[str, Any], now: datetime) -> bool:
        expires = row.get("expires_at")
        return expires is None or expires > now

    def check(self, ctx: Ctx, email: Optional[str], *, campaign_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The suppression that blocks mail to ``email`` (for ``campaign_id``), or None.

        Returns ``{"scope", "kind", "value", "reason", "source", "id", "campaign_id"}``.
        """
        email = normalize_email(email)
        if not email:
            return None
        domains = _domains_for(email)
        glob = global_suppressions()
        if email in glob["email"]:
            return {"scope": "global", "kind": "email", "value": email, "reason": "blocked", "source": "operator",
                    "id": None, "campaign_id": None}
        for domain in domains:
            if domain in glob["domain"]:
                return {"scope": "global", "kind": "domain", "value": domain, "reason": "blocked",
                        "source": "operator", "id": None, "campaign_id": None}
        now = utcnow()
        candidates = [("email", email)] + [("domain", d) for d in domains]
        for kind, value in candidates:
            row = self.store.first(ctx, "suppressions", {"kind": kind, "value": value})
            if row is None or not self._active(row, now):
                continue
            scope = row.get("scope") or "workspace"
            if scope == "campaign" and row.get("campaign_id") != campaign_id:
                continue
            return {"scope": scope, "kind": row["kind"], "value": row["value"], "reason": row["reason"],
                    "source": row.get("source"), "id": row["id"], "campaign_id": row.get("campaign_id")}
        return None

    # --- changes --------------------------------------------------------------------

    def add(self, ctx: Ctx, value: str, *, kind: Optional[str] = None, reason: str = "manual",
            scope: str = "workspace", campaign_id: Optional[str] = None, expires_at: Optional[datetime] = None,
            note: Optional[str] = None, source: Optional[str] = None) -> Dict[str, Any]:
        """Suppress an address or domain. Idempotent: an existing entry is returned
        (and widened from campaign to workspace scope when asked)."""
        ctx.require_write()
        kind, value = _parse_value(value, kind)
        if reason not in REASONS:
            raise ValidationError(f"reason must be one of {', '.join(REASONS)}")
        if scope not in ("workspace", "campaign"):
            raise ValidationError("scope must be workspace or campaign (the global list is operator-managed)")
        if scope == "campaign":
            if not campaign_id:
                raise ValidationError("a campaign suppression needs campaign_id")
            self.store.get(ctx, "campaigns", campaign_id)
        else:
            campaign_id = None
        existing = self.store.first(ctx, "suppressions", {"kind": kind, "value": value})
        if existing is not None:
            existing_scope = existing.get("scope") or "workspace"
            if existing_scope == "workspace" or (scope == "campaign" and existing.get("campaign_id") == campaign_id):
                return existing
            if scope == "workspace":
                row = self.store.update(ctx, "suppressions", existing["id"], {
                    "scope": "workspace", "campaign_id": None, "reason": reason, "expires_at": expires_at})
                audit(self.store, ctx, "suppression.widen", entity_type="suppressions", entity_id=row["id"],
                      changes={"value": value, "from_campaign": existing.get("campaign_id")})
                return row
            raise ConflictError(f"{value} is already suppressed for another campaign; suppress it for the whole "
                                "workspace instead")
        try:
            row = self.store.insert(ctx, "suppressions", {
                "value": value, "kind": kind, "reason": reason, "source": (source or "manual")[:100],
                "scope": scope, "campaign_id": campaign_id, "expires_at": expires_at,
                "note": (note or None) and note[:1000]})
        except ConflictError:
            return self.store.first(ctx, "suppressions", {"kind": kind, "value": value})
        audit(self.store, ctx, "suppression.add", entity_type="suppressions", entity_id=row["id"],
              changes={"value": value, "kind": kind, "reason": reason, "scope": scope, "campaign_id": campaign_id})
        return row

    def remove(self, ctx: Ctx, suppression_id: str) -> Dict[str, Any]:
        ctx.require_write()
        row = self.store.get(ctx, "suppressions", suppression_id)
        if row["reason"] in PROTECTED_REASONS and not ctx.can_admin:
            raise ForbiddenError(f"removing a {row['reason']} suppression needs workspace admin rights")
        self.store.delete(ctx, "suppressions", suppression_id)
        audit(self.store, ctx, "suppression.remove", entity_type="suppressions", entity_id=suppression_id,
              changes={"value": row["value"], "kind": row["kind"], "reason": row["reason"]})
        return {"removed": True, "id": suppression_id, "value": row["value"]}

    def bulk_add(self, ctx: Ctx, values: Iterable[str], *, reason: str = "manual", scope: str = "workspace",
                 campaign_id: Optional[str] = None, kind: Optional[str] = None, source: str = "bulk",
                 expires_at: Optional[datetime] = None, note: Optional[str] = None) -> Dict[str, Any]:
        added = existing = 0
        invalid: List[str] = []
        seen = set()
        for raw in values:
            if not (raw or "").strip():
                continue
            try:
                parsed = _parse_value(raw, kind)
            except ValidationError:
                invalid.append(str(raw)[:320])
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            before = self.store.first(ctx, "suppressions", {"kind": parsed[0], "value": parsed[1]})
            try:
                self.add(ctx, parsed[1], kind=parsed[0], reason=reason, scope=scope, campaign_id=campaign_id,
                         source=source, expires_at=expires_at, note=note)
            except ConflictError:
                existing += 1
                continue
            if before is None:
                added += 1
            else:
                existing += 1
        return {"added": added, "existing": existing, "invalid": invalid[:200], "invalid_count": len(invalid)}

    def bulk_remove(self, ctx: Ctx, ids: Iterable[str]) -> Dict[str, Any]:
        removed, refused = 0, []
        for suppression_id in ids:
            try:
                self.remove(ctx, suppression_id)
                removed += 1
            except (ForbiddenError, ValidationError) as error:
                refused.append({"id": suppression_id, "reason": str(error)})
        return {"removed": removed, "refused": refused}

    # --- import / export --------------------------------------------------------------

    def import_csv(self, ctx: Ctx, text: str, *, reason: str = "manual", scope: str = "workspace",
                   campaign_id: Optional[str] = None) -> Dict[str, Any]:
        """CSV with a ``value``/``email``/``domain`` column (optional ``reason``), or one value per line."""
        text = (text or "").lstrip("﻿")
        if not text.strip():
            raise ValidationError("the file is empty")
        first = text.splitlines()[0].lower()
        if any(h in first.replace(";", ",").split(",") for h in ("value", "email", "domain", "reason")):
            reader = csv.DictReader(io.StringIO(text))
            by_reason: Dict[str, List[str]] = {}
            for row in reader:
                clean = {str(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                value = clean.get("value") or clean.get("email") or clean.get("domain") or ""
                row_reason = clean.get("reason") or reason
                if row_reason not in REASONS:
                    row_reason = reason
                by_reason.setdefault(row_reason, []).append(value)
            total = {"added": 0, "existing": 0, "invalid": [], "invalid_count": 0}
            for row_reason, values in by_reason.items():
                part = self.bulk_add(ctx, values, reason=row_reason, scope=scope, campaign_id=campaign_id,
                                     source="import")
                for key in ("added", "existing", "invalid_count"):
                    total[key] += part[key]
                total["invalid"] += part["invalid"]
            return total
        return self.bulk_add(ctx, [line.split(",")[0] for line in text.splitlines()], reason=reason, scope=scope,
                             campaign_id=campaign_id, source="import")

    def export_csv(self, ctx: Ctx, filters: Optional[Mapping[str, Any]] = None) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["value", "kind", "reason", "scope", "campaign_id", "source", "expires_at", "created_at"])
        for row in self.store.all(ctx, "suppressions", dict(filters or {}), cap=100_000):
            writer.writerow([row["value"], row["kind"], row["reason"], row.get("scope") or "workspace",
                             row.get("campaign_id") or "", row.get("source") or "",
                             row["expires_at"].isoformat() if row.get("expires_at") else "",
                             row["created_at"].isoformat() if row.get("created_at") else ""])
        audit(self.store, ctx, "suppression.export", summary="suppression list exported")
        return out.getvalue()

    def global_list(self) -> Dict[str, Any]:
        glob = global_suppressions()
        return {"emails": sorted(glob["email"]), "domains": sorted(glob["domain"]), "read_only": True,
                "source": "operator file / CAREERCLOUD_GLOBAL_SUPPRESSIONS"}

    def stats(self, ctx: Ctx) -> Dict[str, Any]:
        by_reason = self.store.group_count(ctx, "suppressions", "reason")
        by_scope = self.store.group_count(ctx, "suppressions", "scope")
        glob = global_suppressions()
        return {"total": sum(by_reason.values()), "by_reason": {str(k): v for k, v in by_reason.items()},
                "by_scope": {str(k): v for k, v in by_scope.items()},
                "global": len(glob["email"]) + len(glob["domain"])}
