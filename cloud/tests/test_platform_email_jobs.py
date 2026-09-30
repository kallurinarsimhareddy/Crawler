"""Email validation jobs (Phase 1/2/17): upload, column detection, the task, pause/cancel,
results, export, EmailListVerify batching/retries/errors, and the explicit GTM actions."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

from cloud.intel.core.context import ConflictError, Ctx, ValidationError
from cloud.intel.email.jobs import detect_email_columns, extract_email, reason_for
from cloud.intel.email.providers import EmailListVerifyProvider, EmailProviderError, LocalValidator, ValidationResult
from cloud.intel.email.service import EmailValidationService
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.tests._platform_intel_helpers import RecordingAutomation
from cloud.tests.test_platform_sources_support import FakeResponse, FakeSession

NO_MX = {"nomx.example"}


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

    def test_bad_uploads_are_422(self) -> None:
        r = self.client.post(self.base + "/email/jobs/upload", files={"files": ("x.pdf", b"%PDF", "application/pdf")})
        self.assertEqual(r.status_code, 422)
        r = self.client.post(self.base + "/email/jobs", json={"source": "nope"})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
