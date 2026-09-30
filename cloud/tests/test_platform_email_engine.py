"""The layered email evidence engine (cloud/intel/email/engine.py) and how validation jobs
use it: format, DNS/MX, SPF/DMARC, SMTP preflight, catch-all, risk, contact matching,
public-web evidence, the three final statuses, and large-job chunking. Offline: DNS,
SMTP and web fetches are all fakes."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest import mock

from cloud.intel.email import engine
from cloud.intel.email.engine import (DnsAnswer, PublicEvidenceFinder, SmtpPreflight, check_format, contact_signals,
                                      evidence_summary, final_status, inspect_domain, public_evidence_for,
                                      risk_signals)
from cloud.intel.email.providers import EmailListVerifyProvider
from cloud.intel.tasks.worker import run_task_inline
from cloud.tests.test_platform_email_jobs import make_platform
from cloud.tests.test_platform_sources_support import FakeResponse, FakeSession


class ScriptedDns:
    """``{(name, rtype): DnsAnswer}``; anything unscripted is NXDOMAIN."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def lookup(self, name, rtype):
        self.calls.append((name, rtype))
        return self.answers.get((name, rtype), DnsAnswer(error="nxdomain"))


def healthy(domain, *, spf=True, dmarc=True):
    answers = {(domain, "MX"): DnsAnswer([f"10 mx1.{domain}.", f"20 mx2.{domain}."]),
               (domain, "TXT"): DnsAnswer(["v=spf1 include:_spf.google.com ~all"] if spf else ["google-site=x"]),
               (f"_dmarc.{domain}", "TXT"): DnsAnswer(["v=DMARC1; p=reject"]) if dmarc else DnsAnswer(error="nxdomain")}
    return answers


class FormatTests(unittest.TestCase):
    def test_valid_syntax(self) -> None:
        for email in ("john@company.com", "john.smith@company.co.uk", "o'brien@acme.io", "first+tag@acme.io"):
            self.assertTrue(check_format(email)["ok"], email)

    def test_invalid_syntax(self) -> None:
        cases = {"a..b@acme.com": "consecutive dots", ".ab@acme.com": "local part starts or ends with a dot",
                 "jo hn@acme.com": "whitespace inside the address", "x@acme": "malformed domain",
                 "a@b@acme.com": "more than one @", "nobody": "no @"}
        for email, issue in cases.items():
            result = check_format(email)
            self.assertFalse(result["ok"], email)
            self.assertIn(issue, result["issues"], email)

    def test_idn_placeholders_and_typos(self) -> None:
        idn = check_format("anna@bücher.de")
        self.assertEqual((idn["ok"], idn["idn"], idn["email"]), (True, True, "anna@xn--bcher-kva.de"))
        self.assertTrue(check_format("test@example.com")["placeholder"])
        self.assertEqual(check_format("jane@gmial.com")["did_you_mean"], "jane@gmail.com")
        self.assertEqual(check_format("jane@acme.con")["did_you_mean"], "jane@acme.com")
        self.assertEqual(check_format("  jane@acme.com ")["ok"], True)  # surrounding whitespace is trimmed


