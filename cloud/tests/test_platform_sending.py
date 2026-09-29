"""SANA GTM phases 3-6: mailboxes, sending gates, campaigns/sequences, provider events, suppression.

Offline: MemoryStore, a recording HTTP fake. Nothing here ever sends email.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from cloud.intel.core.context import Ctx, ForbiddenError, ValidationError, utcnow
from cloud.intel.gtm import sequences as seq_mod
from cloud.intel.gtm.sequences import cadence_days, next_send_window, stop_conditions
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.sending.events import normalize
from cloud.intel.store.memory import MemoryStore

OAUTH_ENV = {
    "CAREERCLOUD_GOOGLE_OAUTH_CLIENT_ID": "google-client", "CAREERCLOUD_GOOGLE_OAUTH_CLIENT_SECRET": "g-secret",
    "CAREERCLOUD_MS_OAUTH_CLIENT_ID": "ms-client", "CAREERCLOUD_MS_OAUTH_CLIENT_SECRET": "ms-secret",
    "CAREERCLOUD_OAUTH_REDIRECT_BASE": "https://api.example.test",
}


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeHttp:
    """Records every call; answers token/send endpoints like the real providers would."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if "token" in url:
            data = kw.get("data") or {}
            if data.get("grant_type") == "authorization_code":
                claims = json.dumps({"email": "Rep@RiseIT.example"}).encode()
                import base64

                id_token = "x." + base64.urlsafe_b64encode(claims).decode().rstrip("=") + ".y"
                return FakeResponse(200, {"access_token": "at", "refresh_token": "refresh-token-1234",
                                          "id_token": id_token, "scope": "openid email gmail.send"})
            return FakeResponse(200, {"access_token": "fresh-access"})
        if "gmail" in url:
            return FakeResponse(200, {"id": f"gm-{len(self.calls)}"})
        return FakeResponse(404, {"error": "unexpected"})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return FakeResponse(200, {"mail": "rep@contoso.example"})


