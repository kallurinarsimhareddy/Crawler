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

Every send is also queued in ``outbound_messages`` (what was rendered, through
which mailbox, and what happened), and respects the campaign/sequence schedule
window, daily cap, and each mailbox's daily/hourly limit. A sequence stops on
reply (configurable), and always on unsubscribe, hard bounce, complaint,
suppression, a disabled contact, or a stopped (archived) campaign.

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
_TERMINAL = frozenset({"completed", "replied", "bounced", "unsubscribed", "suppressed", "stopped", "failed"})
_DISABLED_STATUSES = ("do_not_contact", "left_company", "archived", "merged")
#: Stop conditions. Compliance ones are always on and cannot be switched off.
STOP_CONDITIONS = ("reply", "unsubscribe", "bounce", "contact_disabled", "campaign_stopped", "suppressed")
LOCKED_STOP_CONDITIONS = frozenset({"unsubscribe", "bounce", "suppressed"})
#: The default cadence: Day 1 initial, Day 3 follow-up, Day 6 follow-up, Day 10 final.
DEFAULT_CADENCE = ((1, "initial"), (3, "follow_up"), (6, "follow_up"), (10, "final"))


def stop_conditions(sequence: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
    """The effective stop conditions of a sequence (defaults on; compliance always on)."""
    sequence = sequence or {}
    out = {name: True for name in STOP_CONDITIONS}
    if sequence.get("stop_on_reply") is False:
        out["reply"] = False
    for name, value in (sequence.get("stop_conditions") or {}).items():
        if name in out and name not in LOCKED_STOP_CONDITIONS:
            out[name] = bool(value)
    return out


def cadence_days(delays: Sequence[int]) -> List[int]:
    """Step delays (days after the previous step) -> the day each step runs, Day 1 first."""
    day, out = 1, []
    for i, delay in enumerate(delays):
        day = 1 + int(delay) if i == 0 else day + int(delay)
        out.append(day)
    return out


def next_send_window(now: datetime, schedule: Optional[Mapping[str, Any]]) -> Optional[datetime]:
    """None when ``now`` is inside the schedule window, else the next window start.

    ``schedule``: ``{"timezone": "America/New_York", "days": [1..7 ISO weekdays],
    "start_hour": 9, "end_hour": 17}``. An empty schedule means "any time".
    """
    schedule = schedule or {}
    days = [int(d) for d in schedule.get("days") or []] or list(range(1, 8))
    start = int(schedule.get("start_hour", 0) or 0)
    end = int(schedule.get("end_hour", 24) or 24)
    if not schedule or (len(days) == 7 and start <= 0 and end >= 24):
        return None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(str(schedule.get("timezone") or "UTC"))
    except Exception:  # noqa: BLE001 - unknown zone: fall back to UTC rather than never sending
        from datetime import timezone as _tz

        tz = _tz.utc
    local = now.astimezone(tz)
    if local.isoweekday() in days and start <= local.hour < end:
        return None
    for offset in range(0, 8):
        day = (local + timedelta(days=offset)).replace(hour=start, minute=0, second=0, microsecond=0)
        if day.isoweekday() in days and day > local:
            return day.astimezone(now.tzinfo or tz)
    return None


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
                        source: Optional[str] = None, scope: str = "workspace",
                        campaign_id: Optional[str] = None) -> Dict[str, Any]:
        """Suppress an address or domain (see :mod:`cloud.intel.gtm.suppression`)."""
        return self.platform.service("suppression").add(ctx, value, kind=kind, reason=reason, source=source,
                                                        scope=scope, campaign_id=campaign_id)

    def suppression_for(self, ctx: Ctx, email: Optional[str], *,
                        campaign_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self.platform.service("suppression").check(ctx, email, campaign_id=campaign_id)

    def contact_block_reason(self, ctx: Ctx, contact: Mapping[str, Any], *,
                             campaign_id: Optional[str] = None) -> Optional[str]:
        """Why this contact must not be emailed, or None. Checked at enrollment and before every send."""
        if contact.get("unsubscribed"):
            return "unsubscribed"
        if contact.get("status") in _DISABLED_STATUSES:
            return f"contact status {contact.get('status')}"
        email = normalize_email(contact.get("email"))
        if not email:
            return "no valid email address"
        if contact.get("email_status") in _UNDELIVERABLE:
            return f"email status {contact.get('email_status')}"
        hit = self.suppression_for(ctx, email, campaign_id=campaign_id)
        if hit is not None:
            scope = "" if hit.get("scope", "workspace") == "workspace" else f"{hit['scope']} "
            return f"suppressed ({scope}{hit['kind']} {hit['value']}: {hit['reason']})"
        return None

    @staticmethod
    def _block_status(reason: str) -> str:
        if reason == "unsubscribed":
            return "unsubscribed"
        if reason.startswith("suppressed"):
            return "suppressed"
        if reason.startswith("email status"):
            return "bounced" if "INVALID" in reason else "suppressed"
        return "stopped"

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
            reason = self.contact_block_reason(ctx, contact, campaign_id=campaign_id)
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

    def stop_enrollment(self, ctx: Ctx, enrollment_id: str, status: str = "stopped",
                        reason: Optional[str] = None) -> Dict[str, Any]:
        row = self.store.update(ctx, "sequence_enrollments", enrollment_id, {
            "status": status, "next_step_at": None, "stop_reason": (reason or "stopped by a user")[:300]})
        self._cancel_queued(ctx, enrollment_id, reason or status)
        audit(self.store, ctx, f"sequence.{status}", entity_type="sequence_enrollments", entity_id=enrollment_id)
        return row

    def _cancel_queued(self, ctx: Ctx, enrollment_id: str, reason: str) -> int:
        cancelled = 0
        for row in self.store.all(ctx, "outbound_messages", {"enrollment_id": enrollment_id,
                                                             "status__in": ["queued", "scheduled"]}, cap=100):
            self.store.update(ctx, "outbound_messages", row["id"], {"status": "cancelled",
                                                                    "block_reason": reason[:500]})
            cancelled += 1
        return cancelled

    # --- step editor -----------------------------------------------------------------------

    def save_steps(self, ctx: Ctx, sequence_id: str, steps: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Replace a sequence's steps in the given order (the step editor's Save).

        Each step: ``channel`` (email/wait/task/call/linkedin_task), ``delay_days``,
        ``step_type`` (initial/follow_up/final/wait/task), ``template_id`` (email),
        ``subject_override``, ``instructions``, ``condition``.
        """
        ctx.require_write()
        sequence = self.store.get(ctx, "sequences", sequence_id)
        if sequence["status"] == "archived":
            raise ValidationError("an archived sequence cannot be edited")
        if len(steps) > 30:
            raise ValidationError("a sequence can have at most 30 steps")
        clean: List[Dict[str, Any]] = []
        for i, step in enumerate(steps):
            channel = str(step.get("channel") or "email")
            if channel not in ("email", "wait", "task", "call", "linkedin_task"):
                raise ValidationError(f"step {i + 1}: unknown channel {channel!r}")
            template_id = step.get("template_id") or None
            if channel == "email":
                if not template_id:
                    raise ValidationError(f"step {i + 1}: an email step needs a template")
                self.store.get(ctx, "email_templates", template_id)
            default_type = ("wait" if channel == "wait" else "task" if channel != "email"
                            else "initial" if i == 0 else "follow_up")
            step_type = str(step.get("step_type") or default_type)
            if step_type not in ("initial", "follow_up", "final", "wait", "task"):
                raise ValidationError(f"step {i + 1}: unknown step type {step_type!r}")
            delay = int(step.get("delay_days") or 0)
            if not 0 <= delay <= 365:
                raise ValidationError(f"step {i + 1}: delay must be 0-365 days")
            clean.append({"sequence_id": sequence_id, "position": i, "channel": channel, "delay_days": delay,
                          "template_id": template_id if channel == "email" else None, "step_type": step_type,
                          "subject_override": step.get("subject_override") or None,
                          "instructions": step.get("instructions") or None,
                          "condition": dict(step.get("condition") or {})})
        for old in self.steps(ctx, sequence_id):
            self.store.delete(ctx, "sequence_steps", old["id"])
        rows = [self.store.insert(ctx, "sequence_steps", values) for values in clean]
        audit(self.store, ctx, "sequence.save_steps", entity_type="sequences", entity_id=sequence_id,
              changes={"steps": len(rows), "days": cadence_days([r["delay_days"] for r in rows])})
        return rows

    def apply_cadence(self, ctx: Ctx, sequence_id: str, template_ids: Sequence[str],
                      days: Sequence[int] = tuple(d for d, _ in DEFAULT_CADENCE)) -> List[Dict[str, Any]]:
        """Email steps on the given days (default Day 1, 3, 6, 10); templates are reused when fewer."""
        if not template_ids:
            raise ValidationError("choose at least one template")
        days = [int(d) for d in days]
        if not days or days != sorted(days) or days[0] < 1:
            raise ValidationError("days must be increasing and start at 1 or later")
        steps, previous = [], days[0]
        for i, day in enumerate(days):
            steps.append({"channel": "email", "template_id": template_ids[min(i, len(template_ids) - 1)],
                          "delay_days": day - 1 if i == 0 else day - previous,
                          "step_type": "initial" if i == 0 else ("final" if i == len(days) - 1 else "follow_up"),
                          "condition": {"only_if_no_reply": i > 0}})
            previous = day
        return self.save_steps(ctx, sequence_id, steps)

    def update_stop_conditions(self, ctx: Ctx, sequence_id: str, conditions: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_write()
        sequence = self.store.get(ctx, "sequences", sequence_id)
        clean = {k: bool(v) for k, v in conditions.items() if k in STOP_CONDITIONS and k not in LOCKED_STOP_CONDITIONS}
        merged = {**(sequence.get("stop_conditions") or {}), **clean}
        changes: Dict[str, Any] = {"stop_conditions": merged}
        if "reply" in clean:
            changes["stop_on_reply"] = clean["reply"]
        row = self.store.update(ctx, "sequences", sequence_id, changes)
        audit(self.store, ctx, "sequence.stop_conditions", entity_type="sequences", entity_id=sequence_id,
              changes=clean)
        return row

    def overview(self, ctx: Ctx, sequence_id: str) -> Dict[str, Any]:
        sequence = self.store.get(ctx, "sequences", sequence_id)
        steps = self.steps(ctx, sequence_id)
        enrollments = self.store.group_count(ctx, "sequence_enrollments", "status", {"sequence_id": sequence_id})
        ids = [e["id"] for e in self.store.all(ctx, "sequence_enrollments", {"sequence_id": sequence_id}, cap=5000)]
        events = self.store.group_count(ctx, "message_events", "event", {"enrollment_id__in": ids}) if ids else {}
        return {"sequence": sequence, "steps": steps, "days": cadence_days([s["delay_days"] for s in steps]),
                "stop_conditions": stop_conditions(sequence),
                "locked_stop_conditions": sorted(LOCKED_STOP_CONDITIONS),
                "enrollments": {str(k): v for k, v in enrollments.items()},
                "events": {str(k): v for k, v in events.items()}}

    # --- sending -----------------------------------------------------------------------------

    def _sender_for(self, campaign: Optional[Mapping[str, Any]], ctx: Optional[Ctx] = None,
                    enrollment: Optional[Mapping[str, Any]] = None, now: Optional[datetime] = None
                    ) -> Tuple[SenderProvider, Optional[str]]:
        """Pick the sender, and say why mail is blocked when it is.

        Gates first (environment, campaign); then a test override; then a connected
        mailbox (the enrollment's, the campaign's senders, or the default); and only
        when no mailbox is connected at all, the server's SMTP relay.
        """
        self._last_mailbox = None
        if not self.platform.config.allow_email_sending:
            return NullSender("email sending is disabled in this environment"), "sending disabled"
        if campaign is None or not campaign.get("sending_enabled"):
            return NullSender("the campaign does not have sending enabled"), "campaign sending disabled"
        if self.sender_override is not None:
            return self.sender_override, None
        if ctx is not None:
            wants = (enrollment or {}).get("mailbox_id")
            has_any = self.store.count(ctx, "mailboxes", {"status": "connected"}) > 0
            if has_any or wants or campaign.get("mailbox_ids"):
                mailboxes = self.platform.service("mailboxes")
                mailbox, reason = mailboxes.pick(ctx, campaign, mailbox_id=wants, now=now)
                if mailbox is None:
                    return NullSender(reason), "deferred:" + reason
                self._last_mailbox = mailbox
                return mailboxes.sender(ctx, mailbox), None
        return SmtpSender.from_env(), None

    def _event(self, ctx: Ctx, enrollment: Mapping[str, Any], event: str, *, provider: Optional[str] = None,
               provider_message_id: Optional[str] = None, data: Optional[Mapping[str, Any]] = None,
               mailbox_id: Optional[str] = None) -> Dict[str, Any]:
        row = self.store.insert(ctx, "message_events", {
            "enrollment_id": enrollment.get("id"), "contact_id": enrollment.get("contact_id"),
            "campaign_id": enrollment.get("campaign_id"), "event": event, "provider": provider,
            "provider_message_id": provider_message_id, "occurred_at": utcnow(), "data": dict(data or {}),
            "mailbox_id": mailbox_id})
        if enrollment.get("signal_id"):
            self.platform.service("signal_outcomes").on_message_event(ctx, row)   # never raises
        return row

    def _stop(self, ctx: Ctx, enrollment: Mapping[str, Any], status: str, reason: str) -> None:
        self.store.update(ctx, "sequence_enrollments", enrollment["id"], {
            "status": status, "next_step_at": None, "stop_reason": reason[:300]})
        self._cancel_queued(ctx, enrollment["id"], reason)

    def _defer(self, ctx: Ctx, enrollment: Mapping[str, Any], until: datetime, stats: Dict[str, int]) -> None:
        self.store.update(ctx, "sequence_enrollments", enrollment["id"], {"next_step_at": until})
        stats["deferred"] = stats.get("deferred", 0) + 1

    def _campaign_sent_today(self, ctx: Ctx, campaign_id: str, now: datetime) -> int:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.store.count(ctx, "outbound_messages", {"campaign_id": campaign_id, "status": "sent",
                                                           "sent_at__gte": start})

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
            done = self.store.update(ctx, "sequence_enrollments", enrollment["id"], {"status": "completed",
                                                                                     "next_step_at": None})
            stats["completed"] += 1
            if done.get("signal_id"):
                self.platform.service("signal_outcomes").on_enrollment_finished(ctx, done)
            return
        step = steps[position]
        contact = self.store.get(ctx, "contacts", enrollment["contact_id"])
        campaign = self.store.find(ctx, "campaigns", enrollment["campaign_id"]) if enrollment.get("campaign_id") \
            else None
        sequence = self.store.find(ctx, "sequences", enrollment["sequence_id"])
        conditions = stop_conditions(sequence)

        if campaign is not None and campaign.get("status") == "archived" and conditions["campaign_stopped"]:
            self._stop(ctx, enrollment, "stopped", "campaign stopped")
            stats["stopped"] += 1
            return
        if (campaign is not None and campaign.get("status") == "paused") or \
                (sequence is not None and sequence.get("status") == "paused"):
            self._defer(ctx, enrollment, now + timedelta(days=1), stats)
            return
        if conditions["contact_disabled"] and contact.get("status") in _DISABLED_STATUSES:
            self._stop(ctx, enrollment, "stopped", f"contact disabled ({contact.get('status')})")
            stats["stopped"] += 1
            return
        if (step.get("condition") or {}).get("only_if_no_reply") and conditions["reply"] and self.store.count(
                ctx, "message_events", {"contact_id": contact["id"], "event": "replied"}) > 0:
            self._stop(ctx, enrollment, "replied", "reply received before this follow-up")
            stats["stopped"] += 1
            return

        if step["channel"] == "wait":
            pass  # a pure delay: advance to the next step on schedule
        elif step["channel"] == "email":
            reason = self.contact_block_reason(ctx, contact, campaign_id=enrollment.get("campaign_id"))
            if reason is not None:
                self._event(ctx, enrollment, "blocked", data={"reason": reason, "step": position})
                self._stop(ctx, enrollment, self._block_status(reason), reason)
                stats["blocked"] += 1
                stats["stopped"] += 1
                return
            schedule = (campaign or {}).get("schedule") or (sequence or {}).get("schedule") or {}
            window = next_send_window(now, schedule)
            if window is not None:
                self._defer(ctx, enrollment, window, stats)
                return
            cap = int(schedule.get("daily_cap") or 0)
            if cap and campaign is not None and self._campaign_sent_today(ctx, campaign["id"], now) >= cap:
                self._defer(ctx, enrollment, now + timedelta(days=1), stats)
                return
            template = self.store.get(ctx, "email_templates", step["template_id"])
            variables = self.variables_for(ctx, contact, enrollment=enrollment)
            try:
                subject = render_template(step.get("subject_override") or template["subject"], variables)
                body = render_template(template["body"], variables)
            except TemplateError as error:
                self._event(ctx, enrollment, "failed", data={"reason": str(error), "step": position})
                self.store.update(ctx, "sequence_enrollments", enrollment["id"], {"status": "paused",
                                                                                  "next_step_at": None})
                stats["failed"] += 1
                return
            sender, blocked_reason = self._sender_for(campaign, ctx, enrollment, now)
            mailbox = getattr(self, "_last_mailbox", None)
            if blocked_reason and blocked_reason.startswith("deferred:"):
                # Sending is allowed but no mailbox has capacity: wait; nothing was sent.
                self._defer(ctx, enrollment, now + timedelta(hours=1), stats)
                return
            self._event(ctx, enrollment, "rendered", data={"step": position, "subject": subject[:300]})
            headers = {}
            if "unsubscribe" in variables:
                headers["List-Unsubscribe"] = f"<{variables['unsubscribe']['url']}>"
                headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
            queued = self._queue(ctx, enrollment, position, contact["email"], subject, body, mailbox, now)
            outcome = sender.send(OutboundMessage(to=contact["email"], subject=subject, body=body, headers=headers,
                                                  campaign_id=enrollment.get("campaign_id"),
                                                  enrollment_id=enrollment["id"]))
            self._event(ctx, enrollment, outcome["event"], provider=sender.name,
                        provider_message_id=outcome.get("provider_message_id"),
                        data={"step": position, "detail": outcome.get("detail") or blocked_reason},
                        mailbox_id=(mailbox or {}).get("id"))
            self._settle(ctx, queued, outcome, sender.name, blocked_reason, now)
            if outcome["event"] == "sent" and mailbox is not None:
                self.platform.service("mailboxes").record_send(ctx, mailbox, now)
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
        changes: Dict[str, Any] = {"current_step": next_position, "last_event_at": now}
        if next_position >= len(steps):
            changes.update({"status": "completed", "next_step_at": None})
            stats["completed"] += 1
        else:
            changes["next_step_at"] = now + timedelta(days=steps[next_position]["delay_days"])
        updated = self.store.update(ctx, "sequence_enrollments", enrollment["id"], changes)
        if changes.get("status") == "completed" and updated.get("signal_id"):
            # The sequence ran out without a reply: NO RESPONSE for the signal (a later reply
            # still records REPLIED, which then becomes the signal's latest outcome).
            self.platform.service("signal_outcomes").on_enrollment_finished(ctx, updated)

    def _queue(self, ctx: Ctx, enrollment: Mapping[str, Any], step: int, to: str, subject: str, body: str,
               mailbox: Optional[Mapping[str, Any]], now: datetime) -> Dict[str, Any]:
        """One outbound row per enrollment step; a retried step reuses its row."""
        existing = self.store.first(ctx, "outbound_messages", {
            "enrollment_id": enrollment["id"], "step": step, "status__in": ["queued", "blocked", "failed"]})
        values = {"to_email": to, "subject": subject[:1000], "body": body[:50000], "status": "sending",
                  "mailbox_id": (mailbox or {}).get("id"), "scheduled_for": now}
        if existing is not None:
            return self.store.update(ctx, "outbound_messages", existing["id"],
                                     {**values, "attempts": existing["attempts"] + 1})
        return self.store.insert(ctx, "outbound_messages", {
            **values, "campaign_id": enrollment.get("campaign_id"), "sequence_id": enrollment.get("sequence_id"),
            "enrollment_id": enrollment["id"], "step": step, "contact_id": enrollment.get("contact_id"),
            "attempts": 1})

    def _settle(self, ctx: Ctx, queued: Mapping[str, Any], outcome: Mapping[str, Any], provider: str,
                blocked_reason: Optional[str], now: datetime) -> None:
        event = outcome.get("event")
        status = event if event in ("sent", "blocked", "failed") else "failed"
        detail = outcome.get("detail") or blocked_reason
        self.store.update(ctx, "outbound_messages", queued["id"], {
            "status": status, "provider": provider[:60], "provider_message_id": outcome.get("provider_message_id"),
            "sent_at": now if status == "sent" else None,
            "block_reason": (detail or None) and str(detail)[:500] if status == "blocked" else None,
            "error": (detail or None) and str(detail)[:2000] if status == "failed" else None})

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
            if kind == "reply" and sequence is not None and not stop_conditions(sequence)["reply"]:
                continue
            if enrollment["status"] in ("unsubscribed",) and kind != "unsubscribe":
                continue
            changed.append(self.store.update(ctx, "sequence_enrollments", enrollment["id"], {
                "status": event_name, "next_step_at": None, "last_event_at": utcnow(),
                "stop_reason": {"reply": "reply received", "bounce": "permanent bounce",
                                "unsubscribe": "unsubscribed"}[kind]}))
            self._cancel_queued(ctx, enrollment["id"], event_name)
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
