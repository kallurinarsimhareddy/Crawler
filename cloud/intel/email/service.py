"""Email validation with a cache that never spends twice.

``EmailValidationService.validate(ctx, emails, allow_paid=False, max_age_days=30)``:

1. normalise; unparseable input is ``INVALID`` immediately;
2. **cache** — an ``email_validations`` row newer than ``max_age_days`` is
   returned as-is (``cached: True``); nothing is re-checked or re-paid;
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
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, utcnow
from cloud.intel.core.normalize import normalize_email
from cloud.intel.email.providers import EmailListVerifyProvider, LocalValidator, ValidationResult
from cloud.intel.providers.base import PaidCallRefused, ProviderError

__all__ = ["EmailValidationService", "run_validation_task"]

log = logging.getLogger(__name__)

CACHE_DAYS = 30
MAX_SYNC = 25


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
        if not registry.enabled(ctx, "emaillistverify"):  # stored AND verified by a live check
            return None
        return EmailListVerifyProvider(registry.get_secrets(ctx, "emaillistverify").get("api_key", ""))

    def cached(self, ctx: Ctx, email: str, *, max_age_days: int = CACHE_DAYS) -> Optional[Dict[str, Any]]:
        row = self.store.first(ctx, "email_validations", {"email": email})
        if row is None or row["validated_at"] < utcnow() - timedelta(days=max_age_days):
            return None
        return row

    def validate(self, ctx: Ctx, emails: Iterable[str], *, allow_paid: bool = False, max_age_days: int = CACHE_DAYS,
                 task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        ctx.require_write()
        results: List[Dict[str, Any]] = []
        undecided: List[ValidationResult] = []
        seen = set()
        for raw in emails:
            email = normalize_email(raw) or (raw or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            hit = self.cached(ctx, email, max_age_days=max_age_days) if normalize_email(email) else None
            if hit is not None:
                results.append(self._out(hit, cached=True))
                continue
            local = self.local.check(email)
            if local.decisive:
                results.append(self._save(ctx, local))
            else:
                undecided.append(local)

        if undecided:
            provider = self._paid(ctx) if allow_paid else None
            if provider is None:
                reason = ("allow_paid was not set" if not allow_paid
                          else "no paid validation provider is connected")
                for result in undecided:
                    result.checks["paid_skipped"] = reason
                    results.append(self._save(ctx, result))
            else:
                results.extend(self._paid_checks(ctx, provider, undecided, task_id=task_id))
        return results

    def _paid_checks(self, ctx: Ctx, provider, undecided: List[ValidationResult], *, task_id: Optional[str]
                     ) -> List[Dict[str, Any]]:
        ledger = self.platform.service("credits")
        reservation = ledger.reserve(ctx, "emaillistverify", len(undecided), task_id=task_id,
                                     reason=f"validate {len(undecided)} email(s)", action="email_validation")
        used = 0
        out: List[Dict[str, Any]] = []
        try:
            for local in undecided:
                try:
                    result = provider.check(local.email)
                    used += 1
                    result.checks = {**local.checks, **result.checks}
                    ledger.record_usage(ctx, "emaillistverify", "verify_email", task_id=task_id)
                except (ProviderError, PaidCallRefused) as error:
                    ledger.record_usage(ctx, "emaillistverify", "verify_email", success=False, error=str(error),
                                        task_id=task_id)
                    local.checks["paid_error"] = str(error)[:300]
                    result = local
                out.append(self._save(ctx, result))
        finally:
            if used:
                ledger.consume(ctx, reservation["id"], used)
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

    def _update_contacts(self, ctx: Ctx, row: Dict[str, Any]) -> None:
        for contact in self.store.all(ctx, "contacts", {"email": row["email"]}, cap=100):
            self.store.update(ctx, "contacts", contact["id"], {
                "email_status": row["status"], "email_score": row["score"], "email_validated_at": row["validated_at"],
                "validation_status": "verified" if row["status"] == "VALID" else (
                    "rejected" if row["status"] in ("INVALID", "DISPOSABLE") else "needs_verification")})
            try:
                self.platform.service("automation").emit(ctx, "email_validated", f"email:{row['email']}:{row['id']}"
                                                         f":{row['version']}", {"contact_id": contact["id"],
                                                                                "email": row["email"],
                                                                                "status": row["status"]})
            except Exception:  # noqa: BLE001 - emitting is best-effort
                log.debug("email_validated emit skipped", exc_info=True)

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