class DomainTests(unittest.TestCase):
    def test_domain_exists_with_mx_spf_and_dmarc(self) -> None:
        info = inspect_domain("acme.com", ScriptedDns(healthy("acme.com")))
        self.assertEqual((info["exists"], info["mail"], info["verdict"]), (True, True, "ok"))
        self.assertEqual(info["mx"], ["mx1.acme.com", "mx2.acme.com"])
        self.assertEqual((info["spf"], info["spf_all"], info["dmarc"], info["dmarc_policy"]),
                         ("present", "~all", "present", "reject"))

    def test_domain_missing(self) -> None:
        info = inspect_domain("no-such-domain.com", ScriptedDns({}))
        self.assertEqual((info["exists"], info["mail"], info["verdict"], info["dns_error"]),
                         (False, False, "invalid", "nxdomain"))
        self.assertIn("does not exist", info["invalid_reason"])

    def test_mx_fail_null_mx_and_no_mail_server(self) -> None:
        null = inspect_domain("nomail.com", ScriptedDns({("nomail.com", "MX"): DnsAnswer(["0 ."]),
                                                         ("nomail.com", "TXT"): DnsAnswer(["v=spf1 -all"])}))
        self.assertEqual((null["null_mx"], null["mail"], null["verdict"]), (True, False, "invalid"))
        bare = inspect_domain("bare.com", ScriptedDns({("bare.com", "MX"): DnsAnswer(error="no_answer"),
                                                       ("bare.com", "A"): DnsAnswer(error="no_answer"),
                                                       ("bare.com", "AAAA"): DnsAnswer(error="no_answer")}))
        self.assertEqual((bare["exists"], bare["mail"], bare["verdict"]), (True, False, "invalid"))

    def test_mx_pass_by_a_record_fallback(self) -> None:
        info = inspect_domain("small.com", ScriptedDns({("small.com", "MX"): DnsAnswer(error="no_answer"),
                                                        ("small.com", "A"): DnsAnswer(["192.0.2.10"])}))
        self.assertEqual((info["mail"], info["a_fallback"], info["verdict"]), (True, True, "ok"))

    def test_spf_and_dmarc_missing_are_signals_not_invalid(self) -> None:
        info = inspect_domain("acme.com", ScriptedDns(healthy("acme.com", spf=False, dmarc=False)))
        self.assertEqual((info["spf"], info["dmarc"], info["verdict"]), ("missing", "missing", "ok"))

    def test_transient_dns_is_unknown(self) -> None:
        for error in ("servfail", "timeout"):
            info = inspect_domain("flaky.com", ScriptedDns({("flaky.com", "MX"): DnsAnswer(error=error),
                                                            ("flaky.com", "TXT"): DnsAnswer(error=error),
                                                            ("_dmarc.flaky.com", "TXT"): DnsAnswer(error=error)}))
            self.assertEqual((info["mail"], info["verdict"], info["dns_error"], info["spf"]),
                             (None, "unknown", error, "unknown"))


class SmtpTests(unittest.TestCase):
    def test_smtp_success_and_failure_never_touch_a_mailbox(self) -> None:
        seen = []

        def ok(host):
            seen.append(host)
            return {"smtp": "pass", "code": 250, "starttls": True}

        self.assertEqual(SmtpPreflight(connect=ok).check("mx1.acme.com"),
                         {"smtp": "pass", "code": 250, "starttls": True, "host": "mx1.acme.com"})
        refused = SmtpPreflight(connect=lambda h: {"smtp": "fail", "detail": "connection refused"}).check("mx.x.com")
        self.assertEqual(refused["smtp"], "fail")
        self.assertEqual(seen, ["mx1.acme.com"])

    def test_the_real_connector_only_greets_and_quits(self) -> None:
        commands = []

        class FakeSMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                commands.append("quit")

            def connect(self, host, port):
                commands.append(f"connect {host}:{port}")
                return 220, b"hello"

            def ehlo(self):
                commands.append("ehlo")
                return 250, b"ok"

            def has_extn(self, name):
                return name == "starttls"

            def __getattr__(self, name):  # mail(), rcpt(), sendmail() ... must never be called
                raise AssertionError(f"SMTP preflight called {name}()")

        with mock.patch("smtplib.SMTP", FakeSMTP):
            result = SmtpPreflight().check("mx1.acme.com")
        self.assertEqual((result["smtp"], result["starttls"]), ("pass", True))
        self.assertEqual(commands, ["connect mx1.acme.com:25", "ehlo", "quit"])

    def test_timeout_is_unknown_not_invalid(self) -> None:
        class Slow:
            def __init__(self, *a, **k):
                raise TimeoutError("timed out")

        with mock.patch("smtplib.SMTP", Slow):
            self.assertEqual(SmtpPreflight().check("mx1.acme.com")["smtp"], "unknown")


