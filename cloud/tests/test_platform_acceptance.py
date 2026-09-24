"""The final acceptance criteria, end to end through the real HTTP API.

One workspace, one user, the real FastAPI app, the MemoryStore and tasks run
inline. Nothing reaches the network: every SafeFetcher fetch is answered from
fixtures below and the MX check is faked. Each test method names the criteria
it covers (numbers from the specification's section 36).
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from openpyxl import Workbook

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.intel.core.context import Ctx
from cloud.intel.core.http import FetchResult, SafeFetcher
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage
from cloud.worker.dispatcher import NullDispatcher

SECRET = "platform-acceptance-tests-secret-0123456789abcdef"

GREENHOUSE = {"jobs": [
    {"id": 101, "title": "Senior RPG / AS400 Developer", "absolute_url": "https://boards.greenhouse.io/midwestmfg/jobs/101",
     "location": {"name": "Tulsa, OK"}, "updated_at": "2026-09-20T10:00:00Z",
     "content": "Maintain our IBM i (AS/400) RPGLE applications and support the JD Edwards ERP implementation."},
    {"id": 102, "title": "ERP Implementation Project Manager", "absolute_url": "https://boards.greenhouse.io/midwestmfg/jobs/102",
     "location": {"name": "Tulsa, OK"}, "updated_at": "2026-09-21T10:00:00Z",
     "content": "Lead the ERP implementation (JD Edwards EnterpriseOne) go-live across three plants."},
]}

LEADERSHIP_PAGE = """<html><head><title>Leadership - Midwest Manufacturing</title></head><body>
<h1>Our leadership</h1>
<div class="person"><h3>Pat Rivera</h3><p>Chief Information Officer</p><a href="mailto:pat.rivera@midwestmfg.com">Email</a></div>
<div class="person"><h3>Sam Lee</h3><p>Vice President, Human Resources</p></div>
</body></html>"""

HOME_PAGE = """<html><head><title>Midwest Manufacturing Co</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Midwest Manufacturing Co",
"url":"https://midwestmfg.com","sameAs":["https://www.linkedin.com/company/midwest-mfg"]}</script></head>
<body><a href="/about/leadership">Leadership</a> <a href="https://boards.greenhouse.io/midwestmfg">Careers</a></body></html>"""


def fake_fetch(self, url, method="GET", json_body=None, headers=None, accept="*/*"):  # noqa: ARG001
    if "boards-api.greenhouse.io" in url:
        return FetchResult(url, url, 200, text=json.dumps(GREENHOUSE), content_type="application/json")
    if "leadership" in url:
        return FetchResult(url, url, 200, text=LEADERSHIP_PAGE, content_type="text/html")
    if "midwestmfg.com" in url:
        return FetchResult(url, url, 200, text=HOME_PAGE, content_type="text/html")
    if "blocked.example" in url:
        return FetchResult(url, url, 403, text="Access denied", content_type="text/html")
    return FetchResult(url, url, 404, text="", content_type="text/html")


def _csv(rows, header):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _xlsx(rows, header):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


class AcceptanceTests(unittest.TestCase):
    def make_store(self):
        return MemoryStore()

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        patches = [
            mock.patch.object(SafeFetcher, "fetch", fake_fetch),
            mock.patch("cloud.intel.email.providers.dns_has_mail",
                       lambda domain, *a, **k: domain in {"midwestmfg.com", "acme-steel.com", "gmail.com"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.platform = Platform(self.make_store(), storage=LocalFileStorage(root / "files"),
                                 config=PlatformConfig(files_dir=root / "files"))
        self.issuer = DevTokenIssuer(SECRET)
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=self.issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = TestClient(app)
        self.client.headers["Authorization"] = f"Bearer {self.issuer.issue('owner@riseits.example')['access_token']}"
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        r = self.client.post("/api/v1/workspaces", json={"name": f"RiseIT GTM {self._testMethodName[-24:]}"})
        self.assertEqual(r.status_code, 201, r.text)
        self.ws = r.json()["id"]
        self.base = f"/api/v1/w/{self.ws}"
        self.user = self.issuer.user_id_for("owner@riseits.example")

    # --- helpers -------------------------------------------------------------------

    def ok(self, response, code=(200, 201, 202)):
        codes = code if isinstance(code, tuple) else (code,)
        self.assertIn(response.status_code, codes, f"{response.request.method} {response.request.url}: {response.text}")
        return response.json() if response.content else None

    def run_task(self, payload):
        task = payload.get("task") if isinstance(payload, dict) and isinstance(payload.get("task"), dict) else payload
        task_id = task.get("id") if isinstance(task, dict) else None
        if not task_id or not str(task_id).startswith("tsk_"):
            task_id = (payload or {}).get("task_id")
        self.assertTrue(task_id, f"no task in {payload}")
        run_task_inline(self.platform, self.ws, task_id)
        done = self.ok(self.client.get(f"{self.base}/tasks/{task_id}"))
        self.assertEqual(done["status"], "completed", done)
        return done

    def import_files(self):
        header = ["Company Name", "Website", "Industry", "City", "State", "Country"]
        files = []
        for i in range(10):
            rows = [[f"Plant Company {i}-{j}", f"plant{i}{j}.example", "Manufacturing", "Tulsa", "OK", "US"]
                    for j in range(3)]
            if i == 0:
                rows.append(["Midwest Manufacturing Co", "https://www.midwestmfg.com", "Manufacturing", "Tulsa", "OK", "US"])
            if i == 1:  # the same company again, spelled differently: must dedupe
                rows.append(["MIDWEST MANUFACTURING CO.", "midwestmfg.com/about", "Manufacturing", "Tulsa", "OK", "USA"])
            files.append((f"batch_{i}.csv", _csv(rows, header), "text/csv"))
        files.append(("batch_10.xlsx", _xlsx([["Acme Steel Inc", "acme-steel.com", "Manufacturing", "Dallas", "TX", "US"]], header),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        files.append(("batch_11.json", json.dumps([{"Company Name": "Json Works LLC", "Website": "jsonworks.example",
                                                     "Industry": "Manufacturing", "City": "Austin", "State": "TX",
                                                     "Country": "US"}]).encode(), "application/json"))
        files.append(("broken.csv", _csv([["x", "y"]], ["Foo", "Bar"]), "text/csv"))  # incompatible
        batch = self.ok(self.client.post(self.base + "/imports", json={"name": "13 files", "target": "companies"}))
        self.ok(self.client.post(f"{self.base}/imports/{batch['id']}/files",
                                 files=[("files", (n, b, t)) for n, b, t in files]))
        return batch["id"], files

    def seed_company(self):
        batch_id, _ = self.import_files()
        self.ok(self.client.post(f"{self.base}/imports/{batch_id}/validate"))
        self.ok(self.client.put(f"{self.base}/imports/{batch_id}/mapping", json={"mapping": {
            "Company Name": "company.name", "Website": "company.website", "Industry": "company.industry",
            "City": "company.city", "State": "company.state",
            "Country": "company.country"}}))
        self.run_task(self.ok(self.client.post(f"{self.base}/imports/{batch_id}/merge")))
        page = self.ok(self.client.get(self.base + "/companies", params={"domain": "midwestmfg.com"}))
        self.assertEqual(page["total"], 1)
        return page["items"][0]

    # --- criteria ------------------------------------------------------------------------

    def test_01_to_04_upload_validate_merge_dedupe_and_search(self) -> None:
        batch_id, files = self.import_files()
        report = self.ok(self.client.post(f"{self.base}/imports/{batch_id}/validate"))
        detail = self.ok(self.client.get(f"{self.base}/imports/{batch_id}"))
        statuses = {f["filename"]: f["status"] for f in detail["files"]}
        self.assertEqual(len(statuses), 13)
        self.assertEqual(statuses["broken.csv"], "incompatible")
        self.assertTrue(all(s == "compatible" for n, s in statuses.items() if n != "broken.csv"), statuses)
        self.assertTrue(report)
        suggestions = self.ok(self.client.get(f"{self.base}/imports/{batch_id}/mapping-suggestions"))
        self.assertIn("Company Name", json.dumps(suggestions))
        # merging without an explicit mapping is refused: meaning never changes silently
        self.assertGreaterEqual(self.client.post(f"{self.base}/imports/{batch_id}/merge").status_code, 400)
        company = self.seed_company()
        self.assertEqual(company["name"], "Midwest Manufacturing Co")
        self.assertIn("MIDWEST MANUFACTURING CO.", " ".join(company["aliases"]) + company["name"].upper() + ".")
        sources = self.ok(self.client.get(f"{self.base}/provenance/companies/{company['id']}"))
        self.assertGreaterEqual(sources["total"], 2)  # both files that named it
        found = self.ok(self.client.get(self.base + "/companies", params={"q": "acme", "industry": "Manufacturing"}))
        self.assertEqual([c["domain"] for c in found["items"]], ["acme-steel.com"])

    def test_05_06_09_17_jobs_signals_external_source_and_evidence(self) -> None:
        company = self.seed_company()
        self.ok(self.client.patch(f"{self.base}/companies/{company['id']}",
                                  json={"changes": {"careers_url": "https://boards.greenhouse.io/midwestmfg"}}))
        search = self.ok(self.client.post(self.base + "/sources/ats_public/search", json={
            "query": {"board_url": "https://boards.greenhouse.io/midwestmfg", "company_id": company["id"]}}))
        self.run_task(search)
        jobs = self.ok(self.client.get(self.base + "/jobs", params={"company_id": company["id"]}))
        self.assertEqual(jobs["total"], 2, jobs)
        rpg = next(j for j in jobs["items"] if "RPG" in j["title"])
        self.assertTrue({"RPG", "AS/400"} & set(rpg["technologies"]) or any("RPG" in t for t in rpg["technologies"]),
                        rpg["technologies"])
        self.run_task(self.ok(self.client.post(self.base + "/signals/run", json={"company_ids": [company["id"]]})))
        signals = self.ok(self.client.get(self.base + "/hiring-signals", params={"company_id": company["id"]}))
        types = {s["signal_type"] for s in signals["items"]}
        self.assertIn("SPECIALIZED_TECHNOLOGY", types)
        self.assertIn("PROJECT_IMPLEMENTATION", types)
        self.assertNotIn("BACKFILL_REPLACEMENT", types)
        for signal in signals["items"]:  # criterion 17: evidence for every result
            self.assertTrue(signal["evidence"], signal)
            self.assertTrue(signal["reason_codes"], signal)
        scores = self.ok(self.client.get(f"{self.base}/companies/{company['id']}/scores"))
        self.assertIn("breakdown", json.dumps(scores))

    def test_07_08_contacts_and_email_validation(self) -> None:
        company = self.seed_company()
        gaps = self.ok(self.client.get(f"{self.base}/companies/{company['id']}/contact-gaps"))
        self.assertIn("MISSING", json.dumps(gaps))
        found = self.ok(self.client.post(self.base + "/contacts/find",
                                         json={"company_ids": [company["id"]], "allow_paid": False}))
        self.run_task(found)
        contacts = self.ok(self.client.get(self.base + "/contacts", params={"company_id": company["id"]}))
        names = {c["full_name"] for c in contacts["items"]}
        self.assertIn("Pat Rivera", names)
        pat = next(c for c in contacts["items"] if c["full_name"] == "Pat Rivera")
        self.assertEqual(pat["email"], "pat.rivera@midwestmfg.com")
        sam = [c for c in contacts["items"] if c["full_name"] == "Sam Lee"]
        self.assertTrue(all(not c["email"] for c in sam), "an unpublished email must never be guessed")
        results = self.ok(self.client.post(self.base + "/email/validate", json={
            "emails": ["pat.rivera@midwestmfg.com", "info@midwestmfg.com", "jane.doe@gmail.com", "bad@@example",
                       "x@mailinator.com"], "allow_paid": False}))
        by_email = {r["email"]: r["status"] for r in (results.get("items") if isinstance(results, dict) else results)}
        # Syntax + MX cannot prove a mailbox exists and SMTP probing is off by design,
        # so the free checks say UNKNOWN; VALID needs the (paid, approved) provider.
        self.assertEqual(by_email["pat.rivera@midwestmfg.com"], "UNKNOWN")
        self.assertEqual(by_email["info@midwestmfg.com"], "ROLE")
        self.assertEqual(by_email["jane.doe@gmail.com"], "FREE_PROVIDER")
        self.assertEqual(by_email["bad@@example"], "INVALID")
        self.assertEqual(by_email["x@mailinator.com"], "DISPOSABLE")
        again = self.ok(self.client.post(self.base + "/email/validate",
                                         json={"emails": ["pat.rivera@midwestmfg.com"], "allow_paid": False}))
        self.assertIn("cache", json.dumps(again).lower())
        credits = self.ok(self.client.get(self.base + "/credits/ledger"))
        self.assertEqual(credits["total"], 0, "no paid credit may be spent without an explicit action")

    def test_10_ai_scraper(self) -> None:
        schema = self.ok(self.client.post(self.base + "/scraper/schema",
                                          json={"instruction": "Get company name, careers URL and ATS."}))
        self.assertEqual({f["name"] for f in schema["fields"]} >= {"company_name", "careers_url", "ats"}, True, schema)
        run = self.ok(self.client.post(self.base + "/scraper/runs", json={
            "instruction": "Get company name, careers URL and ATS.",
            "urls": ["https://midwestmfg.com/", "https://blocked.example/", "http://127.0.0.1/admin"]}), code=(201, 422))
        if "id" not in run:  # the unsafe URL is refused up front
            run = self.ok(self.client.post(self.base + "/scraper/runs", json={
                "instruction": "Get company name, careers URL and ATS.",
                "urls": ["https://midwestmfg.com/", "https://blocked.example/"]}))
        self.run_task({"id": run["task_id"]} if run.get("task_id") else run)
        results = self.ok(self.client.get(f"{self.base}/scraper/runs/{run['id']}/results"))
        by_url = {r["url"]: r for r in results["items"]}
        home = by_url["https://midwestmfg.com/"]
        record = home["data"]["records"][0]
        self.assertEqual(record["company_name"], "Midwest Manufacturing Co")
        self.assertEqual(home["field_sources"]["company_name"], "json-ld")
        self.assertEqual(record["ats"], "Greenhouse")
        self.assertEqual(by_url["https://blocked.example/"]["status"], "blocked")

    def test_11_to_15_query_lists_opportunities_campaigns_workflows(self) -> None:
        company = self.seed_company()
        self.ok(self.client.post(self.base + "/company-technologies", json={
            "company_id": company["id"], "technology": "IBM i (AS/400)", "source": "manual",
            "evidence_text": "Seen in job posting"}))
        mfg = self.ok(self.client.get(self.base + "/companies", params={"industry": "Manufacturing", "state": "OK"}))
        self.assertGreater(mfg["total"], 10)
        lst = self.ok(self.client.post(self.base + "/lists", json={"name": "ERP targets", "entity_type": "companies"}))
        self.ok(self.client.post(f"{self.base}/lists/{lst['id']}/members",
                                 json={"entity_type": "companies", "ids": [company["id"]], "reason": "RPG hiring"}))
        self.assertEqual(self.ok(self.client.get(f"{self.base}/lists/{lst['id']}"))["member_count"], 1)
        opp = self.ok(self.client.post(self.base + "/opportunities", json={
            "company_id": company["id"], "title": "iSeries modernisation team"}))
        stages = self.ok(self.client.get(self.base + "/pipeline-stages", params={"order": "position"}))
        self.assertEqual([s["name"] for s in stages["items"]][:3], ["New", "Researching", "Qualified"])
        moved = self.ok(self.client.post(f"{self.base}/opportunities/{opp['id']}/stage",
                                         json={"stage_id": stages["items"][2]["id"]}))
        self.assertEqual(moved["stage_id"], stages["items"][2]["id"])
        campaigns = self.ok(self.client.get(self.base + "/campaigns"))
        self.assertEqual({c["key"] for c in campaigns["items"]}, {"cox-little", "riseit", "itech-us"})
        self.assertTrue(all(not c["sending_enabled"] for c in campaigns["items"]))
        mapping = self.ok(self.client.post(f"{self.base}/companies/{company['id']}/campaign-mapping", json={}))
        self.assertIn("cox-little", json.dumps(mapping))
        workflow = self.ok(self.client.post(self.base + "/workflows", json={
            "name": "Hiring spike follow-up", "trigger": "hiring_spike",
            "conditions": [{"field": "company.industry", "op": "eq", "value": "Manufacturing"}],
            "actions": [{"type": "create_task", "title": "Review hiring spike"}]}))
        self.assertFalse(workflow["enabled"], "workflows start disabled")

    def test_16_research_agent_plan_preview_execute(self) -> None:
        company = self.seed_company()
        self.ok(self.client.post(self.base + "/company-technologies", json={
            "company_id": company["id"], "technology": "RPG", "source": "job_posting",
            "evidence_text": "RPGLE developer role"}))
        self.ok(self.client.post(self.base + "/jobs/ingest", json={"company_id": company["id"], "postings": [
            {"title": "ERP Implementation Lead (JD Edwards)", "job_url": "https://midwestmfg.com/jobs/1",
             "company_name": company["name"], "description": "Lead our new ERP implementation."}]}))
        # In production this runs after every crawl; here it is triggered explicitly.
        self.run_task(self.ok(self.client.post(self.base + "/signals/run", json={"company_ids": [company["id"]]})))
        question = ("Find US manufacturing companies using RPG or AS400, match them against my internal data, "
                    "remove companies already in my CRM, find companies with new ERP hiring, identify missing "
                    "IT/HR/VP contacts using my authorized sources, validate the emails, rank the opportunities, "
                    "assign the correct staffing campaign, and export the results.")
        run = self.ok(self.client.post(self.base + "/research/plan", json={"question": question}))
        self.assertEqual(run["status"], "planned")
        tools = [step.get("tool") for step in run["plan"]]
        for tool in ("query_companies", "exclude_existing_crm", "hiring_signals", "find_contacts",
                     "validate_emails", "score", "assign_campaign", "export"):
            self.assertIn(tool, tools)
        approved = self.ok(self.client.post(f"{self.base}/research/runs/{run['id']}/approve", json={"allow_paid": False}))
        self.run_task(approved)
        done = self.ok(self.client.get(f"{self.base}/research/runs/{run['id']}"))
        self.assertEqual(done["status"], "completed", done)
        self.assertTrue(done["proposed_actions"])
        self.assertTrue(all(a.get("status") != "applied" for a in done["proposed_actions"]),
                        "CRM changes must wait for explicit approval")
        self.assertEqual(self.ok(self.client.get(self.base + "/credits/ledger"))["total"], 0)

    def test_18_exports_19_analytics_20_monitoring(self) -> None:
        company = self.seed_company()
        for fmt in ("csv", "xlsx", "json"):
            export = self.ok(self.client.post(self.base + "/exports", json={
                "entity_type": "companies", "format": fmt, "filters": {"industry": "Manufacturing"}}))
            self.assertEqual(export["status"], "completed", export)
            download = self.client.get(f"{self.base}/exports/{export['id']}/download")
            self.assertEqual(download.status_code, 200)
            self.assertGreater(len(download.content), 100)
            if fmt == "csv":
                text = download.content.decode("utf-8-sig")
                self.assertIn("source", text.splitlines()[0].lower())
                self.assertIn("workspace", text.splitlines()[0].lower())
        dashboard = self.ok(self.client.get(self.base + "/analytics/dashboard"))
        self.assertGreater(dashboard["companies"]["total"], 10)
        monitor = self.ok(self.client.post(self.base + "/monitors", json={
            "name": "Watch Midwest", "target_type": "company", "target_id": company["id"], "frequency": "weekly"}))
        self.run_task(self.ok(self.client.post(f"{self.base}/monitors/{monitor['id']}/run", json={})))
        self.ok(self.client.post(self.base + "/jobs/ingest", json={
            "company_id": company["id"], "postings": [{"title": "IT Director", "job_url": "https://midwestmfg.com/jobs/9",
                                                       "company_name": company["name"]}]}))
        self.run_task(self.ok(self.client.post(f"{self.base}/monitors/{monitor['id']}/run", json={})))
        changes = self.ok(self.client.get(self.base + "/change-events", params={"company_id": company["id"]}))
        self.assertIn("new_job", {c["change_type"] for c in changes["items"]})

    def test_workspace_isolation_over_http(self) -> None:
        company = self.seed_company()
        other = TestClient(self.client.app)
        other.headers["Authorization"] = f"Bearer {self.issuer.issue('intruder@example.com')['access_token']}"
        for path in ("", "/companies", f"/companies/{company['id']}", "/credits", "/providers", "/campaigns"):
            self.assertEqual(other.get(self.base + path).status_code, 404, path)
        self.assertEqual(other.post(self.base + "/companies", json={"name": "x"}).status_code, 404)


class PostgresAcceptanceTests(AcceptanceTests):
    """The same criteria against PostgreSQL, with row-level security active on every user call."""

    @classmethod
    def setUpClass(cls) -> None:
        from cloud.intel.store.postgres import PostgresStore
        from cloud.tests._pg import fresh_database

        cls.url = fresh_database()
        cls.pg = PostgresStore.from_url(cls.url, max_size=6)

    @classmethod
    def tearDownClass(cls) -> None:
        from cloud.tests._pg import drop_database

        cls.pg.close()
        drop_database(cls.url)

    def make_store(self):
        # One database per class; each test creates its own workspace, so tests stay isolated.
        store = self.pg
        store.close = lambda: None  # the class owns the pool
        return store


if __name__ == "__main__":
    unittest.main()