class Base(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"CAREERCLOUD_UNSUBSCRIBE_SECRET": "unsub-secret-0123456789",
                                               "CAREERCLOUD_GLOBAL_SUPPRESSIONS": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = MemoryStore()
        self.platform = Platform(self.store, config=PlatformConfig(environment="development",
                                                                  secrets_key=Fernet.generate_key().decode()))
        self.user = str(uuid.uuid4())
        self.ws = self.store.create_workspace(self.user, "Send", f"send-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(self.ws["id"], self.user, "owner")
        self.seq = self.platform.service("sequences")
        self.sup = self.platform.service("suppression")
        self.mailboxes = self.platform.service("mailboxes")
        self.http = FakeHttp()
        self.mailboxes.http = self.http
        self.platform.service("campaigns").ensure_defaults(self.ctx)
        self.campaign = self.store.first(self.ctx, "campaigns", {"key": "riseit"})
        self.company = self.store.insert(self.ctx, "companies", {"name": "Acme", "domain": "acme.example"})
        self.contact = self.store.insert(self.ctx, "contacts", {
            "company_id": self.company["id"], "full_name": "Jane Doe", "first_name": "Jane",
            "email": "jane@acme.example"})
        self.t1 = self.seq.create_template(self.ctx, name="T1", subject="Hi {{contact.first_name}}",
                                           body="About {{company.name}}. {{unsubscribe.url}}")
        self.t2 = self.seq.create_template(self.ctx, name="T2", subject="Following up", body="Again. "
                                           "{{unsubscribe.url}}")
        self.sequence = self.store.insert(self.ctx, "sequences", {"name": "Intro", "campaign_id": self.campaign["id"]})

    def allow_sending(self):
        self.platform.config.allow_email_sending = True
        self.store.update(self.ctx, "campaigns", self.campaign["id"], {"sending_enabled": True})

    def google_mailbox(self, **extra):
        with mock.patch.dict(os.environ, OAUTH_ENV):
            start = self.mailboxes.start_oauth(self.ctx, "google")
            from urllib.parse import parse_qs, urlparse

            state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
            result = self.mailboxes.complete_oauth("google", state, "auth-code")
        mailbox = result["mailbox"]
        if extra:
            self.store.update(self.ctx, "mailboxes", mailbox["id"], extra)
        return mailbox

    def enroll_and_approve(self, contact_id=None):
        [result] = self.seq.enroll(self.ctx, self.sequence["id"], [contact_id or self.contact["id"]])
        [row] = self.seq.approve_enrollments(self.ctx, [result["enrollment"]["id"]])
        return row


class SuppressionTests(Base):
    def test_scopes_and_domains(self):
        other = self.store.first(self.ctx, "campaigns", {"key": "itech-us"})
        self.sup.add(self.ctx, "jane@acme.example", scope="campaign", campaign_id=other["id"])
        self.assertIsNone(self.sup.check(self.ctx, "jane@acme.example", campaign_id=self.campaign["id"]))
        self.assertEqual(self.sup.check(self.ctx, "jane@acme.example", campaign_id=other["id"])["scope"], "campaign")
        # widening to workspace applies everywhere
        self.sup.add(self.ctx, "jane@acme.example")
        self.assertEqual(self.sup.check(self.ctx, "JANE@acme.example")["scope"], "workspace")
        self.sup.add(self.ctx, "@blocked.example")
        self.assertEqual(self.sup.check(self.ctx, "x@mail.blocked.example")["kind"], "domain")

    def test_global_operator_list_is_read_only_and_checked_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.txt"
            path.write_text("# ops\nceo@acme.example\nevil.example\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CAREERCLOUD_GLOBAL_SUPPRESSIONS_FILE": str(path)}):
                self.assertEqual(self.sup.check(self.ctx, "ceo@acme.example")["scope"], "global")
                self.assertEqual(self.sup.check(self.ctx, "a@evil.example")["scope"], "global")
                self.assertTrue(self.sup.global_list()["read_only"])
        with self.assertRaises(ValidationError):
            self.sup.add(self.ctx, "x@y.example", scope="global")

    def test_expired_entries_do_not_suppress(self):
        self.sup.add(self.ctx, "old@acme.example", expires_at=utcnow() - timedelta(days=1))
        self.assertIsNone(self.sup.check(self.ctx, "old@acme.example"))

    def test_protected_reasons_need_admin_to_remove(self):
        member = str(uuid.uuid4())
        self.store.add_member(self.ctx, member, "member")
        mctx = Ctx(self.ws["id"], member, "member")
        unsub = self.sup.add(self.ctx, "u@acme.example", reason="unsubscribe")
        manual = self.sup.add(self.ctx, "m@acme.example", reason="manual")
        with self.assertRaises(ForbiddenError):
            self.sup.remove(mctx, unsub["id"])
        self.assertTrue(self.sup.remove(mctx, manual["id"])["removed"])
        self.assertTrue(self.sup.remove(self.ctx, unsub["id"])["removed"])

    def test_bulk_import_export(self):
        result = self.sup.bulk_add(self.ctx, ["a@x.example", "A@x.example", "not an email @", "@y.example"])
        self.assertEqual((result["added"], result["invalid_count"]), (2, 1))
        imported = self.sup.import_csv(self.ctx, "value,reason\nb@x.example,legal\nz.example,\n")
        self.assertEqual(imported["added"], 2)
        self.assertEqual(self.sup.check(self.ctx, "b@x.example")["reason"], "legal")
        text = self.sup.export_csv(self.ctx)
        self.assertIn("b@x.example,email,legal,workspace", text)
        self.assertEqual(self.sup.stats(self.ctx)["total"], 4)

    def test_enrollment_respects_campaign_scope(self):
        self.sup.add(self.ctx, "jane@acme.example", scope="campaign", campaign_id=self.campaign["id"])
        [result] = self.seq.enroll(self.ctx, self.sequence["id"], [self.contact["id"]])
        self.assertEqual(result["status"], "skipped")
        self.assertIn("campaign", result["reason"])


class MailboxTests(Base):
    def test_providers_report_not_configured_without_server_credentials(self):
        with mock.patch.dict(os.environ, {k: "" for k in OAUTH_ENV}):
            providers = {p["provider"]: p for p in self.mailboxes.providers()}
            self.assertFalse(providers["google"]["configured"])
            self.assertIn("CAREERCLOUD_GOOGLE_OAUTH_CLIENT_ID", providers["google"]["missing"])
            self.assertFalse(providers["microsoft365"]["configured"])
            with self.assertRaises(ValidationError):
                self.mailboxes.start_oauth(self.ctx, "google")
        self.assertEqual(self.store.count(self.ctx, "mailboxes"), 0)

    def test_google_oauth_with_pkce_stores_only_an_encrypted_refresh_token(self):
        with mock.patch.dict(os.environ, OAUTH_ENV):
            start = self.mailboxes.start_oauth(self.ctx, "google")
        self.assertIn("code_challenge_method=S256", start["authorize_url"])
        self.assertIn("gmail.send", start["authorize_url"])
        mailbox = self.google_mailbox()
        self.assertEqual((mailbox["address"], mailbox["status"], mailbox["is_default"]),
                         ("rep@riseit.example", "connected", True))
        self.assertNotIn("secret_ciphertext", mailbox)
        raw = self.store.get(self.ctx, "mailboxes", mailbox["id"])
        self.assertNotIn("refresh-token-1234", raw["secret_ciphertext"])
        token_call = [c for c in self.http.calls if c[2].get("data", {}).get("grant_type") == "authorization_code"][0]
        self.assertTrue(token_call[2]["data"]["code_verifier"])

    def test_oauth_state_is_single_use(self):
        with mock.patch.dict(os.environ, OAUTH_ENV):
            start = self.mailboxes.start_oauth(self.ctx, "microsoft365")
            from urllib.parse import parse_qs, urlparse

            state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
            self.mailboxes.complete_oauth("microsoft365", state, "code")
            with self.assertRaises(ValidationError):
                self.mailboxes.complete_oauth("microsoft365", state, "code")
            with self.assertRaises(ValidationError):
                self.mailboxes.complete_oauth("microsoft365", "garbage", "code")
        self.assertEqual(self.store.first(self.ctx, "mailboxes", {"provider": "microsoft365"})["address"],
                         "rep@contoso.example")

    def test_api_and_smtp_connectors_never_take_passwords(self):
        with self.assertRaises(ValidationError):
            self.mailboxes.connect_api(self.ctx, address="s@acme.example", api_key="")
        box = self.mailboxes.connect_api(self.ctx, address="s@acme.example", api_key="SG.abcdefgh12345678")
        self.assertEqual((box["status"], box["secret_hint"]), ("connected", "…5678"))
        with mock.patch.dict(os.environ, {"CAREERCLOUD_SMTP_HOST": "", "CAREERCLOUD_SMTP_FROM": ""}):
            smtp = self.mailboxes.connect_smtp(self.ctx, address="relay@acme.example")
        self.assertEqual(smtp["status"], "pending")
        self.assertIn("not configured", smtp["last_error"])

    def test_test_send_is_refused_outside_production_but_connection_test_works(self):
        mailbox = self.google_mailbox()
        with mock.patch.dict(os.environ, OAUTH_ENV):
            refused = self.mailboxes.test(self.ctx, mailbox["id"], to="me@riseit.example")
            self.assertFalse(refused["sent"])
            self.assertIn("disabled in this environment", refused["detail"])
            ok = self.mailboxes.test(self.ctx, mailbox["id"])
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["mailbox"]["health"], "healthy")
        self.assertFalse(any("gmail" in c[1] for c in self.http.calls))

    def test_default_and_disconnect(self):
        first = self.google_mailbox()
        second = self.mailboxes.connect_api(self.ctx, address="b@acme.example", api_key="key-12345678")
        self.mailboxes.set_default(self.ctx, second["id"])
        self.assertFalse(self.store.get(self.ctx, "mailboxes", first["id"])["is_default"])
        gone = self.mailboxes.disconnect(self.ctx, second["id"])
        self.assertEqual((gone["status"], gone["has_secret"], gone["is_default"]), ("disconnected", False, False))
        with self.assertRaises(ValidationError):
            self.mailboxes.set_default(self.ctx, second["id"])