class RiskAndContactTests(unittest.TestCase):
    def test_role_disposable_and_free_provider(self) -> None:
        self.assertTrue(risk_signals("info@acme.com")["role"])
        self.assertTrue(risk_signals("partssales@acme.com")["role"])
        self.assertTrue(risk_signals("x@mailinator.com")["disposable"])
        self.assertTrue(risk_signals("jane.doe@gmail.com")["free_provider"])
        self.assertTrue(risk_signals("no-reply@acme.com")["no_reply"])
        plain = risk_signals("jane.doe@acme.com")
        self.assertFalse(plain["role"] or plain["disposable"] or plain["free_provider"])

    def test_person_email_vs_role_email(self) -> None:
        person = {"first_name": "John", "last_name": "Smith", "company": "Acme Corp"}
        mine = contact_signals("john.smith@acmecorp.com", person)
        self.assertEqual((mine["person_name_match"], mine["company_match"], mine["role_email"]), ("yes", "yes", False))
        self.assertEqual(mine["contact_confidence_label"], "high")
        shared = contact_signals("info@acmecorp.com", person)
        self.assertEqual((shared["person_name_match"], shared["role_email"]), ("no", True))
        self.assertLessEqual(shared["contact_evidence_confidence"], 20)
        self.assertEqual(contact_signals("jsmith@acmecorp.com", person)["person_name_match"], "yes")
        self.assertEqual(contact_signals("john@acme.com", {})["person_name_match"], "unknown")

    def test_person_and_company_match(self) -> None:
        self.assertEqual(contact_signals("n.reddy@riseits.com", {"full_name": "Narsimha Reddy",
                                                                 "company": "RiseITS Inc."})["company_match"], "yes")
        self.assertEqual(contact_signals("jane@gmail.com", {"first_name": "Jane", "company": "Acme"})["company_match"],
                         "no")
        self.assertEqual(contact_signals("jane@other.io", {"first_name": "Jane", "company": "Acme",
                                                           "website": "https://www.other.io/about"})["company_match"],
                         "yes")
        self.assertEqual(contact_signals("jane@ibm.com", {"first_name": "Jane",
                                                          "company": "International Business Machines"}
                                         )["company_match"], "yes")
        self.assertEqual(engine.detect_contact_columns(["First Name", "Last Name", "Company", "Job Title", "Email"]),
                         {"first_name": "First Name", "last_name": "Last Name", "company": "Company",
                          "title": "Job Title"})


@dataclass
class Page:
    status: int
    text: str = ""
    final_url: str = ""
    blocked: bool = False
    error: str = None

    @property
    def ok(self):
        return 200 <= self.status < 300


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        page = self.pages.get(url, Page(404))
        page.final_url = page.final_url or url
        return page


class PublicEvidenceTests(unittest.TestCase):
    def test_exact_email_on_the_company_team_page(self) -> None:
        fetcher = FakeFetcher({
            "https://acme.com": Page(200, "<html>Welcome</html>"),
            "https://acme.com/team": Page(200, "<li><b>John Smith</b>, CFO — john.smith&#64;acme.com</li>"
                                               "<li>Press: press@acme.com</li>")})
        scan = PublicEvidenceFinder(fetcher).scan("acme.com")
        self.assertTrue(all(u.startswith("https://acme.com") for u in fetcher.urls))  # only the company's own site
        evidence = public_evidence_for("john.smith@acme.com", scan, {"first_name": "John", "last_name": "Smith"},
                                       checked_at="2026-09-30T00:00:00")
        self.assertEqual((evidence["public_email_evidence"], evidence["source_type"], evidence["evidence_confidence"]),
                         (True, "team page", "high"))
        self.assertEqual(evidence["source_url"], "https://acme.com/team")
        self.assertEqual(public_evidence_for("jane@acme.com", scan, {})["public_email_evidence"], False)
        self.assertNotIn("john.smith@acme.com", str(scan))  # only hashes of addresses are kept

    def test_blocked_or_unreachable_site_is_unknown(self) -> None:
        scan = PublicEvidenceFinder(FakeFetcher({"https://closed.com": Page(403, blocked=True)})).scan("closed.com")
        self.assertIsNone(public_evidence_for("a@closed.com", scan, {})["public_email_evidence"])

    def test_public_evidence_alone_never_makes_an_address_valid(self) -> None:
        checks = {"evidence": {"public": {"public_email_evidence": True}}}
        self.assertEqual(final_status("UNKNOWN", "local"), "NOT_VERIFIED")
        summary = evidence_summary("UNKNOWN", "local", checks)
        self.assertEqual((summary["public_evidence"], summary["mailbox_verification"], summary["source"]),
                         ("YES", "UNKNOWN", "Built-in + Public evidence"))


