"""Email validation with a cache that never spends twice.

``EmailValidationService.validate(ctx, emails, allow_paid=False, max_age_days=30)``:

1. normalise; unparseable input is ``INVALID`` immediately;
2. **cache** — an ``email_validations`` row newer than ``max_age_days`` is
   returned as-is (``cached: True``); nothing is re-checked or re-paid. The one
   exception: with ``allow_paid``, a cached *built-in* ``UNKNOWN`` (never answered
   by EmailListVerify, see :func:`is_unresolved`) is checked again so the paid
   provider can resolve it; an EmailListVerify answer is never paid for twice;
3. **local** — :class:`~cloud.intel.email.providers.LocalValidator`; a decisive
   answer (INVALID, DISPOSABLE, ROLE, FREE_PROVIDER, no MX) is final and never
   goes to a paid provider;
4. **paid** — only for undecided addresses, only with ``allow_paid``, only if
   EmailListVerify is connected, and only against a ledger reservation that is
   consumed for the calls actually made. Without ``allow_paid`` the local
   ``UNKNOWN`` is stored with ``paid_skipped`` saying why — not silently.

Every result updates the cache, the matching contacts' ``email_status`` /
``email_score`` / ``email_validated_at``, and emits ``email_validated``.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, utcnow
from cloud.intel.core.normalize import normalize_email
from cloud.intel.email.providers import FATAL_CODES, EmailListVerifyProvider, elv_enabled, LocalValidator, ValidationResult
from cloud.intel.providers.base import PaidCallRefused, ProviderError

__all__ = ["EmailValidationService", "is_unresolved", "run_validation_task"]

log = logging.getLogger(__name__)

CACHE_DAYS = 30
MAX_SYNC = 25
#: Addresses per paid batch (one ledger reservation each).
PAID_BATCH = 50


def is_unresolved(row: Optional[Dict[str, Any]], paid_statuses: Iterable[str] = ("UNKNOWN",)) -> bool:
    """A result only the built-in checks produced, with a status a paid check may settle
    (``paid_statuses``): EmailListVerify has not answered it (not asked, skipped, or the paid
    call failed). An EmailListVerify answer, even an inconclusive one, is not sent again."""
    return bool(row) and row.get("status") in tuple(paid_statuses) and row.get("provider") != "emaillistverify"


class EmailValidationService:
    def __init__(self, platform: Any, *, local: Optional[LocalValidator] = None) -> None:
        self.platform = platform
        self.store = platform.store
        self.local = local or LocalValidator()
        #: Tests (and a future provider factory) may inject the paid provider.
        self.paid_factory = None

    def _paid(self, ctx: Ctx) -> Optional[EmailListVerifyProvider]:
        if self.paid_factory is not None:
            return self.paid_factory(ctx)
        registry = self.platform.service("providers")
        if not elv_enabled(registry, ctx):
            return None
        settings = (registry.connection(ctx, "emaillistverify") or {}).get("settings") or {}
        return EmailListVerifyProvider(registry.get_secrets(ctx, "emaillistverify").get("api_key", ""),
                                       cost_per_check=float(settings.get("cost_per_check") or 1.0))

    def cached(self, ctx: Ctx, email: str, *, max_age_days: int = CACHE_DAYS) -> Optional[Dict[str, Any]]:
        row = self.store.first(ctx, "email_validations", {"email": email})
        if row is None or row["validated_at"] < utcnow() - timedelta(days=max_age_days):
            return None
        return row

    def validate(self, ctx: Ctx, emails: Iterable[str], *, allow_paid: bool = False, max_age_days: int = CACHE_DAYS,
                 task_id: Optional[str] = None, paid_statuses: Iterable[str] = ("UNKNOWN",)) -> List[Dict[str, Any]]:
        """``paid_statuses``: built-in results a paid check may settle. The default sends only
        undecided (UNKNOWN) addresses; a confirmed "verify" run may add ROLE and FREE_PROVIDER.
        DISPOSABLE and INVALID are never sent."""
        paid_statuses = tuple(s for s in paid_statuses if s not in ("DISPOSABLE", "INVALID", "VALID"))
        ctx.require_write()
        results: List[Dict[str, Any]] = []
        undecided: List[ValidationResult] = []
        to_save: List[ValidationResult] = []
        ordered: List[str] = []
        seen = set()
        for raw in emails:
            email = normalize_email(raw) or (raw or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            ordered.append(email)
        # One cache read for the whole call instead of one per address.
        existing = self._prefetch(ctx, [e for e in ordered if normalize_email(e)])
        cutoff = utcnow() - timedelta(days=max_age_days)
        for email in ordered:
            hit = existing.get(email) if normalize_email(email) else None
            if hit is not None and hit["validated_at"] < cutoff:
                hit = None
            if hit is not None and not (allow_paid and is_unresolved(hit, paid_statuses)):
                results.append(self._out(hit, cached=True))
                continue
            local = self.local.check(email)
            if not local.decisive or (allow_paid and local.status in paid_statuses):
                undecided.append(local)
            else:
                to_save.append(local)

        if undecided:
            provider = self._paid(ctx) if allow_paid else None
            if provider is None:
                reason = ("allow_paid was not set" if not allow_paid
                          else "no paid validation provider is connected")
                for result in undecided:
                    result.checks["paid_skipped"] = reason
                to_save.extend(undecided)
            else:
                results.extend(self._paid_checks(ctx, provider, undecided, task_id=task_id))
        results.extend(self._save_many(ctx, to_save, existing))
        return results

    def _prefetch(self, ctx: Ctx, emails: List[str]) -> Dict[str, Dict[str, Any]]:
        """Existing ``email_validations`` rows for ``emails`` (any age), in chunks of 500."""
        found: Dict[str, Dict[str, Any]] = {}
        for offset in range(0, len(emails), 500):
            chunk = emails[offset:offset + 500]
            for row in self.store.all(ctx, "email_validations", {"email__in": chunk}, cap=len(chunk) * 2):
                found[row["email"]] = row
        return found

    def _save_many(self, ctx: Ctx, results: List[ValidationResult], existing: Dict[str, Dict[str, Any]]
                   ) -> List[Dict[str, Any]]:
        """:meth:`_save` for many results: one bulk insert for new addresses, one bulk update
        for known ones, one bulk contact update. Same stored values as :meth:`_save`."""
        if not results:
            return []
        now = utcnow()
        inserts: List[Dict[str, Any]] = []
        updates: List[Any] = []
        for result in results:
            values = {"status": result.status, "score": result.score, "checks": result.checks,
                      "provider": result.provider, "validated_at": now,
                      "expires_at": now + timedelta(days=CACHE_DAYS), "raw": result.raw}
            row = existing.get(result.email)
            if row is None:
                inserts.append({"email": result.email, **values})
            else:
                updates.append((row["id"], values))
        try:
            saved = self.store.insert_many(ctx, "email_validations", inserts) if inserts else []
        except ConflictError:  # another run cached one of these first: the per-row path handles the race
            return [self._save(ctx, result) for result in results]
        saved += self.store.update_many(ctx, "email_validations", updates) if updates else []
        missing = {r.email for r in results} - {row["email"] for row in saved}
        extra = [self._save(ctx, r) for r in results if r.email in missing]  # rows deleted meanwhile
        self._update_contacts_many(ctx, saved)
        return [self._out(row, cached=False) for row in saved] + extra

    def _update_contacts_many(self, ctx: Ctx, rows: List[Dict[str, Any]]) -> None:
        by_email = {row["email"]: row for row in rows}
        emails = list(by_email)
        contacts: List[Dict[str, Any]] = []
        for offset in range(0, len(emails), 500):
            chunk = emails[offset:offset + 500]
            contacts += self.store.all(ctx, "contacts", {"email__in": chunk}, cap=len(chunk) * 100)
        if not contacts:
            return
        self.store.update_many(ctx, "contacts", [(c["id"], self._contact_values(by_email[c["email"]]))
                                                 for c in contacts])
        for contact in contacts:
            self._emit_validated(ctx, contact, by_email[contact["email"]])

    def _paid_checks(self, ctx: Ctx, provider, undecided: List[ValidationResult], *, task_id: Optional[str]
                     ) -> List[Dict[str, Any]]:
        """Paid checks in batches of :data:`PAID_BATCH`, each against its own ledger
        reservation that is consumed for the calls actually made (released when none
        were). An account-level failure (bad key, no credits) stops further calls; the
        remaining addresses keep their local result with the reason recorded."""
        ledger = self.platform.service("credits")
        cost = float(getattr(provider, "cost_per_check", 1.0) or 1.0)
        out: List[Dict[str, Any]] = []
        fatal: Optional[str] = None
        for offset in range(0, len(undecided), PAID_BATCH):
            batch = undecided[offset:offset + PAID_BATCH]
            if fatal is not None:
                for local in batch:
                    local.checks["paid_error"] = fatal
                    out.append(self._save(ctx, local))
                continue
            reservation = ledger.reserve(ctx, "emaillistverify", len(batch) * cost, task_id=task_id,
                                         reason=f"validate {len(batch)} email(s)", action="email_validation")
            used = 0
            try:
                for local in batch:
                    if fatal is not None:
                        local.checks["paid_error"] = fatal
                        out.append(self._save(ctx, local))
                        continue
                    started = time.monotonic()
                    try:
                        result = provider.check(local.email)
                        used += 1
                        # Provenance: the built-in verdict this external answer replaces.
                        result.checks = {**local.checks, **result.checks, "builtin_status": local.status}
                        ledger.record_usage(ctx, "emaillistverify", "verify_email", task_id=task_id, units=1,
                                            latency_ms=round((time.monotonic() - started) * 1000, 1))
                    except (ProviderError, PaidCallRefused) as error:
                        code = getattr(error, "code", "provider_error")
                        ledger.record_usage(ctx, "emaillistverify", "verify_email", success=False, units=0,
                                            error=f"{code}: {error}", task_id=task_id)
                        local.checks["paid_error"] = str(error)[:300]
                        local.checks["paid_error_code"] = code
                        if code in FATAL_CODES:
                            fatal = f"stopped after an account error ({code}): {error}"[:300]
                        result = local
                    out.append(self._save(ctx, result))
            finally:
                if used:
                    ledger.consume(ctx, reservation["id"], used * cost)
                else:
                    ledger.release(ctx, reservation["id"], reason="no paid checks were made")
        return out

    @staticmethod
    def _out(row: Dict[str, Any], *, cached: bool) -> Dict[str, Any]:
        return {"email": row["email"], "status": row["status"], "score": row["score"], "checks": row["checks"],
                "provider": row["provider"], "validated_at": row["validated_at"], "cached": cached}

    def _save(self, ctx: Ctx, result: ValidationResult) -> Dict[str, Any]:
        now = utcnow()
        values = {"status": result.status, "score": result.score, "checks": result.checks,
                  "provider": result.provider, "validated_at": now,
                  "expires_at": now + timedelta(days=CACHE_DAYS), "raw": result.raw}
        existing = self.store.first(ctx, "email_validations", {"email": result.email})
        try:
            if existing is None:
                row = self.store.insert(ctx, "email_validations", {"email": result.email, **values})
            else:
                row = self.store.update(ctx, "email_validations", existing["id"], values)
        except ConflictError:
            row = self.store.first(ctx, "email_validations", {"email": result.email})
            row = self.store.update(ctx, "email_validations", row["id"], values)
        self._update_contacts(ctx, row)
        return self._out(row, cached=False)

    @staticmethod
    def _contact_values(row: Dict[str, Any]) -> Dict[str, Any]:
        return {"email_status": row["status"], "email_score": row["score"], "email_validated_at": row["validated_at"],
                "validation_status": "verified" if row["status"] == "VALID" else (
                    "rejected" if row["status"] in ("INVALID", "DISPOSABLE") else "needs_verification")}

    def _emit_validated(self, ctx: Ctx, contact: Dict[str, Any], row: Dict[str, Any]) -> None:
        try:
            self.platform.service("automation").emit(ctx, "email_validated", f"email:{row['email']}:{row['id']}"
                                                     f":{row['version']}", {"contact_id": contact["id"],
                                                                            "email": row["email"],
                                                                            "status": row["status"]})
        except Exception:  # noqa: BLE001 - emitting is best-effort
            log.debug("email_validated emit skipped", exc_info=True)

    def _update_contacts(self, ctx: Ctx, row: Dict[str, Any]) -> None:
        for contact in self.store.all(ctx, "contacts", {"email": row["email"]}, cap=100):
            self.store.update(ctx, "contacts", contact["id"], self._contact_values(row))
            self._emit_validated(ctx, contact, row)

    def emails_for(self, ctx: Ctx, params: Dict[str, Any]) -> List[str]:
        """Resolve a task's target: explicit emails, contact ids, or a contacts list."""
        emails = list(params.get("emails") or [])
        for contact_id in params.get("contact_ids") or []:
            contact = self.store.find(ctx, "contacts", contact_id)
            if contact and contact.get("email"):
                emails.append(contact["email"])
        if params.get("list_id"):
            for member in self.store.all(ctx, "list_members", {"list_id": params["list_id"]}, cap=50_000):
                contact = self.store.find(ctx, "contacts", member["entity_id"])
                if contact and contact.get("email"):
                    emails.append(contact["email"])
        if params.get("company_id"):
            emails += [c["email"] for c in self.store.all(ctx, "contacts", {"company_id": params["company_id"]})
                       if c.get("email")]
        return emails


def run_validation_task(platform: Any, ctx: Ctx, task: Dict[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import TaskPaused

    service: EmailValidationService = platform.service("email")
    params = task["params"]
    emails = service.emails_for(ctx, params)
    start = int(reporter.checkpoint.get("done", 0))
    counts: Dict[str, int] = dict(reporter.checkpoint.get("counts") or {})
    for offset in range(start, len(emails), 50):
        if reporter.is_cancelled():
            break
        if reporter.should_pause():
            raise TaskPaused({"done": offset, "counts": counts})
        batch = emails[offset:offset + 50]
        for result in service.validate(ctx, batch, allow_paid=bool(params.get("allow_paid")),
                                       max_age_days=int(params.get("max_age_days") or CACHE_DAYS),
                                       task_id=task["id"]):
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        reporter.progress(f"validated {min(offset + 50, len(emails))} of {len(emails)}",
                          done=min(offset + 50, len(emails)), total=len(emails))
    audit(platform.store, ctx, "email.validate", summary=f"{len(emails)} email(s) validated",
          changes={"counts": counts})
    return {"emails": len(emails), "counts": counts}
