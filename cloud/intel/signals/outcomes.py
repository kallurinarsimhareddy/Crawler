"""Closed loop: signal -> prospect -> campaign -> outcome -> back to the signal.

A prospect enters a campaign through a ``sequence_enrollments`` row; ``signal_id`` on
that row remembers which hiring signal produced it (:meth:`link_enrollment`).
Every outcome is an append-only ``signal_outcomes`` row linked to the signal, its
company, the prospect (contact), the campaign and the enrollment, with a timestamp
and its source; the signal keeps its latest outcome and per-outcome counts.

Outcomes written automatically:

* ``contacted``  — the first email of the enrollment was sent (message event ``sent``)
* ``replied``    — a reply was recorded for the enrollment
* ``no_response`` — the enrollment finished its sequence without a reply
* ``opportunity`` — an opportunity was created that cites the signal

``meeting``, ``opportunity`` and ``disqualified`` can also be recorded by a person
(:meth:`record`, source ``manual``). There is no predictive scoring here: the rows
are the auditable relationship a later model can learn from.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError, utcnow
from cloud.intel.store.spec import SIGNAL_OUTCOMES

__all__ = ["SignalOutcomeService", "SIGNAL_OUTCOMES"]

log = logging.getLogger(__name__)

_EVENT_OUTCOMES = {"sent": "contacted", "replied": "replied"}


class SignalOutcomeService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    def link_enrollment(self, ctx: Ctx, enrollment_id: str, signal_id: str) -> Dict[str, Any]:
        """Remember that ``signal_id`` put this prospect into this campaign."""
        ctx.require_write()
        signal = self.store.get(ctx, "hiring_signals", signal_id)
        row = self.store.update(ctx, "sequence_enrollments", enrollment_id, {"signal_id": signal["id"]})
        audit(self.store, ctx, "signal.enrollment_linked", entity_type="sequence_enrollments", entity_id=enrollment_id,
              summary=f"signal {signal['id']} ({signal['signal_type']})")
        return row

    def record(self, ctx: Ctx, signal_id: str, outcome: str, *, contact_id: Optional[str] = None,
               campaign_id: Optional[str] = None, enrollment_id: Optional[str] = None,
               opportunity_id: Optional[str] = None, source: str = "manual", note: Optional[str] = None,
               occurred_at: Optional[datetime] = None, data: Optional[Mapping[str, Any]] = None
               ) -> Optional[Dict[str, Any]]:
        """Write one outcome back to its signal. The same outcome for the same signal and
        enrollment is recorded once (a resent event or a retried task does not double it)."""
        if outcome not in SIGNAL_OUTCOMES:
            raise ValidationError(f"outcome must be one of {', '.join(SIGNAL_OUTCOMES)}")
        signal = self.store.get(ctx, "hiring_signals", signal_id)
        enrollment = self.store.find(ctx, "sequence_enrollments", enrollment_id) if enrollment_id else None
        if enrollment is not None:
            contact_id = contact_id or enrollment.get("contact_id")
            campaign_id = campaign_id or enrollment.get("campaign_id")
            opportunity_id = opportunity_id or enrollment.get("opportunity_id")
        duplicate = {"signal_id": signal["id"], "outcome": outcome}
        duplicate["enrollment_id"] = enrollment_id if enrollment_id else None
        if enrollment_id is None and opportunity_id:
            duplicate["opportunity_id"] = opportunity_id
        if outcome != "meeting" and self.store.first(ctx, "signal_outcomes", duplicate) is not None:
            return None
        when = occurred_at or utcnow()
        row = self.store.insert(ctx, "signal_outcomes", {
            "signal_id": signal["id"], "company_id": signal.get("company_id"), "contact_id": contact_id,
            "campaign_id": campaign_id, "enrollment_id": enrollment_id, "opportunity_id": opportunity_id,
            "outcome": outcome, "occurred_at": when, "source": source, "note": (note or "")[:2000] or None,
            "data": dict(data or {})})
        counts = dict(signal.get("outcome_counts") or {})
        counts[outcome] = int(counts.get(outcome, 0)) + 1
        latest = signal.get("outcome_at")
        changes: Dict[str, Any] = {"outcome_counts": counts}
        if latest is None or when >= latest:
            changes.update({"outcome": outcome, "outcome_at": when})
        self.store.update(ctx.as_system() if not ctx.system else ctx, "hiring_signals", signal["id"], changes)
        audit(self.store, ctx, "signal.outcome", entity_type="hiring_signals", entity_id=signal["id"],
              summary=f"{outcome} ({source})", changes={"outcome_id": row["id"], "enrollment_id": enrollment_id,
                                                         "campaign_id": campaign_id, "contact_id": contact_id})
        return row

    def outcomes(self, ctx: Ctx, signal_id: str) -> List[Dict[str, Any]]:
        self.store.get(ctx, "hiring_signals", signal_id)
        return self.store.all(ctx, "signal_outcomes", {"signal_id": signal_id}, order="-occurred_at", cap=1000)

    # --- automatic write-back (best effort: never breaks sending) -------------------------

    def on_message_event(self, ctx: Ctx, event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        outcome = _EVENT_OUTCOMES.get(str(event.get("event") or ""))
        if outcome is None or not event.get("enrollment_id"):
            return None
        return self._from_enrollment(ctx, str(event["enrollment_id"]), outcome, source="message_event",
                                     occurred_at=event.get("occurred_at"), data={"message_event_id": event.get("id")})

    def on_reply(self, ctx: Ctx, contact_id: str, *, occurred_at: Optional[datetime] = None) -> int:
        written = 0
        for enrollment in self.store.all(ctx, "sequence_enrollments", {"contact_id": contact_id,
                                                                        "signal_id__isnull": False}, cap=100):
            if self._from_enrollment(ctx, enrollment["id"], "replied", source="message_event",
                                     occurred_at=occurred_at):
                written += 1
        return written

    def on_enrollment_finished(self, ctx: Ctx, enrollment: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """A sequence that ran to the end without a reply is a NO RESPONSE for its signal."""
        if enrollment.get("status") != "completed":
            return None
        return self._from_enrollment(ctx, str(enrollment["id"]), "no_response", source="enrollment")

    def on_opportunity(self, ctx: Ctx, opportunity: Mapping[str, Any]) -> int:
        written = 0
        for signal_id in opportunity.get("signal_ids") or []:
            if self.store.find(ctx, "hiring_signals", signal_id) is None:
                continue
            if self.record(ctx, signal_id, "opportunity", opportunity_id=opportunity.get("id"),
                           campaign_id=opportunity.get("campaign_id"), source="opportunity"):
                written += 1
        return written

    def _from_enrollment(self, ctx: Ctx, enrollment_id: str, outcome: str, *, source: str,
                         occurred_at: Optional[datetime] = None, data: Optional[Mapping[str, Any]] = None
                         ) -> Optional[Dict[str, Any]]:
        try:
            enrollment = self.store.find(ctx, "sequence_enrollments", enrollment_id)
            if enrollment is None or not enrollment.get("signal_id"):
                return None
            if self.store.find(ctx, "hiring_signals", enrollment["signal_id"]) is None:
                return None
            return self.record(ctx, enrollment["signal_id"], outcome, enrollment_id=enrollment_id, source=source,
                               occurred_at=occurred_at, data=data)
        except Exception:  # noqa: BLE001 - the outcome loop must never stop a send or a reply
            log.exception("could not write the %s outcome of enrollment %s back to its signal", outcome,
                          enrollment_id)
            return None