class FinalStatusTests(unittest.TestCase):
    def test_only_three_final_statuses(self) -> None:
        self.assertEqual(final_status("VALID", "emaillistverify"), "VALID")
        self.assertEqual(final_status("VALID", "local"), "NOT_VERIFIED")  # never VALID without a verifier
        self.assertEqual(final_status("INVALID", "local"), "INVALID")
        self.assertEqual(final_status("INVALID", "emaillistverify"), "INVALID")
        for status in ("UNKNOWN", "RISKY", "ROLE", "DISPOSABLE", "FREE_PROVIDER"):
            self.assertEqual(final_status(status, "local"), "NOT_VERIFIED", status)
            self.assertEqual(final_status(status, "emaillistverify"), "NOT_VERIFIED", status)
        self.assertEqual(set(engine.FINAL_STATUSES), {"VALID", "INVALID", "NOT_VERIFIED"})

    def test_catch_all_comes_from_the_verifier(self) -> None:
        self.assertIs(engine.catch_all_from("emaillistverify", {"result_code": "ok_for_all"}), True)
        self.assertIs(engine.catch_all_from("emaillistverify", {"result_code": "ok"}), False)
        self.assertIsNone(engine.catch_all_from("local", {"mx": True}))
        summary = evidence_summary("RISKY", "emaillistverify", {"result_code": "ok_for_all",
                                                                "evidence": {"catch_all": True}})
        self.assertEqual((summary["catch_all"], summary["mailbox_verification"]), ("YES", "UNKNOWN"))


