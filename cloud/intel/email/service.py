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
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, utcnow
from cloud.intel.core.normalize import normalize_email
from cloud.intel.email.engine import final_status
from cloud.intel.email.providers import FATAL_CODES, EmailListVerifyProvider, elv_enabled, LocalValidator, ValidationResult
from cloud.intel.providers.base import PaidCallRefused, ProviderError

__all__ = ["EmailValidationService", "is_unresolved", "max_age", "run_validation_task"]

log = logging.getLogger(__name__)

CACHE_DAYS = 30
MAX_SYNC = 25
#: Addresses per paid batch (one ledger reservation each).
PAID_BATCH = 50
#: Consecutive transient provider failures (timeouts, 5xx, 429 after retries) after which a
#: run stops calling the provider, and how long the workspace then waits before trying again.
BREAKER_FAILURES = 5
BREAKER_COOLDOWN = 120.0


def max_age(value: Any, default: int = CACHE_DAYS) -> int:
    """Cache age in days. ``0`` is honoured (re-run the checks now); missing/invalid -> default."""
    if value is None or value == "":
        return default
    try:
        return max(0, min(365, int(value)))
    except (TypeError, ValueError):
        return default


def is_unresolved(row: Optional[Dict[str, Any]], paid_statuses: Iterable[str] = ("UNKNOWN",)) -> bool:
    """A result only the built-in checks produced, with a status a paid check may settle
    (``paid_statuses``): EmailListVerify has not answered it (not asked, skipped, or the paid
    call failed). An EmailListVerify answer, even an inconclusive one, is not sent again."""
    return bool(row) and row.get("status") in tuple(paid_statuses) and row.get("provider") != "emaillistverify"


def _superseded(row: Dict[str, Any]) -> bool:
    """A cached built-in result produced by a rule that has since been corrected, so it is
    checked again instead of being served for the rest of its cache life: older versions
    marked placeholder-looking addresses (test@, user@, @company.com) INVALID outright."""
    checks = row.get("checks") or {}
    return (row.get("provider") == "local" and row.get("status") == "INVALID" and bool(checks.get("placeholder"))
            and not checks.get("reserved_domain") and checks.get("mx") is not False)


