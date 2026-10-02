"""Email validation jobs (Phase 1/2/17): upload, column detection, the task, pause/cancel,
results, export, EmailListVerify batching/retries/errors, and the explicit GTM actions."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from cryptography.fernet import Fernet

from cloud.intel.core.context import ConflictError, Ctx, ValidationError, utcnow
from cloud.intel.email.jobs import detect_email_columns, extract_email, reason_for, result_source
from cloud.intel.email.providers import EmailListVerifyProvider, EmailProviderError, LocalValidator, ValidationResult
from cloud.intel.email.service import EmailValidationService
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.tests._platform_intel_helpers import RecordingAutomation
from cloud.tests.test_platform_sources_support import FakeResponse, FakeSession

NO_MX = {"nomx.example"}


class FakeDns:
    """Offline DNS for the evidence layer, consistent with :func:`resolver`: every domain has an
    MX, SPF and DMARC record except the ones in NO_MX, which exist but take no mail."""

    def lookup(self, name, rtype):
        from cloud.intel.email.engine import DnsAnswer

        domain = name.removeprefix("_dmarc.")
        if domain in NO_MX:
            return DnsAnswer(error="no_answer")
        if rtype == "MX":
            return DnsAnswer([f"10 mx.{domain}."])
        if rtype == "TXT":
            return DnsAnswer(["v=DMARC1; p=none"] if name.startswith("_dmarc.") else ["v=spf1 -all"])
        return DnsAnswer(["192.0.2.1"])


def resolver(domain):
    return domain not in NO_MX


def csv_bytes(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


SAMPLE = csv_bytes([
    ["Name", "Work Email", "Company"],
    ["Ann Test", "ann.test@acme-test.example", "Acme"],
    ["Info Box", "info@acme-test.example", "Acme"],
    ["Throw Away", "x@mailinator.com", "Temp"],
    ["No Mx", "bob@nomx.example", "Dead"],
    ["Free Mail", "free.user@gmail.com", "Home"],
    ["Broken", "not-an-email", "Bad"],
    ["Empty", "", "None"],
])


def make_platform():
    store = MemoryStore()
    platform = Platform(store, config=PlatformConfig(environment="test", secrets_key=Fernet.generate_key().decode()))
    platform.override("automation", RecordingAutomation())
    email = EmailValidationService(platform, local=LocalValidator(resolver=resolver))
    platform.override("email", email)
    platform.service("email_jobs").dns_client = FakeDns()  # the evidence layer never touches the network
    owner = str(uuid.uuid4())
    ws = store.create_workspace(owner, "Acme", f"acme-{uuid.uuid4().hex[:8]}")
    return platform, Ctx(ws["id"], owner, "owner"), email


class PaidOk:
    name, paid, cost_per_check = "emaillistverify", True, 1.0

    def __init__(self):
        self.calls = []

    def check(self, email):
        self.calls.append(email)
        return ValidationResult(email, "VALID", 95.0, self.name, {"result_code": "ok"})


class HelperTests(unittest.TestCase):
    def test_detects_the_email_column_by_content_and_header(self) -> None:
        rows = [{"Name": "A", "Contact": "a@x.example", "Notes": "call"}, {"Name": "B", "Contact": "b@x.example",
                                                                          "Notes": "email me"}]
        found = detect_email_columns(["Name", "Contact", "Notes"], rows)
        self.assertEqual(found[0]["column"], "Contact")
        self.assertNotIn("Name", [c["column"] for c in found])

    def test_extracts_addresses_from_cells(self) -> None:
        self.assertEqual(extract_email("Ann <ann@x.example>"), "ann@x.example")
        self.assertEqual(extract_email("mailto:bob@x.example?subject=hi"), "bob@x.example")
        self.assertEqual(extract_email("  "), "")
        self.assertEqual(extract_email("garbage"), "garbage")

    def test_reasons_are_honest(self) -> None:
        self.assertIn("not verified", reason_for("UNKNOWN", {"mx": True}))
        self.assertEqual(reason_for("INVALID", {"mx": False, "syntax": True}), "The domain does not accept email")


class JobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, self.email = make_platform()
        self.jobs = self.platform.service("email_jobs")

    def run_job(self, job):
        job = self.jobs.start(self.ctx, job["id"])
        done = run_task_inline(self.platform, self.ctx.workspace_id, job["task_id"])
        self.assertEqual(done["status"], "completed", done.get("error"))
        return self.jobs.get(self.ctx, job["id"])

    def test_upload_detects_column_and_runs_to_completion(self) -> None:
        job = self.jobs.create_upload(self.ctx, "leads.csv", SAMPLE)
        self.assertEqual(job["email_column"], "Work Email")
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["row_count"], 7)
        self.assertEqual(job["columns"], ["Name", "Work Email", "Company"])
        job = self.run_job(job)
        self.assertEqual(job["status"], "completed")
        counts = job["counts"]
        self.assertEqual(counts["processed"], 7)
        self.assertEqual(counts["pending"], 0)
        self.assertEqual(counts["UNKNOWN"], 1)      # local checks never say VALID
        self.assertEqual(counts["VALID"], 0)
        self.assertEqual(counts["ROLE"], 1)
        self.assertEqual(counts["DISPOSABLE"], 1)
        self.assertEqual(counts["FREE_PROVIDER"], 1)
        self.assertEqual(counts["INVALID"], 3)      # no MX, bad syntax, empty
        empty = self.jobs.items(self.ctx, job["id"], {"status": "INVALID"}).rows
        self.assertTrue(any((r["checks"] or {}).get("empty") for r in empty))
        by_domain = self.jobs.items(self.ctx, job["id"], {"domain": "acme-test.example"}).rows
        self.assertEqual(len(by_domain), 2)
        self.assertTrue(self.platform.store.all(self.ctx, "notifications", {"kind": "email_validation.completed"}))

    def test_ambiguous_columns_need_a_choice(self) -> None:
        data = csv_bytes([["Email", "Alt Email"], ["a@x.example", "b@x.example"]])
        job = self.jobs.create_upload(self.ctx, "two.csv", data)
        self.assertIsNone(job["email_column"])
        with self.assertRaises(ValidationError):
            self.jobs.start(self.ctx, job["id"])
        with self.assertRaises(ValidationError):
            self.jobs.set_email_column(self.ctx, job["id"], "Nope")
        job = self.jobs.set_email_column(self.ctx, job["id"], "Alt Email")
        self.assertEqual(job["status"], "ready")

    def test_xlsx_upload_and_rejects(self) -> None:
        from openpyxl import Workbook

        wb = Workbook()
        wb.active.append(["email", "name"])
        wb.active.append(["ann@x.example", "Ann"])
        out = io.BytesIO()
        wb.save(out)
        job = self.jobs.create_upload(self.ctx, "a.xlsx", out.getvalue())
        self.assertEqual((job["format"], job["email_column"], job["row_count"]), ("xlsx", "email", 1))
        with self.assertRaises(ValidationError):
            self.jobs.create_upload(self.ctx, "a.pdf", b"%PDF")
        with self.assertRaises(ValidationError):
            self.jobs.create_upload(self.ctx, "a.csv", b"")

    def test_pause_resume_and_cancel(self) -> None:
        job = self.jobs.create_upload(self.ctx, "leads.csv", SAMPLE)
        job = self.jobs.start(self.ctx, job["id"])
        paused = self.jobs.pause(self.ctx, job["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertIsNone(run_task_inline(self.platform, self.ctx.workspace_id, job["task_id"]))
        resumed = self.jobs.resume(self.ctx, job["id"])
        self.assertEqual(resumed["status"], "queued")
        cancelled = self.jobs.cancel(self.ctx, job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["counts"]["pending"], 7)
        with self.assertRaises(ConflictError):
            self.jobs.cancel(self.ctx, job["id"])

    def test_export_csv_and_xlsx_keep_original_columns(self) -> None:
        job = self.run_job(self.jobs.create_upload(self.ctx, "leads.csv", SAMPLE))
        name, content, media = self.jobs.export(self.ctx, job["id"], "csv")
        self.assertTrue(name.endswith(".csv"))
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(rows[0][:3], ["Name", "Work Email", "Company"])
        self.assertIn("validation_status", rows[0])
        self.assertEqual(len(rows), 8)
        name, content, _ = self.jobs.export(self.ctx, job["id"], "csv", ["ROLE"])
        self.assertEqual(len(list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))), 2)
        name, content, media = self.jobs.export(self.ctx, job["id"], "xlsx")
        self.assertTrue(content.startswith(b"PK"))
        with self.assertRaises(ValidationError):
            self.jobs.export(self.ctx, job["id"], "pdf")

    def test_nothing_reaches_the_crm_unless_asked(self) -> None:
        job = self.run_job(self.jobs.create_upload(self.ctx, "leads.csv", SAMPLE))
        self.assertEqual(self.platform.store.count(self.ctx, "contacts"), 0)
        result = self.jobs.add_to_list(self.ctx, job["id"], list_name="Validated", statuses=["UNKNOWN", "ROLE"])
        self.assertEqual((result["added"], result["not_in_crm"]), (0, 2))
        self.assertEqual(self.platform.store.count(self.ctx, "contacts"), 0)
        result = self.jobs.add_to_list(self.ctx, job["id"], list_id=result["list"]["id"], statuses=["UNKNOWN"],
                                       create_missing_contacts=True)
        self.assertEqual((result["added"], result["created_contacts"]), (1, 1))
        contact = self.platform.store.first(self.ctx, "contacts", {"email": "ann.test@acme-test.example"})
        self.assertEqual(contact["full_name"], "Ann Test")

    def test_list_to_campaign_to_sequence_never_sends(self) -> None:
        crm = self.platform.service("crm")
        a = crm.upsert_contact(self.ctx, {"full_name": "Ann", "email": "ann@acme-test.example"}, source_kind="manual",
                               source_name="test")["contact"]
        b = crm.upsert_contact(self.ctx, {"full_name": "Hr", "email": "hr@acme-test.example"}, source_kind="manual",
                               source_name="test")["contact"]
        target = crm.create_list(self.ctx, "Targets", "contacts")
        crm.add_to_list(self.ctx, target["id"], "contacts", [a["id"], b["id"]])
        job = self.jobs.create_from_contacts(self.ctx, name="Targets check", list_id=target["id"])
        job = self.run_job(job)
        self.assertEqual(job["counts"]["UNKNOWN"], 1)
        self.assertEqual(job["counts"]["ROLE"], 1)
        made = self.jobs.create_campaign(self.ctx, job["id"], name="Q4 outreach", statuses=["UNKNOWN"])
        self.assertEqual(made["campaign"]["status"], "draft")
        self.assertFalse(made["campaign"]["sending_enabled"])
        self.assertEqual(made["campaign"]["audience"]["list_ids"], [made["list"]["id"]])
        self.assertEqual(made["added"], 1)
        seq = self.platform.store.insert(self.ctx, "sequences", {"name": "Follow up"})
        enrolled = self.jobs.enroll(self.ctx, job["id"], sequence_id=seq["id"], statuses=["UNKNOWN"])
        self.assertEqual(enrolled["enrolled"], 1)
        rows = self.platform.store.all(self.ctx, "sequence_enrollments", {"sequence_id": seq["id"]})
        self.assertEqual([r["status"] for r in rows], ["pending_approval"])
        self.assertEqual(self.platform.store.count(self.ctx, "outbound_messages"), 0)

    def test_rows_source(self) -> None:
        job = self.jobs.create_from_rows(self.ctx, name="scrape", rows=[{"email": "a@x.example", "n": 1}],
                                         email_field="email", start=True)
        self.assertEqual(job["status"], "queued")
        with self.assertRaises(ValidationError):
            self.jobs.create_from_rows(self.ctx, name="bad", rows=[{"mail": "a"}], email_field="email")

    def test_paid_needs_a_verified_provider(self) -> None:
        job = self.jobs.create_upload(self.ctx, "leads.csv", SAMPLE)
        with self.assertRaises(ValidationError):
            self.jobs.start(self.ctx, job["id"], settings={"allow_paid": True})
        status = self.jobs.provider_status(self.ctx)
        self.assertEqual(status["emaillistverify"]["status"], "not_configured")
        self.assertEqual(status["local"]["status"], "active")
        self.assertEqual(self.jobs.test_provider(self.ctx)["status"], "not_configured")

    def test_viewer_cannot_create(self) -> None:
        viewer = str(uuid.uuid4())
        self.platform.store.add_member(self.ctx, viewer, "viewer")
        with self.assertRaises(Exception):
            self.jobs.create_upload(Ctx(self.ctx.workspace_id, viewer, "viewer"), "leads.csv", SAMPLE)


class PaidBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, self.email = make_platform()
        self.ledger = self.platform.service("credits")

    def test_batches_each_have_a_reservation(self) -> None:
        paid = PaidOk()
        self.email.paid_factory = lambda ctx: paid
        self.ledger.sync(self.ctx, "emaillistverify", 500, source="test")
        emails = [f"person{i}@acme-test.example" for i in range(120)]
        results = self.email.validate(self.ctx, emails, allow_paid=True)
        self.assertEqual({r["status"] for r in results}, {"VALID"})
        self.assertEqual(len(paid.calls), 120)
        self.assertEqual(self.ledger.balance(self.ctx, "emaillistverify")["consumed"], 120)
        reserves = self.platform.store.all(self.ctx, "credit_ledger", {"entry_type": "reserve"})
        self.assertEqual(len(reserves), 3)

    def test_account_error_stops_further_calls(self) -> None:
        session = FakeSession({"verifyEmail": FakeResponse(200, text="insufficient_credits")})
        provider = EmailListVerifyProvider("k", session=session, sleep=lambda s: None)
        self.email.paid_factory = lambda ctx: provider
        self.ledger.sync(self.ctx, "emaillistverify", 500, source="test")
        results = self.email.validate(self.ctx, ["a@acme-test.example", "b@acme-test.example"], allow_paid=True)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual([r["status"] for r in results], ["UNKNOWN", "UNKNOWN"])
        self.assertIn("no_credits", results[1]["checks"]["paid_error"])
        self.assertEqual(self.ledger.balance(self.ctx, "emaillistverify")["consumed"], 0)


class ProviderRetryTests(unittest.TestCase):
    def test_retries_transient_errors_then_succeeds(self) -> None:
        session = FakeSession({"verifyEmail": [FakeResponse(503, text=""), FakeResponse(200, text="ok")]})
        sleeps = []
        provider = EmailListVerifyProvider("secret-k", session=session, sleep=sleeps.append)
        self.assertEqual(provider.check("a@x.example").status, "VALID")
        self.assertEqual((len(session.calls), provider.retries, sleeps), (2, 1, [1.0]))

    def test_gives_up_with_a_normalized_error_without_the_key(self) -> None:
        class Boom:
            def get(self, url, **kw):
                raise TimeoutError("secret-k")

        provider = EmailListVerifyProvider("secret-k", session=Boom(), sleep=lambda s: None, max_retries=2)
        with self.assertRaises(EmailProviderError) as caught:
            provider.check("a@x.example")
        self.assertEqual(caught.exception.code, "timeout")
        self.assertNotIn("secret-k", str(caught.exception))

    def test_permanent_errors_are_not_retried(self) -> None:
        session = FakeSession({"verifyEmail": FakeResponse(200, text="key_not_valid")})
        provider = EmailListVerifyProvider("k", session=session, sleep=lambda s: None)
        with self.assertRaises(EmailProviderError) as caught:
            provider.check("a@x.example")
        self.assertEqual((caught.exception.code, len(session.calls)), ("auth", 1))
        results = provider.check_many(["a@x.example", "b@x.example"])
        self.assertEqual(len(session.calls), 2)  # the second address was never sent
        self.assertTrue(all(isinstance(r, EmailProviderError) for r in results))

    def test_result_code_normalization(self) -> None:
        session = FakeSession({"verifyEmail": FakeResponse(200, text=" OK_FOR_ALL|catch-all \n")})
        self.assertEqual(EmailListVerifyProvider("k", session=session).check("a@x.example").status, "RISKY")


#: EmailListVerify's answer per local part (the fake session matches the URL-encoded address).
ELV_ANSWERS = {"ann": "ok", "bob": "email_disabled", "cara": "ok_for_all", "dan": "unknown", "eve": "ok",
               "fay": "ok", "info": "role"}


class VerifyUnknownsTests(unittest.TestCase):
    """Built-in checks first; only unresolved UNKNOWNs go to EmailListVerify, after confirmation."""

    def setUp(self) -> None:
        self.platform, self.ctx, self.email = make_platform()
        self.jobs = self.platform.service("email_jobs")
        self.ledger = self.platform.service("credits")
        registry = self.platform.service("providers")
        registry.set_credentials(self.ctx, "emaillistverify", {"api_key": "elv-test-key"})
        registry.verify(self.ctx, "emaillistverify", session=FakeSession(
            {"/api/credits": FakeResponse(200, {"onDemand": {"available": 1000}, "subscription": None})}))
        self.ledger.sync(self.ctx, "emaillistverify", 100, source="test")
        self.session = FakeSession({f"email={local}%40": FakeResponse(200, text=code)
                                    for local, code in ELV_ANSWERS.items()})
        self.email.paid_factory = lambda ctx: EmailListVerifyProvider("elv-test-key", session=self.session,
                                                                      sleep=lambda s: None)

    def sent(self):
        return [url.split("email=", 1)[1].split("%40", 1)[0] for _, url, _ in self.session.calls
                if "verifyEmail" in url]

    def built_in_job(self, locals_, extra=(), settings=None):
        rows = [{"email": f"{name}@acme-test.example"} for name in locals_] + [{"email": e} for e in extra]
        job = self.jobs.create_from_rows(self.ctx, name="t", rows=rows, email_field="email", source_type="manual")
        job = self.jobs.start(self.ctx, job["id"], settings=settings)
        self.assertEqual(run_task_inline(self.platform, self.ctx.workspace_id, job["task_id"])["status"], "completed")
        return self.jobs.get(self.ctx, job["id"])

    def verify(self, job):
        estimate = self.jobs.unknowns_estimate(self.ctx, job["id"])
        queued = self.jobs.verify_unknowns(self.ctx, job["id"], confirm=True, expected_credits=estimate["credits"])
        self.assertEqual(run_task_inline(self.platform, self.ctx.workspace_id, queued["task_id"])["status"],
                         "completed")
        return estimate, {i["email"].split("@")[0]: i for i in
                          self.jobs.items(self.ctx, job["id"], limit=500).rows}

    def test_unknowns_are_resolved_by_emaillistverify(self) -> None:
        job = self.built_in_job(["ann", "bob", "cara", "dan"], extra=["info@acme-test.example", "x@mailinator.com"])
        self.assertEqual(self.sent(), [])  # the built-in run never spends
        self.assertEqual((job["counts"]["UNKNOWN"], job["counts"]["unknown_builtin"]), (4, 4))
        estimate, items = self.verify(job)
        # Every NOT VERIFIED address the built-in checks answered is a candidate (the role inbox too);
        # the disposable one is not: a verifier would only confirm it is throw-away.
        self.assertEqual((estimate["unresolved"], estimate["to_check"], estimate["credits"]), (5, 5, 5.0))
        self.assertEqual(estimate["skipped_disposable"], 1)
        self.assertEqual(sorted(self.sent()), ["ann", "bob", "cara", "dan", "info"])
        self.assertEqual({k: items[k]["status"] for k in ("ann", "bob", "cara", "dan", "info", "x")},
                         {"ann": "VALID", "bob": "INVALID", "cara": "RISKY", "dan": "UNKNOWN", "info": "ROLE",
                          "x": "DISPOSABLE"})
        self.assertEqual(result_source(items["ann"]["provider"], items["ann"]["checks"]), "Built-in + EmailListVerify")
        self.assertEqual(items["ann"]["checks"]["builtin_status"], "UNKNOWN")
        self.assertEqual(result_source(items["x"]["provider"], items["x"]["checks"]), "Built-in")
        # EmailListVerify's own "unknown" is final (inconclusive), not a built-in unknown.
        self.assertEqual(items["dan"]["provider"], "emaillistverify")
        self.assertIn("EmailListVerify could not confirm", reason_for("UNKNOWN", items["dan"]["checks"]))
        self.assertEqual(self.ledger.balance(self.ctx, "emaillistverify")["consumed"], 5)
        done = self.jobs.get(self.ctx, job["id"])
        self.assertEqual((done["status"], done["settings"]["allow_paid"]), ("completed", False))
        self.assertEqual((done["counts"]["provider_emaillistverify"], done["counts"]["unknown_builtin"]), (5, 0))
        self.assertEqual((done["counts"]["final_valid"], done["counts"]["final_invalid"],
                          done["counts"]["final_not_verified"]), (1, 1, 4))
        # Recheck: nothing unresolved is left, so nothing is sent or charged again.
        again = self.jobs.unknowns_estimate(self.ctx, job["id"])
        self.assertEqual((again["unresolved"], again["credits"], again["can_verify"]), (0, 0, False))
        with self.assertRaises(ValidationError):
            self.jobs.verify_unknowns(self.ctx, job["id"], confirm=True, expected_credits=0)
        self.assertEqual(len(self.sent()), 5)

    def test_paid_run_needs_the_exact_confirmed_estimate(self) -> None:
        job = self.built_in_job(["ann", "bob"])
        for confirm, expected in ((False, 2), (True, None), (True, 1), (True, "3")):
            with self.assertRaises(ConflictError):
                self.jobs.verify_unknowns(self.ctx, job["id"], confirm=confirm, expected_credits=expected)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.jobs.get(self.ctx, job["id"])["status"], "completed")

    def test_already_resolved_emails_are_not_sent_again(self) -> None:
        first = self.built_in_job(["ann", "fay"])
        self.verify(first)
        self.assertEqual(sorted(self.sent()), ["ann", "fay"])
        # A new job: ann's VALID comes from the cache, only eve is unresolved.
        second = self.built_in_job(["ann", "eve"])
        self.assertEqual(second["counts"]["VALID"], 1)
        estimate, items = self.verify(second)
        self.assertEqual((estimate["unresolved"], estimate["credits"]), (1, 1.0))
        self.assertEqual(sorted(self.sent()), ["ann", "eve", "fay"])  # ann was not sent a second time
        self.assertEqual(items["eve"]["status"], "VALID")

    def test_estimate_counts_only_unresolved_and_reuses_fresh_answers(self) -> None:
        stale = self.built_in_job(["fay", "dan", "dan"])  # dan twice: one address, one credit
        before = self.jobs.unknowns_estimate(self.ctx, stale["id"])
        self.assertEqual((before["unresolved"], before["to_check"], before["credits"]), (2, 2, 2.0))
        self.verify(self.built_in_job(["fay"]))  # another job resolves fay
        after = self.jobs.unknowns_estimate(self.ctx, stale["id"])
        self.assertEqual((after["unresolved"], after["reused_from_cache"], after["to_check"], after["credits"]),
                         (2, 1, 1, 1.0))
        _, items = self.verify(stale)
        self.assertEqual(items["fay"]["status"], "VALID")
        self.assertEqual(self.sent().count("fay"), 1)

    def test_blockers_unknown_balance_and_no_provider(self) -> None:
        platform, ctx, _ = make_platform()
        jobs = platform.service("email_jobs")
        job = jobs.create_from_rows(ctx, name="t", rows=[{"email": "ann@acme-test.example"}], email_field="email")
        job = jobs.start(ctx, job["id"])
        run_task_inline(platform, ctx.workspace_id, job["task_id"])
        self.assertIn("not configured", jobs.unknowns_estimate(ctx, job["id"])["blocker"])
        registry = platform.service("providers")
        registry.set_credentials(ctx, "emaillistverify", {"api_key": "elv-test-key"})
        registry.verify(ctx, "emaillistverify", session=FakeSession(
            {"/api/credits": FakeResponse(200, {"onDemand": {"available": 50}, "subscription": None})}))
        self.assertIn("balance is unknown", jobs.unknowns_estimate(ctx, job["id"])["blocker"])
        # "Test connection" is the same free call; it now records the balance it reads.
        registry.verify = lambda c, name, **kw: {"status": "ok", "credits": 50}
        jobs.test_provider(ctx)
        estimate = jobs.unknowns_estimate(ctx, job["id"])
        self.assertEqual((estimate["credits_known"], estimate["credits_remaining"], estimate["can_verify"]),
                         (True, 50.0, True))

    def test_strict_mode_keeps_valid_for_external_verification(self) -> None:
        # A legacy cached VALID that EmailListVerify never produced.
        self.platform.store.insert(self.ctx, "email_validations", {
            "email": "legacy@acme-test.example", "status": "VALID", "score": 90.0, "checks": {}, "provider": "local",
            "validated_at": utcnow(), "expires_at": utcnow() + timedelta(days=30)})
        strict = self.built_in_job([], extra=["legacy@acme-test.example"])
        self.assertEqual((strict["counts"]["VALID"], strict["counts"]["UNKNOWN"]), (0, 1))
        item = self.jobs.items(self.ctx, strict["id"]).rows[0]
        self.assertTrue(item["checks"]["strict_demoted"])
        self.assertEqual(reason_for("UNKNOWN", item["checks"]), "Not externally verified (strict validation)")
        loose = self.built_in_job([], extra=["legacy@acme-test.example"], settings={"strict_validation": False})
        self.assertEqual(loose["counts"]["VALID"], 1)
        self.assertTrue(self.jobs.set_strict(self.ctx, loose["id"], True)["settings"]["strict_validation"])
        # Exports of VALID contain only VALID rows, with their source.
        _, content, _ = self.jobs.export(self.ctx, strict["id"], "csv", ["VALID"])
        self.assertEqual(len(list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))), 1)  # header only
        latest = self.built_in_job(["ann", "cara", "dan"])   # this job itself: "-created_at" can tie within a ms
        self.verify(latest)
        _, content, _ = self.jobs.export(self.ctx, latest["id"], "csv", ["VALID"], provider="emaillistverify")
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual([(r["validation_email"], r["validation_status"], r["validation_source"]) for r in rows],
                         [("ann@acme-test.example", "VALID", "Built-in + EmailListVerify")])


class EmailJobApiTests(unittest.TestCase):
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
                                 config=PlatformConfig(files_dir=root / "platform"))
        self.platform.override("email", EmailValidationService(self.platform, local=LocalValidator(resolver=resolver)))
        self.platform.service("email_jobs").dns_client = FakeDns()
        issuer = DevTokenIssuer("email-jobs-tests-secret-0123456789abcdef")
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = TestClient(app)
        self.client.headers["Authorization"] = f"Bearer {issuer.issue('alice@example.com')['access_token']}"
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        ws = self.client.post("/api/v1/workspaces", json={"name": "Alice", "seed": False}).json()["id"]
        self.base = f"/api/v1/w/{ws}"
        self.ws = ws

    def test_upload_start_items_export(self) -> None:
        r = self.client.post(self.base + "/email/jobs/upload", files={"files": ("leads.csv", SAMPLE, "text/csv")})
        self.assertEqual(r.status_code, 201, r.text)
        job = r.json()
        self.assertEqual(job["email_column"], "Work Email")
        self.assertIn("candidates", job["settings"])
        r = self.client.post(f"{self.base}/email/jobs/{job['id']}/start", json={"settings": {"allow_paid": False}})
        self.assertEqual(r.status_code, 200, r.text)
        done = run_task_inline(self.platform, self.ws, r.json()["task_id"])
        self.assertEqual(done["status"], "completed")
        r = self.client.get(f"{self.base}/email/jobs/{job['id']}")
        self.assertEqual(r.json()["status"], "completed")
        self.assertEqual(r.json()["counts"]["processed"], 7)
        r = self.client.get(f"{self.base}/email/jobs/{job['id']}/items", params={"status": "ROLE"})
        self.assertEqual(r.json()["total"], 1)
        r = self.client.get(f"{self.base}/email/jobs/{job['id']}/export", params={"format": "xlsx"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        r = self.client.get(self.base + "/email/provider")
        self.assertEqual(r.json()["emaillistverify"]["status"], "not_configured")
        self.assertNotIn("api_key", r.text)
        self.assertEqual(self.client.get(self.base + "/email/jobs").json()["total"], 1)
        self.assertEqual(self.client.post(f"{self.base}/email/jobs/{job['id']}/bogus").status_code, 404)
        self.assertEqual(self.client.delete(f"{self.base}/email/jobs/{job['id']}").status_code, 204)

    def test_pasted_emails_use_the_same_job_pipeline(self) -> None:
        """The dashboard's Paste Emails box posts the parsed addresses as a rows job (source_type
        "manual", start=True); it must behave exactly like an uploaded file's job."""
        pasted = ["ann.test@acme-test.example", "info@acme-test.example", "x@mailinator.com", "bob@nomx.example",
                  "free.user@gmail.com"]
        body = {"source": "rows", "source_type": "manual", "email_field": "email", "name": "Pasted emails (5)",
                "rows": [{"email": e} for e in pasted], "start": True}
        contacts_before = self.client.get(self.base + "/contacts").json()["total"]
        # Paid checks need a configured AND verified EmailListVerify, exactly as for uploads.
        r = self.client.post(self.base + "/email/jobs", json={**body, "settings": {"allow_paid": True}})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.post(self.base + "/email/jobs", json={**body, "settings": {"allow_paid": False,
                                                                                   "max_age_days": 30}})
        self.assertEqual(r.status_code, 201, r.text)
        job = r.json()
        self.assertEqual((job["source_type"], job["email_column"], job["row_count"], job["status"]),
                         ("manual", "email", 5, "queued"))
        done = run_task_inline(self.platform, self.ws, job["task_id"])
        self.assertEqual(done["status"], "completed")
        job = self.client.get(f"{self.base}/email/jobs/{job['id']}").json()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["counts"]["processed"], 5)
        self.assertEqual([job["counts"].get(s) for s in ("INVALID", "ROLE", "DISPOSABLE", "FREE_PROVIDER", "UNKNOWN")],
                         [1, 1, 1, 1, 1])
        items = self.client.get(f"{self.base}/email/jobs/{job['id']}/items", params={"limit": 500}).json()["items"]
        self.assertEqual(sorted(i["email"] for i in items), sorted(pasted))
        invalid = self.client.get(f"{self.base}/email/jobs/{job['id']}/items", params={"status": "INVALID"}).json()
        self.assertEqual([i["email"] for i in invalid["items"]], ["bob@nomx.example"])
        for fmt in ("csv", "xlsx"):
            r = self.client.get(f"{self.base}/email/jobs/{job['id']}/export", params={"format": fmt})
            self.assertEqual(r.status_code, 200, fmt)
            self.assertIn("attachment", r.headers["content-disposition"])
        # Validation never touches the CRM on its own.
        self.assertEqual(self.client.get(self.base + "/contacts").json()["total"], contacts_before)
        # The file upload path still works alongside it.
        r = self.client.post(self.base + "/email/jobs/upload", files={"files": ("leads.csv", SAMPLE, "text/csv")})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["email_column"], "Work Email")

    def test_unknowns_estimate_verify_and_strict_routes(self) -> None:
        r = self.client.post(self.base + "/email/jobs", json={
            "source": "rows", "source_type": "manual", "email_field": "email", "name": "Pasted emails (1)",
            "rows": [{"email": "ann.test@acme-test.example"}], "start": True})
        job = r.json()
        run_task_inline(self.platform, self.ws, job["task_id"])
        estimate = self.client.get(f"{self.base}/email/jobs/{job['id']}/unknowns").json()
        self.assertEqual((estimate["unresolved"], estimate["can_verify"]), (1, False))
        self.assertIn("not configured", estimate["blocker"])
        self.assertNotIn("api_key", json.dumps(estimate))
        r = self.client.post(f"{self.base}/email/jobs/{job['id']}/verify-unknowns",
                             json={"confirm": True, "expected_credits": 1})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.post(f"{self.base}/email/jobs/{job['id']}/strict", json={"enabled": False})
        self.assertEqual((r.status_code, r.json()["settings"]["strict_validation"]), (200, False))
        r = self.client.get(f"{self.base}/email/jobs/{job['id']}/export",
                            params={"format": "csv", "status_filter": "UNKNOWN", "provider_filter": "local"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("validation_source", r.text.splitlines()[0])
        self.assertIn("Built-in", r.text.splitlines()[1])
        r = self.client.get(f"{self.base}/email/jobs/{job['id']}/export", params={"provider_filter": "bogus"})
        self.assertEqual(r.status_code, 422)
        counts = self.client.get(f"{self.base}/email/jobs/{job['id']}").json()["counts"]
        self.assertEqual((counts["provider_local"], counts["unknown_builtin"]), (1, 1))

    def test_bad_uploads_are_422(self) -> None:
        r = self.client.post(self.base + "/email/jobs/upload", files={"files": ("x.pdf", b"%PDF", "application/pdf")})
        self.assertEqual(r.status_code, 422)
        r = self.client.post(self.base + "/email/jobs", json={"source": "nope"})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
