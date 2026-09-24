"""Track I: templates, enrollment approval, suppression, no-send guarantees, inbound events, unsubscribe."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import timedelta
from unittest import mock

from cloud.intel.core.context import Ctx, ValidationError, utcnow
from cloud.intel.gtm import sequences as seq_mod
from cloud.intel.gtm.sequences import NullSender, TemplateError, render_template
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore

SECRET = "unsubscribe-test-secret-0123456789"


class RecordingSender(seq_mod.SenderProvider):
    name = "recording"
    delivers = True

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return {"event": "sent", "provider_message_id": f"<m{len(self.sent)}@test>", "detail": None}


class SequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"CAREERCLOUD_UNSUBSCRIBE_SECRET": SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = MemoryStore()
        self.platform = Platform(self.store, config=PlatformConfig(environment="development"))
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "Seq", "seq-ws")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.seq = self.platform.service("sequences")
        self.platform.service("campaigns").ensure_defaults(self.ctx)
        self.campaign = self.store.first(self.ctx, "campaigns", {"key": "itech-us"})
        self.company = self.store.insert(self.ctx, "companies", {"name": "Acme Mfg", "domain": "acme.example"})
        self.contact = self.store.insert(self.ctx, "contacts", {
            "company_id": self.company["id"], "full_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe",
            "title": "HR Director", "email": "jane@acme.example"})
        self.template = self.seq.create_template(
            self.ctx, name="Intro", subject="Hello {{contact.first_name}} at {{company.name}}",
            body="Hi {{contact.first_name}},\nWe help {{company.name}}.\nUnsubscribe: {{unsubscribe.url}}")
        self.sequence = self.store.insert(self.ctx, "sequences", {"name": "Staffing intro",
                                                                  "campaign_id": self.campaign["id"]})
        self.seq.add_step(self.ctx, self.sequence["id"], channel="email", delay_days=0,
                          template_id=self.template["id"])
        self.seq.add_step(self.ctx, self.sequence["id"], channel="call", delay_days=2, instructions="Call Jane")

    # --- templates -------------------------------------------------------------

    def test_missing_variables_are_an_error_not_blank(self) -> None:
        self.assertEqual(render_template("Hi {{a.b}}", {"a": {"b": "X"}}), "Hi X")
        with self.assertRaises(TemplateError):
            render_template("Hi {{contact.first_name}} {{job.title}}", {"contact": {"first_name": "J"}, "job": {}})
        self.assertEqual(self.template["variables"], ["company.name", "contact.first_name", "unsubscribe.url"])

    def test_preview_renders_signed_unsubscribe_link(self) -> None:
        preview = self.seq.preview(self.ctx, self.template["id"], self.contact["id"])
        self.assertEqual(preview["subject"], "Hello Jane at Acme Mfg")
        self.assertIn("/api/v1/unsubscribe/", preview["body"])
        self.assertIsNone(preview["block_reason"])

    def test_no_secret_means_no_unsubscribe_link_and_no_render(self) -> None:
        with mock.patch.dict(os.environ, {"CAREERCLOUD_UNSUBSCRIBE_SECRET": ""}):
            with self.assertRaises(TemplateError):
                self.seq.preview(self.ctx, self.template["id"], self.contact["id"])
            with self.assertRaises(ValidationError):
                self.seq.unsubscribe_token(self.ctx, self.contact["id"])

    # --- enrollment -----------------------------------------------------------------

    def test_enroll_is_pending_approval_and_skips_blocked_contacts(self) -> None:
        bad = self.store.insert(self.ctx, "contacts", {"full_name": "No Mail", "email": "x@blocked.example"})
        unsub = self.store.insert(self.ctx, "contacts", {"full_name": "Gone", "email": "gone@acme.example",
                                                         "unsubscribed": True})
        invalid = self.store.insert(self.ctx, "contacts", {"full_name": "Bad", "email": "bad@acme.example",
                                                           "email_status": "INVALID"})
        dnc = self.store.insert(self.ctx, "contacts", {"full_name": "Dnc", "email": "dnc@acme.example",
                                                       "status": "do_not_contact"})
        self.seq.add_suppression(self.ctx, "blocked.example", kind="domain", reason="customer")
        results = self.seq.enroll(self.ctx, self.sequence["id"],
                                  [self.contact["id"], bad["id"], unsub["id"], invalid["id"], dnc["id"], "ct_missing"])
        by_id = {r["contact_id"]: r for r in results}
        self.assertEqual(by_id[self.contact["id"]]["status"], "enrolled")
        self.assertEqual(by_id[self.contact["id"]]["enrollment"]["status"], "pending_approval")
        self.assertIn("suppressed", by_id[bad["id"]]["reason"])
        self.assertEqual(by_id[unsub["id"]]["reason"], "unsubscribed")
        self.assertIn("INVALID", by_id[invalid["id"]]["reason"])
        self.assertIn("do_not_contact", by_id[dnc["id"]]["reason"])
        self.assertEqual(by_id["ct_missing"]["reason"], "contact not found")
        again = self.seq.enroll(self.ctx, self.sequence["id"], [self.contact["id"]])
        self.assertEqual(again[0]["reason"], "already enrolled")

    def test_pending_enrollments_are_never_processed(self) -> None:
        self.seq.enroll(self.ctx, self.sequence["id"], [self.contact["id"]])
        stats = self.seq.process_due(self.ctx, utcnow() + timedelta(days=30))
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(self.store.count(self.ctx, "message_events"), 0)

    def test_system_context_cannot_approve(self) -> None:
        [result] = self.seq.enroll(self.ctx, self.sequence["id"], [self.contact["id"]])
        with self.assertRaises(ValidationError):
            self.seq.approve_enrollments(self.ctx.as_system(), [result["enrollment"]["id"]])

    def _approve(self):
        [result] = self.seq.enroll(self.ctx, self.sequence["id"], [self.contact["id"]])
        [approved] = self.seq.approve_enrollments(self.ctx, [result["enrollment"]["id"]])
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved["approved_by"], self.user)
        return approved

    # --- no sending in development ---------------------------------------------------

    def test_process_due_never_sends_in_development(self) -> None:
        enrollment = self._approve()
        with mock.patch.object(seq_mod.SmtpSender, "__init__", side_effect=AssertionError("SMTP constructed")):
            stats = self.seq.process_due(self.ctx, utcnow() + timedelta(minutes=1))
        self.assertEqual((stats["blocked"], stats["sent"]), (1, 0))
        events = [e["event"] for e in self.store.all(self.ctx, "message_events", order="occurred_at")]
        self.assertEqual(sorted(events), ["blocked", "rendered"])
        # a blocked step is not skipped: the enrollment waits on the same step
        current = self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])
        self.assertEqual((current["status"], current["current_step"]), ("active", 0))

    def test_production_still_needs_campaign_sending_enabled(self) -> None:
        self.platform.config.allow_email_sending = True
        self._approve()
        with mock.patch.object(seq_mod.SmtpSender, "__init__", side_effect=AssertionError("SMTP constructed")):
            stats = self.seq.process_due(self.ctx, utcnow() + timedelta(minutes=1))
        self.assertEqual(stats["blocked"], 1)

    def test_fully_allowed_path_sends_then_creates_call_task(self) -> None:
        self.platform.config.allow_email_sending = True
        self.store.update(self.ctx, "campaigns", self.campaign["id"], {"sending_enabled": True})
        sender = RecordingSender()
        self.seq.sender_override = sender
        enrollment = self._approve()
        now = utcnow() + timedelta(minutes=1)
        self.assertEqual(self.seq.process_due(self.ctx, now)["sent"], 1)
        self.assertEqual(sender.sent[0]["to"], "jane@acme.example")
        self.assertIn("List-Unsubscribe", sender.sent[0]["headers"])
        stats = self.seq.process_due(self.ctx, now + timedelta(days=3))
        self.assertEqual((stats["tasks"], stats["completed"]), (1, 1))
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "completed")
        self.assertEqual(self.store.count(self.ctx, "crm_tasks", {"contact_id": self.contact["id"]}), 1)

    def test_suppression_is_rechecked_at_send_time(self) -> None:
        self.platform.config.allow_email_sending = True
        self.store.update(self.ctx, "campaigns", self.campaign["id"], {"sending_enabled": True})
        sender = RecordingSender()
        self.seq.sender_override = sender
        enrollment = self._approve()
        self.seq.add_suppression(self.ctx, "jane@acme.example", reason="manual")
        stats = self.seq.process_due(self.ctx, utcnow() + timedelta(minutes=1))
        self.assertEqual((stats["sent"], stats["blocked"]), (0, 1))
        self.assertEqual(sender.sent, [])
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "suppressed")

    # --- inbound events ------------------------------------------------------------------

    def test_reply_stops_the_sequence(self) -> None:
        enrollment = self._approve()
        result = self.seq.handle_event(self.ctx, "reply", email="Jane@Acme.example")
        self.assertEqual(result["enrollments_updated"], 1)
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "replied")
        self.assertEqual(self.store.count(self.ctx, "activities", {"kind": "email_reply"}), 1)

    def test_bounce_invalidates_email_and_suppresses(self) -> None:
        enrollment = self._approve()
        self.seq.handle_event(self.ctx, "bounce", email="jane@acme.example")
        self.assertEqual(self.store.get(self.ctx, "contacts", self.contact["id"])["email_status"], "INVALID")
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "bounced")
        self.assertIsNotNone(self.seq.suppression_for(self.ctx, "jane@acme.example"))

    def test_unsubscribe_token_roundtrip_and_tamper(self) -> None:
        enrollment = self._approve()
        token = self.seq.unsubscribe_token(self.ctx, self.contact["id"])
        payload, sig = token.split(".")
        other = self.seq.unsubscribe_token(self.ctx, "ct_" + "0" * 32)
        with self.assertRaises(ValidationError):
            self.seq.unsubscribe_by_token(payload + "." + other.split(".")[1])
        with self.assertRaises(ValidationError):
            self.seq.unsubscribe_by_token("garbage")
        self.assertEqual(self.seq.unsubscribe_by_token(token), {"unsubscribed": True, "already": False})
        self.assertTrue(self.store.get(self.ctx, "contacts", self.contact["id"])["unsubscribed"])
        self.assertEqual(self.store.get(self.ctx, "sequence_enrollments", enrollment["id"])["status"], "unsubscribed")
        self.assertEqual(self.seq.unsubscribe_by_token(token)["already"], True)
        self.assertEqual(self.seq.suppression_for(self.ctx, "jane@acme.example")["reason"], "unsubscribe")


if __name__ == "__main__":
    unittest.main()