class SendingTests(Base):
    def test_outbox_tick_is_a_no_op_in_development(self):
        self.seq.add_step(self.ctx, self.sequence["id"], template_id=self.t1["id"])
        self.enroll_and_approve()
        with mock.patch.object(self.seq, "process_due", side_effect=AssertionError("must not run")):
            self.assertEqual(self.platform.service("outbox").tick(self.ctx), 0)

    def test_fully_allowed_send_goes_through_the_connected_mailbox_and_is_queued(self):
        self.allow_sending()
        mailbox = self.google_mailbox()
        self.seq.add_step(self.ctx, self.sequence["id"], template_id=self.t1["id"])
        self.enroll_and_approve()
        with mock.patch.dict(os.environ, OAUTH_ENV), \
                mock.patch.object(seq_mod.SmtpSender, "__init__", side_effect=AssertionError("SMTP constructed")):
            stats = self.platform.service("outbox").tick(self.ctx)
        self.assertEqual(stats, 1)
        [out] = self.store.all(self.ctx, "outbound_messages")
        self.assertEqual((out["status"], out["mailbox_id"], out["to_email"]), ("sent", mailbox["id"],
                                                                               "jane@acme.example"))
        self.assertTrue(any("gmail" in c[1] for c in self.http.calls))
        self.assertEqual(self.store.get(self.ctx, "mailboxes", mailbox["id"])["sent_today"], 1)

    def test_mailbox_limit_defers_without_sending(self):
        self.allow_sending()
        self.google_mailbox(daily_limit=0)
        self.seq.add_step(self.ctx, self.sequence["id"], template_id=self.t1["id"])
        enrollment = self.enroll_and_approve()
        stats = self.seq.process_due(self.ctx, utcnow() + timedelta(minutes=1))
        self.assertEqual((stats["sent"], stats.get("deferred")), (0, 1))
        self.assertEqual(self.store.count(self.ctx, "outbound_messages"), 0)
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "active")

    def test_schedule_window_defers(self):
        self.allow_sending()
        self.seq.sender_override = mock.Mock(name="sender", send=mock.Mock(side_effect=AssertionError("sent")))
        self.seq.add_step(self.ctx, self.sequence["id"], template_id=self.t1["id"])
        self.platform.service("campaigns").configure(self.ctx, self.campaign["id"], {
            "schedule": {"timezone": "UTC", "days": [1, 2, 3, 4, 5], "start_hour": 9, "end_hour": 17}})
        enrollment = self.enroll_and_approve()
        saturday_night = datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)
        stats = self.seq.process_due(self.ctx, saturday_night)
        self.assertEqual(stats.get("deferred"), 1)
        nxt = self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["next_step_at"]
        self.assertEqual((nxt.isoweekday(), nxt.hour), (1, 9))

    def test_cadence_and_window_helpers(self):
        self.assertEqual(cadence_days([0, 2, 3, 4]), [1, 3, 6, 10])
        steps = self.seq.apply_cadence(self.ctx, self.sequence["id"], [self.t1["id"], self.t2["id"]])
        self.assertEqual([s["delay_days"] for s in steps], [0, 2, 3, 4])
        self.assertEqual([s["step_type"] for s in steps], ["initial", "follow_up", "follow_up", "final"])
        self.assertEqual(steps[3]["template_id"], self.t2["id"])
        monday = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
        self.assertIsNone(next_send_window(monday, {"days": [1], "start_hour": 9, "end_hour": 17}))
        self.assertIsNone(next_send_window(monday, {}))
        self.assertTrue(stop_conditions({"stop_conditions": {"unsubscribe": False}})["unsubscribe"])
        self.assertFalse(stop_conditions({"stop_on_reply": False})["reply"])

    def test_save_steps_with_wait_and_validation(self):
        steps = self.seq.save_steps(self.ctx, self.sequence["id"], [
            {"channel": "email", "template_id": self.t1["id"]},
            {"channel": "wait", "delay_days": 2},
            {"channel": "email", "template_id": self.t2["id"], "delay_days": 1, "step_type": "final"}])
        self.assertEqual([s["step_type"] for s in steps], ["initial", "wait", "final"])
        with self.assertRaises(ValidationError):
            self.seq.save_steps(self.ctx, self.sequence["id"], [{"channel": "email"}])

    def test_stop_conditions_campaign_contact_and_reply(self):
        self.seq.apply_cadence(self.ctx, self.sequence["id"], [self.t1["id"]], days=[1, 3])
        e1 = self.enroll_and_approve()
        other = self.store.insert(self.ctx, "contacts", {"full_name": "Bob", "email": "bob@acme.example",
                                                         "company_id": self.company["id"]})
        e2 = self.enroll_and_approve(other["id"])
        self.store.update(self.ctx, "contacts", other["id"], {"status": "do_not_contact"})
        self.seq.process_due(self.ctx, utcnow() + timedelta(minutes=1))
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", e2["id"])["stop_reason"],
                         "contact disabled (do_not_contact)")
        self.platform.service("campaigns").set_status(self.ctx, self.campaign["id"], "stop")
        row = self.store.get(self.ctx, "sequence_enrollments", e1["id"])
        self.assertEqual((row["status"], row["stop_reason"]), ("stopped", "campaign stopped"))

    def test_followup_skipped_after_reply_even_without_webhook_stop(self):
        self.allow_sending()
        self.seq.sender_override = mock.Mock(name="s", send=mock.Mock(return_value={
            "event": "sent", "provider_message_id": "<m1@t>", "detail": None}))
        self.seq.sender_override.name = "rec"
        self.store.update(self.ctx, "sequences", self.sequence["id"], {"stop_on_reply": False})
        self.seq.apply_cadence(self.ctx, self.sequence["id"], [self.t1["id"], self.t2["id"]], days=[1, 3])
        self.store.update(self.ctx, "sequences", self.sequence["id"], {"stop_conditions": {"reply": True}})
        enrollment = self.enroll_and_approve()
        now = utcnow() + timedelta(minutes=1)
        self.seq.process_due(self.ctx, now)
        self.seq._event(self.ctx, enrollment, "replied")
        self.seq.process_due(self.ctx, now + timedelta(days=3))
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "replied")
        self.assertEqual(self.seq.sender_override.send.call_count, 1)