class ContactJobTests(unittest.TestCase):
    """The job pipeline with every layer on, offline."""

    def setUp(self) -> None:
        self.platform, self.ctx, self.email = make_platform()
        self.jobs = self.platform.service("email_jobs")
        dns = {**healthy("acme-corp.com"), **healthy("widgets.io"), **healthy("acme-test.example")}
        dns[("gone-company.com", "MX")] = DnsAnswer(error="nxdomain")
        self.jobs.dns_client = ScriptedDns(dns)
        self.smtp_hosts = []
        self.jobs.smtp_connect = lambda host: self.smtp_hosts.append(host) or {"smtp": "pass", "code": 250}
        self.fetcher = FakeFetcher({"https://acme-corp.com": Page(200, "Acme"),
                                    "https://acme-corp.com/team": Page(200, "John Smith CFO john.smith@acme-corp.com")})
        self.jobs.public_fetcher = self.fetcher

    def run_rows(self, rows, **settings):
        job = self.jobs.create_from_rows(self.ctx, name="contacts", rows=rows, email_field="email",
                                         source_type="manual", start=True, settings=settings)
        self.assertEqual(run_task_inline(self.platform, self.ctx.workspace_id, job["task_id"])["status"], "completed")
        return self.jobs.get(self.ctx, job["id"]), {r["email"]: r for r in self.jobs.items(self.ctx, job["id"],
                                                                                          limit=500).rows}

    def test_contact_mode_signals_public_evidence_and_three_statuses(self) -> None:
        rows = [{"first_name": "John", "last_name": "Smith", "company": "Acme Corp", "title": "CFO",
                 "email": "john.smith@acme-corp.com"},
                {"first_name": "John", "last_name": "Smith", "company": "Acme Corp", "title": "CFO",
                 "email": "info@acme-corp.com"},
                {"first_name": "Mia", "last_name": "Lee", "company": "Gone Co", "title": "VP",
                 "email": "mia.lee@gone-company.com"},
                {"first_name": "Bad", "last_name": "Row", "company": "X", "title": "", "email": "bad..row@x.com"}]
        job, items = self.run_rows(rows, public_evidence=True, smtp_preflight=True)
        self.assertTrue(job["settings"]["contact_mode"])
        person, shared = items["john.smith@acme-corp.com"], items["info@acme-corp.com"]
        self.assertEqual(person["final_status"], "NOT_VERIFIED")  # published + SMTP OK is still not a verified mailbox
        self.assertEqual({k: person["summary"][k] for k in ("technical", "domain", "mx", "spf", "dmarc", "smtp",
                                                             "role", "person_match", "public_evidence",
                                                             "mailbox_verification", "source")},
                         {"technical": "PASS", "domain": "PASS", "mx": "PASS", "spf": "PASS", "dmarc": "PASS",
                          "smtp": "PASS", "role": "NO", "person_match": "YES", "public_evidence": "YES",
                          "mailbox_verification": "UNKNOWN", "source": "Built-in + Public evidence"})
        contact = person["checks"]["evidence"]["contact"]
        self.assertEqual((contact["company_match"], contact["contact_confidence_label"]), ("yes", "high"))
        self.assertEqual((shared["summary"]["role"], shared["summary"]["person_match"]), ("YES", "NO"))
        self.assertEqual(items["mia.lee@gone-company.com"]["final_status"], "INVALID")  # NXDOMAIN
        self.assertIn("does not exist", items["mia.lee@gone-company.com"]["checks"]["domain_invalid"])
        self.assertEqual(items["bad..row@x.com"]["final_status"], "INVALID")
        self.assertEqual((job["counts"]["final_valid"], job["counts"]["final_invalid"],
                          job["counts"]["final_not_verified"]), (0, 2, 2))
        self.assertEqual(self.smtp_hosts, ["mx1.acme-corp.com"])  # once per mail host, not per address
        self.assertEqual(len([u for u in self.fetcher.urls if u == "https://acme-corp.com"]), 1)  # once per site

    def test_slow_layers_are_opt_in_and_cached_across_jobs(self) -> None:
        rows = [{"email": "john.smith@acme-corp.com"}]
        _, items = self.run_rows(rows)
        self.assertNotIn("smtp", items["john.smith@acme-corp.com"]["checks"]["evidence"])
        self.assertEqual((self.smtp_hosts, self.fetcher.urls), ([], []))
        self.run_rows(rows, public_evidence=True, smtp_preflight=True)
        dns_calls, fetches = len(self.jobs.dns_client.calls), len(self.fetcher.urls)
        self.run_rows(rows, public_evidence=True, smtp_preflight=True)  # a second job reuses the cache
        self.assertEqual((len(self.jobs.dns_client.calls), len(self.fetcher.urls), len(self.smtp_hosts)),
                         (dns_calls, fetches, 1))
        cached = self.platform.store.all(self.ctx, "email_check_cache", {})
        self.assertEqual({r["check_type"] for r in cached}, {"dns", "smtp", "public"})
        self.assertNotIn("acme-corp.com", str([r["subject_hash"] for r in cached]))  # hashed subjects only

    def test_catch_all_domains_are_learned_and_not_paid_for_again(self) -> None:
        registry = self.platform.service("providers")
        registry.set_credentials(self.ctx, "emaillistverify", {"api_key": "elv-test-key"})
        registry.verify(self.ctx, "emaillistverify", session=FakeSession(
            {"/api/credits": FakeResponse(200, {"onDemand": {"available": 1000}, "subscription": None})}))
        self.platform.service("credits").sync(self.ctx, "emaillistverify", 100, source="test")
        session = FakeSession({"email=ann%40": FakeResponse(200, text="accept_all"),
                               "email=bob%40": FakeResponse(200, text="ok")})
        self.email.paid_factory = lambda ctx: EmailListVerifyProvider("elv-test-key", session=session,
                                                                      sleep=lambda s: None)
        job, _ = self.run_rows([{"email": "ann@widgets.io"}])
        estimate = self.jobs.unknowns_estimate(self.ctx, job["id"])
        self.jobs.verify_unknowns(self.ctx, job["id"], confirm=True, expected_credits=estimate["credits"])
        run_task_inline(self.platform, self.ctx.workspace_id, self.jobs.get(self.ctx, job["id"])["task_id"])
        ann = self.jobs.items(self.ctx, job["id"]).rows[0]
        self.assertEqual((ann["final_status"], ann["summary"]["catch_all"]), ("NOT_VERIFIED", "YES"))
        # A new job on the same domain: the known catch-all address is not sent (or charged).
        second, _ = self.run_rows([{"email": "carl@widgets.io"}, {"email": "bob@acme-corp.com"}])
        estimate = self.jobs.unknowns_estimate(self.ctx, second["id"])
        self.assertEqual((estimate["unresolved"], estimate["skipped_catch_all"], estimate["credits"]), (1, 1, 1.0))


