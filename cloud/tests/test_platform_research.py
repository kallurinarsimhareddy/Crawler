"""The AI research agent: intent, plan, preview, approval, execution with
evidence, proposals that change nothing until applied, and the HTTP API."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, utcnow
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.research.intent import parse_intent
from cloud.intel.research.planner import build_plan
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage

EXAMPLE = ("Find US manufacturing companies using RPG or AS400, match them against my internal data, remove "
           "companies already in my CRM, find companies with new ERP hiring, identify missing IT/HR/VP contacts "
           "using my authorized sources, validate the emails, rank the opportunities, assign the correct staffing "
           "campaign, and export the results.")
SECOND = ("Find 500 US manufacturing companies with ERP hiring, remove companies already in our CRM, find missing "
          "IT leaders, validate emails and create an outreach list.")


class Recorder:
    def __init__(self) -> None:
        self.calls: List[tuple] = []


class StubContacts(Recorder):
    def find_contacts(self, ctx, company_ids, *, functions=(), allow_paid=False, providers=None):
        self.calls.append(("find_contacts", tuple(company_ids), tuple(functions), allow_paid))
        return {"companies": {cid: {"searched": list(functions)} for cid in company_ids}}


class StubEmail(Recorder):
    def validate(self, ctx, emails, *, allow_paid=False, max_age_days=30):
        self.calls.append(("validate", tuple(emails), allow_paid))
        return [{"email": e, "status": "VALID"} for e in emails]


class StubSignals(Recorder):
    def __init__(self, scores: Dict[str, float]) -> None:
        super().__init__()
        self.scores = scores

    def score_company(self, ctx, company_id):
        self.calls.append(("score", company_id))
        return {"opportunity_score": self.scores.get(company_id, 10.0), "breakdown": {"stub": True}}


class StubCampaigns(Recorder):
    def match_campaigns(self, ctx, company, signals, jobs):
        return [{"campaign": {"id": "cp_" + "1" * 32, "key": "cox-little", "name": "COX-LITTLE ERP"},
                 "score": 80.0, "reasons": ["ERP hiring"]}]


class StubCrm(Recorder):
    def __init__(self, store) -> None:
        super().__init__()
        self.store = store

    def add_to_list(self, ctx, list_id, entity_type, ids, reason=None):
        self.calls.append(("add_to_list", list_id, tuple(ids)))
        return len(ids)

    def create_opportunity(self, ctx, company_id, title, **kwargs):
        self.calls.append(("create_opportunity", company_id, kwargs.get("campaign_id")))
        return {"id": "op_" + uuid.uuid4().hex}


class StubAutomation(Recorder):
    def emit(self, ctx, trigger, event_key, payload):
        self.calls.append((trigger, event_key))
        return []


class IntentAndPlanTests(unittest.TestCase):
    def test_the_example_request_is_understood(self) -> None:
        intent = parse_intent(EXAMPLE)
        self.assertEqual(intent["country"], "United States")
        self.assertEqual(intent["industries"], ["Manufacturing"])
        self.assertEqual(intent["technologies"], ["RPG", "AS400"])
        self.assertEqual(intent["hiring"]["keywords"], ["ERP"])
        self.assertIn("NEW_ROLE", intent["hiring"]["signal_types"])
        self.assertTrue(intent["match_internal"] and intent["exclude_existing_crm"])
        self.assertEqual(intent["contact_functions"], ["it", "hr"])
        self.assertIn("VP", intent["contact_seniorities"])
        self.assertTrue(intent["use_authorized_sources"] and intent["validate_emails"] and intent["rank"])
        self.assertTrue(intent["assign_campaign"] and intent["export"])

    def test_the_example_plan_gates_paid_and_mutating_steps(self) -> None:
        plan = build_plan(parse_intent(EXAMPLE))
        self.assertEqual([s["tool"] for s in plan], [
            "query_companies", "match_internal", "exclude_existing_crm", "hiring_signals", "find_contacts",
            "validate_emails", "score", "assign_campaign", "create_opportunities", "export"])
        by_tool = {s["tool"]: s for s in plan}
        for tool in ("find_contacts", "validate_emails"):
            self.assertTrue(by_tool[tool]["spends_credits"])
            self.assertEqual(by_tool[tool]["execution"], "paid_if_approved")
        for tool in ("assign_campaign", "create_opportunities"):
            self.assertTrue(by_tool[tool]["mutates_crm"])
            self.assertEqual(by_tool[tool]["execution"], "proposal")
        self.assertEqual(by_tool["query_companies"]["execution"], "auto")

    def test_second_example(self) -> None:
        intent = parse_intent(SECOND)
        self.assertEqual(intent["count"], 500)
        self.assertTrue(intent["create_list"])
        self.assertEqual(intent["contact_functions"], ["it"])
        self.assertIn("create_list", [s["tool"] for s in build_plan(intent)])


class ResearchRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "W", "w-research")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.platform = Platform(self.store, storage=LocalFileStorage(Path(self.scratch.name)))
        self.seed()
        self.contacts, self.email = StubContacts(), StubEmail()
        self.signals = StubSignals({self.target: 90.0, self.second: 60.0})
        self.crm, self.automation = StubCrm(self.store), StubAutomation()
        for name, service in (("contacts", self.contacts), ("email", self.email), ("signals", self.signals),
                              ("campaigns", StubCampaigns()), ("crm", self.crm), ("automation", self.automation)):
            self.platform.override(name, service)

    def company(self, name: str, **values: Any) -> str:
        values.setdefault("industry", "Manufacturing")
        values.setdefault("country", "USA")
        return self.store.insert(self.ctx, "companies", {"name": name, **values})["id"]

    def job(self, company_id: str, title: str, days_ago: int = 3) -> str:
        seen = utcnow() - timedelta(days=days_ago)
        url = f"https://jobs.example/{uuid.uuid4().hex}"
        return self.store.insert(self.ctx, "job_postings", {
            "company_id": company_id, "company_name": "x", "title": title, "job_url": url, "url_key": url,
            "first_seen_at": seen, "last_seen_at": seen, "source_kind": "crawler", "source_name": "careercrawler"})["id"]

    def seed(self) -> None:
        self.target = self.company("Tulsa Parts", technologies=["IBM iSeries"], domain="tulsaparts.com")
        self.second = self.company("Ohio Castings", domain="ohiocastings.com")
        self.store.insert(self.ctx, "company_technologies", {
            "company_id": self.second, "technology": "RPG IV", "source": "zoominfo", "observed_at": utcnow(),
            "evidence_url": "https://zoominfo.example/tech"})
        customer = self.company("Existing Customer", technologies=["AS400"], lifecycle="customer")
        no_hiring = self.company("Quiet Co", technologies=["RPG"])
        overseas = self.company("Berlin GmbH", technologies=["RPG"], country="Germany")
        no_tech = self.company("Plain Co")
        for cid in (self.target, self.second, customer, overseas, no_tech):
            self.job(cid, "ERP Systems Analyst")
        self.job(no_hiring, "ERP Analyst", days_ago=200)  # too old for "new ERP hiring"
        self.store.insert(self.ctx, "hiring_signals", {
            "company_id": self.target, "signal_type": "NEW_ROLE", "detected_at": utcnow(), "confidence": 0.8,
            "summary": "New ERP role", "fingerprint": f"{self.target}:NEW_ROLE:1"})
        self.store.insert(self.ctx, "hiring_signals", {
            "company_id": self.second, "signal_type": "NEW_ROLE", "detected_at": utcnow(), "confidence": 0.7,
            "summary": "New ERP role", "fingerprint": f"{self.second}:NEW_ROLE:1"})
        self.store.insert(self.ctx, "contacts", {"company_id": self.target, "full_name": "Pat Lee",
                                                 "title": "IT Director", "email": "pat@tulsaparts.com"})

    def test_plan_approve_execute_then_apply(self) -> None:
        research = self.platform.service("research")
        run = research.plan(self.ctx, EXAMPLE)
        self.assertEqual(run["status"], "planned")
        self.assertEqual({a["type"] for a in run["proposed_actions"]}, {"assign_campaign", "create_opportunities"})
        self.assertGreater(run["estimated_credits"]["upper_bound"]["contact_enrichment"], 0)
        self.assertEqual(self.store.count(self.ctx, "platform_tasks"), 0)  # nothing runs before approval

        run = research.approve(self.ctx, run["id"], allow_paid=False)
        with self.assertRaises(ConflictError):
            research.approve(self.ctx, run["id"])
        task = run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        self.assertEqual(task["status"], "completed", task.get("error"))

        run = self.store.get(self.ctx, "research_runs", run["id"])
        self.assertEqual(run["status"], "completed")
        results = research.results(self.ctx, run["id"]).rows
        self.assertEqual([r["company_id"] for r in results], [self.target, self.second])
        top = results[0]
        self.assertEqual(top["score"], 90.0)
        reasons = " ".join(e["reason"] for e in top["evidence"])
        self.assertIn("technology=IBM iSeries", reasons)
        self.assertIn("NEW_ROLE", reasons)
        self.assertIn("open role: ERP Systems Analyst", reasons)
        self.assertEqual(top["data"]["contact_gap"], {"it": "NEEDS_VERIFICATION", "hr": "MISSING"})
        second_evidence = results[1]["evidence"][0]
        self.assertEqual(second_evidence["source"], "zoominfo")  # technology evidence carries its source

        # paid providers were not used without approval
        self.assertEqual(self.contacts.calls[0][3], False)
        self.assertEqual(self.email.calls[0][2], False)
        self.assertIn("paid providers not used", run["plan"][4]["result"]["detail"])
        # the export file exists
        self.assertTrue(self.platform.storage.exists(run["progress"]["files"]["storage_key"]))
        # nothing in the CRM changed yet
        self.assertEqual(self.crm.calls, [])
        self.assertIn(("research_completed", f"research:{run['id']}"), self.automation.calls)

        actions = {a["type"]: a["id"] for a in run["proposed_actions"]}
        run = research.apply_actions(self.ctx, run["id"], [actions["create_opportunities"]])
        created = [c for c in self.crm.calls if c[0] == "create_opportunity"]
        self.assertEqual([c[1] for c in created], [self.target, self.second])
        self.assertEqual(created[0][2], "cp_" + "1" * 32)
        applied = {a["type"]: a["status"] for a in run["proposed_actions"]}
        self.assertEqual(applied, {"assign_campaign": "proposed", "create_opportunities": "applied"})
        # applying twice does not duplicate
        research.apply_actions(self.ctx, run["id"], [actions["create_opportunities"]])
        self.assertEqual(len([c for c in self.crm.calls if c[0] == "create_opportunity"]), 2)
        # assign_campaign tags the company
        research.apply_actions(self.ctx, run["id"], [actions["assign_campaign"]])
        self.assertIn("campaign:cox-little", self.store.get(self.ctx, "companies", self.target)["tags"])
        self.assertTrue(self.store.count(self.ctx, "audit_log", {"action": "research.apply.create_opportunities"}))

    def test_paid_approval_is_passed_through(self) -> None:
        research = self.platform.service("research")
        run = research.approve(self.ctx, research.plan(self.ctx, EXAMPLE)["id"], allow_paid=True)
        run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        self.assertTrue(self.contacts.calls[0][3])

    def test_list_proposal_and_viewer_cannot_plan(self) -> None:
        research = self.platform.service("research")
        run = research.approve(self.ctx, research.plan(self.ctx, SECOND)["id"])
        run_task_inline(self.platform, self.ctx.workspace_id, run["task_id"])
        run = self.store.get(self.ctx, "research_runs", run["id"])
        lst = next(a for a in run["proposed_actions"] if a["type"] == "create_list")
        research.apply_actions(self.ctx, run["id"], [lst["id"]])
        self.assertEqual(self.store.count(self.ctx, "lists"), 1)
        viewer = str(uuid.uuid4())
        self.store.add_member(self.ctx, viewer, "viewer")
        with self.assertRaises(ForbiddenError):
            research.plan(Ctx(self.ctx.workspace_id, viewer, "viewer"), SECOND)

    def test_missing_services_degrade_honestly(self) -> None:
        platform = Platform(self.store, storage=LocalFileStorage(Path(self.scratch.name)))
        for name in ("contacts", "email", "signals", "campaigns", "dedupe", "automation"):
            platform.override(name, None)

        class Broken:
            def __getattr__(self, item):
                raise RuntimeError("not built")

        for name in ("contacts", "email", "signals", "campaigns", "dedupe"):
            platform.override(name, Broken())
        research = platform.service("research")
        run = research.approve(self.ctx, research.plan(self.ctx, EXAMPLE)["id"])
        task = run_task_inline(platform, self.ctx.workspace_id, run["task_id"])
        self.assertEqual(task["status"], "completed", task.get("error"))
        run = self.store.get(self.ctx, "research_runs", run["id"])
        self.assertEqual(run["result_count"], 2)
        details = " ".join(s["result"]["detail"] for s in run["plan"])
        self.assertIn("fallback scoring", details)


class ResearchApiTests(unittest.TestCase):
    def test_http_flow(self) -> None:
        from cloud.api.auth import DevTokenIssuer
        from cloud.api.main import create_app
        from cloud.api.settings import Settings
        from cloud.worker.dispatcher import NullDispatcher

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        store = MemoryStore()
        pages = {"https://www.acme.example/": (200, "<html><head><title>Acme</title>"
                                                   "<meta property='og:site_name' content='Acme'></head></html>")}
        from cloud.tests.test_platform_ai_fakes import fetcher_for

        platform = Platform(store, storage=LocalFileStorage(Path(scratch.name) / "p"),
                            config=PlatformConfig(extra={"fetcher_factory": lambda: fetcher_for(pages)}))
        issuer = DevTokenIssuer("platform-ai-tests-secret-0123456789abcdef")
        app = create_app(Settings(fake_step_seconds=0, auth_mode="dev", results_dir=Path(scratch.name) / "r"),
                         dispatcher=NullDispatcher(), token_verifier=issuer, platform=platform,
                         storage=LocalFileStorage(Path(scratch.name) / "r"))
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {issuer.issue('a@example.com')['access_token']}"
        ws = client.post("/api/v1/workspaces", json={"name": "Research WS", "seed": False})
        self.assertEqual(ws.status_code, 201, ws.text)
        base = f"/api/v1/w/{ws.json()['id']}"

        providers = client.get(f"{base}/ai/providers").json()
        self.assertEqual(providers["in_use"]["name"], "rules")
        schema = client.post(f"{base}/scraper/schema", json={"instruction": "Get company name and ATS"}).json()
        self.assertEqual([f["name"] for f in schema["fields"]], ["company_name", "ats"])

        run = client.post(f"{base}/scraper/runs", json={"urls": ["https://www.acme.example/"],
                                                        "instruction": "Get company name"})
        self.assertEqual(run.status_code, 201, run.text)
        run_task_inline(platform, ws.json()["id"], run.json()["task_id"])
        detail = client.get(f"{base}/scraper/runs/{run.json()['id']}").json()
        self.assertEqual(detail["status"], "completed")
        csv_file = client.get(f"{base}/scraper/runs/{run.json()['id']}/files/csv")
        self.assertEqual(csv_file.status_code, 200)
        self.assertIn("Acme", csv_file.text)
        self.assertEqual(client.get(f"{base}/scraper/runs/{run.json()['id']}/files/pdf").status_code, 404)

        upload = client.post(f"{base}/scraper/runs", data={"instruction": "Get company name", "column": "Site"},
                             files={"file": ("list.csv", b"Site\nhttps://www.acme.example/\n", "text/csv")})
        self.assertEqual(upload.status_code, 201, upload.text)

        plan = client.post(f"{base}/research/plan", json={"question": SECOND})
        self.assertEqual(plan.status_code, 201, plan.text)
        run_id = plan.json()["id"]
        self.assertEqual(client.get(f"{base}/research/runs").json()["total"], 1)
        approved = client.post(f"{base}/research/runs/{run_id}/approve", json={"allow_paid": False})
        self.assertEqual(approved.json()["status"], "approved")
        self.assertEqual(client.post(f"{base}/research/runs/{run_id}/actions", json={"action_ids": ["a1"]}).status_code,
                         409)  # not completed yet
        self.assertEqual(client.get(f"{base}/research/runs/{run_id}/results").json()["total"], 0)

        other = TestClient(app)
        other.headers["Authorization"] = f"Bearer {issuer.issue('b@example.com')['access_token']}"
        self.assertEqual(other.get(f"{base}/research/runs/{run_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
