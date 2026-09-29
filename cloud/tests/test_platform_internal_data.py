"""Internal data (12–30 file batches: schema comparison, ambiguity-aware mapping,
per-file mapping, conflict review, history), company enrichment routing, job-source
status/normalisation, and the scraper/research → GTM bridge."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.imports.internal import type_hint, value_conflicts
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.providers.base import ProviderError
from cloud.intel.providers.enrichment import PartnerApiConnector, PublicCompanyResearch, same_company
from cloud.intel.sources.adapters import IndeedAdapter, LinkedInAdapter
from cloud.intel.sources.base import JOB_FIELDS, SourceQuery, SourceUnavailable, normalize_posting
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage


def csv_bytes(header, rows):
    return ("\n".join([",".join(header)] + [",".join(r) for r in rows]) + "\n").encode()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "Internal", f"int-{uuid.uuid4().hex[:8]}")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.platform = Platform(self.store, storage=LocalFileStorage(Path(scratch.name)),
                                 config=PlatformConfig(files_dir=Path(scratch.name)))
        self.svc = self.platform.service("internal_data")

    def run_task(self, task):
        run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        done = self.platform.tasks.get(self.ctx, task["id"])
        self.assertEqual(done["status"], "completed", done["error"])
        return done


class TestTypeHintsAndConflicts(unittest.TestCase):
    def test_type_hints(self) -> None:
        self.assertEqual(type_hint(["a@b.com", "c@d.org"]), "email")
        self.assertEqual(type_hint(["https://a.com", "www.b.com"]), "url")
        self.assertEqual(type_hint(["acme.com", "beta.io"]), "domain")
        self.assertEqual(type_hint(["1,200", "35"]), "number")
        self.assertEqual(type_hint(["2024-01-02"]), "date")
        self.assertEqual(type_hint(["", ""]), "empty")
        self.assertEqual(type_hint(["Acme", "a@b.com"]), "text")

    def test_value_conflicts_ignore_blanks_case_and_derived(self) -> None:
        out = value_conflicts({"industry": "Manufacturing", "city": "", "name": "Acme"},
                              {"industry": "Retail", "city": "Tulsa", "name": "Acme Inc"}, "company")
        self.assertEqual([c["field"] for c in out], ["company.industry"])
        self.assertEqual(value_conflicts({"industry": "retail"}, {"industry": "Retail"}, "company"), [])


class TestInternalDataBatch(Base):
    def _batch_of_thirteen(self):
        batch = self.svc.create_batch(self.ctx, "Q3 internal", "companies")
        files = []
        for i in range(12):
            files.append((f"accounts_{i}.csv", csv_bytes(("Company Name", "Website", "Industry"),
                                                         [(f"Co{i}a", f"co{i}a.com", "Manufacturing"),
                                                          (f"Co{i}b", f"co{i}b.com", "Retail")])))
        files.append(("crm_export.csv", csv_bytes(("Account", "Website", "Name", "Owner"),
                                                  [("Zeta", "zeta.com", "Pat Lee", "pat@zeta.com")])))
        result = self.svc.add_files(self.ctx, batch["id"], files)
        self.assertEqual(len(result["added"]), 13)
        self.assertEqual(result["failed"], [])
        return batch

    def test_schema_comparison_across_thirteen_files(self) -> None:
        batch = self._batch_of_thirteen()
        report = self.svc.compare_schema(self.ctx, batch["id"])
        self.assertEqual(report["file_count"], 13)
        self.assertEqual(report["common_columns"], ["Website"])
        by_key = {c["key"]: c for c in report["columns"]}
        self.assertEqual(by_key["companyname"]["files_present"], 12)
        self.assertEqual(by_key["website"]["type_hint"], "domain")
        export = next(f for f in report["files"] if f["filename"] == "crm_export.csv")
        self.assertIn("Company Name", export["missing"])
        self.assertIn("Account", export["extra"])
        self.assertEqual(len(report["layouts"]), 2)
        stored = self.store.get(self.ctx, "import_batches", batch["id"])
        self.assertEqual(stored["schema_report"]["file_count"], 13)

    def test_duplicate_and_bad_files_do_not_stop_the_rest(self) -> None:
        batch = self.svc.create_batch(self.ctx, "B", "companies")
        data = csv_bytes(("Company",), [("A",)])
        result = self.svc.add_files(self.ctx, batch["id"], [("a.csv", data), ("a2.csv", data), ("x.pdf", b"%PDF")])
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(len(result["failed"]), 2)

    def test_more_than_thirty_files_refused(self) -> None:
        batch = self.svc.create_batch(self.ctx, "B", "companies")
        files = [(f"f{i}.csv", csv_bytes(("Company",), [(f"C{i}",)])) for i in range(31)]
        with self.assertRaises(ValidationError):
            self.svc.add_files(self.ctx, batch["id"], files)

    def test_ambiguous_columns_need_an_explicit_decision(self) -> None:
        batch = self._batch_of_thirteen()
        review = self.svc.mapping_review(self.ctx, batch["id"])
        statuses = {c["column"]: c["status"] for c in review["columns"]}
        self.assertEqual(statuses["Company Name"], "confident")
        self.assertEqual(statuses["Name"], "ambiguous")
        self.assertIn("Name", review["requires_decision"])
        # the "Owner" column holds emails: never guessed
        self.assertEqual(statuses["Owner"], "unmapped")
        mapping = {"Company Name": "company.name", "Account": "company.name", "Website": "company.website",
                   "Industry": "company.industry"}
        with self.assertRaises(ValidationError) as caught:
            self.svc.apply_mapping(self.ctx, batch["id"], mapping)
        self.assertIn("Name", str(caught.exception))
        report = self.svc.apply_mapping(self.ctx, batch["id"], {**mapping, "Name": None})
        self.assertTrue(report["mapped"])

    def test_per_file_mapping_and_conflict_review(self) -> None:
        crm = self.platform.service("crm")
        crm.upsert_company(self.ctx, {"name": "Acme", "domain": "acme.com", "industry": "Manufacturing"},
                           source_kind="manual", source_name="seed")
        batch = self.svc.create_batch(self.ctx, "Conflicts", "companies")
        self.svc.add_files(self.ctx, batch["id"], [
            ("a.csv", csv_bytes(("Company Name", "Website", "Industry"), [("Acme", "acme.com", "Retail"),
                                                                           ("Beta", "beta.com", "Food")])),
            ("b.csv", csv_bytes(("Company Name", "Website", "Segment"), [("Gamma", "gamma.com", "Chemicals")]))])
        files = {f["filename"]: f for f in self.platform.service("imports").files(self.ctx, batch["id"])}
        self.svc.apply_mapping(self.ctx, batch["id"],
                               {"Company Name": "company.name", "Website": "company.website",
                                "Industry": "company.industry"},
                               file_mappings={files["b.csv"]["id"]: {"Segment": "company.industry"}})
        task = self.svc.merge(self.ctx, batch["id"])
        self.assertTrue(task["params"]["conflict_review"])
        done = self.run_task(task)
        self.assertEqual(done["result"]["conflicts"], 1)
        gamma = self.store.first(self.ctx, "companies", {"domain": "gamma.com"})
        self.assertEqual(gamma["industry"], "Chemicals")          # per-file mapping applied
        acme = self.store.first(self.ctx, "companies", {"domain": "acme.com"})
        self.assertEqual(acme["industry"], "Manufacturing")       # existing value kept
        listed = self.svc.conflicts(self.ctx, batch["id"])
        self.assertEqual(listed["total"], 1)
        row = listed["items"][0]
        self.assertEqual(row["conflicts"][0]["field"], "company.industry")
        self.assertEqual(self.store.get(self.ctx, "import_batches", batch["id"])["conflict_count"], 1)
        with self.assertRaises(ValidationError):
            self.svc.resolve_conflicts(self.ctx, batch["id"], row["id"], {"company.city": "keep"})
        resolved = self.svc.resolve_conflicts(self.ctx, batch["id"], row["id"], {"company.industry": "take_new"})
        self.assertEqual(resolved["status"], "merged")
        self.assertEqual(self.store.get(self.ctx, "companies", acme["id"])["industry"], "Retail")
        self.assertEqual(self.store.get(self.ctx, "import_batches", batch["id"])["conflict_count"], 0)
        rules = [r["match_rule"] for r in self.store.all(self.ctx, "source_records", {"entity_id": acme["id"]})]
        self.assertIn("conflict review", rules)
        history = self.svc.history(self.ctx)
        self.assertEqual(history["items"][0]["id"], batch["id"])

    def test_plain_import_merge_is_unchanged_without_conflict_review(self) -> None:
        crm = self.platform.service("crm")
        crm.upsert_company(self.ctx, {"name": "Acme", "domain": "acme.com", "industry": "Manufacturing"},
                           source_kind="manual", source_name="seed")
        imports = self.platform.service("imports")
        batch = imports.create_batch(self.ctx, "Plain", "companies")
        imports.add_file(self.ctx, batch["id"], "a.csv", csv_bytes(("Company Name", "Website", "Industry"),
                                                                   [("Acme", "acme.com", "Retail")]))
        imports.set_mapping(self.ctx, batch["id"], {"Company Name": "company.name", "Website": "company.website",
                                                    "Industry": "company.industry"})
        self.run_task(imports.merge(self.ctx, batch["id"]))
        self.assertEqual(self.store.count(self.ctx, "import_rows", {"status": "conflict"}), 0)

    def test_schema_task_runs_in_the_worker(self) -> None:
        batch = self._batch_of_thirteen()
        task = self.platform.tasks.submit(self.ctx, "internal_data", {"batch_id": batch["id"], "action": "schema"})
        done = self.run_task(task)
        self.assertEqual(done["result"]["files"], 13)


# --- enrichment -----------------------------------------------------------------------------


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url, **kw):
        self.calls.append((url, kw))
        status, text = self.pages.get(url, (404, ""))
        return SimpleNamespace(ok=200 <= status < 300, blocked=status in (401, 403, 429), status=status,
                               text=text, final_url=url, error=None, json=lambda: json.loads(text))


HOME = """<html><head><meta name="description" content="Acme makes widgets">
<script type="application/ld+json">{"@type": "Organization", "name": "Acme", "url": "https://acme.com",
"address": {"addressLocality": "Tulsa", "addressRegion": "OK", "addressCountry": "US"},
"numberOfEmployees": {"value": "250"}, "sameAs": ["https://www.linkedin.com/company/acme"]}</script>
</head><body></body></html>"""


class FakeConnector:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, 0

    def estimate_cost(self, op, n):
        return 1.0

    def enrich_company(self, identifiers, *, allow_paid=False):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answer


class TestEnrichment(Base):
    def setUp(self) -> None:
        super().setUp()
        self.company = self.platform.service("crm").upsert_company(
            self.ctx, {"name": "Acme", "domain": "acme.com"}, source_kind="manual", source_name="seed")["company"]
        self.enrich = self.platform.service("enrichment")

    def test_public_web_fills_blanks_with_provenance(self) -> None:
        self.enrich.fetcher = FakeFetcher({"https://acme.com": (200, HOME)})
        result = self.enrich.enrich_company(self.ctx, self.company["id"], providers=["public_web"])
        company = self.store.get(self.ctx, "companies", self.company["id"])
        self.assertEqual(company["city"], "Tulsa")
        self.assertEqual(company["employee_count"], 250)
        self.assertEqual(company["linkedin_url"], "https://www.linkedin.com/company/acme")
        self.assertEqual(result["filled"]["city"]["source"], "public_web")
        kinds = [r["source_kind"] for r in self.store.all(self.ctx, "source_records",
                                                          {"entity_id": self.company["id"]})]
        self.assertIn("public_web", kinds)

    def test_blocked_site_is_not_bypassed(self) -> None:
        self.enrich.fetcher = FakeFetcher({"https://acme.com": (403, "")})
        result = self.enrich.enrich_company(self.ctx, self.company["id"], providers=["public_web"])
        step = next(s for s in result["steps"] if s["source"] == "public_web")
        self.assertEqual(step["status"], "blocked")
        self.assertEqual(len(self.enrich.fetcher.calls), 1)

    def test_unconfigured_paid_sources_are_honest(self) -> None:
        self.enrich.fetcher = FakeFetcher({})
        result = self.enrich.enrich_company(self.ctx, self.company["id"], allow_paid=True)
        states = {s["source"]: s["status"] for s in result["steps"]}
        for name in ("zoominfo", "seamless", "partner_api"):
            self.assertEqual(states[name], "not_configured")
        sources = {s["name"]: s for s in self.enrich.sources(self.ctx)}
        self.assertEqual(sources["partner_api"]["status"], "not_configured")
        self.assertTrue(sources["partner_api"]["paid"])

    def test_paid_needs_allow_paid_and_reserves_then_consumes(self) -> None:
        self.enrich.fetcher = FakeFetcher({})
        connector = FakeConnector({"name": "Acme", "website": "https://acme.com", "industry": "Industrial",
                                   "employee_count": 900})
        self.enrich.connectors["zoominfo"] = connector
        ledger = self.platform.service("credits")
        ledger.sync(self.ctx, "zoominfo", 10, source="test")
        skipped = self.enrich.enrich_company(self.ctx, self.company["id"], providers=["zoominfo"])
        self.assertEqual({s["source"]: s["status"] for s in skipped["steps"]}["zoominfo"], "skipped_paid")
        self.assertEqual(connector.calls, 0)
        result = self.enrich.enrich_company(self.ctx, self.company["id"], providers=["zoominfo"], allow_paid=True)
        self.assertEqual(result["filled"]["industry"]["source"], "zoominfo")
        self.assertEqual(ledger.balance(self.ctx, "zoominfo")["consumed"], 1.0)

    def test_identity_mismatch_writes_nothing_and_errors_release_credits(self) -> None:
        self.enrich.fetcher = FakeFetcher({})
        ledger = self.platform.service("credits")
        ledger.sync(self.ctx, "zoominfo", 10, source="test")
        self.enrich.connectors["zoominfo"] = FakeConnector({"name": "Other", "website": "https://other.com",
                                                            "industry": "Banking"})
        result = self.enrich.enrich_company(self.ctx, self.company["id"], providers=["zoominfo"], allow_paid=True)
        self.assertEqual(result["steps"][-1]["status"], "identity_mismatch")
        self.assertIsNone(self.store.get(self.ctx, "companies", self.company["id"])["industry"])
        self.enrich.connectors["zoominfo"] = FakeConnector(error=ProviderError("HTTP 500"))
        result = self.enrich.enrich_company(self.ctx, self.company["id"], providers=["zoominfo"], allow_paid=True)
        self.assertEqual(result["steps"][-1]["status"], "error")
        balance = ledger.balance(self.ctx, "zoominfo")
        self.assertEqual(balance["reserved"], 0)

    def test_priority_is_configurable(self) -> None:
        order = self.enrich.set_priority(self.ctx, ["partner_api", "public_web"])
        self.assertEqual(order[:3], ["internal", "partner_api", "public_web"])
        with self.assertRaises(ValidationError):
            self.enrich.set_priority(self.ctx, ["scraping_service"])

    def test_same_company_and_partner_connector(self) -> None:
        self.assertIsNone(same_company({"domain": "acme.com"}, {"website": "https://www.acme.com/x"}))
        self.assertIn("does not match", same_company({"domain": "acme.com"}, {"domain": "b.com"}))
        partner = PartnerApiConnector({"api_key": "k" * 20}, settings={"base_url": "http://insecure"})
        self.assertEqual(partner.health()["status"], "not_configured")
        fetcher = FakeFetcher({"https://partner.example/companies/enrich": (200, json.dumps(
            {"company": {"name": "Acme", "industry": "Tools"}}))})
        partner = PartnerApiConnector({"api_key": "k" * 20}, settings={"base_url": "https://partner.example"},
                                      fetcher=fetcher)
        from cloud.intel.providers.base import PaidCallRefused

        with self.assertRaises(PaidCallRefused):
            partner.enrich_company({"domain": "acme.com"})
        self.assertEqual(partner.enrich_company({"domain": "acme.com"}, allow_paid=True)["industry"], "Tools")
        self.assertEqual(fetcher.calls[0][1]["headers"]["Authorization"], "Bearer " + "k" * 20)

    def test_public_parse_without_json_ld(self) -> None:
        parsed = PublicCompanyResearch.parse("https://x.com", "<meta name='description' content='Hi'>")
        self.assertEqual(parsed["values"]["description"], "Hi")


# --- job sources -------------------------------------------------------------------------------


class TestJobSources(Base):
    def test_every_row_has_the_unified_schema(self) -> None:
        row = normalize_posting({"title": " Engineer ", "job_url": "https://x/1", "external_id": 7}, "adzuna")
        self.assertEqual(set(JOB_FIELDS) - set(row), set())
        self.assertEqual(row["title"], "Engineer")
        self.assertEqual(row["external_id"], "7")
        self.assertEqual(row["workplace_type"], "unknown")
        self.assertEqual(row["source_name"], "adzuna")

    def test_partner_sources_refuse_without_a_partner_feed(self) -> None:
        with self.assertRaises(SourceUnavailable):
            IndeedAdapter(credentials={"partner_api_token": "t" * 20}).run(SourceQuery())
        with self.assertRaises(SourceUnavailable):
            LinkedInAdapter(credentials={"partner_api_token": "t" * 20},
                            settings={"partner_feed_url": "http://not-https"}).run(SourceQuery())

    def test_partner_feed_is_read_with_the_partner_token(self) -> None:
        class Fetch:
            def __init__(self):
                self.headers = None

            def fetch(self, url, method="GET", json_body=None, headers=None, accept=None):
                self.headers = headers
                body = json.dumps({"jobs": [{"title": "SAP Analyst", "url": "https://p/1", "company": "Acme",
                                             "remote": True}]})
                return SimpleNamespace(ok=True, blocked=False, status=200, text=body, error=None,
                                       json=lambda: json.loads(body), final_url=url)

        fetch = Fetch()
        rows = LinkedInAdapter(credentials={"partner_api_token": "t" * 20},
                               settings={"partner_feed_url": "https://partner.linkedin.example/feed"},
                               fetcher=fetch).run(SourceQuery(keywords="sap"))
        self.assertEqual(rows[0]["source_name"], "linkedin")
        self.assertEqual(rows[0]["company_name"], "Acme")
        self.assertEqual(fetch.headers["Authorization"], "Bearer " + "t" * 20)

    def test_status_lists_all_eight_providers_honestly(self) -> None:
        status = self.platform.service("sources").status(self.ctx)
        by_name = {i["name"]: i for i in status["items"]}
        for name in ("linkedin", "indeed", "dice", "ziprecruiter", "wellfound", "builtin", "usajobs", "adzuna"):
            self.assertEqual(by_name[name]["state"], "not_configured", name)
            self.assertFalse(by_name[name]["verified"])
            self.assertTrue(by_name[name]["missing"])
        self.assertEqual(by_name["ats_public"]["state"], "ok")
        self.assertIn("no anti-bot", status["policy"])


# --- scraper / research → GTM ---------------------------------------------------------------------


class FakeScraper:
    def __init__(self, rows):
        self.rows = rows
        self.proposed = []

    def get(self, ctx, run_id):
        return {"id": run_id, "name": "Plants scrape", "stats": {}}

    def records(self, ctx, run_id, view="all", limit=500, offset=0):
        return {"items": self.rows, "total": len(self.rows)}

    def propose(self, ctx, run_id, actions):
        self.proposed.append(tuple(actions))
        return {"created": 3, "total": 3}


class TestGtmBridge(Base):
    def setUp(self) -> None:
        super().setUp()
        crm = self.platform.service("crm")
        self.acme = crm.upsert_company(self.ctx, {"name": "Acme", "domain": "acme.com"}, source_kind="manual",
                                       source_name="seed")["company"]
        self.pat = crm.upsert_contact(self.ctx, {"full_name": "Pat Lee", "email": "pat@acme.com",
                                                 "company_id": self.acme["id"]},
                                      source_kind="manual", source_name="seed")["contact"]
        self.scraper = FakeScraper([
            {"company_name": "Acme", "website": "https://acme.com", "email": "pat@acme.com",
             "hiring_manager": "Pat Lee", "source_url": "https://acme.com/jobs"},
            {"company_name": "Acme", "website": "https://www.acme.com/about"},
            {"company_name": "Newco", "website": "https://newco.io", "email": "jo@newco.io"}])
        self.platform.override("scraper", self.scraper)
        self.bridge = self.platform.service("gtm_bridge")

    def test_prepare_dedupes_and_matches(self) -> None:
        preview = self.bridge.prepare(self.ctx, "scrape", "sc_1")
        self.assertEqual(preview["summary"]["companies"], 2)
        self.assertEqual(preview["summary"]["duplicates_removed"], 1)
        self.assertEqual(preview["summary"]["companies_existing"], 1)
        self.assertEqual(preview["summary"]["contacts_existing"], 1)
        self.assertEqual(preview["summary"]["contacts_new"], 1)
        companies_before = self.store.count(self.ctx, "companies")
        self.assertEqual(companies_before, 1)  # read-only

    def test_add_to_list_only_adds_existing_records(self) -> None:
        result = self.bridge.add_to_list(self.ctx, "scrape", "sc_1", entity_type="companies")
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["not_in_crm"], 1)
        self.assertIsNotNone(result["hint"])
        self.assertEqual(self.store.count(self.ctx, "companies"), 1)

    def test_validate_emails_uses_the_job_service_when_present(self) -> None:
        calls = []

        class Jobs:
            def create_from_rows(self, ctx, **kw):
                calls.append(kw)
                return {"id": "evj_1", "status": "ready"}

        self.platform.override("email_jobs", Jobs())
        result = self.bridge.validate_emails(self.ctx, "scrape", "sc_1")
        self.assertEqual(result["emails"], 2)
        self.assertEqual(calls[0]["email_field"], "email")
        self.assertEqual(calls[0]["source_type"], "scrape")
        self.assertEqual({r["email"] for r in calls[0]["rows"]}, {"pat@acme.com", "jo@newco.io"})
        # the CRM contact's email status is untouched by preparing validation
        self.assertEqual(self.store.get(self.ctx, "contacts", self.pat["id"])["email_status"], "UNVERIFIED")

    def test_campaign_is_a_draft_that_never_sends(self) -> None:
        result = self.bridge.create_campaign(self.ctx, "scrape", "sc_1", name="Plants")
        campaign = result["campaign"]
        self.assertEqual(campaign["status"], "draft")
        self.assertFalse(campaign["sending_enabled"])
        self.assertEqual(campaign["audience"]["list_ids"], [result["list"]["id"]])
        self.assertEqual(self.store.count(self.ctx, "message_events"), 0)

    def test_crm_proposal_and_research_are_reviewable(self) -> None:
        self.bridge.create_crm_proposal(self.ctx, "scrape", "sc_1")
        self.assertEqual(self.scraper.proposed, [("company", "contact")])
        run = self.bridge.research_these(self.ctx, "scrape", "sc_1")["run"]
        self.assertEqual(run["status"], "planned")
        self.assertIn("Newco", run["question"])

    def test_pipeline_reports_each_step(self) -> None:
        result = self.bridge.pipeline(self.ctx, "scrape", "sc_1", name="Pipe")
        self.assertEqual([s["step"] for s in result["steps"]], ["add_to_list", "validate_emails", "create_campaign"])
        self.assertTrue(all(s["status"] == "ok" for s in result["steps"]))

    def test_research_source(self) -> None:
        run = self.store.insert(self.ctx, "research_runs", {"question": "plants in ohio", "status": "completed"})
        self.store.insert(self.ctx, "research_results", {"run_id": run["id"], "rank": 0,
                                                         "company_id": self.acme["id"], "data": {}})
        preview = self.bridge.prepare(self.ctx, "research", run["id"])
        self.assertEqual(preview["companies"][0]["company_id"], self.acme["id"])
        with self.assertRaises(ValidationError):
            self.bridge.prepare(self.ctx, "nowhere", "x")


class TestInternalApi(unittest.TestCase):
    """Over HTTP: upload → schema → review → mapping (ambiguity refused) → merge; status endpoints."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from cloud.api.auth import DevTokenIssuer
        from cloud.api.main import create_app
        from cloud.api.settings import Settings
        from cloud.worker.dispatcher import NullDispatcher

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.platform = Platform(MemoryStore(), storage=LocalFileStorage(root / "platform"),
                                 config=PlatformConfig(files_dir=root / "platform"))
        issuer = DevTokenIssuer("internal-data-api-tests-secret-0123456789abcdef")
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = TestClient(app)
        self.client.headers["Authorization"] = f"Bearer {issuer.issue('alice@example.com')['access_token']}"
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        r = self.client.post("/api/v1/workspaces", json={"name": "Alice", "seed": False})
        self.assertEqual(r.status_code, 201, r.text)
        self.ws = r.json()["id"]
        self.base = f"/api/v1/w/{self.ws}"

    def test_batch_flow_over_http(self) -> None:
        r = self.client.post(self.base + "/internal-data/batches", json={"name": "HTTP", "target": "companies"})
        self.assertEqual(r.status_code, 201, r.text)
        batch = r.json()["id"]
        files = [("files", (f"f{i}.csv", csv_bytes(("Company Name", "Website", "Name"), [(f"C{i}", f"c{i}.com",
                                                                                           "x")]), "text/csv"))
                 for i in range(12)]
        r = self.client.post(f"{self.base}/internal-data/batches/{batch}/files", files=files)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(r.json()["added"]), 12)
        r = self.client.post(f"{self.base}/internal-data/batches/{batch}/schema")
        self.assertEqual(r.json()["file_count"], 12)
        review = self.client.get(f"{self.base}/internal-data/batches/{batch}/mapping-review").json()
        self.assertIn("Name", review["requires_decision"])
        mapping = {"Company Name": "company.name", "Website": "company.website"}
        r = self.client.put(f"{self.base}/internal-data/batches/{batch}/mapping", json={"mapping": mapping})
        self.assertEqual(r.status_code, 422)
        r = self.client.put(f"{self.base}/internal-data/batches/{batch}/mapping",
                            json={"mapping": {**mapping, "Name": None}})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(f"{self.base}/internal-data/batches/{batch}/merge")
        self.assertEqual(r.status_code, 202, r.text)
        run_task_inline(self.platform, self.ws, r.json()["id"])
        history = self.client.get(self.base + "/internal-data/batches").json()
        self.assertEqual(history["items"][0]["status"], "merged")

    def test_status_endpoints(self) -> None:
        status = self.client.get(self.base + "/sources/status").json()
        self.assertEqual(len([i for i in status["items"] if i["state"] == "not_configured"]), 8)
        sources = self.client.get(self.base + "/enrichment/sources").json()["items"]
        self.assertEqual(sources[0]["name"], "internal")
        r = self.client.post(self.base + "/gtm-bridge/nowhere/x/add-to-list")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.post(self.base + "/gtm-bridge/scrape/x/explode").status_code, 404)


if __name__ == "__main__":
    unittest.main()
