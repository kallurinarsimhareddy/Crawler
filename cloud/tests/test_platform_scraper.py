"""The AI scraper: instruction parsing, deterministic extraction, the official
ATS API path, SSRF and blocked pages, validation/filters/dedupe, and a whole run
through the task queue — all offline."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cloud.intel.ai.base import AIProvider
from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.scraper.extract import extract_from_html, scrape_url
from cloud.intel.scraper.schema import instruction_to_schema
from cloud.intel.scraper.service import read_urls_from_file
from cloud.intel.scraper.validate import apply_filters, dedupe_records, validate_record
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage
from cloud.tests.test_platform_ai_fakes import fetcher_for

TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=40)).isoformat()

ORG_PAGE = """<html><head><title>Acme Manufacturing | Home</title>
<meta property="og:site_name" content="Acme Manufacturing">
<meta name="description" content="Precision parts since 1952.">
<link rel="canonical" href="https://www.acme-mfg.com/">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme Manufacturing",
 "legalName":"Acme Manufacturing Inc.","url":"https://www.acme-mfg.com","sameAs":["https://www.linkedin.com/company/acme-mfg"],
 "address":{"@type":"PostalAddress","addressLocality":"Tulsa","addressRegion":"OK","addressCountry":"US"}}</script>
</head><body>
<nav><a href="/about">About</a> <a href="https://boards.greenhouse.io/acmemfg">Careers</a>
<a href="mailto:info@acme-mfg.com">Email us</a></nav>
<p>Leadership: Jane Smith, CEO. Call (918) 555-0142.</p>
</body></html>"""

JOB_PAGE = f"""<html><head><script type="application/ld+json">[
 {{"@type":"JobPosting","title":"SAP FICO Consultant","datePosted":"{TODAY}","url":"https://acme-mfg.com/jobs/1",
   "hiringOrganization":{{"@type":"Organization","name":"Acme Manufacturing"}},
   "jobLocation":{{"@type":"Place","address":{{"addressLocality":"Tulsa","addressRegion":"OK","addressCountry":"US"}}}}}},
 {{"@type":"JobPosting","title":"RPG Developer","datePosted":"{OLD}","url":"https://acme-mfg.com/jobs/2",
   "hiringOrganization":{{"@type":"Organization","name":"Acme Manufacturing"}}}}]</script></head><body></body></html>"""


class SchemaTests(unittest.TestCase):
    def test_the_specs_example_instructions(self) -> None:
        cases = {
            "Get company name, website, job title and location.":
                ["company_name", "website", "job_title", "location"],
            "Get company name, careers URL and ATS.": ["company_name", "careers_url", "ats"],
            "Get company name, website, CEO and LinkedIn URL.": ["company_name", "website", "ceo", "linkedin_url"],
        }
        for instruction, expected in cases.items():
            schema = instruction_to_schema(instruction)
            self.assertEqual([f["name"] for f in schema["fields"]], expected, instruction)
            self.assertEqual(schema["unknown"], [], instruction)

    def test_posted_within_filter(self) -> None:
        schema = instruction_to_schema("Get job titles posted in the last 7 days.")
        self.assertEqual(schema["entity"], "job")
        self.assertEqual(schema["filters"], [{"field": "posted_date", "op": "within_days", "value": 7}])

    def test_unknown_fields_go_to_ai_only_when_given(self) -> None:
        schema = instruction_to_schema("Get company name and number of patents")
        self.assertEqual(schema["unknown"], ["number patents"])

        class FakeAI(AIProvider):
            name, external = "fake", True

            def complete_json(self, system, prompt, schema, *, max_tokens=4000):
                return {"fields": [{"name": "Patent Count", "type": "number", "description": "Patents held"}]}

            def complete_text(self, *a, **k):
                return ""

        extended = instruction_to_schema("Get company name and number of patents", ai=FakeAI())
        self.assertIn("patent_count", [f["name"] for f in extended["fields"]])
        self.assertEqual(extended["parser"], "rules+fake")


class ExtractionTests(unittest.TestCase):
    def test_organization_page(self) -> None:
        schema = instruction_to_schema("Get company name, website, CEO, LinkedIn URL, careers URL, ATS, email, phone")
        fields = [f["name"] for f in schema["fields"]]
        page = extract_from_html(ORG_PAGE, "https://www.acme-mfg.com/", fields)
        record = page.records[0]
        self.assertEqual(record["company_name"], "Acme Manufacturing Inc.")
        self.assertEqual(page.field_sources["company_name"], "json-ld")
        self.assertEqual(record["linkedin_url"], "https://www.linkedin.com/company/acme-mfg")
        self.assertEqual(record["ceo"], "Jane Smith")
        self.assertEqual(page.field_sources["ceo"], "regex")
        self.assertEqual(record["ats"], "Greenhouse")
        self.assertEqual(record["careers_url"], "https://boards.greenhouse.io/acmemfg")
        self.assertEqual(record["email"], "info@acme-mfg.com")
        self.assertIn("555-0142", record["phone"])

    def test_job_postings_from_json_ld(self) -> None:
        page = extract_from_html(JOB_PAGE, "https://acme-mfg.com/careers", ["job_title", "location", "posted_date"],
                                 entity="job")
        self.assertEqual([r["job_title"] for r in page.records], ["SAP FICO Consultant", "RPG Developer"])
        self.assertEqual(page.records[0]["location"], "Tulsa, OK, US")

    def test_job_board_uses_the_official_ats_api(self) -> None:
        api = {"jobs": [{"title": "ERP Analyst", "absolute_url": "https://boards.greenhouse.io/acmemfg/jobs/9",
                         "location": {"name": "Remote"}, "updated_at": f"{TODAY}T10:00:00Z"}]}
        fetcher = fetcher_for({
            "https://boards.greenhouse.io/acmemfg": (200, "<html><body>Loading…</body></html>"),
            "https://boards-api.greenhouse.io/v1/boards/acmemfg/jobs": (200, api, {"Content-Type": "application/json"}),
        })
        schema = instruction_to_schema("Get job titles and location")
        page = scrape_url("https://boards.greenhouse.io/acmemfg", schema, fetcher=fetcher)
        self.assertEqual(page.status, "ok")
        self.assertEqual(page.records, [{"job_title": "ERP Analyst", "location": "Remote"}])
        self.assertTrue(page.method.endswith("ats-api"))

    def test_private_address_is_refused_and_recorded(self) -> None:
        fetcher = fetcher_for({})
        page = scrape_url("https://internal.example/admin", instruction_to_schema("Get company name"),
                          fetcher=fetcher)
        self.assertEqual(page.status, "error")
        self.assertIn("unsafe target", page.problems[0])
        self.assertEqual(fetcher._session.calls, [])  # never connected

    def test_blocked_page_is_not_bypassed(self) -> None:
        fetcher = fetcher_for({"https://walled.example/": (403, "Access denied")})
        page = scrape_url("https://walled.example/", instruction_to_schema("Get company name"), fetcher=fetcher)
        self.assertEqual(page.status, "blocked")
        self.assertIn("not bypassed", page.problems[0])

    def test_redirect_to_a_private_address_is_refused(self) -> None:
        fetcher = fetcher_for({"https://ok.example/": (302, "", {"Location": "https://internal.example/"})})
        page = scrape_url("https://ok.example/", instruction_to_schema("Get company name"), fetcher=fetcher)
        self.assertEqual(page.status, "error")
        self.assertNotIn("https://internal.example/", fetcher._session.calls)

    def test_robots_txt_is_respected(self) -> None:
        fetcher = fetcher_for({"https://shy.example/robots.txt": (200, "User-agent: *\nDisallow: /"),
                               "https://shy.example/": (200, ORG_PAGE)})
        page = scrape_url("https://shy.example/", instruction_to_schema("Get company name"), fetcher=fetcher)
        self.assertEqual(page.status, "blocked")
        self.assertNotIn("https://shy.example/", fetcher._session.calls)


class ValidateTests(unittest.TestCase):
    def test_types_filters_dedupe(self) -> None:
        fields = [{"name": "website", "type": "url"}, {"name": "email", "type": "email"},
                  {"name": "posted_date", "type": "date"}]
        clean, problems = validate_record({"website": "javascript:alert(1)", "email": "X@Acme.com",
                                           "posted_date": "2026-13-40"}, fields)
        self.assertIsNone(clean["website"])
        self.assertEqual(clean["email"], "x@acme.com")
        self.assertIsNone(clean["posted_date"])
        self.assertEqual(len(problems), 2)

        now = datetime.now(timezone.utc)
        rows = [{"posted_date": TODAY}, {"posted_date": OLD}, {"posted_date": None}]
        kept, dropped = apply_filters(rows, [{"field": "posted_date", "op": "within_days", "value": 7}], now=now)
        self.assertEqual((len(kept), dropped), (1, 2))

        rows, dupes = dedupe_records([{"job_url": "https://a.com/j/1"}, {"job_url": "https://a.com/j/1/"},
                                      {"company_name": "Acme"}, {"company_name": "ACME"}])
        self.assertEqual((len(rows), dupes), (2, 2))

    def test_urls_from_csv_and_xlsx(self) -> None:
        data = "Company,Website\nAcme,acme.com\nBeta,https://beta.io\n".encode()
        self.assertEqual(read_urls_from_file(data, "list.csv"), ["https://acme.com", "https://beta.io"])
        from openpyxl import Workbook

        book = Workbook()
        book.active.append(["Name", "Homepage"])
        book.active.append(["Acme", "https://acme.com"])
        buffer = io.BytesIO()
        book.save(buffer)
        self.assertEqual(read_urls_from_file(buffer.getvalue(), "l.xlsx", "Homepage"), ["https://acme.com"])
        with self.assertRaises(ValidationError):
            read_urls_from_file(data, "list.csv", "Nope")


class ScrapeRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "W", "w-scrape")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        pages = {
            "https://www.acme-mfg.com/": (200, ORG_PAGE),
            "https://walled.example/": (403, "denied"),
            "https://acme-mfg.com/careers": (200, JOB_PAGE),
        }
        config = PlatformConfig(extra={"fetcher_factory": lambda: fetcher_for(pages)})
        self.platform = Platform(self.store, storage=LocalFileStorage(Path(self.scratch.name)), config=config)

    def test_company_run_end_to_end(self) -> None:
        service = self.platform.service("scraper")
        run = service.start(self.ctx, ["https://www.acme-mfg.com/", "https://walled.example/",
                                       "http://127.0.0.1/admin", "https://www.acme-mfg.com/"],
                            "Get company name, careers URL and ATS.")
        self.assertEqual(run["stats"]["url_count"], 2)  # duplicate dropped, loopback rejected up front
        self.assertEqual(len(run["stats"]["rejected"]), 1)
        task = run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        self.assertEqual(task["status"], "completed", task.get("error"))
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["stats"]["counts"]["blocked"], 1)
        results = self.store.list(self.ctx, "scrape_results", {"run_id": run["id"]}).rows
        self.assertEqual({r["status"] for r in results}, {"ok", "blocked"})
        with self.platform.storage.open(run["stats"]["files"]["json"]["storage_key"]) as handle:
            payload = json.loads(handle.read())
        self.assertEqual(payload["records"][0]["company_name"], "Acme Manufacturing Inc.")
        self.assertEqual(payload["records"][0]["ats"], "Greenhouse")
        with self.platform.storage.open(run["stats"]["files"]["csv"]["storage_key"]) as handle:
            rows = list(csv.DictReader(io.StringIO(handle.read().decode())))
        self.assertEqual(rows[0]["careers_url"], "https://boards.greenhouse.io/acmemfg")

    def test_job_run_applies_the_date_filter(self) -> None:
        run = self.platform.service("scraper").start(self.ctx, ["https://acme-mfg.com/careers"],
                                                     "Get job titles posted in the last 7 days.")
        run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertEqual(run["stats"]["records"], 1)
        self.assertEqual(run["stats"]["filtered_out"], 1)

    def test_formula_cells_are_neutralised(self) -> None:
        page = "<html><head><title>=HYPERLINK(\"x\")</title></head></html>"
        self.platform.config.extra["fetcher_factory"] = lambda: fetcher_for({"https://f.example/": (200, page)})
        run = self.platform.service("scraper").start(self.ctx, ["https://f.example/"], "Get the page title")
        run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        with self.platform.storage.open(run["stats"]["files"]["csv"]["storage_key"]) as handle:
            text = handle.read().decode()
        self.assertIn("'=HYPERLINK", text)

    def test_nothing_fetchable_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.service("scraper").start(self.ctx, ["file:///etc/passwd", "http://10.0.0.1/"], "Get company")


if __name__ == "__main__":
    unittest.main()
