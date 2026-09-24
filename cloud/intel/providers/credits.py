"""Provider credit awareness: per-workspace balances and an append-only ledger.

Every paid provider call (ZoomInfo enrich, Seamless research, EmailListVerify)
goes through the same three steps::

    reservation = ledger.reserve(ctx, "seamless", 10, reason="research 10 contacts", task_id=...)
    ...make the call...
    ledger.consume(ctx, reservation["id"], actual=7)     # the unused 3 are released
    # or, if the call never happened:
    ledger.release(ctx, reservation["id"])

**Nothing is spent without a reservation, and nothing is reserved without a
known balance.** A provider whose balance was never synced cannot be reserved
against: the platform does not guess how many credits an account has.

Limits, all checked in :meth:`CreditLedger.reserve`:

* the account's available credits (``total - consumed - reserved``);
* ``platform.config.max_credits_per_task`` — one action may not spend more;
* the account's optional ``hard_limit`` on ``consumed + reserved``.

``credit_accounts`` and ``credit_ledger`` are ``system_write`` tables: users can
read them (RLS, workspace members only) but only the platform writes them, so a
user can neither forge a grant nor erase a spend. Ledger rows are append-only.
The account row is updated with optimistic concurrency (``version``), retried,
so two workers reserving at once cannot both take the last credits.

The ledger is workspace-owned: one workspace's Seamless balance is never visible
to, or spendable by, another.

``ZoomInfoCreditLedger`` / ``SeamlessCreditLedger`` / ``EmailListVerifyCreditLedger``
are the same ledger pinned to one provider.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow

__all__ = [
    "CreditError",
    "CreditLedger",
    "EmailListVerifyCreditLedger",
    "ProviderCreditLedger",
    "SeamlessCreditLedger",
    "ZoomInfoCreditLedger",
]

log = logging.getLogger(__name__)

_RETRIES = 8


class CreditError(ValidationError):
    """A reservation the ledger refuses: no balance, over a limit, bad amount."""

    status = 402


def _num(value: Any) -> float:
    return float(value or 0.0)


class CreditLedger:
    """Balances and ledger entries for every provider in a workspace."""

    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- reads ----------------------------------------------------------------

    def _account(self, ctx: Ctx, provider: str) -> Optional[Dict[str, Any]]:
        return self.store.first(ctx, "credit_accounts", {"provider": provider})

    def balance(self, ctx: Ctx, provider: str) -> Dict[str, Any]:
        account = self._account(ctx, provider)
        if account is None:
            return {"provider": provider, "total": 0.0, "reserved": 0.0, "consumed": 0.0, "remaining": 0.0,
                    "last_sync_at": None, "known": False, "hard_limit": None}
        total, reserved, consumed = (_num(account["total_credits"]), _num(account["reserved_credits"]),
                                     _num(account["consumed_credits"]))
        return {"provider": provider, "total": total, "reserved": reserved, "consumed": consumed,
                "remaining": max(0.0, total - consumed - reserved), "last_sync_at": account["last_sync_at"],
                "known": account["last_sync_at"] is not None or total > 0, "hard_limit": account["hard_limit"],
                "sync_source": account["sync_source"]}

    def balances(self, ctx: Ctx) -> List[Dict[str, Any]]:
        return [self.balance(ctx, row["provider"]) for row in self.store.all(ctx, "credit_accounts", order="provider")]

    def entries(self, ctx: Ctx, *, provider: Optional[str] = None, limit: int = 100, offset: int = 0):
        filters = {"provider": provider} if provider else {}
        return self.store.list(ctx, "credit_ledger", filters, limit=limit, offset=offset)

    def open_amount(self, ctx: Ctx, reservation_id: str) -> float:
        """How much of a reservation has not been consumed or released yet."""
        reservation = self.store.find(ctx, "credit_ledger", reservation_id)
        if reservation is None or reservation["entry_type"] != "reserve":
            raise NotFoundError(f"reservation {reservation_id} not found")
        settled = sum(_num(e["amount"]) for e in self.store.all(ctx, "credit_ledger",
                                                                   {"reservation_id": reservation_id})
                      if e["entry_type"] in ("consume", "release"))
        return max(0.0, _num(reservation["amount"]) - settled)

    # --- account mutation (optimistic, retried) ---------------------------------

    def _ensure_account(self, sys: Ctx, provider: str) -> Dict[str, Any]:
        account = self._account(sys, provider)
        if account is not None:
            return account
        try:
            return self.store.insert(sys, "credit_accounts", {"provider": provider})
        except ConflictError:
            return self._account(sys, provider)

    def _mutate(self, sys: Ctx, provider: str, change) -> Dict[str, Any]:
        """Apply ``change(account) -> dict of new values`` with optimistic retries."""
        for _ in range(_RETRIES):
            account = self._ensure_account(sys, provider)
            changes = change(account)
            try:
                return self.store.update(sys, "credit_accounts", account["id"], changes,
                                         expected_version=account["version"])
            except ConflictError:
                continue
        raise ConflictError(f"the {provider} credit account is busy; retry")

    def _entry(self, sys: Ctx, provider: str, entry_type: str, amount: float, *, reason: str,
               balance_after: Optional[float] = None, **extra: Any) -> Dict[str, Any]:
        return self.store.insert(sys, "credit_ledger", {
            "provider": provider, "entry_type": entry_type, "amount": float(amount), "reason": reason[:500],
            "balance_after": balance_after, **{k: v for k, v in extra.items() if v is not None}})

    def _by_key(self, sys: Ctx, key: Optional[str]) -> Optional[Dict[str, Any]]:
        return self.store.first(sys, "credit_ledger", {"idempotency_key": key}) if key else None

    # --- the spending path --------------------------------------------------------

    def reserve(self, ctx: Ctx, provider: str, amount: float, *, reason: str, task_id: Optional[str] = None,
                idempotency_key: Optional[str] = None, action: Optional[str] = None) -> Dict[str, Any]:
        """Hold ``amount`` credits for one explicit action. Raises :class:`CreditError`."""
        ctx.require_write()
        sys = ctx.as_system()
        amount = float(amount)
        if amount <= 0:
            raise CreditError("a reservation must be for a positive number of credits")
        existing = self._by_key(sys, idempotency_key)
        if existing is not None:
            return existing
        cap = float(self.platform.config.max_credits_per_task)
        if amount > cap:
            raise CreditError(f"{amount:g} {provider} credits exceeds the per-task cap of {cap:g}")

        def change(account):
            total, reserved, consumed = (_num(account["total_credits"]), _num(account["reserved_credits"]),
                                         _num(account["consumed_credits"]))
            if account["last_sync_at"] is None and total <= 0:
                raise CreditError(f"no known {provider} credit balance; sync the account before spending")
            available = total - consumed - reserved
            if amount > available:
                raise CreditError(f"only {available:g} {provider} credits available, {amount:g} requested")
            limit = account["hard_limit"]
            if limit is not None and consumed + reserved + amount > float(limit):
                raise CreditError(f"reserving {amount:g} would pass the {provider} hard limit of {float(limit):g}")
            return {"reserved_credits": reserved + amount}

        account = self._mutate(sys, provider, change)
        entry = self._entry(sys, provider, "reserve", amount, reason=reason, task_id=task_id,
                            idempotency_key=idempotency_key, action=action,
                            balance_after=self._remaining(account))
        audit(self.store, ctx, "credits.reserve", entity_type="credit_ledger", entity_id=entry["id"],
              summary=f"reserved {amount:g} {provider} credits: {reason}"[:1000])
        return entry

    def consume(self, ctx: Ctx, reservation_id: str, actual: float, *, reason: Optional[str] = None
                ) -> Dict[str, Any]:
        """Record what a reserved call actually cost; any remainder is released."""
        sys = ctx.as_system()
        reservation = self.store.find(sys, "credit_ledger", reservation_id)
        if reservation is None or reservation["entry_type"] != "reserve":
            raise NotFoundError(f"reservation {reservation_id} not found")
        provider = reservation["provider"]
        open_amount = self.open_amount(sys, reservation_id)
        actual = float(actual)
        if actual < 0:
            raise CreditError("consumption cannot be negative")
        if actual > open_amount + 1e-9:
            raise CreditError(f"consumed {actual:g} exceeds the {open_amount:g} still reserved")
        remainder = open_amount - actual
        account = self._mutate(sys, provider, lambda a: {
            "reserved_credits": max(0.0, _num(a["reserved_credits"]) - open_amount),
            "consumed_credits": _num(a["consumed_credits"]) + actual})
        entry = self._entry(sys, provider, "consume", actual, reason=reason or reservation["reason"],
                            reservation_id=reservation_id, task_id=reservation["task_id"],
                            balance_after=self._remaining(account))
        if remainder > 0:
            self._entry(sys, provider, "release", remainder, reason="unused part of the reservation",
                        reservation_id=reservation_id, task_id=reservation["task_id"],
                        balance_after=self._remaining(account))
        return entry

    def release(self, ctx: Ctx, reservation_id: str, *, reason: str = "released unused") -> Dict[str, Any]:
        sys = ctx.as_system()
        reservation = self.store.find(sys, "credit_ledger", reservation_id)
        if reservation is None or reservation["entry_type"] != "reserve":
            raise NotFoundError(f"reservation {reservation_id} not found")
        open_amount = self.open_amount(sys, reservation_id)
        provider = reservation["provider"]
        account = self._mutate(sys, provider, lambda a: {
            "reserved_credits": max(0.0, _num(a["reserved_credits"]) - open_amount)})
        return self._entry(sys, provider, "release", open_amount, reason=reason, reservation_id=reservation_id,
                           task_id=reservation["task_id"], balance_after=self._remaining(account))

    # --- balance maintenance ----------------------------------------------------------

    def sync(self, ctx: Ctx, provider: str, total: Optional[float] = None, *, source: str,
             remaining: Optional[float] = None) -> Dict[str, Any]:
        """Record a provider-reported balance.

        Pass ``total`` when the provider reports the plan size, or ``remaining``
        when it reports what is left (Seamless' ``X-PublicAPI-Credits``); the
        total is then derived so that ``remaining`` matches exactly.
        """
        if total is None and remaining is None:
            raise ValidationError("sync needs a total or a remaining balance")
        sys = ctx.as_system()

        def change(account):
            consumed, reserved = _num(account["consumed_credits"]), _num(account["reserved_credits"])
            new_total = float(total) if total is not None else float(remaining) + consumed + reserved
            if new_total < 0:
                raise ValidationError("a balance cannot be negative")
            return {"total_credits": new_total, "last_sync_at": utcnow(), "sync_source": source[:100]}

        account = self._mutate(sys, provider, change)
        self._entry(sys, provider, "sync", _num(account["total_credits"]), reason=f"balance synced from {source}",
                    balance_after=self._remaining(account))
        return self.balance(sys, provider)

    def grant(self, ctx: Ctx, provider: str, amount: float, *, reason: str) -> Dict[str, Any]:
        """Admin: add credits to the known total (e.g. a purchased top-up)."""
        ctx.require_admin()
        sys = ctx.as_system()
        account = self._mutate(sys, provider, lambda a: {"total_credits": _num(a["total_credits"]) + float(amount),
                                                          "last_sync_at": a["last_sync_at"] or utcnow(),
                                                          "sync_source": a["sync_source"] or "manual grant"})
        self._entry(sys, provider, "grant" if amount >= 0 else "adjust", float(amount), reason=reason,
                    balance_after=self._remaining(account))
        audit(self.store, ctx, "credits.grant", summary=f"{amount:g} {provider} credits: {reason}"[:1000])
        return self.balance(sys, provider)

    def set_hard_limit(self, ctx: Ctx, provider: str, limit: Optional[float]) -> Dict[str, Any]:
        ctx.require_admin()
        sys = ctx.as_system()
        self._mutate(sys, provider, lambda a: {"hard_limit": limit})
        audit(self.store, ctx, "credits.hard_limit", summary=f"{provider} hard limit {limit}")
        return self.balance(sys, provider)

    def record_usage(self, ctx: Ctx, provider: str, operation: str, *, units: int = 1, success: bool = True,
                     latency_ms: Optional[float] = None, task_id: Optional[str] = None,
                     error: Optional[str] = None) -> None:
        try:
            self.store.insert(ctx.as_system(), "usage_events", {
                "provider": provider, "operation": operation[:100], "units": max(0, int(units)),
                "success": success, "latency_ms": latency_ms, "task_id": task_id,
                "error": (error or None) and error[:1000]})
        except Exception:  # noqa: BLE001 - usage telemetry must not break the work
            log.exception("could not record usage for %s", provider)

    def usage(self, ctx: Ctx, provider: Optional[str] = None) -> Dict[str, Any]:
        filters = {"provider": provider} if provider else {}
        rows = self.store.all(ctx, "usage_events", filters, cap=20_000)
        by_op: Dict[str, Dict[str, int]] = {}
        for row in rows:
            key = f"{row['provider']}:{row['operation']}"
            bucket = by_op.setdefault(key, {"calls": 0, "units": 0, "failures": 0})
            bucket["calls"] += 1
            bucket["units"] += int(row["units"] or 0)
            bucket["failures"] += 0 if row["success"] else 1
        return {"operations": by_op, "events": len(rows)}

    @staticmethod
    def _remaining(account: Dict[str, Any]) -> float:
        return max(0.0, _num(account["total_credits"]) - _num(account["consumed_credits"])
                   - _num(account["reserved_credits"]))


class ProviderCreditLedger:
    """A :class:`CreditLedger` pinned to one provider."""

    provider = ""

    def __init__(self, ledger: CreditLedger) -> None:
        self.ledger = ledger

    def balance(self, ctx: Ctx) -> Dict[str, Any]:
        return self.ledger.balance(ctx, self.provider)

    def reserve(self, ctx: Ctx, amount: float, **kw: Any) -> Dict[str, Any]:
        return self.ledger.reserve(ctx, self.provider, amount, **kw)

    def consume(self, ctx: Ctx, reservation_id: str, actual: float, **kw: Any) -> Dict[str, Any]:
        return self.ledger.consume(ctx, reservation_id, actual, **kw)

    def release(self, ctx: Ctx, reservation_id: str, **kw: Any) -> Dict[str, Any]:
        return self.ledger.release(ctx, reservation_id, **kw)

    def sync(self, ctx: Ctx, total: Optional[float] = None, **kw: Any) -> Dict[str, Any]:
        return self.ledger.sync(ctx, self.provider, total, **kw)


class ZoomInfoCreditLedger(ProviderCreditLedger):
    provider = "zoominfo"


class SeamlessCreditLedger(ProviderCreditLedger):
    provider = "seamless"


class EmailListVerifyCreditLedger(ProviderCreditLedger):
    provider = "emaillistverify"