class EmailValidationService:
    def __init__(self, platform: Any, *, local: Optional[LocalValidator] = None) -> None:
        self.platform = platform
        self.store = platform.store
        self.local = local or LocalValidator()
        #: Tests (and a future provider factory) may inject the paid provider.
        self.paid_factory = None
        #: workspace id -> monotonic time before which paid calls are not attempted (breaker).
        self._cooldown: Dict[str, float] = {}

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

    def _plan(self, ctx: Ctx, emails: Iterable[str], *, allow_paid: bool, max_age_days: int,
              paid_statuses: Tuple[str, ...]):
        """Shared by :meth:`validate` and :meth:`estimate_paid`: dedupe (normalized), read the
        cache once, run the free checks, and split into (cached results, decided, undecided)."""
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
        cached: List[Dict[str, Any]] = []
        decided: List[ValidationResult] = []
        undecided: List[ValidationResult] = []
        for email in ordered:
            hit = existing.get(email) if normalize_email(email) else None
            if hit is not None and (hit["validated_at"] < cutoff or _superseded(hit)):
                hit = None
            if hit is not None and not (allow_paid and is_unresolved(hit, paid_statuses)):
                cached.append(self._out(hit, cached=True))
                continue
            local = self.local.check(email)
            if not local.decisive or (allow_paid and local.status in paid_statuses):
                undecided.append(local)
            else:
                decided.append(local)
        return existing, cached, decided, undecided

    def estimate_paid(self, ctx: Ctx, emails: Iterable[str], *, max_age_days: int = CACHE_DAYS,
                      paid_statuses: Iterable[str] = ("UNKNOWN",)) -> Dict[str, Any]:
        """Exactly how many paid checks :meth:`validate` with ``allow_paid`` would make for
        ``emails`` right now (free: cache + built-in checks only, nothing is saved or sent)."""
        paid_statuses = tuple(s for s in paid_statuses if s not in ("DISPOSABLE", "INVALID", "VALID"))
        _, cached, decided, undecided = self._plan(ctx, emails, allow_paid=True, max_age_days=max_age_days,
                                                   paid_statuses=paid_statuses)
        provider = self._paid(ctx)
        cost = float(getattr(provider, "cost_per_check", 1.0) or 1.0) if provider is not None else 1.0
        return {"addresses": len(cached) + len(decided) + len(undecided), "cached": len(cached),
                "decided_free": len(decided), "to_check": len(undecided), "cost_per_check": cost,
                "credits": round(len(undecided) * cost, 4), "provider_active": provider is not None}

    def validate(self, ctx: Ctx, emails: Iterable[str], *, allow_paid: bool = False, max_age_days: int = CACHE_DAYS,
                 task_id: Optional[str] = None, paid_statuses: Iterable[str] = ("UNKNOWN",),
                 paid_budget: Optional[int] = None, on_credit_error: str = "raise") -> List[Dict[str, Any]]:
        """``paid_statuses``: built-in results a paid check may settle. The default sends only
        undecided (UNKNOWN) addresses; a confirmed "verify" run may add ROLE and FREE_PROVIDER.
        DISPOSABLE and INVALID are never sent.

        ``paid_budget``: the most paid checks this call may make (a confirmed estimate); the
        rest keep their free result with ``paid_skipped`` saying why. ``on_credit_error``:
        ``"raise"`` (interactive callers) or ``"record"`` (jobs: a refused reservation is
        recorded on the affected rows instead of failing the whole batch)."""
        paid_statuses = tuple(s for s in paid_statuses if s not in ("DISPOSABLE", "INVALID", "VALID"))
        ctx.require_write()
        existing, results, to_save, undecided = self._plan(ctx, emails, allow_paid=allow_paid,
                                                           max_age_days=max_age_days, paid_statuses=paid_statuses)
        if undecided:
            provider = self._paid(ctx) if allow_paid else None
            reason = None
            if not allow_paid:
                reason = "allow_paid was not set"
            elif provider is None:
                reason = "no paid validation provider is connected"
            elif self._cooldown.get(ctx.workspace_id, 0.0) > time.monotonic():
                reason = "the provider failed repeatedly; paid checks are paused for a few minutes"
            if reason is not None:
                for result in undecided:
                    result.checks["paid_skipped"] = reason
                to_save.extend(undecided)
            else:
                if paid_budget is not None and len(undecided) > max(0, paid_budget):
                    over = undecided[max(0, paid_budget):]
                    undecided = undecided[:max(0, paid_budget)]
                    for result in over:
                        result.checks["paid_skipped"] = "the confirmed credit budget for this run is used up"
                    to_save.extend(over)
                if undecided:
                    results.extend(self._paid_checks(ctx, provider, undecided, task_id=task_id,
                                                     on_credit_error=on_credit_error))
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

    def _paid_checks(self, ctx: Ctx, provider, undecided: List[ValidationResult], *, task_id: Optional[str],
                     on_credit_error: str = "raise") -> List[Dict[str, Any]]:
        """Paid checks in batches of :data:`PAID_BATCH`, each against its own ledger
        reservation that is consumed for the calls actually made (released when none
        were). An account-level failure (bad key, no credits) stops further calls; so do
        :data:`BREAKER_FAILURES` consecutive transient failures (and the workspace then
        waits :data:`BREAKER_COOLDOWN` seconds). The remaining addresses keep their free
        result with the reason recorded, so a later "Verify Unknowns" can still settle them.
        A failure after which the provider may have charged anyway (read timeout, HTTP
        500/504, an unrecognized reply) is counted as used, so the ledger never under-reports."""
        from cloud.intel.providers.credits import CreditError

        ledger = self.platform.service("credits")
        cost = float(getattr(provider, "cost_per_check", 1.0) or 1.0)
        out: List[Dict[str, Any]] = []
        stop: Optional[str] = None
        consecutive = 0
        for offset in range(0, len(undecided), PAID_BATCH):
            batch = undecided[offset:offset + PAID_BATCH]
            if stop is not None:
                for local in batch:
                    local.checks["paid_error"] = stop
                    out.append(self._save(ctx, local))
                continue
            try:
                reservation = ledger.reserve(ctx, "emaillistverify", len(batch) * cost, task_id=task_id,
                                             reason=f"validate {len(batch)} email(s)", action="email_validation")
            except CreditError as error:
                if on_credit_error != "record":
                    raise
                stop = f"credits could not be reserved: {error}"[:300]
                for local in batch:
                    local.checks.update(paid_error=stop, paid_error_code="no_credits")
                    out.append(self._save(ctx, local))
                continue
            used = 0
            try:
                for local in batch:
                    if stop is not None:
                        local.checks["paid_error"] = stop
                        out.append(self._save(ctx, local))
                        continue
                    started = time.monotonic()
                    try:
                        result = provider.check(local.email)
                        used += 1
                        consecutive = 0
                        # Provenance: the built-in verdict this external answer replaces.
                        result.checks = {**local.checks, **result.checks, "builtin_status": local.status}
                        ledger.record_usage(ctx, "emaillistverify", "verify_email", task_id=task_id, units=1,
                                            latency_ms=round((time.monotonic() - started) * 1000, 1))
                    except (ProviderError, PaidCallRefused) as error:
                        code = getattr(error, "code", "provider_error")
                        uncertain = bool(getattr(error, "charge_uncertain", False))
                        used += 1 if uncertain else 0
                        ledger.record_usage(ctx, "emaillistverify", "verify_email", success=False,
                                            units=1 if uncertain else 0, error=f"{code}: {error}"[:500],
                                            task_id=task_id)
                        local.checks["paid_error"] = str(error)[:300]
                        local.checks["paid_error_code"] = code
                        if uncertain:
                            local.checks["paid_charge_uncertain"] = True
                        if code in FATAL_CODES:
                            stop = f"stopped after an account error ({code}): {error}"[:300]
                        else:
                            consecutive += 1
                            if consecutive >= BREAKER_FAILURES:
                                stop = (f"stopped after {consecutive} provider failures in a row "
                                        f"(last: {code}); try again later")
                                self._cooldown[ctx.workspace_id] = time.monotonic() + BREAKER_COOLDOWN
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
                "provider": row["provider"], "validated_at": row["validated_at"], "cached": cached,
                "final_status": final_status(row["status"], row["provider"])}

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
        # email_status keeps the detailed result (evidence); validation_status follows the one
        # resolver, so a contact is "verified" only for a mailbox-verified address and
        # "rejected" only for a confirmed-invalid one (a disposable domain is a risk, not proof).
        final = final_status(row["status"], row["provider"])
        return {"email_status": row["status"], "email_score": row["score"], "email_validated_at": row["validated_at"],
                "validation_status": {"VALID": "verified", "INVALID": "rejected"}.get(final, "needs_verification")}

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
                                       max_age_days=max_age(params.get("max_age_days")),
                                       task_id=task["id"], on_credit_error="record"):
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        reporter.progress(f"validated {min(offset + 50, len(emails))} of {len(emails)}",
                          done=min(offset + 50, len(emails)), total=len(emails))
    audit(platform.store, ctx, "email.validate", summary=f"{len(emails)} email(s) validated",
          changes={"counts": counts})
    return {"emails": len(emails), "counts": counts}
