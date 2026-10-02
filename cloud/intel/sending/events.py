"""Provider-neutral delivery events: normalize, de-duplicate, then apply.

``EventService.ingest(ctx, provider, payload)``:

1. **normalize** the provider's webhook body into SANA GTM events —
   ``delivered``, ``bounced`` (``hard``/``soft``), ``deferred``, ``opened``,
   ``clicked``, ``replied``, ``unsubscribed``, ``complained``;
2. **de-duplicate** on ``(provider, provider_event_id)`` in ``inbound_events``:
   a webhook retried by the provider changes nothing the second time;
3. **apply**: a ``message_events`` row for the matching enrollment, and for
   reply / hard bounce / unsubscribe / complaint the stop logic —
   the sequence stops, the contact is updated, the address is suppressed, an
   activity is logged, and for a reply a follow-up task (and a notification)
   is created. Soft bounces and deferrals are recorded, never suppressed.

What each provider can actually report is declared in :data:`CAPABILITIES`;
the platform never shows open/click/reply numbers a provider cannot produce.
Gmail and Microsoft Graph send through the user's own mailbox and have no
delivery webhooks; reply/bounce detection for them would need mailbox read
access and polling, which is **not built** — replies there are recorded by a
user (``manual``) or a forwarding rule posting to the ``generic`` webhook.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ValidationError, utcnow
from cloud.intel.core.normalize import normalize_email

__all__ = ["CAPABILITIES", "EventService", "KINDS", "normalize"]

log = logging.getLogger(__name__)

KINDS = ("delivered", "bounced", "deferred", "opened", "clicked", "replied", "unsubscribed", "complained")

#: What each provider can tell us. False means SANA GTM never claims that event for it.
CAPABILITIES: Dict[str, Dict[str, bool]] = {
    "sendgrid": {"delivered": True, "bounced": True, "deferred": True, "opened": True, "clicked": True,
                 "replied": False, "unsubscribed": True, "complained": True},
    "postmark": {"delivered": True, "bounced": True, "deferred": False, "opened": True, "clicked": True,
                 "replied": True, "unsubscribed": True, "complained": True},
    "google": {k: False for k in KINDS},
    "microsoft365": {k: False for k in KINDS},
    "smtp": {k: False for k in KINDS},
    "api": {"delivered": True, "bounced": True, "deferred": True, "opened": True, "clicked": True,
            "replied": False, "unsubscribed": True, "complained": True},
    #: A forwarding rule / other system posting normalized events; a user recording one by hand.
    "generic": {k: True for k in KINDS},
    "manual": {"delivered": False, "bounced": True, "deferred": False, "opened": False, "clicked": False,
               "replied": True, "unsubscribed": True, "complained": True},
}
WEBHOOK_PROVIDERS = ("sendgrid", "postmark", "generic")

_SENDGRID = {"delivered": "delivered", "bounce": "bounced", "dropped": "bounced", "deferred": "deferred",
             "open": "opened", "click": "clicked", "unsubscribe": "unsubscribed",
             "group_unsubscribe": "unsubscribed", "spamreport": "complained"}
_POSTMARK = {"Delivery": "delivered", "Bounce": "bounced", "Open": "opened", "Click": "clicked",
             "SpamComplaint": "complained", "SubscriptionChange": "unsubscribed", "Inbound": "replied"}
_POSTMARK_SOFT = {"SoftBounce", "Transient", "DnsError", "AutoResponder", "Blocked", "Unknown"}


def _ts(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return utcnow()


def _fallback_id(provider: str, item: Mapping[str, Any]) -> str:
    return hashlib.sha256(f"{provider}:{json.dumps(item, sort_keys=True, default=str)}".encode()).hexdigest()


def normalize(provider: str, payload: Any) -> List[Dict[str, Any]]:
    """The provider body as a list of normalized events (unknown ones kept as ``unknown``)."""
    out: List[Dict[str, Any]] = []
    if provider == "sendgrid":
        for item in payload if isinstance(payload, list) else [payload]:
            if not isinstance(item, Mapping):
                continue
            native = str(item.get("event") or "")
            kind = _SENDGRID.get(native, "unknown")
            bounce = "none"
            if native == "bounce":
                bounce = "soft" if item.get("type") == "blocked" else "hard"
            elif native == "dropped":
                bounce = "hard" if "bounce" in str(item.get("reason", "")).lower() else "soft"
            message_id = str(item.get("sg_message_id") or "").split(".")[0] or None
            out.append({"provider_event_id": str(item.get("sg_event_id") or _fallback_id(provider, item)),
                        "kind": kind, "native": native, "bounce_type": bounce,
                        "email": normalize_email(item.get("email")), "provider_message_id": message_id,
                        "occurred_at": _ts(item.get("timestamp")), "raw": dict(item)})
    elif provider == "postmark":
        for item in payload if isinstance(payload, list) else [payload]:
            if not isinstance(item, Mapping):
                continue
            native = str(item.get("RecordType") or ("Inbound" if item.get("FromFull") else ""))
            kind = _POSTMARK.get(native, "unknown")
            bounce = "none"
            if kind == "bounced":
                bounce = "soft" if str(item.get("Type") or "") in _POSTMARK_SOFT else "hard"
            if native == "SubscriptionChange" and not item.get("SuppressSending", True):
                kind = "unknown"  # a re-subscribe is never applied automatically
            email = item.get("Recipient") or item.get("Email")
            if kind == "replied":
                email = (item.get("FromFull") or {}).get("Email") or item.get("From")
            event_id = item.get("ID") or item.get("MessageID") and f"{native}:{item.get('MessageID')}"
            out.append({"provider_event_id": str(event_id or _fallback_id(provider, item)), "kind": kind,
                        "native": native, "bounce_type": bounce, "email": normalize_email(email),
                        "provider_message_id": item.get("MessageID") if kind != "replied" else None,
                        "occurred_at": _ts(item.get("DeliveredAt") or item.get("BouncedAt") or
                                           item.get("ReceivedAt") or item.get("ChangedAt") or item.get("Date")),
                        "raw": dict(item)})
    else:  # generic / manual: already SANA GTM shaped
        items = payload.get("events") if isinstance(payload, Mapping) and "events" in payload else payload
        for item in items if isinstance(items, list) else [items]:
            if not isinstance(item, Mapping):
                continue
            native = str(item.get("type") or item.get("kind") or "")
            aliases = {"reply": "replied", "bounce": "bounced", "unsubscribe": "unsubscribed",
                       "complaint": "complained", "open": "opened", "click": "clicked", "delivery": "delivered",
                       "deferral": "deferred"}
            kind = aliases.get(native, native if native in KINDS else "unknown")
            bounce = str(item.get("bounce_type") or ("hard" if kind == "bounced" else "none"))
            if bounce not in ("hard", "soft", "none"):
                bounce = "hard" if kind == "bounced" else "none"
            out.append({"provider_event_id": str(item.get("id") or _fallback_id(provider, item)), "kind": kind,
                        "native": native, "bounce_type": bounce, "email": normalize_email(item.get("email")),
                        "provider_message_id": item.get("message_id"), "occurred_at": _ts(item.get("timestamp")),
                        "raw": dict(item)})
    return out


class EventService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    @staticmethod
    def capabilities() -> Dict[str, Dict[str, bool]]:
        return {k: dict(v) for k, v in CAPABILITIES.items()}

    def ingest(self, ctx: Ctx, provider: str, payload: Any) -> Dict[str, Any]:
        if provider not in CAPABILITIES:
            raise ValidationError(f"unknown event provider {provider!r}")
        events = normalize(provider, payload)
        stats = {"received": len(events), "applied": 0, "duplicates": 0, "ignored": 0}
        for event in events:
            if event["kind"] == "unknown" or not CAPABILITIES[provider].get(event["kind"], False):
                stats["ignored"] += 1
                continue
            try:
                row = self.store.insert(ctx, "inbound_events", {
                    "provider": provider, "provider_event_id": event["provider_event_id"][:300],
                    "kind": event["kind"], "bounce_type": event["bounce_type"], "email": event["email"],
                    "provider_message_id": (event["provider_message_id"] or None) and
                    str(event["provider_message_id"])[:300],
                    "occurred_at": event["occurred_at"], "raw": _jsonable(event["raw"])})
            except ConflictError:
                stats["duplicates"] += 1
                continue
            try:
                result = self._apply(ctx, provider, event)
                self.store.update(ctx, "inbound_events", row["id"], {"processed": True, "result": result})
                stats["applied"] += 1
            except Exception as error:  # noqa: BLE001 - one bad event must not lose the rest
                log.exception("applying %s event failed", provider)
                self.store.update(ctx, "inbound_events", row["id"], {"result": {"error": str(error)[:500]}})
        return stats

    # --- applying ---------------------------------------------------------------------------

    def _outbound(self, ctx: Ctx, provider_message_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not provider_message_id:
            return None
        pmid = str(provider_message_id)
        row = self.store.first(ctx, "outbound_messages", {"provider_message_id": pmid})
        if row is None and not pmid.startswith("<"):
            row = self.store.first(ctx, "outbound_messages", {"provider_message_id": f"<{pmid}>"})
        return row

    def _apply(self, ctx: Ctx, provider: str, event: Mapping[str, Any]) -> Dict[str, Any]:
        kind, email = event["kind"], event["email"]
        sequences = self.platform.service("sequences")
        outbound = self._outbound(ctx, event.get("provider_message_id"))
        if outbound is not None and not email:
            email = outbound["to_email"]
        stop_kind = None
        if kind == "replied":
            stop_kind = "reply"
        elif kind == "bounced" and event["bounce_type"] == "hard":
            stop_kind = "bounce"
        elif kind in ("unsubscribed", "complained"):
            stop_kind = "unsubscribe"
        if stop_kind is not None:
            result = sequences.handle_event(ctx, stop_kind, email=email,
                                            provider_message_id=(outbound or {}).get("provider_message_id")
                                            or event.get("provider_message_id"),
                                            data={"provider": provider, "native": event.get("native"),
                                                  "bounce_type": event["bounce_type"]})
            precise = {"complained": "complaint", "bounced": "hard_bounce"}.get(kind)
            if precise and email:
                # handle_event suppressed the address generically; record the precise reason.
                row = self.store.first(ctx, "suppressions", {"kind": "email", "value": normalize_email(email)})
                if row is not None and row["reason"] in ("bounce", "unsubscribe"):
                    self.store.update(ctx, "suppressions", row["id"], {"reason": precise})
            if kind == "replied" and result.get("contact_id"):
                self._after_reply(ctx, result["contact_id"], provider)
                try:
                    self.platform.service("signal_outcomes").on_reply(ctx, result["contact_id"],
                                                                      occurred_at=event.get("occurred_at"))
                except Exception:  # noqa: BLE001 - the outcome loop never blocks reply handling
                    log.exception("could not write the reply outcome back to its signal")
            return {"action": stop_kind, **result}
        # Informational events: record on the enrollment's timeline, change nothing else.
        event_name = {"bounced": "bounced", "deferred": "deferred", "delivered": "delivered", "opened": "opened",
                      "clicked": "clicked"}[kind]
        enrollment_id = contact_id = campaign_id = None
        if outbound is not None:
            enrollment_id, contact_id, campaign_id = (outbound.get("enrollment_id"), outbound.get("contact_id"),
                                                      outbound.get("campaign_id"))
        elif email:
            contact = self.store.first(ctx, "contacts", {"email": email})
            contact_id = (contact or {}).get("id")
        self.store.insert(ctx, "message_events", {
            "enrollment_id": enrollment_id, "contact_id": contact_id, "campaign_id": campaign_id,
            "event": event_name, "provider": provider, "normalized_from": str(event.get("native") or "")[:60],
            "provider_message_id": (event.get("provider_message_id") or None) and str(event["provider_message_id"])[:300],
            "mailbox_id": (outbound or {}).get("mailbox_id"), "occurred_at": event["occurred_at"],
            "data": {"bounce_type": event["bounce_type"]} if kind == "bounced" else {}})
        if enrollment_id:
            try:
                self.store.update(ctx, "sequence_enrollments", enrollment_id, {"last_event_at": event["occurred_at"]})
            except Exception:  # noqa: BLE001
                pass
        return {"action": "recorded", "event": event_name}

    def _after_reply(self, ctx: Ctx, contact_id: str, provider: str) -> None:
        contact = self.store.find(ctx, "contacts", contact_id)
        if contact is None:
            return
        settings = {}
        try:
            settings = (self.store.system_membership(ctx.workspace_id) or {}).get("settings") or {}  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            settings = {}
        if (settings.get("sending") or {}).get("task_on_reply", True):
            self.store.insert(ctx, "crm_tasks", {
                "title": f"Reply from {contact['full_name']}"[:300], "status": "open", "priority": "high",
                "description": "A sequence reply was detected; the sequence was stopped. Follow up personally.",
                "due_at": utcnow(), "contact_id": contact_id, "company_id": contact.get("company_id"),
                "source": "reply_detected"})
        try:
            self.platform.service("notifications").notify(
                ctx, title=f"{contact['full_name']} replied", body="Their sequence was stopped automatically.",
                kind="email_reply", link=f"/contacts/{contact_id}", severity="success")
        except Exception:  # noqa: BLE001 - notifications are optional
            log.debug("reply notification skipped", exc_info=True)
        try:  # workflows on "reply_received"; best-effort
            self.platform.service("automation").emit(
                ctx, "reply_received", f"reply:{contact_id}:{utcnow().isoformat(timespec='seconds')}",
                {"contact_id": contact_id, "company_id": contact.get("company_id"), "provider": provider,
                 "email": contact.get("email")})
        except Exception:  # noqa: BLE001
            log.debug("reply_received emit skipped", exc_info=True)

    def record_manual(self, ctx: Ctx, kind: str, *, email: str, note: Optional[str] = None) -> Dict[str, Any]:
        """A signed-in user recording a reply / bounce / unsubscribe they saw themselves."""
        ctx.require_write()
        email_n = normalize_email(email)
        if not email_n:
            raise ValidationError("a valid email is required")
        payload = {"id": f"manual:{kind}:{email_n}:{utcnow().isoformat()}", "type": kind, "email": email_n,
                   "note": note}
        result = self.ingest(ctx, "manual", payload)
        audit(self.store, ctx, f"message.manual_{kind}", summary=f"{kind} recorded for {email_n}")
        return result


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