class ChunkingTests(unittest.TestCase):
    def test_large_uploads_are_streamed_and_processed_in_batches(self) -> None:
        from cloud.intel.email import jobs as jobs_module

        platform, ctx, _ = make_platform()
        jobs = platform.service("email_jobs")
        header = "Email,Name\n"
        body = "".join(f"user{i}@acme-test.example,User {i}\n" for i in range(1234))
        seen_batches = []
        original = jobs_module.EmailValidationJobService._process

        def spy(self, ctx, email_service, batch, *a, **k):
            seen_batches.append(len(batch))
            return original(self, ctx, email_service, batch, *a, **k)

        with mock.patch.object(jobs_module, "BATCH", 100), mock.patch.object(
                jobs_module.EmailValidationJobService, "_process", spy):
            job = jobs.create_upload(ctx, "big.csv", (header + body).encode())
            self.assertEqual((job["row_count"], job["counts"]["total"]), (1234, 1234))
            job = jobs.start(ctx, job["id"])
            self.assertEqual(run_task_inline(platform, ctx.workspace_id, job["task_id"])["status"], "completed")
        self.assertEqual(sum(seen_batches), 1234)
        self.assertEqual(max(seen_batches), 100)
        self.assertEqual(len(seen_batches), 13)
        done = jobs.get(ctx, job["id"])
        self.assertEqual((done["counts"]["processed"], done["counts"]["pending"]), (1234, 0))

    def test_limits_allow_a_million_rows_without_manual_splitting(self) -> None:
        from cloud.intel.email import jobs as jobs_module

        self.assertGreaterEqual(jobs_module.MAX_ROWS, 1_000_000)
        platform, ctx, _ = make_platform()
        jobs = platform.service("email_jobs")
        with mock.patch.object(jobs_module, "MAX_ROWS", 50):
            job = jobs.create_upload(ctx, "big.csv", ("Email\n" + "".join(f"u{i}@a.example\n" for i in range(80))
                                                      ).encode())
        self.assertEqual(job["row_count"], 50)
        self.assertTrue(job["settings"]["truncated"])