class CampaignOpsTests(Base):
    def test_launch_enrolls_audience_pending_approval_and_reports_readiness(self):
        crm = self.platform.service("crm")
        target = crm.create_list(self.ctx, "Targets", "contacts")
        blocked = self.store.insert(self.ctx, "contacts", {"full_name": "Gone", "email": "gone@acme.example",
                                                           "unsubscribed": True})
        crm.add_to_list(self.ctx, target["id"], "contacts", [self.contact["id"], blocked["id"]])
        campaigns = self.platform.service("campaigns")
        campaigns.configure(self.ctx, self.campaign["id"], {"audience": {"list_ids": [target["id"]]}})
        with self.assertRaises(ValidationError):
            campaigns.launch(self.ctx, self.campaign["id"])  # no sequence yet
        self.seq.add_step(self.ctx, self.sequence["id"], template_id=self.t1["id"])
        campaigns.configure(self.ctx, self.campaign["id"], {"default_sequence_id": self.sequence["id"]})
        preview = campaigns.audience_preview(self.ctx, self.campaign["id"])
        self.assertEqual((preview["total"], preview["eligible"], preview["blocked"]), (2, 1, {"unsubscribed": 1}))
        result = campaigns.launch(self.ctx, self.campaign["id"])
        self.assertEqual((result["enrolled"], result["approved"], result["pending_approval"]), (1, 0, 1))
        self.assertTrue(any("disabled in this environment" in n for n in result["notes"]))
        self.assertEqual(self.store.get(self.ctx, "campaigns", self.campaign["id"])["status"], "active")
        perf = campaigns.performance(self.ctx, self.campaign["id"])
        self.assertEqual(perf["sent"], 0)
        self.assertIsNone(perf["reply_rate"])
        self.assertIsNone(perf["opened"])  # no connected provider reports opens

    def test_auto_after_review_is_approved_by_the_launching_user_only(self):
        campaigns = self.platform.service("campaigns")
        self.seq.add_step(self.ctx, self.sequence["id"], template_id=self.t1["id"])
        campaigns.configure(self.ctx, self.campaign["id"], {
            "default_sequence_id": self.sequence["id"], "approval_policy": "auto_after_review",
            "audience": {"contact_ids": [self.contact["id"]]}})
        system = campaigns.launch(self.ctx.as_system(), self.campaign["id"])
        self.assertEqual(system["approved"], 0)
        campaigns.set_status(self.ctx, self.campaign["id"], "pause")
        with self.assertRaises(ValidationError):
            campaigns.set_status(self.ctx, self.campaign["id"], "pause")
        campaigns.set_status(self.ctx, self.campaign["id"], "resume")

    def test_schedule_validation_and_sending_toggle_needs_admin(self):
        campaigns = self.platform.service("campaigns")
        with self.assertRaises(ValidationError):
            campaigns.configure(self.ctx, self.campaign["id"], {"schedule": {"start_hour": 18, "end_hour": 9}})
        with self.assertRaises(ValidationError):
            campaigns.configure(self.ctx, self.campaign["id"], {"schedule": {"timezone": "Mars/Base"}})
        member = str(uuid.uuid4())
        self.store.add_member(self.ctx, member, "member")
        with self.assertRaises(ForbiddenError):
            campaigns.configure(Ctx(self.ws["id"], member, "member"), self.campaign["id"], {"sending_enabled": True})


