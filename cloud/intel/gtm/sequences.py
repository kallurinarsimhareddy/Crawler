"""Sales engagement: templates, sequences, enrollments, suppression, unsubscribe.

**Nothing is sent by default.** A message leaves the platform only when all of
these hold, checked at send time, every time:

1. ``platform.config.allow_email_sending`` — true only in ``production`` with
   ``CAREERCLOUD_ALLOW_EMAIL_SENDING`` set (see :mod:`cloud.intel.bootstrap`);
2. the enrollment's campaign has ``sending_enabled``;
3. the enrollment was explicitly approved by a user (``approved_by`` set);
4. the contact is not unsubscribed, not ``do_not_contact``, has a deliverable
   email, and neither the address nor its domain is suppressed.

Otherwise the step is recorded as a ``blocked`` message event by the
:class:`NullSender`, with the reason. :class:`SmtpSender` is only ever
*constructed* when (1) holds.

Enrollments always start in ``pending_approval``. Approval is an explicit,
audited user action; automation can enroll but never approve.

Unsubscribe links carry an HMAC-signed token (workspace + contact). Without a
server secret the platform refuses to render them — and a template that needs
``{{unsubscribe.url}}`` then fails to render rather than going out without one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cloud.intel.core.audit import audit, record_activity
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow
from cloud.intel.core.normalize import domain_of, normalize_email

__all__ = [
    "NullSender",
    "OutboundMessage",
    "SenderProvider",
    "SequenceService",
    "SmtpSender",
    "TemplateError",
    "render_template",
]

log = logging.getLogger(__name__)

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*\}\}")
_UNDELIVERABLE = frozenset({"INVALID", "DISPOSABLE"})
_TERMINAL = frozenset({"completed", "replied", "bounced", "unsubscribed", "suppressed", "stopped"})


class TemplateError(ValidationError):
    """A template references a variable that has no value. Never rendered blank."""


def template_variables(text: str) -> List[str]:
    return sorted({m.group(1) for m in _VAR.finditer(text or "")})


def render_template(text: str, variables: Mapping[str, Any]) -> str:
    """Replace ``{{a.b}}`` with ``variables["a"]["b"]``. A missing or empty value raises."""
    missing: List[str] = []

    def lookup(match: "re.Match[str]") -> str:
        value: Any = variables
        for part in match.group(1).split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
            if value is None:
                break
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(match.group(1))
            return ""
        return str(value)

    rendered = _VAR.sub(lookup, text or "")
    if missing:
        raise TemplateError(f"missing template variables: {', '.join(sorted(set(missing)))}")
    return rendered


# --- senders ---------------------------------------------------------------------


class OutboundMessage(dict):
    """``to``, ``subject``, ``body``, ``headers``, ``campaign_id``, ``enrollment_id``."""


class SenderProvider(ABC):
    name = "sender"
    #: Whether this provider actually delivers mail.
    delivers = False

    @abstractmethod
    def send(self, message: OutboundMessage) -> Dict[str, Any]:
        """Return ``{"event": "sent"|"blocked"|"failed", "provider_message_id", "detail"}``."""


class NullSender(SenderProvider):
    """The default everywhere: records what *would* have been sent, sends nothing."""

    name = "null"

    def __init__(self, reason: str = "email sending is disabled in this environment") -> None:
        self.reason = reason
        self.outbox: List[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> Dict[str, Any]:
        self.outbox.append(message)
        return {"event": "blocked", "provider_message_id": None, "detail": self.reason}


class SmtpSender(SenderProvider):
    """Authenticated SMTP with STARTTLS. Constructed only when sending is allowed.

    Credentials come from the server environment (``CAREERCLOUD_SMTP_*``), never
    from the browser or the database in plaintext.
    """

    name = "smtp"
    delivers = True

    def __init__(self, *, host: str, port: int = 587, username: str = "", password: str = "",
                 from_address: str) -> None:
        if not host or not from_address:
            raise ValidationError("SMTP host and from address are required")
        self.host, self.port, self.username, self.password, self.from_address = (
            host, port, username, password, from_address)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SmtpSender":
        env = env if env is not None else os.environ
        return cls(host=env.get("CAREERCLOUD_SMTP_HOST", ""), port=int(env.get("CAREERCLOUD_SMTP_PORT", "587")),
                   username=env.get("CAREERCLOUD_SMTP_USER", ""), password=env.get("CAREERCLOUD_SMTP_PASSWORD", ""),
                   from_address=env.get("CAREERCLOUD_SMTP_FROM", ""))

    def send(self, message: OutboundMessage) -> Dict[str, Any]:  # pragma: no cover - never run in tests
        import smtplib
        import uuid
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = self.from_address
        msg["To"] = message["to"]
        msg["Subject"] = message["subject"]
        message_id = f"<{uuid.uuid4().hex}@careercrawler>"
        msg["Message-ID"] = message_id
        for key, value in (message.get("headers") or {}).items():
            msg[key] = value
        msg.set_content(message["body"])
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(msg)
            return {"event": "sent", "provider_message_id": message_id, "detail": None}
        except smtplib.SMTPException as error:
            return {"event": "failed", "provider_message_id": None, "detail": str(error)[:500]}


# --- unsubscribe tokens ------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SequenceService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store
        #: Tests (or a production wiring step) may replace this.
        self.sender_override: Optional[SenderProvider] = None

    # --- secrets / unsubscribe ----------------------------------------------------

    def _unsubscribe_secret(self) -> Optional[bytes]:
        secret = os.environ.get("CAREERCLOUD_UNSUBSCRIBE_SECRET") or self.platform.config.secrets_key
        return secret.encode() if secret else None

    def unsubscribe_token(self, ctx: Ctx, contact_id: str) -> str:
        secret = self._unsubscribe_secret()
        if not secret:
            raise ValidationError("unsubscribe links need CAREERCLOUD_UNSUBSCRIBE_SECRET; refusing to render one")
        payload = f"{ctx.workspace_id}.{contact_id}".encode()
        sig = hmac.new(secret, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(sig)}"

    def verify_unsubscribe_token(self, token: str) -> Tuple[str, str]:
        secret = self._unsubscribe_secret()
        if not secret:
            raise ValidationError("unsubscribe is not configured")
        try:
            payload_b64, sig_b64 = token.split(".", 1)
            payload = _unb64(payload_b64)
            sig = _unb64(sig_b64)
        except (ValueError, TypeError):
            raise ValidationError("invalid unsubscribe link") from None
        if not hmac.compare_digest(hmac.new(secret, payload, hashlib.sha256).digest(), sig):
            raise ValidationError("invalid unsubscribe link")
        workspace_id, _, contact_id = payload.decode().partition(".")
        if not workspace_id or not contact_id:
            raise ValidationError("invalid unsubscribe link")
        return workspace_id, contact_id

    def unsubscribe_url(self, ctx: Ctx, contact_id: str) -> str:
        base = os.environ.get("CAREERCLOUD_PUBLIC_API_URL", "").rstrip("/")
        return f"{base}/api/v1/unsubscribe/{self.unsubscribe_token(ctx, contact_id)}"

    # --- suppression ----------------------------------------------------------------

    def add_suppression(self, ctx: Ctx, value: str, *, kind: str = "email", reason: str = "manual",
                        source: Optional[str] = None) -> Dict[str, Any]:
        value = (normalize_email(value) if kind == "email" else domain_of(value)) or ""
        if not value:
            raise ValidationError(f"not a valid {kind}")
        existing = self.store.first(ctx, "suppressions", {"kind": kind, "value": value})
        if existing is not None:
            return existing
        try:
            row = self.store.insert(ctx, "suppressions", {"value": value, "kind": kind, "reason": reason,
                                                          "source": source})
        except ConflictError:
            return self.store.first(ctx, "suppressions", {"kind": kind, "value": value})
        audit(self.store, ctx, "suppression.add", entity_type="suppressions", entity_id=row["id"],
              changes={"value": value, "kind": kind, "reason": reason})
        return row

    def suppression_for(self, ctx: Ctx, email: Optional[str]) -> Optional[Dict[str, Any]]:
        email = normalize_email(email)
        if not email:
            return None
        hit = self.store.first(ctx, "suppressions", {"kind": "email", "value": email})
        if hit is None:
            domain = email.rpartition("@")[2]
            hit = self.store.first(ctx, "suppressions", {"kind": "domain", "value": domain})
            if hit is None and domain_of(domain) and domain_of(domain) != domain:
                hit = self.store.first(ctx, "suppressions", {"kind": "domain", "value": domain_of(domain)})
        return hit

    def contact_block_reason(self, ctx: Ctx, contact: Mapping[str, Any]) -> Optional[str]:
        """Why this contact must not be emailed, or None."""
        if contact.get("unsubscribed"):
            return "unsubscribed"
        if contact.get("status") in ("do_not_contact", "left_company", "archived", "merged"):
            return f"contact status {contact.get('status')}"
        email = normalize_email(contact.get("email"))
        if not email:
            return "no valid email address"
        if contact.get("email_status") in _UNDELIVERABLE:
            return f"email status {contact.get('email_status')}"
        hit = self.suppression_for(ctx, email)
        if hit is not None:
            return f"suppressed ({hit['kind']} {hit['value']}: {hit['reason']})"
        return None

    # --- sequences & steps -----------------------------------------------------------

    def steps(self, ctx: Ctx, sequence_id: str) -> List[Dict[str, Any]]:
        return self.store.all(ctx, "sequence_steps", {"sequence_id": sequence_id}, order="position")

    def add_step(self, ctx: Ctx, sequence_id: str, *, channel: str = "email", delay_days: int = 0,
                 template_id: Optional[str] = None, instructions: Optional[str] = None,
                 position: Optional[int] = None) -> Dict[str, Any]:
        self.store.get(ctx, "sequences", sequence_id)
        if channel == "email":
            if not template_id:
                raise ValidationError("an email step needs a template")
            self.store.get(ctx, "email_templates", template_id)
        if position is None:
            position = len(self.steps(ctx, sequence_id))
        row = self.store.insert(ctx, "sequence_steps", {"sequence_id": sequence_id, "position": position,
                                                        "channel": channel, "delay_days": delay_days,
                                                        "template_id": template_id, "instructions": instructions})
        audit(self.store, ctx, "sequence.add_step", entity_type="sequence_steps", entity_id=row["id"])
        return row

    def create_template(self, ctx: Ctx, *, name: str, subject: str, body: str,
                        campaign_id: Optional[str] = None) -> Dict[str, Any]:
        variables = sorted(set(template_variables(subject)) | set(template_variables(body)))
        row = self.store.insert(ctx, "email_templates", {"name": name, "subject": subject, "body": body,
                                                         "variables": variables, "campaign_id": campaign_id})
        audit(self.store, ctx, "template.create", entity_type="email_templates", entity_id=row["id"])
        return row

    # --- variables & rendering ----------------------------------------------------------

    def variables_for(self, ctx: Ctx, contact: Mapping[str, Any], *, enrollment: Optional[Mapping[str, Any]] = None,
                      extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        company = None
        if contact.get("company_id"):
            company = self.store.find(ctx, "companies", contact["company_id"])
        signal = job = None
        if company is not None:
            signal = self.store.first(ctx, "hiring_signals", {"company_id": company["id"], "status": "active"},
                                      order="-detected_at")
            job = (self.store.first(ctx, "job_postings", {"company_id": company["id"], "status": "open",
                                                          "is_relevant": True}, order="-first_seen_at")
                   or self.store.first(ctx, "job_postings", {"company_id": company["id"], "status": "open"},
                                       order="-first_seen_at"))
        membership = None
        try:
            membership = self.store.system_membership(ctx.workspace_id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            membership = None
        settings = (membership or {}).get("settings") or {}
        variables: Dict[str, Any] = {
            "contact": {"full_name": contact.get("full_name"), "first_name": contact.get("first_name"),
                        "last_name": contact.get("last_name"), "title": contact.get("title"),
                        "email": contact.get("email")},
            "company": ({"name": company.get("name"), "domain": company.get("domain"),
                         "industry": company.get("industry"), "city": company.get("city"),
                         "state": company.get("state")} if company else {}),
            "signal": ({"summary": signal.get("summary"), "type": signal.get("signal_type")} if signal else {}),
            "job": ({"title": job.get("title"), "url": job.get("job_url"), "location": job.get("location")}
                    if job else {}),
            "sender": dict(settings.get("sender") or {}),
        }
        if self._unsubscribe_secret() and contact.get("id"):
            variables["unsubscribe"] = {"url": self.unsubscribe_url(ctx, contact["id"])}
        for source in ((enrollment or {}).get("variables") or {}, extra or {}):
            for key, value in source.items():
                if isinstance(value, Mapping) and isinstance(variables.get(key), dict):
                    variables[key] = {**variables[key], **value}
                else:
                    variables[key] = value
        return variables

    def preview(self, ctx: Ctx, template_id: str, contact_id: str,
                extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        template = self.store.get(ctx, "email_templates", template_id)
        contact = self.store.get(ctx, "contacts", contact_id)
        variables = self.variables_for(ctx, contact, extra=extra)
        return {"subject": render_template(template["subject"], variables),
                "body": render_template(template["body"], variables),
                "to": contact.get("email"), "block_reason": self.contact_block_reason(ctx, contact)}

    # --- enrollment -----------------------------------------------------------------------

    def enroll(self, ctx: Ctx, sequence_id: str, contact_ids: Iterable[str], *, campaign_id: Optional[str] = None,
               opportunity_id: Optional[str] = None, variables: Optional[Mapping[str, Any]] = None
               ) -> List[Dict[str, Any]]:
        """Create ``pending_approval`` enrollments. Returns one result per contact:
        ``{"contact_id", "status": "enrolled"|"skipped", "reason", "enrollment"}``."""
        ctx.require_write()
        sequence = self.store.get(ctx, "sequences", sequence_id)
        if sequence["status"] == "archived":
            raise ValidationError("cannot enroll into an archived sequence")
        campaign_id = campaign_id or sequence.get("campaign_id")
        results = []
        for contact_id in contact_ids:
            contact = self.store.find(ctx, "contacts", contact_id)
            if contact is None:
                results.append({"contact_id": contact_id, "status": "skipped", "reason": "contact not found"})
                continue
            reason = self.contact_block_reason(ctx, contact)
            if reason is not None:
                results.append({"contact_id": contact_id, "status": "skipped", "reason": reason})
                continue
            try:
                row = self.store.insert(ctx, "sequence_enrollments", {
                    "sequence_id": sequence_id, "contact_id": contact_id, "campaign_id": campaign_id,
                    "opportunity_id": opportunity_id, "status": "pending_approval",
                    "variables": dict(variables or {})})
            except ConflictError:
                results.append({"contact_id": contact_id, "status": "skipped", "reason": "already enrolled"})
                continue
            audit(self.store, ctx, "sequence.enroll", entity_type="sequence_enrollments", entity_id=row["id"],
                  changes={"sequence_id": sequence_id, "contact_id": contact_id})
            results.append({"contact_id": contact_id, "status": "enrolled", "reason": None, "enrollment": row})
        return results

    def approve_enrollments(self, ctx: Ctx, enrollment_ids: Iterable[str]) -> List[Dict[str, Any]]:
        """The explicit human step. Only a signed-in writer can approve; automation never does."""
        ctx.require_write()
        if ctx.system or ctx.user_id is None:
            raise ValidationError("enrollments must be approved by a signed-in user")
        now = utcnow()
        approved = []
        for enrollment_id in enrollment_ids:
            enrollment = self.store.get(ctx, "sequence_enrollments", enrollment_id)
            if enrollment["status"] != "pending_approval":
                continue
            steps = self.steps(ctx, enrollment["sequence_id"])
            first_delay = steps[0]["delay_days"] if steps else 0
            row = self.store.update(ctx, "sequence_enrollments", enrollment_id, {
                "status": "active", "approved_by": ctx.user_id, "approved_at": now, "current_step": 0,
                "next_step_at": now + timedelta(days=first_delay)}, expected_version=enrollment["version"])
            audit(self.store, ctx, "sequence.approve", entity_type="sequence_enrollments", entity_id=enrollment_id)
            approved.append(row)
        return approved

    def stop_enrollment(self, ctx: Ctx, enrollment_id: str, status: str = "stopped") -> Dict[str, Any]:
        row = self.store.update(ctx, "sequence_enrollments", enrollment_id, {"status": status, "next_step_at": None})
        audit(self.store, ctx, f"sequence.{status}", entity_type="sequence_enrollments", entity_id=enrollment_id)
        return row

    # --- sending -----------------------------------------------------------------------------

    def _sender_for(self, campaign: Optional[Mapping[str, Any]]) -> Tuple[SenderProvider, Optional[str]]:
        """Pick the sender, and say why mail is blocked when it is."""
        if not self.platform.config.allow_email_sending:
            return NullSender("email sending is disabled in this environment"), "sending disabled"
        if campaign is None or not campaign.get("sending_enabled"):
            return NullSender("the campaign does not have sending enabled"), "campaign sending disabled"
        if self.sender_override is not None:
            return self.sender_override, None
        return SmtpSender.from_env(), None

    def _event(self, ctx: Ctx, enrollment: Mapping[str, Any], event: str, *, provider: Optional[str] = None,
               provider_message_id: Optional[str] = None, data: Optional[Mapping[str, Any]] = None
               ) -> Dict[str, Any]:
        return self.store.insert(ctx, "message_events", {
            "enrollment_id": enrollment.get("id"), "contact_id": enrollment.get("contact_id"),
            "campaign_id": enrollment.get("campaign_id"), "event": event, "provider": provider,
            "provider_message_id": provider_message_id, "occurred_at": utcnow(), "data": dict(data or {})})

    def process_due(self, ctx: Ctx, now: Optional[datetime] = None, *, limit: int = 200) -> Dict[str, Any]:
        """Advance active, approved enrollments whose next step is due."""
        now = now or utcnow()
        stats = {"processed": 0, "sent": 0, "blocked": 0, "failed": 0, "tasks": 0, "completed": 0, "stopped": 0}
        due = self.store.list(ctx, "sequence_enrollments", {"status": "active", "next_step_at__lte": now},
                              order="next_step_at", limit=limit).rows
        for enrollment in due:
            stats["processed"] += 1
            try:
                self._advance(ctx, enrollment, now, stats)
            except Exception as error:  # noqa: BLE001 - one bad enrollment must not stop the rest
                log.exception("enrollment %s failed", enrollment["id"])
                self._event(ctx, enrollment, "failed", data={"error": str(error)[:500]})
                stats["failed"] += 1
        return stats

    def _advance(self, ctx: Ctx, enrollment: Dict[str, Any], now: datetime, stats: Dict[str, int]) -> None:
        if not enrollment.get("approved_by"):
            self.store.update(ctx, "sequence_enrollments", enrollment["id"],
                              {"status": "pending_approval", "next_step_at": None})
            stats["stopped"] += 1
            return
        steps = self.steps(ctx, enrollment["sequence_id"])
        position = enrollment["current_step"]
        if position >= len(steps):
            self.store.update(ctx, "sequence_enrollments", enrollment["id"], {"status": "completed",
                                                                              "next_step_at": None})
            stats["completed"] += 1
            return
        step = steps[position]
        contact = self.store.get(ctx, "contacts", enrollment["contact_id"])
        campaign = self.store.find(ctx, "campaigns", enrollment["campaign_id"]) if enrollment.get("campaign_id") \
            else None

        if step["channel"] == "email":
            reason = self.contact_block_reason(ctx, contact)
            if reason is not None:
                status = "unsubscribed" if reason == "unsubscribed" else "suppressed"
                self._event(ctx, enrollment, "blocked", data={"reason": reason, "step": position})
                self.store.update(ctx, "sequence_enrollments", enrollment["id"], {"status": status,
                                                                                  "next_step_at": None})
                stats["blocked"] += 1
                stats["stopped"] += 1
                return
            template = self.store.get(ctx, "email_templates", step["template_id"])
            variables = self.variables_for(ctx, contact, enrollment=enrollment)
            try:
                subject = render_template(template["subject"], variables)
                body = render_template(template["body"], variables)
            except TemplateError as error:
                self._event(ctx, enrollment, "failed", data={"reason": str(error), "step": position})
                self.store.update(ctx, "sequence_enrollments", enrollment["id"], {"status": "paused",
                                                                                  "next_step_at": None})
                stats["failed"] += 1
                return
            self._event(ctx, enrollment, "rendered", data={"step": position, "subject": subject[:300]})
            sender, blocked_reason = self._sender_for(campaign)
            headers = {}
            if "unsubscribe" in variables:
                headers["List-Unsubscribe"] = f"<{variables['unsubscribe']['url']}>"
            outcome = sender.send(OutboundMessage(to=contact["email"], subject=subject, body=body, headers=headers,
                                                  campaign_id=enrollment.get("campaign_id"),
                                                  enrollment_id=enrollment["id"]))
            self._event(ctx, enrollment, outcome["event"], provider=sender.name,
                        provider_message_id=outcome.get("provider_message_id"),
                        data={"step": position, "detail": outcome.get("detail") or blocked_reason})
            stats[outcome["event"] if outcome["event"] in stats else "failed"] += 1
            if outcome["event"] == "sent":
                record_activity(self.store, ctx, "email_sent", f"Sequence email: {subject[:200]}",
                                contact_id=contact["id"], company_id=contact.get("company_id"),
                                campaign_id=enrollment.get("campaign_id"))
            elif outcome["event"] == "blocked":
                # Do not advance: nothing was delivered. The enrollment waits until
                # sending is allowed; it is re-attempted on the next due check.
                self.store.update(ctx, "sequence_enrollments", enrollment["id"],
                                  {"next_step_at": now + timedelta(days=1)})
                return
            elif outcome["event"] == "failed":
                self.store.update(ctx, "sequence_enrollments", enrollment["id"],
                                  {"next_step_at": now + timedelta(hours=6)})
                return
        else:
            self.store.insert(ctx, "crm_tasks", {
                "title": f"{step['channel'].replace('_', ' ').title()}: {contact['full_name']}"[:300],
                "description": step.get("instructions"), "status": "open", "priority": "normal",
                "due_at": now, "contact_id": contact["id"], "company_id": contact.get("company_id"),
                "source": "sequence"})
            stats["tasks"] += 1
        next_position = position + 1
        changes: Dict[str, Any] = {"current_step": next_position}
        if next_position >= len(steps):
            changes.update({"status": "completed", "next_step_at": None})
            stats["completed"] += 1
        else:
            changes["next_step_at"] = now + timedelta(days=steps[next_position]["delay_days"])
        self.store.update(ctx, "sequence_enrollments", enrollment["id"], changes)

    # --- inbound events -------------------------------------------------------------------------

    def _find_enrollments(self, ctx: Ctx, email: Optional[str], provider_message_id: Optional[str]
                          ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        contact = None
        enrollments: List[Dict[str, Any]] = []
        if provider_message_id:
            event = self.store.first(ctx, "message_events", {"provider_message_id": provider_message_id})
            if event is not None and event.get("contact_id"):
                contact = self.store.find(ctx, "contacts", event["contact_id"])
        if contact is None and email:
            normalized = normalize_email(email)
            if normalized:
                contact = self.store.first(ctx, "contacts", {"email": normalized})
        if contact is not None:
            enrollments = self.store.all(ctx, "sequence_enrollments", {"contact_id": contact["id"]}, cap=200)
        return contact, enrollments

    def handle_event(self, ctx: Ctx, kind: str, *, email: Optional[str] = None,
                     provider_message_id: Optional[str] = None, data: Optional[Mapping[str, Any]] = None
                     ) -> Dict[str, Any]:
        """Apply an inbound reply / bounce / unsubscribe."""
        if kind not in ("reply", "bounce", "unsubscribe"):
            raise ValidationError("kind must be reply, bounce or unsubscribe")
        contact, enrollments = self._find_enrollments(ctx, email, provider_message_id)
        target_email = normalize_email(email) or (contact or {}).get("email")
        changed = []
        event_name = {"reply": "replied", "bounce": "bounced", "unsubscribe": "unsubscribed"}[kind]
        for enrollment in enrollments:
            self._event(ctx, enrollment, event_name, provider_message_id=provider_message_id, data=data)
            if enrollment["status"] in _TERMINAL and kind == "reply":
                continue
            sequence = self.store.find(ctx, "sequences", enrollment["sequence_id"])
            if kind == "reply" and sequence is not None and not sequence.get("stop_on_reply"):
                continue
            if enrollment["status"] in ("unsubscribed",) and kind != "unsubscribe":
                continue
            changed.append(self.store.update(ctx, "sequence_enrollments", enrollment["id"],
                                             {"status": event_name, "next_step_at": None}))
        if contact is not None:
            if kind == "reply":
                record_activity(self.store, ctx, "email_reply", f"{contact['full_name']} replied",
                                contact_id=contact["id"], company_id=contact.get("company_id"), data=dict(data or {}))
            elif kind == "bounce":
                self.store.update(ctx, "contacts", contact["id"], {"email_status": "INVALID"})
                record_activity(self.store, ctx, "email_bounce", f"Email to {contact.get('email')} bounced",
                                contact_id=contact["id"], company_id=contact.get("company_id"))
            elif kind == "unsubscribe":
                self.store.update(ctx, "contacts", contact["id"], {"unsubscribed": True})
                record_activity(self.store, ctx, "unsubscribed", f"{contact['full_name']} unsubscribed",
                                contact_id=contact["id"], company_id=contact.get("company_id"))
        if target_email and kind in ("bounce", "unsubscribe"):
            self.add_suppression(ctx, target_email, kind="email", reason=kind, source="inbound_event")
        audit(self.store, ctx, f"message.{kind}", entity_type="contacts", entity_id=(contact or {}).get("id"),
              changes={"email": target_email, "enrollments": [e["id"] for e in changed]})
        return {"contact_id": (contact or {}).get("id"), "enrollments_updated": len(changed),
                "suppressed": bool(target_email and kind in ("bounce", "unsubscribe"))}

    def unsubscribe_by_token(self, token: str) -> Dict[str, Any]:
        """Public unsubscribe: the token proves workspace + contact; no sign-in."""
        workspace_id, contact_id = self.verify_unsubscribe_token(token)
        ctx = Ctx.for_system(workspace_id)
        contact = self.store.find(ctx, "contacts", contact_id)
        if contact is None:
            raise NotFoundError("unknown recipient")
        if contact.get("unsubscribed"):
            return {"unsubscribed": True, "already": True}
        self.handle_event(ctx, "unsubscribe", email=contact.get("email"))
        if not contact.get("email"):
            self.store.update(ctx, "contacts", contact_id, {"unsubscribed": True})
        return {"unsubscribed": True, "already": False}
