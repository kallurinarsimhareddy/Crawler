"""The AI scraper, offline: inputs (paste, CSV, XLSX), instruction -> schema,
company and job extraction, careers-link following, official ATS APIs, refused
pages (blocked, CAPTCHA, WAF, login, robots, timeout, unsafe), missing fields,
the AI fallback and its evidence checks, free-quota exhaustion, normalisation,
validation, dedupe, exports, progress, cancellation, retry, restart recovery
and the HTTP API.

Nothing here touches the network: pages come from a fake HTTP session and AI
from fake providers (or the real Gemini adapter over a scripted session)."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from cloud.intel.ai.base import AIProvider, AIUnavailable
from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.scraper.dedupe import dedupe_records
from cloud.intel.scraper.fetcher import PageFetcher, classify
from cloud.intel.scraper.models import Outcome
from cloud.intel.scraper.normalizer import canonical_url, normalize_date
from cloud.intel.scraper.planner import instruction_to_schema
from cloud.intel.scraper.runner import AIBudget, run_scrape_task, scrape_one
from cloud.intel.scraper.service import parse_inputs, read_file_rows
from cloud.intel.scraper.validator import apply_filters, check_input_url, validate_record
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage
from cloud.tests.test_platform_ai_fakes import FakeSession, fake_resolver, fetcher_for

TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=40)).isoformat()

ORG_PAGE = """<html><head><title>Acme Manufacturing | Home</title>
<meta property="og:site_name" content="Acme Manufacturing">
<meta name="description" content="Precision parts since 1952.">
<link rel="canonical" href="https://www.acme-mfg.com/">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme Manufacturing",
 "legalName":"Acme Manufacturing Inc.","url":"https://www.acme-mfg.com","sameAs":["https://www.linkedin.com/company/acme-mfg",
 "https://twitter.com/acmemfg"],
 "address":{"@type":"PostalAddress","addressLocality":"Tulsa","addressRegion":"OK","addressCountry":"US"}}</script>
</head><body>
<nav><a href="/about">About</a> <a href="https://boards.greenhouse.io/acmemfg">Careers</a>
<a href="/contact">Contact</a> <a href="mailto:info@acme-mfg.com">Email us</a></nav>
<p>Leadership: Jane Smith, CEO. Call (918) 555-0142. We build hydraulic valves for the energy sector.</p>
</body></html>"""

JOB_PAGE = f"""<html><head><title>Careers | Acme Manufacturing</title><script type="application/ld+json">[
 {{"@type":"JobPosting","title":"SAP FICO Consultant","datePosted":"{TODAY}","url":"https://acme-mfg.com/jobs/1",
   "employmentType":"FULL_TIME","hiringOrganization":{{"@type":"Organization","name":"Acme Manufacturing"}},
   "jobLocation":{{"@type":"Place","address":{{"addressLocality":"Tulsa","addressRegion":"OK","addressCountry":"US"}}}}}},
 {{"@type":"JobPosting","title":"RPG Developer","datePosted":"{OLD}","url":"https://acme-mfg.com/jobs/2",
   "jobLocationType":"TELECOMMUTE","hiringOrganization":{{"@type":"Organization","name":"Acme Manufacturing"}}}}]</script>