class EventTests(Base):
    def sent_enrollment(self):
        self.allow_sending()
        self.seq.sender_override = mock.Mock(send=mock.Mock(return_value={
            "event": "sent", "provider_message_id": "sg-abc", "detail": None}))
        self.seq.sender_override.name = "sendgrid"
        self.seq.apply_cadence(self.ctx, self.sequence["id"], [self.t1["id"]], days=[1, 3])
        enrollment = self.enroll_and_approve()
        self.seq.process_due(self.ctx, utcnow() + timedelta(minutes=1))
        return enrollment

    def test_sendgrid_normalization(self):
        events = normalize("sendgrid", [
            {"event": "delivered", "email": "a@x.example", "sg_event_id": "1", "sg_message_id": "m.1", "timestamp": 1},
            {"event": "bounce", "type": "bounce", "email": "a@x.example", "sg_event_id": "2"},
            {"event": "bounce", "type": "blocked", "email": "a@x.example", "sg_event_id": "3"},
            {"event": "spamreport", "email": "a@x.example", "sg_event_id": "4"},
            {"event": "processed", "email": "a@x.example", "sg_event_id": "5"}])
        self.assertEqual([(e["kind"], e["bounce_type"]) for e in events],
                         [("delivered", "none"), ("bounced", "hard"), ("bounced", "soft"), ("complained", "none"),
                          ("unknown", "none")])
        self.assertEqual(events[0]["provider_message_id"], "m")

    def test_hard_bounce_stops_and_suppresses_duplicates_are_ignored(self):
        enrollment = self.sent_enrollment()
        events = self.platform.service("events")
        payload = [{"event": "bounce", "type": "bounce", "email": "jane@acme.example", "sg_event_id": "e1",
                    "sg_message_id": "sg-abc.filter"}]
        self.assertEqual(events.ingest(self.ctx, "sendgrid", payload)["applied"], 1)
        self.assertEqual(events.ingest(self.ctx, "sendgrid", payload)["duplicates"], 1)
        row = self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])
        self.assertEqual((row["status"], row["stop_reason"]), ("bounced", "permanent bounce"))
        self.assertEqual(self.sup.check(self.ctx, "jane@acme.example")["reason"], "hard_bounce")

    def test_soft_bounce_and_open_are_recorded_only(self):
        enrollment = self.sent_enrollment()
        self.platform.service("events").ingest(self.ctx, "sendgrid", [
            {"event": "bounce", "type": "blocked", "email": "jane@acme.example", "sg_event_id": "s1",
             "sg_message_id": "sg-abc"},
            {"event": "open", "email": "jane@acme.example", "sg_event_id": "o1", "sg_message_id": "sg-abc"}])
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "active")
        self.assertIsNone(self.sup.check(self.ctx, "jane@acme.example"))
        self.assertEqual(self.store.count(self.ctx, "message_events", {"event": "opened",
                                                                       "enrollment_id": enrollment["id"]}), 1)

    def test_postmark_reply_stops_sequence_and_creates_task(self):
        enrollment = self.sent_enrollment()
        result = self.platform.service("events").ingest(self.ctx, "postmark", {
            "FromFull": {"Email": "Jane@Acme.example"}, "MessageID": "in-1", "Date": "2026-10-01T10:00:00Z"})
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "replied")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks", {"source": "reply_detected"}), 1)
        self.assertEqual(self.store.count(self.ctx, "activities", {"kind": "email_reply"}), 1)

    def test_providers_without_support_never_produce_events(self):
        result = self.platform.service("events").ingest(self.ctx, "google", {"type": "opened",
                                                                             "email": "jane@acme.example"})
        self.assertEqual((result["applied"], result["ignored"]), (0, 1))
        with self.assertRaises(ValidationError):
            self.platform.service("events").ingest(self.ctx, "nope", {})

    def test_complaint_suppresses_with_complaint_reason(self):
        self.sent_enrollment()
        self.platform.service("events").record_manual(self.ctx, "complaint", email="jane@acme.example")
        self.assertEqual(self.sup.check(self.ctx, "jane@acme.example")["reason"], "complaint")
        self.assertTrue(self.store.get(self.ctx, "contacts", self.contact["id"])["unsubscribed"])