class BackgroundIngestTests(unittest.TestCase):
    """Large uploads: the request only samples the file; the worker reads it in bulk chunks."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from cloud.intel.email import jobs as jobs_module
        from cloud.shared.storage import LocalFileStorage

        self.jobs_module = jobs_module
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.platform, self.ctx, _ = make_platform()
        self.platform.storage = LocalFileStorage(Path(self.scratch.name))
        self.jobs = self.platform.service("email_jobs")
        patches = [mock.patch.object(jobs_module, "INLINE_INGEST_BYTES", 100),
                   mock.patch.object(jobs_module, "INGEST_CHUNK", 100)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def csv(n):
        return ("Email,First Name,Last Name,Company\n" + "".join(
            f"user{i}@acme-test.example,First{i},Last{i},Acme\n" for i in range(n))).encode()

    def ingest(self, job):
        task_id = job["settings"]["ingest"]["task_id"]
        done = run_task_inline(self.platform, self.ctx.workspace_id, task_id)
        self.assertEqual(done["status"], "completed", done.get("error"))
        return self.jobs.get(self.ctx, job["id"])

    def test_the_upload_request_only_samples_and_queues(self) -> None:
        from cloud.intel.core.context import ConflictError

        inserts = []
        original = self.platform.store.insert_many
        self.platform.store.insert_many = lambda ctx, entity, rows: inserts.append(len(rows)) or original(ctx, entity,
                                                                                                          rows)
        job = self.jobs.create_upload(self.ctx, "big.csv", self.csv(1234))
        self.assertEqual(inserts, [])  # no rows written during the request
        ingest = job["settings"]["ingest"]
        self.assertEqual((ingest["state"], job["row_count"], job["email_column"]), ("pending", 0, "Email"))
        self.assertTrue(job["settings"]["contact_mode"])
        self.assertEqual(len(job["preview"]), 8)
        self.assertTrue(self.platform.storage.exists(ingest["storage_key"]))
        with self.assertRaises(ConflictError):  # validation waits until the file is read
            self.jobs.start(self.ctx, job["id"])
        job = self.ingest(job)
        self.assertEqual((job["row_count"], job["counts"]["total"], job["settings"]["ingest"]["state"]),
                         (1234, 1234, "done"))
        self.assertEqual(inserts, [100] * 12 + [34])  # bulk chunks of INGEST_CHUNK
        self.assertFalse(self.platform.storage.exists(ingest["storage_key"]))  # the rows now live in the job
        job = self.jobs.start(self.ctx, job["id"])
        self.assertEqual(run_task_inline(self.platform, self.ctx.workspace_id, job["task_id"])["status"], "completed")
        self.assertEqual(self.jobs.get(self.ctx, job["id"])["counts"]["processed"], 1234)

    def test_ingest_resumes_without_duplicating_rows(self) -> None:
        from cloud.intel.imports.parse import iter_rows

        data = self.csv(500)
        job = self.jobs.create_upload(self.ctx, "big.csv", data)
        # A previous attempt got 300 rows in before it stopped.
        self.jobs._insert_items(self.ctx, job["id"], (pair for pair in iter_rows("csv", data, max_rows=300)))  # noqa: SLF001
        job = self.ingest(job)
        rows = self.platform.store.all(self.ctx, "email_validation_items", {"job_id": job["id"]})
        numbers = sorted(r["row_number"] for r in rows)
        self.assertEqual((job["row_count"], len(rows), numbers), (500, 500, list(range(1, 501))))

    def test_ingest_stops_at_the_row_limit(self) -> None:
        with mock.patch.object(self.jobs_module, "MAX_ROWS", 250):
            job = self.ingest(self.jobs.create_upload(self.ctx, "big.csv", self.csv(400)))
        self.assertEqual(job["row_count"], 250)
        self.assertTrue(job["settings"]["truncated"])
        self.assertTrue(any("250" in p for p in job["settings"]["problems"]))

    def test_deleting_a_job_that_is_still_reading_removes_the_upload(self) -> None:
        job = self.jobs.create_upload(self.ctx, "big.csv", self.csv(300))
        key = job["settings"]["ingest"]["storage_key"]
        self.jobs.delete(self.ctx, job["id"])
        self.assertFalse(self.platform.storage.exists(key))
        task = self.platform.tasks.get(self.ctx, job["settings"]["ingest"]["task_id"])
        self.assertEqual(task["status"], "cancelled")

    def test_results_are_written_back_in_one_bulk_update_per_batch(self) -> None:
        job = self.ingest(self.jobs.create_upload(self.ctx, "big.csv", self.csv(1200)))
        store = self.platform.store
        calls = {"update_many_items": 0, "update_items": 0}
        original_many, original_one = store.update_many, store.update

        def many(ctx, entity, changes):
            if entity == "email_validation_items":
                calls["update_many_items"] += 1
            return original_many(ctx, entity, changes)

        def one(ctx, entity, row_id, changes, **kw):
            if entity == "email_validation_items":
                calls["update_items"] += 1
            return original_one(ctx, entity, row_id, changes, **kw)

        store.update_many, store.update = many, one
        job = self.jobs.start(self.ctx, job["id"])
        run_task_inline(self.platform, self.ctx.workspace_id, job["task_id"])
        self.assertEqual(calls, {"update_many_items": 3, "update_items": 0})  # BATCH=500: 500 + 500 + 200
        self.assertEqual(self.jobs.get(self.ctx, job["id"])["counts"]["processed"], 1200)


if __name__ == "__main__":
    unittest.main()