</head><body></body></html>"""

HOME = """<html><head><title>Beta Corp | Home</title></head><body>
<a href="/careers">Careers</a> <a href="/contact-us">Contact us</a></body></html>"""

CAREERS = """<html><head><title>Careers at Beta Corp</title></head><body><ul>
<li><a href="/careers/senior-engineer"><h3>Senior Engineer</h3><span class="location">Austin, TX</span></a></li>
<li><h4>Data Analyst</h4><span class="job-location">Remote</span><a href="/careers/data-analyst?utm_source=x">Apply</a></li>
<li><a href="/careers">All jobs</a></li></ul></body></html>"""

TEXT_CAREERS = """<html><head><title>Join Gamma</title></head><body><h1>Open roles at Gamma Labs</h1>
<p>Plant Controller - Dayton, OH</p><p>Maintenance Technician - Dayton, OH</p>
<a href="https://gamma.example/apply?role=plant-controller">Plant Controller</a></body></html>"""

GREENHOUSE_API = {"jobs": [{"title": "ERP Analyst", "absolute_url": "https://boards.greenhouse.io/acmemfg/jobs/9",
                            "location": {"name": "Remote"}, "updated_at": f"{TODAY}T10:00:00Z",
                            "departments": [{"name": "IT"}]}]}

PAGES = {
    "https://www.acme-mfg.com/": (200, ORG_PAGE),
    "https://acme-mfg.com/careers": (200, JOB_PAGE),
    "https://beta.example/": (200, HOME),
    "https://beta.example/careers": (200, CAREERS),
    "https://gamma.example/careers": (200, TEXT_CAREERS),
    "https://boards.greenhouse.io/acmemfg": (200, "<html><body>Loading…</body></html>"),
    "https://boards-api.greenhouse.io/v1/boards/acmemfg/jobs": (200, GREENHOUSE_API,
                                                                {"Content-Type": "application/json"}),
    "https://walled.example/": (403, "Access denied"),
    "https://cf.example/": (403, "<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x.js'>"
                                 "</script></html>", {"cf-mitigated": "challenge"}),
    "https://waf.example/": (403, "<html>Attention Required! | Cloudflare</html>"),
    "https://members.example/": (401, "Sign in required"),
    "https://form.example/contact": (200, "<html><title>Contact Form Co</title><body><p>" + "Words " * 50 + "</p>"
                                          "<div class='g-recaptcha'></div><a href='mailto:hi@form.example'>x</a>"
                                          "</body></html>"),
}


def pages_fetcher(pages=None, **kwargs) -> PageFetcher:
    return PageFetcher(fetcher_for(dict(PAGES, **(pages or {})), **kwargs))


class FakeAI(AIProvider):
    """Answers ``complete_json`` from a list of scripted replies (or a function)."""

    name, external, model = "fake", True, "fake-1"

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.prompts: List[str] = []

    def complete_json(self, system, prompt, schema, *, max_tokens=4000):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else {}
        if isinstance(reply, Exception):
            raise reply
        return reply(prompt) if callable(reply) else reply

    def complete_text(self, *a, **k):
        return ""


def values(page) -> List[Dict[str, Any]]:
    return [{k: fv.value for k, fv in record.items()} for record in page.records]


# --------------------------------------------------------------------------------------------------


class PlannerTests(unittest.TestCase):
    def names(self, instruction: str) -> List[str]:
        return [f["name"] for f in instruction_to_schema(instruction)["fields"]]

    def test_the_four_example_instructions(self) -> None:
        cases = {
            "Get the company name, company website and all job post titles.": ("job", ["company_name", "website",
                                                                                       "job_title"]),
            "Get company name, careers URL, ATS/platform and job titles.": ("job", ["company_name", "careers_url",
                                                                                    "ats", "job_title"]),
            "Get company name, website, location, industry and contact page.": (
                "company", ["company_name", "website", "location", "industry", "contact_page"]),
            "Find all job titles and job URLs from this page.": ("job", ["job_title", "job_url"]),
        }
        for instruction, (entity, expected) in cases.items():
            schema = instruction_to_schema(instruction)
            self.assertEqual(schema["entity"], entity, instruction)
            self.assertEqual([f["name"] for f in schema["fields"]], expected, instruction)
            self.assertEqual(schema["custom"], [], instruction)

    def test_types_levels_and_required(self) -> None:
        schema = instruction_to_schema("Extract company name, website, job title and job URL.")
        by = {f["name"]: f for f in schema["fields"]}
        self.assertEqual((by["website"]["type"], by["job_url"]["type"]), ("url", "url"))
        self.assertEqual((by["company_name"]["level"], by["job_title"]["level"]), ("company", "job"))
        self.assertTrue(by["job_title"]["required"])
        company = instruction_to_schema("Get company name and website")
        self.assertTrue(company["fields"][0]["required"])
        self.assertEqual(company["fields"][0]["name"], "company_name")

    def test_any_field_can_be_requested(self) -> None:
        schema = instruction_to_schema("Get company name, number of patents and founding year")
        by = {f["name"]: f for f in schema["fields"]}
        self.assertIn("number_patents", by)
        self.assertEqual(by["number_patents"]["type"], "number")
        self.assertIn("founding_year", by)
        self.assertEqual(by["founding_year"]["source"], "custom")
        for request in ("department", "technology", "salary", "posted date", "description", "remote mode"):
            self.assertTrue(self.names(f"job titles and {request}"), request)

    def test_ai_names_custom_fields_and_failure_keeps_rules(self) -> None:
        ai = FakeAI({"fields": [{"name": "Patent Count", "type": "number", "level": "company",
                                 "description": "Patents held"}]})
        extended = instruction_to_schema("Get company name and number of patents", ai=ai)
        self.assertIn("patent_count", [f["name"] for f in extended["fields"]])
        self.assertEqual(extended["parser"], "rules+fake")
        broken = instruction_to_schema("Get company name and number of patents", ai=FakeAI(AIUnavailable("quota")))
        self.assertEqual(broken["parser"], "rules")
        self.assertIn("number_patents", [f["name"] for f in broken["fields"]])
        self.assertIn("quota", broken["ai_note"])

    def test_posted_within_filter(self) -> None:
        schema = instruction_to_schema("Get job titles posted in the last 7 days.")
        self.assertEqual(schema["entity"], "job")
        self.assertEqual(schema["filters"], [{"field": "posted_date", "op": "within_days", "value": 7}])


class InputTests(unittest.TestCase):
    def test_one_url(self) -> None:
        accepted, rejected, report = parse_inputs("acme.com")
        self.assertEqual([a.url for a in accepted], ["https://acme.com"])
        self.assertEqual((accepted[0].row, accepted[0].source), (1, "paste"))
        self.assertTrue(accepted[0].batch.startswith("b_"))
        self.assertEqual(rejected, [])

    def test_pasted_list_is_validated(self) -> None:
        text = "https://a.com\n\nftp://files.b.com\nhttps://a.com/\nnot a url\nhttp://127.0.0.1/admin\nb.io:8080/jobs\n"
        accepted, rejected, report = parse_inputs(text)
        self.assertEqual([a.url for a in accepted], ["https://a.com", "https://b.io:8080/jobs"])
        self.assertEqual([a.row for a in accepted], [1, 7])
        reasons = {r["row"]: r["reason"] for r in rejected}
        self.assertEqual(reasons[3], "unsupported protocol ftp:")
        self.assertEqual(reasons[4], "duplicate")
        self.assertIn("spaces", reasons[5])
        self.assertIn(6, reasons)   # loopback refused before any fetch
        self.assertEqual((report["empty"], report["duplicates"], report["invalid"], report["unsafe"]), (1, 1, 2, 1))
        self.assertEqual([a.url for a in parse_inputs("a.com, b.com")[0]], ["https://a.com", "https://b.com"])

    def test_input_url_checks(self) -> None:
        self.assertEqual(check_input_url(" <https://x.io/a> "), ("https://x.io/a", None))
        self.assertEqual(check_input_url("javascript:alert(1)")[1], "unsupported protocol javascript:")
        self.assertEqual(check_input_url("mailto:a@b.com")[1], "unsupported protocol mailto:")
        self.assertIsNone(check_input_url("https://user:pw@x.io/")[0])
        self.assertIsNone(check_input_url("https://nodot/")[0])

    def test_csv_import(self) -> None:
        data = "Company,Website\nAcme,acme.com\n,\nBeta,https://beta.io\nGamma,gopher://x\n".encode()
        self.assertEqual(read_file_rows(data, "list.csv"), [(2, "acme.com"), (3, ""), (4, "https://beta.io"),
                                                            (5, "gopher://x")])
        accepted, rejected, report = parse_inputs(None, file_bytes=data, filename="list.csv")
        self.assertEqual([(a.url, a.row, a.source) for a in accepted],
                         [("https://acme.com", 2, "list.csv"), ("https://beta.io", 4, "list.csv")])
        self.assertEqual(rejected[0]["row"], 5)
        self.assertEqual(report["empty"], 1)
        bare = "https://one.com\nhttps://two.com\n".encode()
        self.assertEqual(read_file_rows(bare, "urls.txt"), [(1, "https://one.com"), (2, "https://two.com")])
        with self.assertRaises(ValidationError):
            read_file_rows(data, "list.csv", "Nope")

    def test_xlsx_import(self) -> None:
        from openpyxl import Workbook

        book = Workbook()
        book.active.append(["Name", "Homepage", "Notes"])
        book.active.append(["Acme", "https://acme.com", "x"])
        book.active.append(["Beta", None, "y"])
        book.active.append(["Gamma", "gamma.io", "z"])
        buffer = io.BytesIO()
        book.save(buffer)
        rows = read_file_rows(buffer.getvalue(), "l.xlsx", "Homepage")
        self.assertEqual(rows, [(2, "https://acme.com"), (3, ""), (4, "gamma.io")])
        self.assertEqual(read_file_rows(buffer.getvalue(), "l.xlsx"), rows, "the Homepage header is found by itself")
        with self.assertRaises(ValidationError):
            read_file_rows(b"not a workbook", "l.xlsx")
        with self.assertRaises(ValidationError):
            read_file_rows(b"x", "l.pdf")


class CompanyExtractionTests(unittest.TestCase):
    def test_company_fields_with_evidence(self) -> None:
        schema = instruction_to_schema("Get company name, website, domain, CEO, LinkedIn URL, social links, "
                                       "careers URL, ATS, email, phone, contact page")
        page = scrape_one("https://www.acme-mfg.com/", schema, pages_fetcher())
        self.assertEqual(page.outcome, Outcome.OK)
        record = page.records[0]
        got = {k: fv.value for k, fv in record.items()}
        self.assertEqual(got["company_name"], "Acme Manufacturing")
        self.assertEqual((record["company_name"].method, record["company_name"].evidence), ("json-ld",
                                                                                            "Organization.name"))
        self.assertEqual(got["website"], "https://www.acme-mfg.com")
        self.assertEqual(got["linkedin_url"], "https://www.linkedin.com/company/acme-mfg")
        self.assertIn("https://twitter.com/acmemfg", got["social_links"])
        self.assertEqual((got["ceo"], record["ceo"].method), ("Jane Smith", "regex"))
        self.assertEqual(got["ats"], "Greenhouse")
        self.assertEqual(got["careers_url"], "https://boards.greenhouse.io/acmemfg")
        self.assertEqual(got["email"], "info@acme-mfg.com")
        self.assertIn("555-0142", got["phone"])
        self.assertEqual(got["contact_page"], "https://www.acme-mfg.com/contact")
        self.assertTrue(all(fv.source_url == "https://www.acme-mfg.com/" for fv in record.values()))
        self.assertTrue(all(0 < fv.confidence <= 1 for fv in record.values()))

    def test_missing_fields_stay_empty_without_ai(self) -> None:
        schema = instruction_to_schema("Get company name, website, location, industry and contact page.")
        page = scrape_one("https://beta.example/", schema, pages_fetcher())
        got = values(page)[0]
        self.assertEqual(got["company_name"], "Beta Corp")
        self.assertEqual(got["contact_page"], "https://beta.example/contact-us")
        self.assertNotIn("industry", got)
        self.assertNotIn("location", got)
        self.assertFalse(page.ai_used)

    def test_ats_is_found_by_following_the_careers_link(self) -> None:
        home = """<html><head><title>Delta</title></head><body><a href="/jobs">Jobs</a></body></html>"""
        jobs = """<html><body><a href="https://jobs.lever.co/delta">See openings</a></body></html>"""
        fetcher = pages_fetcher({"https://delta.example/": (200, home), "https://delta.example/jobs": (200, jobs)})
        page = scrape_one("https://delta.example/", instruction_to_schema("Get company name, careers URL and ATS"),
                          fetcher)
        got = values(page)[0]
        self.assertEqual(got["ats"], "Lever")
        self.assertEqual(len(page.pages), 2)


class JobExtractionTests(unittest.TestCase):
    def test_json_ld_job_postings(self) -> None:
        schema = instruction_to_schema("Get job titles, job URLs, location, posted date, job type and remote mode")
        page = scrape_one("https://acme-mfg.com/careers", schema, pages_fetcher())
        rows = values(page)
        self.assertEqual([r["job_title"] for r in rows], ["SAP FICO Consultant", "RPG Developer"])
        self.assertEqual(rows[0]["location"], "Tulsa, OK, US")
        self.assertEqual(rows[0]["posted_date"], TODAY)
        self.assertEqual(rows[1]["remote_mode"], "Remote")
        self.assertEqual(page.method, "json-ld")

    def test_job_cards_and_apply_buttons(self) -> None:
        schema = instruction_to_schema("Find all job titles and job URLs from this page, and location")
        page = scrape_one("https://beta.example/careers", schema, pages_fetcher())
        rows = values(page)
        self.assertEqual([r["job_title"] for r in rows], ["Senior Engineer", "Data Analyst"])
        self.assertEqual(rows[0]["job_url"], "https://beta.example/careers/senior-engineer")
        self.assertEqual(rows[0]["location"], "Austin, TX")
        self.assertEqual(page.records[1]["job_title"].method, "heading")   # the "Apply" button's card heading
        self.assertEqual(rows[1]["location"], "Remote")

    def test_homepage_leads_to_its_careers_page(self) -> None:
        schema = instruction_to_schema("Get the company name, company website and all job post titles.")
        page = scrape_one("https://beta.example/", schema, pages_fetcher())
        self.assertEqual([r["job_title"] for r in values(page)], ["Senior Engineer", "Data Analyst"])
        self.assertEqual({r["company_name"] for r in values(page)}, {"Beta Corp"})
        self.assertEqual([p["url"] for p in page.pages], ["https://beta.example/", "https://beta.example/careers"])

    def test_official_ats_api(self) -> None:
        schema = instruction_to_schema("Get company name, careers URL, ATS/platform and job titles, department.")
        page = scrape_one("https://www.acme-mfg.com/", schema, pages_fetcher())
        rows = values(page)
        self.assertEqual(rows, [{"company_name": "Acme Manufacturing", "careers_url": "https://job-boards.greenhouse.io/acmemfg",
                                 "ats": "Greenhouse", "job_title": "ERP Analyst", "department": "IT"}])
        self.assertEqual(page.records[0]["job_title"].method, "ats-api")

    def test_a_board_bigger_than_the_cap_is_cut_and_says_so(self) -> None:
        from cloud.intel.scraper import extractor

        big = {"jobs": [{"title": f"Role {i}", "absolute_url": f"https://boards.greenhouse.io/acmemfg/jobs/{i}"}
                        for i in range(7)]}
        fetcher = pages_fetcher({"https://boards-api.greenhouse.io/v1/boards/acmemfg/jobs": (
            200, big, {"Content-Type": "application/json"})})
        from unittest import mock

        with mock.patch.object(extractor.ats_api_jobs, "__kwdefaults__", {"max_jobs": 5, "notes": None}):
            page = scrape_one("https://boards.greenhouse.io/acmemfg", instruction_to_schema("Get job titles"), fetcher)
        self.assertEqual(len(page.records), 5)
        self.assertTrue(any("more than 5 jobs" in p and "2+ left out" in p for p in page.problems), page.problems)

    def test_no_jobs_is_reported_not_invented(self) -> None:
        empty = "<html><head><title>Epsilon Inc</title></head><body><p>Hello</p></body></html>"
        page = scrape_one("https://eps.example/", instruction_to_schema("Get company name and job titles"),
                          pages_fetcher({"https://eps.example/": (200, empty)}))
        self.assertEqual(values(page), [{"company_name": "Epsilon Inc"}])
        self.assertIn("no job postings found on the page", page.problems)


class RefusedPageTests(unittest.TestCase):
    schema = instruction_to_schema("Get company name")

    def outcome(self, url: str, pages=None, **kwargs) -> str:
        return scrape_one(url, self.schema, pages_fetcher(pages, **kwargs)).outcome

    def test_blocked_captcha_waf_login_are_recorded_not_bypassed(self) -> None:
        self.assertEqual(self.outcome("https://walled.example/"), Outcome.BLOCKED)
        self.assertEqual(self.outcome("https://cf.example/"), Outcome.CAPTCHA)
        self.assertEqual(self.outcome("https://waf.example/"), Outcome.WAF)
        self.assertEqual(self.outcome("https://members.example/"), Outcome.LOGIN_REQUIRED)
        page = scrape_one("https://walled.example/", self.schema, pages_fetcher())
        self.assertIn("not bypassed", page.problems[0])
        self.assertEqual(page.records, [])

    def test_login_redirect_and_contact_form_captcha(self) -> None:
        login = "<html><form><input type='password' name='p'></form></html>"
        self.assertEqual(self.outcome("https://app.example/", {
            "https://app.example/": (302, "", {"Location": "https://app.example/login"}),
            "https://app.example/login": (200, login)}), Outcome.LOGIN_REQUIRED)
        self.assertEqual(self.outcome("https://form.example/contact"), Outcome.OK,
                         "a reCAPTCHA widget on a normal page is not a challenge")

    def test_robots_timeout_unsafe_missing(self) -> None:
        self.assertEqual(self.outcome("https://shy.example/", {
            "https://shy.example/robots.txt": (200, "User-agent: *\nDisallow: /"),
            "https://shy.example/": (200, ORG_PAGE)}), Outcome.ROBOTS)
        self.assertEqual(self.outcome("https://internal.example/admin"), Outcome.UNSAFE)
        self.assertEqual(self.outcome("https://nowhere.example/"), Outcome.NOT_FOUND)
        self.assertEqual(classify(0, "", {}, "https://x", "ReadTimeout: timed out"), Outcome.TIMEOUT)
        self.assertEqual(classify(429, "", {}, "https://x", None), Outcome.RATE_LIMITED)
        self.assertEqual(classify(500, "", {}, "https://x", None), Outcome.FAILED)

    def test_redirect_to_private_address_is_refused(self) -> None:
        fetcher = pages_fetcher({"https://ok.example/": (302, "", {"Location": "https://internal.example/"})})
        page = scrape_one("https://ok.example/", self.schema, fetcher)
        self.assertEqual(page.outcome, Outcome.UNSAFE)
        self.assertNotIn("https://internal.example/", fetcher.http._session.calls)


class AIExtractionTests(unittest.TestCase):
    def test_ai_fills_only_missing_fields_and_needs_evidence(self) -> None:
        schema = instruction_to_schema("Get company name, industry and founding year")
        ai = FakeAI({"industry": {"value": "Energy equipment", "evidence": "hydraulic valves for the energy sector"},
                     "founding_year": {"value": "1890", "evidence": "Founded in 1890"}})
        page = scrape_one("https://www.acme-mfg.com/", schema, pages_fetcher(), AIBudget(ai, 5))
        record = page.records[0]
        self.assertEqual(record["company_name"].method, "json-ld", "rules first")
        self.assertEqual((record["industry"].value, record["industry"].method), ("Energy equipment", "ai"))
        self.assertNotIn("founding_year", record, "an answer whose evidence is not on the page is dropped")
        self.assertTrue(any("founding_year" in p for p in page.problems))
        self.assertTrue(page.ai_used)
        self.assertNotIn("company_name", ai.prompts[0].split("Fields:")[1].split("Page URL")[0],
                         "the model is only asked for what the rules did not find")

    def test_ai_is_not_called_when_rules_found_everything(self) -> None:
        ai = FakeAI()
        page = scrape_one("https://acme-mfg.com/careers",
                          instruction_to_schema("Extract company name, website, job title and job URL."),
                          pages_fetcher(), AIBudget(ai, 5))
        self.assertEqual(len(page.records), 2)
        self.assertEqual(ai.prompts, [])
        self.assertFalse(page.ai_used)

    def test_ai_reads_a_careers_page_the_rules_could_not(self) -> None:
        ai = FakeAI({"jobs": [
            {"job_title": "Plant Controller", "job_url": "https://gamma.example/apply?role=plant-controller",
             "location": "Dayton, OH"},
            {"job_title": "Maintenance Technician", "job_url": "https://evil.example/phish", "location": None},
            {"job_title": "Chief Astronaut", "job_url": None, "location": None}]})
        schema = instruction_to_schema("Get job titles, job URLs and location")
        page = scrape_one("https://gamma.example/careers", schema, pages_fetcher(), AIBudget(ai, 5))
        rows = values(page)
        self.assertEqual([r["job_title"] for r in rows], ["Plant Controller", "Maintenance Technician"])
        self.assertEqual(rows[0]["job_url"], "https://gamma.example/apply?role=plant-controller")
        self.assertNotIn("job_url", rows[1], "a URL that is not a link on the page is dropped")
        self.assertTrue(any("not on the page" in p for p in page.problems))
        self.assertEqual(page.records[0]["job_title"].method, "ai")

    def test_quota_exhaustion_switches_ai_off_for_the_rest_of_the_run(self) -> None:
        ai = FakeAI(AIUnavailable("Free AI quota exhausted"))
        budget = AIBudget(ai, 10)
        schema = instruction_to_schema("Get company name and industry")
        first = scrape_one("https://www.acme-mfg.com/", schema, pages_fetcher(), budget)
        second = scrape_one("https://beta.example/", schema, pages_fetcher(), budget)
        self.assertEqual(len(ai.prompts), 1, "no call after the quota ran out")
        self.assertEqual(values(first)[0]["company_name"], "Acme Manufacturing", "rules still work")
        self.assertEqual(values(second)[0]["company_name"], "Beta Corp")
        self.assertIn("Free AI quota exhausted", budget.note)
        self.assertIn("rules only", budget.note)

    def test_ai_success_is_not_reported_as_a_failure(self) -> None:
        ai = FakeAI({"industry": {"value": "Energy equipment", "evidence": "hydraulic valves for the energy sector"}})
        budget = AIBudget(ai, 5)
        page = scrape_one("https://www.acme-mfg.com/", instruction_to_schema("Get company name and industry"),
                          pages_fetcher(), budget)
        self.assertEqual(values(page)[0]["industry"], "Energy equipment")
        self.assertTrue(page.ai_used)
        self.assertEqual((budget.calls, budget.failures), (1, 0))
        self.assertIsNone(budget.summary)
        self.assertFalse(any("AI" in p for p in page.problems))

    def test_ai_failure_is_noted_on_the_page_and_the_run(self) -> None:
        from cloud.intel.ai.base import AIRetryable

        ai = FakeAI(AIRetryable("AI provider returned 503"))
        budget = AIBudget(ai, 5)
        page = scrape_one("https://www.acme-mfg.com/", instruction_to_schema("Get company name and industry"),
                          pages_fetcher(), budget)
        self.assertIn("AI extraction skipped: AI provider returned 503", page.problems)
        self.assertEqual(values(page)[0], {"company_name": "Acme Manufacturing"}, "rules still work, nothing invented")
        self.assertFalse(page.ai_used)
        self.assertEqual((len(ai.prompts), budget.calls, budget.failures), (1, 1, 1), "not retried")
        self.assertEqual(budget.summary, "1 AI call failed (last: AI provider returned 503); "
                                         "the fields asked for were left empty")
        self.assertIsNotNone(budget.provider, "a transient failure does not switch AI off for later pages")

    def test_ai_failure_summary_keeps_the_stop_reason(self) -> None:
        from cloud.intel.ai.base import AIRetryable

        budget = AIBudget(FakeAI(AIRetryable("AI provider returned 503"), AIUnavailable("Free AI quota exhausted")), 5)
        schema = instruction_to_schema("Get company name and industry")
        for url in ("https://www.acme-mfg.com/", "https://beta.example/"):
            scrape_one(url, schema, pages_fetcher(), budget)
        self.assertIn("Free AI quota exhausted", budget.summary)
        self.assertIn("1 AI call failed (last: AI provider returned 503)", budget.summary)

    def test_ai_call_limit(self) -> None:
        ai = FakeAI(*[{"industry": None}] * 5)
        budget = AIBudget(ai, 1)
        schema = instruction_to_schema("Get company name and industry")
        for url in ("https://www.acme-mfg.com/", "https://beta.example/"):
            scrape_one(url, schema, pages_fetcher(), budget)
        self.assertEqual(len(ai.prompts), 1)
        self.assertIn("limit", budget.note)


class NormalizeValidateDedupeTests(unittest.TestCase):
    def test_normalizer(self) -> None:
        self.assertEqual(canonical_url("HTTPS://Acme.com/Jobs/1/?utm_source=li&id=3#top"), "https://acme.com/Jobs/1?id=3")
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        self.assertEqual(normalize_date("Posted 2 Days Ago", now=now), "2026-09-27")
        self.assertEqual(normalize_date("Sep 3, 2026", now=now), "2026-09-03")
        self.assertEqual(normalize_date("3 September 2026"), "2026-09-03")
        self.assertEqual(normalize_date(1790000000000), "2026-09-21")
        self.assertIsNone(normalize_date("Posted 30+ Days Ago", now=now))
        self.assertIsNone(normalize_date("soon"))

    def test_validator(self) -> None:
        fields = [{"name": "website", "type": "url"}, {"name": "email", "type": "email"},
                  {"name": "posted_date", "type": "date"}, {"name": "employees", "type": "number"},
                  {"name": "company_name", "type": "string", "required": True}]
        clean, problems = validate_record({"website": "javascript:alert(1)", "email": "X@Acme.com",
                                           "posted_date": "2026-13-40", "employees": "1,200"}, fields)
        self.assertIsNone(clean["website"])
        self.assertEqual(clean["email"], "x@acme.com")
        self.assertIsNone(clean["posted_date"])
        self.assertEqual(clean["employees"], 1200)
        self.assertIsNone(clean["company_name"])
        self.assertEqual(len(problems), 3)
        self.assertIn("company_name: required but not found", problems)
        rows = [{"posted_date": TODAY}, {"posted_date": OLD}, {"posted_date": None}]
        kept, dropped = apply_filters(rows, [{"field": "posted_date", "op": "within_days", "value": 7}])
        self.assertEqual((len(kept), dropped), (1, 2))

    def test_dedupe_keeps_source_urls(self) -> None:
        jobs = [{"job_title": "A", "job_url": "https://a.com/j/1?utm_source=x", "source_url": "https://a.com/careers"},
                {"job_title": "A", "job_url": "https://a.com/j/1/", "location": "Remote", "source_url": "https://a.com/"},
                {"job_title": "B", "company_name": "Acme", "source_url": "u1"},
                {"job_title": "b", "company_name": "acme", "source_url": "u2"},
                {"job_title": "B", "company_name": "Other", "source_url": "u3"}]
        out, dupes = dedupe_records(jobs, ["job_title", "job_url", "location", "company_name"], "job")
        self.assertEqual((len(out), dupes), (3, 2))
        self.assertEqual(out[0]["source_urls"], ["https://a.com/careers", "https://a.com/"])
        self.assertEqual(out[0]["location"], "Remote", "empty fields are filled from the duplicate")
        companies = [{"company_name": "Acme", "website": "https://www.acme.com", "source_url": "https://www.acme.com/"},
                     {"company_name": "ACME Inc", "website": "https://acme.com", "source_url": "https://acme.com/about"},
                     {"company_name": "Nameless Co", "source_url": "x"}, {"company_name": "nameless co", "source_url": "y"}]
        out, dupes = dedupe_records(companies, ["company_name", "website"], "company")
        self.assertEqual((len(out), dupes), (2, 2))
        self.assertEqual(out[0]["source_urls"], ["https://www.acme.com/", "https://acme.com/about"])
        fingerprint, dupes = dedupe_records([{"title": "x", "source_url": "1"}, {"title": "x", "source_url": "2"}],
                                            ["title"], "company")
        self.assertEqual((len(fingerprint), dupes), (1, 1))


# --------------------------------------------------------------------------------------------------


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "W", "w-scrape")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.pages = dict(PAGES)
        self.sessions: List[FakeSession] = []

        def factory():
            fetcher = fetcher_for(self.pages)
            self.sessions.append(fetcher._session)
            return fetcher

        self.platform = Platform(self.store, storage=LocalFileStorage(Path(scratch.name)),
                                 config=PlatformConfig(extra={"fetcher_factory": factory}))
        self.service = self.platform.service("scraper")

    def run_task(self, run: Dict[str, Any]) -> Dict[str, Any]:
        task = run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        return task

    def read(self, run: Dict[str, Any], name: str) -> bytes:
        with self.platform.storage.open(run["stats"]["files"][name]["storage_key"]) as handle:
            return handle.read()

    def test_one_url_to_csv_xlsx_json(self) -> None:
        run = self.service.start(self.ctx, "https://acme-mfg.com/careers",
                                 "Extract company name, website, job title and job URL.")
        self.assertEqual(self.run_task(run)["status"], "completed")
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertEqual(run["status"], "completed")
        rows = list(csv.DictReader(io.StringIO(self.read(run, "csv").decode("utf-8-sig"))))
        self.assertEqual([r["job_title"] for r in rows], ["SAP FICO Consultant", "RPG Developer"])
        self.assertEqual(list(rows[0])[:8], ["company_name", "website", "job_title", "job_url", "source_url",
                                             "extraction_method", "confidence", "extracted_at"])
        self.assertEqual(rows[0]["source_url"], "https://acme-mfg.com/careers")
        self.assertEqual(rows[0]["website"], "https://acme-mfg.com")
        self.assertIn("json-ld", rows[0]["extraction_method"])
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(self.read(run, "xlsx")))
        self.assertEqual(book.sheetnames, ["All fields", "Jobs", "Companies", "Pages"])
        self.assertEqual(book["Jobs"].max_row, 3)
        self.assertEqual(book["Pages"]["F2"].value, "OK")
        payload = json.loads(self.read(run, "json"))
        self.assertEqual(payload["records"][0]["_evidence"]["job_title"]["method"], "json-ld")
        jobs_csv = self.read(run, "jobs.csv").decode("utf-8-sig")
        self.assertIn("SAP FICO Consultant", jobs_csv)

    def test_multiple_urls_with_progress_outcomes_and_views(self) -> None:
        run = self.service.start(self.ctx, ["https://www.acme-mfg.com/", "https://walled.example/", "https://cf.example/",
                                            "https://beta.example/", "https://acme-mfg.com/careers"],
                                 "Get the company name, company website and all job post titles.")
        self.assertEqual(self.run_task(run)["status"], "completed")
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        progress = run["stats"]["progress"]
        self.assertEqual((progress["total"], progress["processed"], progress["completed"], progress["failed"]),
                         (5, 5, 3, 2))
        self.assertGreaterEqual(progress["pages"], 6, "careers pages the homepages linked to are counted")
        self.assertEqual(progress["stage"], "Done")
        self.assertEqual(run["stats"]["outcomes"], {"OK": 3, "BLOCKED": 1, "CAPTCHA": 1})
        self.assertEqual(run["stats"]["records"], 5)
        jobs = self.service.records(self.ctx, run["id"], "jobs")
        self.assertEqual(sorted(r["job_title"] for r in jobs["items"]),
                         ["Data Analyst", "ERP Analyst", "RPG Developer", "SAP FICO Consultant", "Senior Engineer"])
        companies = self.service.records(self.ctx, run["id"], "companies")
        self.assertEqual(sorted((r["company_name"], r["job_count"]) for r in companies["items"]),
                         [("Acme Manufacturing", 3), ("Beta Corp", 2)])
        self.assertIn("job_count", companies["columns"])
        results = self.store.all(self.ctx, "scrape_results", {"run_id": run["id"]})
        self.assertEqual({r["status"] for r in results}, {"ok", "blocked"})
        blocked = next(r for r in results if r["url"] == "https://cf.example/")
        self.assertEqual(blocked["data"]["outcome"], "CAPTCHA")

    def test_rejected_inputs_are_reported_with_rows(self) -> None:
        data = "Website\nhttps://beta.example/\nftp://x.com\n\nhttps://beta.example/\n".encode()
        run = self.service.start(self.ctx, None, "Get company name", file_bytes=data, filename="list.csv")
        self.assertEqual(run["stats"]["url_count"], 1)
        self.assertEqual([(r["row"], r["reason"]) for r in run["stats"]["rejected"]],
                         [(3, "unsupported protocol ftp:"), (5, "duplicate")])
        self.assertEqual(run["stats"]["inputs"][0]["row"], 2)
        with self.assertRaises(ValidationError):
            self.service.start(self.ctx, ["file:///etc/passwd", "http://10.0.0.1/"], "Get company")

    def test_formula_cells_are_neutralised(self) -> None:
        self.pages["https://f.example/"] = (200, "<html><head><title>=HYPERLINK(\"x\")</title></head></html>")
        run = self.service.start(self.ctx, ["https://f.example/"], "Get the page title")
        self.run_task(run)
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertIn("'=HYPERLINK", self.read(run, "csv").decode("utf-8-sig"))

    def test_date_filter(self) -> None:
        run = self.service.start(self.ctx, ["https://acme-mfg.com/careers"], "Get job titles posted in the last 7 days.")
        self.run_task(run)
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertEqual((run["stats"]["records"], run["stats"]["filtered_out"]), (1, 1))

    def test_cancel_a_queued_run(self) -> None:
        run = self.service.start(self.ctx, ["https://beta.example/"], "Get company name")
        run = self.service.cancel(self.ctx, run["id"])
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(self.platform.tasks.get(self.ctx, run["task_id"])["status"], "cancelled")
        with self.assertRaises(ValidationError):
            self.service.cancel(self.ctx, run["id"])

    def test_cancel_while_running_keeps_partial_results(self) -> None:
        run = self.service.start(self.ctx, ["https://beta.example/", "https://www.acme-mfg.com/"], "Get company name")
        service, ctx = self.service, self.ctx

        class CancellingSession(FakeSession):
            def request(inner, method, url, **kwargs):
                if url == "https://beta.example/":
                    service.cancel(ctx, run["id"])   # the user presses Cancel during the first URL
                return FakeSession.request(inner, method, url, **kwargs)

        from cloud.intel.core.http import SafeFetcher

        self.platform.config.extra["fetcher_factory"] = lambda: SafeFetcher(
            session=CancellingSession(self.pages), resolver=fake_resolver(), per_host_delay=0)
        task = self.run_task(run)
        self.assertEqual(task["status"], "cancelled")
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["stats"]["progress"]["processed"], 1)
        payload = json.loads(self.read(run, "json"))
        self.assertEqual([r["company_name"] for r in payload["records"]], ["Beta Corp"])

    def test_restart_recovery_skips_saved_urls_and_is_idempotent(self) -> None:
        run = self.service.start(self.ctx, ["https://beta.example/", "https://www.acme-mfg.com/"], "Get company name")
        # A previous attempt saved URL 0 and then the worker died.
        self.store.insert(self.ctx, "scrape_results", {
            "run_id": run["id"], "url": "https://beta.example/", "final_url": "https://beta.example/", "status": "ok",
            "method": "meta", "data": {"index": 0, "outcome": "OK", "input": run["stats"]["inputs"][0], "pages": [],
                                       "records": [{"company_name": "Beta Corp", "source_url": "https://beta.example/"}]},
            "field_sources": {}, "problems": []})
        self.assertEqual(self.run_task(run)["status"], "completed")
        fetched = [c for s in self.sessions for c in s.calls]
        self.assertNotIn("https://beta.example/", fetched)
        self.assertIn("https://www.acme-mfg.com/", fetched)
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertEqual(run["stats"]["records"], 2)

        # The same task delivered again (e.g. a lost ack) fetches nothing and adds no rows.
        before = len(fetched)

        class Reporter:
            checkpoint: Dict[str, Any] = {}

            def progress(self, *a, **k): pass
            def is_cancelled(self): return False
            def should_pause(self): return False

        run_scrape_task(self.platform, self.ctx, {"params": {"run_id": run["id"]}}, Reporter())
        self.assertEqual(len([c for s in self.sessions for c in s.calls]), before)
        self.assertEqual(len(self.store.all(self.ctx, "scrape_results", {"run_id": run["id"]})), 2)

    def test_retry_redoes_only_transient_failures(self) -> None:
        self.pages["https://slow.example/"] = (500, "upstream error")
        run = self.service.start(self.ctx, ["https://slow.example/", "https://walled.example/", "https://beta.example/"],
                                 "Get company name")
        self.run_task(run)
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertEqual(run["stats"]["outcomes"], {"FAILED": 1, "BLOCKED": 1, "OK": 1})
        self.pages["https://slow.example/"] = (200, "<html><head><title>Slow Co</title></head></html>")
        calls_before = len([c for s in self.sessions for c in s.calls])
        run = self.service.retry(self.ctx, run["id"])
        self.assertEqual(run["status"], "queued")
        self.assertEqual(self.run_task(run)["status"], "completed")
        calls = [c for s in self.sessions for c in s.calls][calls_before:]
        self.assertIn("https://slow.example/", calls)
        self.assertNotIn("https://walled.example/", calls, "a refusal is not retried")
        self.assertNotIn("https://beta.example/", calls)
        run = self.store.get(self.ctx, "scrape_runs", run["id"])
        self.assertEqual(run["stats"]["outcomes"], {"OK": 2, "BLOCKED": 1})
        self.assertEqual(len(self.store.all(self.ctx, "scrape_results", {"run_id": run["id"]})), 3)
        with self.assertRaises(ValidationError):
            self.service.retry(self.ctx, run["id"])


class GeminiQuotaRunTests(unittest.TestCase):
    """A whole run in free-only Gemini mode, through the real provider layer, when the
    free quota is used up on the first call: the run finishes on rules, at $0."""

    def test_run_falls_back_to_rules_when_free_quota_is_exhausted(self) -> None:
        from cloud.intel.ai.rest import GeminiProvider
        from cloud.tests.test_platform_ai_providers import GeminiSession, gemini_quota

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        store = MemoryStore()
        user = str(uuid.uuid4())
        ws = store.create_workspace(user, "W", "w-quota")
        ctx = Ctx(ws["id"], user, "owner", ai_external_allowed=True)
        store.update_workspace(ctx, ai_external_allowed=True,
                               settings={"ai": {"provider": "gemini", "model": "gemini-3.8-flash", "free_only": True,
                                                "max_budget_usd": 0}})
        platform = Platform(store, storage=LocalFileStorage(Path(scratch.name)),
                            config=PlatformConfig(extra={"fetcher_factory": lambda: fetcher_for(PAGES)}))
        session = GeminiSession(gemini_quota(daily=True))
        registry = platform.service("ai")
        registry._factory = lambda name, model, secrets=None, settings=None: GeminiProvider(  # noqa: SLF001
            model=model, api_key="g-key-12345678", session=session, free_tier=True)
        registry._cache.clear()  # noqa: SLF001
        run = platform.service("scraper").start(ctx, ["https://www.acme-mfg.com/", "https://beta.example/"],
                                                "Get company name and industry")
        self.assertEqual(run_task_inline(platform, ctx.workspace_id, run["task_id"])["status"], "completed")
        run = store.get(ctx, "scrape_runs", run["id"])
        self.assertEqual(len(session.calls), 1, "one request, then no more until the quota resets")
        self.assertIn("Free AI quota exhausted", run["stats"]["ai_note"])
        with platform.storage.open(run["stats"]["files"]["json"]["storage_key"]) as handle:
            payload = json.loads(handle.read())
        self.assertEqual([r["company_name"] for r in payload["records"]], ["Acme Manufacturing", "Beta Corp"])
        self.assertEqual([r["industry"] for r in payload["records"]], [None, None], "nothing invented")
        usage = store.all(ctx.as_system(), "ai_usage")
        self.assertTrue(all((u["estimated_cost_usd"] or 0) == 0 for u in usage))


class GeminiFailureRunTests(unittest.TestCase):
    """A run in free-only Gemini mode through the real provider layer: one call that
    Gemini answers with 503 (the live-test case) and one that succeeds."""

    def run_with(self, *responses):
        from cloud.intel.ai.rest import GeminiProvider
        from cloud.tests.test_platform_ai_providers import GeminiSession

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        store = MemoryStore()
        user = str(uuid.uuid4())
        ws = store.create_workspace(user, "W", f"w-{uuid.uuid4().hex[:8]}")
        ctx = Ctx(ws["id"], user, "owner", ai_external_allowed=True)
        store.update_workspace(ctx, ai_external_allowed=True,
                               settings={"ai": {"provider": "gemini", "model": "gemini-3.8-flash", "free_only": True,
                                                "max_budget_usd": 0}})
        platform = Platform(store, storage=LocalFileStorage(Path(scratch.name)),
                            config=PlatformConfig(extra={"fetcher_factory": lambda: fetcher_for(PAGES)}))
        session = GeminiSession(*responses)
        registry = platform.service("ai")
        registry._factory = lambda name, model, secrets=None, settings=None: GeminiProvider(  # noqa: SLF001
            model=model, api_key="g-key-12345678", session=session, free_tier=True)
        registry._cache.clear()  # noqa: SLF001
        run = platform.service("scraper").start(ctx, ["https://www.acme-mfg.com/"], "Get company name and industry",
                                                max_ai_calls=1)
        self.assertEqual(run_task_inline(platform, ctx.workspace_id, run["task_id"])["status"], "completed")
        run = store.get(ctx, "scrape_runs", run["id"])
        result = store.all(ctx, "scrape_results", {"run_id": run["id"]})[0]
        return store, ctx, session, run, result

    def test_a_failed_gemini_call_shows_at_run_and_page_level(self) -> None:
        store, ctx, session, run, result = self.run_with((503, {"error": {"code": 503, "status": "UNAVAILABLE",
                                                                          "message": "The model is overloaded."}}))
        self.assertEqual(len(session.calls), 1, "Gemini is not retried")
        page_note = next(p for p in result["problems"] if p.startswith("AI extraction skipped:"))
        self.assertIn("503", page_note)
        for note in (run["stats"]["ai_note"], run["stats"]["progress"]["ai_note"]):
            self.assertIn("1 AI call failed", note)
            self.assertIn("503", note)
        self.assertEqual((run["stats"]["ai_failures"], run["stats"]["progress"]["ai_failures"]), (1, 1))
        self.assertEqual(result["data"]["records"][0]["industry"], None, "nothing invented")
        usage = store.all(ctx.as_system(), "ai_usage")
        self.assertEqual([u["success"] for u in usage], [False])
        self.assertTrue(all((u["estimated_cost_usd"] or 0) == 0 for u in usage), "$0")

    def test_a_successful_gemini_call_reports_no_failure(self) -> None:
        from cloud.tests.test_platform_ai_providers import gemini_function_call

        answer = {"industry": {"value": "Energy equipment", "evidence": "hydraulic valves for the energy sector"}}
        store, ctx, session, run, result = self.run_with(gemini_function_call(answer))
        self.assertEqual(len(session.calls), 1)
        self.assertIsNone(run["stats"]["ai_note"])
        self.assertEqual(run["stats"]["ai_failures"], 0)
        self.assertFalse(any(p.startswith("AI extraction skipped") for p in result["problems"]))
        self.assertEqual(result["data"]["records"][0]["industry"], "Energy equipment")
        self.assertTrue(result["data"]["ai_used"])
        self.assertTrue(all((u["estimated_cost_usd"] or 0) == 0 for u in store.all(ctx.as_system(), "ai_usage")))


class ScraperApiTests(unittest.TestCase):
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
                                 config=PlatformConfig(files_dir=root / "platform",
                                                       extra={"fetcher_factory": lambda: fetcher_for(PAGES)}))
        issuer = DevTokenIssuer("scraper-api-tests-secret-0123456789abcdef")
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = TestClient(app)
        self.client.headers["Authorization"] = f"Bearer {issuer.issue('alice@example.com')['access_token']}"
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        response = self.client.post("/api/v1/workspaces", json={"name": "Scrape", "seed": False})
        self.assertEqual(response.status_code, 201, response.text)
        self.ws = response.json()["id"]
        self.base = f"/api/v1/w/{self.ws}"

    def test_paste_upload_run_records_and_downloads(self) -> None:
        schema = self.client.post(self.base + "/scraper/schema",
                                  json={"instruction": "Get company name, careers URL, ATS/platform and job titles."})
        self.assertEqual(schema.status_code, 200, schema.text)
        self.assertEqual(schema.json()["entity"], "job")
        r = self.client.post(self.base + "/scraper/runs", json={"urls": "https://beta.example/\nhttps://walled.example/",
                                                                "instruction": "Get company name and job titles"})
        self.assertEqual(r.status_code, 201, r.text)
        run = r.json()
        self.assertEqual(run["stats"]["progress"]["stage"], "Queued")
        run_task_inline(self.platform, self.ws, run["task_id"])
        run = self.client.get(f"{self.base}/scraper/runs/{run['id']}").json()
        self.assertEqual(run["status"], "completed")
        records = self.client.get(f"{self.base}/scraper/runs/{run['id']}/records", params={"view": "jobs"}).json()
        self.assertEqual([r["job_title"] for r in records["items"]], ["Senior Engineer", "Data Analyst"])
        pages = self.client.get(f"{self.base}/scraper/runs/{run['id']}/results").json()
        self.assertEqual(len(pages["items"]), 2)
        for fmt, view in (("csv", "all"), ("csv", "jobs"), ("xlsx", "all"), ("json", "all")):
            download = self.client.get(f"{self.base}/scraper/runs/{run['id']}/files/{fmt}", params={"view": view})
            self.assertEqual(download.status_code, 200, (fmt, view))
            self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(self.client.post(f"{self.base}/scraper/runs/{run['id']}/retry").status_code, 422)
        upload = self.client.post(self.base + "/scraper/runs", data={"instruction": "Get company name"},
                                  files={"file": ("urls.csv", b"url\nhttps://beta.example/\n", "text/csv")})
        self.assertEqual(upload.status_code, 201, upload.text)
        self.assertEqual(upload.json()["stats"]["inputs"][0], {"url": "https://beta.example/", "row": 2,
                                                               "batch": upload.json()["stats"]["inputs"][0]["batch"],
                                                               "source": "urls.csv"})
        cancelled = self.client.post(f"{self.base}/scraper/runs/{upload.json()['id']}/cancel")
        self.assertEqual(cancelled.json()["status"], "cancelled")
        bad = self.client.post(self.base + "/scraper/runs", json={"urls": "ftp://x.com", "instruction": "x"})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("unsupported protocol", bad.text)


if __name__ == "__main__":
    unittest.main()