class SendingApiTests(unittest.TestCase):
    SECRET = "platform-sending-tests-secret-0123456789abcdef"

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from cloud.api.auth import DevTokenIssuer
        from cloud.api.main import create_app
        from cloud.api.settings import Settings
        from cloud.shared.storage import LocalFileStorage
        from cloud.worker.dispatcher import NullDispatcher

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.platform = Platform(MemoryStore(), storage=LocalFileStorage(root / "platform"),
                                 config=PlatformConfig(files_dir=root / "platform",
                                                       secrets_key=Fernet.generate_key().decode()))
        issuer = DevTokenIssuer(self.SECRET)
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = TestClient(app)
        self.client.headers["Authorization"] = f"Bearer {issuer.issue('alice@example.com')['access_token']}"
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.anon = TestClient(app)
        r = self.client.post("/api/v1/workspaces", json={"name": "Send API"})
        self.assertEqual(r.status_code, 201, r.text)
        self.ws = r.json()["id"]
        self.base = f"/api/v1/w/{self.ws}"

    def test_status_mailboxes_never_leak_secrets_and_refuse_passwords(self):
        status = self.client.get(self.base + "/sending/status").json()
        self.assertFalse(status["outbox"]["sending_allowed"])
        self.assertIn("google", {p["provider"] for p in status["providers"]})
        r = self.client.post(self.base + "/mailboxes/smtp", json={"address": "a@x.example", "password": "hunter2"})
        self.assertEqual(r.status_code, 422)
        r = self.client.post(self.base + "/mailboxes/api", json={"address": "a@x.example",
                                                                 "api_key": "SG.secret-key-9999"})
        self.assertEqual(r.status_code, 201, r.text)
        listing = self.client.get(self.base + "/mailboxes").text
        self.assertNotIn("secret-key", listing)
        self.assertNotIn("secret_ciphertext", listing)
        test = self.client.post(self.base + f"/mailboxes/{r.json()['id']}/test", json={"to": "me@x.example"})
        self.assertFalse(test.json()["sent"])

    def test_webhook_requires_a_configured_secret_or_signature(self):
        body = json.dumps([{"event": "delivered", "email": "a@x.example", "sg_event_id": "1"}]).encode()
        url = self.base + "/events/sendgrid/webhook"
        with mock.patch.dict(os.environ, {"CAREERCLOUD_WEBHOOK_SECRET_SENDGRID": "",
                                          "CAREERCLOUD_INBOUND_WEBHOOK_SECRET": ""}):
            self.assertEqual(self.anon.post(url, content=body, headers={"X-Webhook-Secret": "x"}).status_code, 401)
        with mock.patch.dict(os.environ, {"CAREERCLOUD_WEBHOOK_SECRET_SENDGRID": "whsec"}):
            self.assertEqual(self.anon.post(url, content=body).status_code, 401)
            self.assertEqual(self.anon.post(url, content=body, headers={"X-Webhook-Secret": "bad"}).status_code, 401)
            signature = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
            r = self.anon.post(url, content=body, headers={"X-Signature": f"sha256={signature}"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["received"], 1)
            self.assertEqual(self.anon.post(self.base + "/events/google/webhook", content=body,
                                            headers={"X-Webhook-Secret": "whsec"}).status_code, 404)

    def test_suppression_tools_and_no_raw_delete(self):
        r = self.client.post(self.base + "/suppression/bulk", json={"values": "a@x.example\nb@x.example,@y.example",
                                                                    "reason": "unsubscribe"})
        self.assertEqual(r.json()["added"], 3, r.text)
        row = self.client.get(self.base + "/suppressions").json()["items"][0]
        self.assertEqual(self.client.delete(self.base + f"/suppressions/{row['id']}").status_code, 405)
        self.assertTrue(self.client.get(self.base + "/suppression/check", params={"email": "z@y.example"})
                        .json()["suppressed"])
        export = self.client.get(self.base + "/suppression/export")
        self.assertIn("text/csv", export.headers["content-type"])
        self.assertEqual(self.client.post(self.base + "/suppression/remove", json={"ids": [row["id"]]})
                         .json()["removed"], 1)

    def test_campaign_and_sequence_routes(self):
        campaign = self.client.get(self.base + "/campaigns").json()["items"][0]
        seq = self.client.post(self.base + "/sequences", json={"name": "S", "campaign_id": campaign["id"]}).json()
        tpl = self.client.post(self.base + "/templates", json={"name": "T", "subject": "Hi", "body": "Body"}).json()
        r = self.client.post(self.base + f"/sequences/{seq['id']}/cadence", json={"template_ids": [tpl["id"]]})
        self.assertEqual(len(r.json()["steps"]), 4, r.text)
        overview = self.client.get(self.base + f"/sequences/{seq['id']}/overview").json()
        self.assertEqual(overview["days"], [1, 3, 6, 10])
        r = self.client.post(self.base + f"/campaigns/{campaign['id']}/configure",
                             json={"default_sequence_id": seq["id"], "schedule": {"days": [1, 2, 3, 4, 5],
                                                                                  "start_hour": 8, "end_hour": 18}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.client.get(self.base + f"/campaigns/{campaign['id']}/readiness").json()["ready"])
        launched = self.client.post(self.base + f"/campaigns/{campaign['id']}/launch").json()
        self.assertEqual(launched["audience"], 0)
        self.assertEqual(self.client.get(self.base + f"/campaigns/{campaign['id']}/performance").json()["sent"], 0)
        self.assertEqual(self.client.post(self.base + f"/campaigns/{campaign['id']}/status/stop").status_code, 200)


if __name__ == "__main__":
    unittest.main()
