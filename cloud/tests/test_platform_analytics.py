"""Track K analytics, plus an API smoke test for the GTM, automation and analytics routes."""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore


def seed(store, ctx, *, companies=3, jobs=4):
    now = utcnow()
    made = []
    for i in range(companies):
        made.append(store.insert(ctx, "companies", {"name": f"Co {i}", "hiring_count": i}))
    for i in range(jobs):
        store.insert(ctx, "job_postings", {
            "company_id": made[0]["id"], "company_name": "Co 0", "title": f"Job {i}", "job_url": f"https://j/{i}",
            "url_key": f"j/{i}", "first_seen_at": now - timedelta(days=i), "last_seen_at": now,
            "source_kind": "crawler", "source_name": "careercrawler", "is_relevant": i % 2 == 0,
            "technologies": ["SAP"] if i < 2 else ["AWS"]})
    store.insert(ctx, "hiring_signals", {"company_id": made[0]["id"], "signal_type": "HIRING_SPIKE",
                                         "detected_at": now, "fingerprint": "f1"})
    store.insert(ctx, "contacts", {"full_name": "A", "email": "a@x.example", "email_status": "VALID",
                                   "function": "it"})
    store.insert(ctx, "contacts", {"full_name": "B", "email": "b@x.example"})
    pipeline = store.insert(ctx, "pipelines", {"name": "Sales"})
    stage = store.insert(ctx, "pipeline_stages", {"pipeline_id": pipeline["id"], "name": "New", "position": 0})
    store.insert(ctx, "opportunities", {"company_id": made[0]["id"], "title": "Deal", "pipeline_id": pipeline["id"],
                                        "stage_id": stage["id"], "amount": 5000})
    system = ctx.as_system()
    store.insert(system, "credit_accounts", {"provider": "zoominfo", "total_credits": 100, "consumed_credits": 30,
                                             "reserved_credits": 5})
    store.insert(system, "platform_tasks", {"kind": "crawl", "status": "completed"})
    store.insert(system, "platform_tasks", {"kind": "crawl", "status": "failed"})
    store.insert(system, "usage_events", {"provider": "zoominfo", "operation": "search", "success": True})
    store.insert(system, "usage_events", {"provider": "zoominfo", "operation": "search", "success": False})
    return made


class AnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.platform = Platform(self.store)
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "An", "an-ws")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        seed(self.store, self.ctx)
        self.analytics = self.platform.service("analytics")

    def test_dashboard_numbers(self) -> None:
        d = self.analytics.dashboard(self.ctx)
        self.assertEqual(d["companies"]["total"], 3)
        self.assertEqual(d["companies"]["with_open_jobs"], 2)
        self.assertEqual(d["jobs"]["discovered"], 4)
        self.assertEqual(d["jobs"]["relevant"], 2)
        self.assertEqual(d["jobs"]["top_technologies"], {"SAP": 2, "AWS": 2})
        self.assertEqual(d["signals"]["by_type"], {"HIRING_SPIKE": 1})
        self.assertEqual(d["contacts"]["verified_emails"], 1)
        self.assertEqual(d["pipeline"]["open_pipeline_value"], 5000.0)
        self.assertEqual(d["pipeline"]["by_stage"][0]["count"], 1)
        self.assertEqual(d["credits"]["accounts"][0]["remaining"], 65.0)
        self.assertEqual(d["sources"]["tasks_by_kind"]["crawl"]["success_rate"], 0.5)
        self.assertEqual(d["sources"]["providers"]["zoominfo"]["success_rate"], 0.5)
        self.assertEqual(sum(p["count"] for p in d["series"]["job_postings"]), 4)

    def test_timeseries_buckets_by_day(self) -> None:
        series = self.analytics.timeseries(self.ctx, "job_postings", 7)
        self.assertEqual(len(series["points"]), 7)
        self.assertEqual([p["count"] for p in series["points"]][-4:], [1, 1, 1, 1])
        with self.assertRaises(Exception):
            self.analytics.timeseries(self.ctx, "audit_log", 7)

    def test_workspace_isolation(self) -> None:
        other_user = str(uuid.uuid4())
        other = self.store.create_workspace(other_user, "Other", "other-an")
        d = self.analytics.dashboard(Ctx(other["id"], other_user, "owner"))
        self.assertEqual(d["companies"]["total"], 0)
        self.assertEqual(d["jobs"]["discovered"], 0)
        self.assertEqual(d["credits"]["accounts"], [])

    def test_analytics_task_snapshot(self) -> None:
        from cloud.intel.tasks.worker import run_task_inline

        task = self.platform.tasks.submit(self.ctx, "analytics")
        done = run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["result"]["dashboard"]["companies"]["total"], 3)


class ApiSmokeTests(unittest.TestCase):
    SECRET = "gtm-api-tests-secret-0123456789abcdef"

    def setUp(self) -> None:
        from cloud.api.auth import DevTokenIssuer
        from cloud.api.main import create_app
        from cloud.api.settings import Settings
        from cloud.shared.storage import LocalFileStorage
        from cloud.worker.dispatcher import NullDispatcher

        env = mock.patch.dict(os.environ, {"CAREERCLOUD_UNSUBSCRIBE_SECRET": "u-secret",
                                           "CAREERCLOUD_INBOUND_WEBHOOK_SECRET": "hook-secret"})
        env.start()
        self.addCleanup(env.stop)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = MemoryStore()
        self.platform = Platform(self.store)
        issuer = DevTokenIssuer(self.SECRET)
        app = create_app(Settings(auth_mode="dev", results_dir=Path(scratch.name) / "r"),
                         storage=LocalFileStorage(Path(scratch.name) / "r"), token_verifier=issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = TestClient(app)
        token = issuer.issue("gtm@example.com")
        self.client.headers["Authorization"] = f"Bearer {token['access_token']}"
        self.user = issuer.verify(token["access_token"]).user_id
        ws = self.store.create_workspace(self.user, "Api", "api-gtm")
        self.ws = ws["id"]
        self.ctx = Ctx(self.ws, self.user, "owner")
        self.platform.service("campaigns").ensure_defaults(self.ctx)

    def url(self, path):
        return f"/api/v1/w/{self.ws}{path}"

    def test_campaigns_sequences_and_unsubscribe(self) -> None:
        r = self.client.get(self.url("/campaigns"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 3)
        company = self.store.insert(self.ctx, "companies", {"name": "Acme"})
        contact = self.store.insert(self.ctx, "contacts", {"company_id": company["id"], "full_name": "Jo",
                                                           "first_name": "Jo", "email": "jo@acme.example"})
        r = self.client.post(self.url(f"/companies/{company['id']}/campaign-mapping"), json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["created"])
        t = self.client.post(self.url("/templates"), json={"name": "T", "subject": "Hi {{contact.first_name}}",
                                                            "body": "{{company.name}}"}).json()
        self.assertEqual(t["variables"], ["company.name", "contact.first_name"])
        r = self.client.post(self.url(f"/templates/{t['id']}/preview"), json={"contact_id": contact["id"]})
        self.assertEqual(r.json()["subject"], "Hi Jo")
        s = self.client.post(self.url("/sequences"), json={"name": "S"}).json()
        r = self.client.post(self.url("/sequence-steps"), json={"sequence_id": s["id"], "template_id": t["id"]})
        self.assertEqual(r.status_code, 201, r.text)
        r = self.client.post(self.url(f"/sequences/{s['id']}/enroll"), json={"contact_ids": [contact["id"]]})
        enrollment = r.json()["results"][0]["enrollment"]
        self.assertEqual(enrollment["status"], "pending_approval")
        self.assertEqual(self.client.post(self.url("/enrollments"), json={}).status_code, 405)
        r = self.client.post(self.url("/enrollments/approve"), json={"enrollment_ids": [enrollment["id"]]})
        self.assertEqual(r.json()["approved"][0]["status"], "active")
        r = self.client.post(self.url("/sequences/process-due"))
        self.assertEqual(r.json()["sent"], 0)
        # inbound webhook needs the shared secret
        bad = self.client.post(self.url("/events/inbound"), json={"kind": "reply", "email": "jo@acme.example"},
                               headers={"X-Webhook-Secret": "wrong"})
        self.assertEqual(bad.status_code, 401)
        # public unsubscribe: GET shows a form and changes nothing; POST unsubscribes
        token = self.platform.service("sequences").unsubscribe_token(self.ctx, contact["id"])
        anon = TestClient(self.client.app)
        self.assertEqual(anon.get(f"/api/v1/unsubscribe/{token}").status_code, 200)
        self.assertFalse(self.store.get(self.ctx, "contacts", contact["id"])["unsubscribed"])
        self.assertEqual(anon.post(f"/api/v1/unsubscribe/{token}x").status_code, 422)
        self.assertEqual(anon.post(f"/api/v1/unsubscribe/{token}").json()["unsubscribed"], True)
        self.assertTrue(self.store.get(self.ctx, "contacts", contact["id"])["unsubscribed"])

    def test_workflows_and_analytics(self) -> None:
        r = self.client.post(self.url("/workflows"), json={"name": "W", "trigger": "new_company",
                                                           "actions": [{"type": "create_task", "title": "x"}]})
        self.assertEqual(r.status_code, 201, r.text)
        wf = r.json()
        self.assertFalse(wf["enabled"])
        r = self.client.post(self.url(f"/workflows/{wf['id']}/test"), json={"payload": {}})
        self.assertTrue(r.json()["conditions_met"])
        bad = self.client.post(self.url("/workflows"), json={"name": "W", "trigger": "bogus", "actions": []})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.client.get(self.url("/workflow-runs")).status_code, 200)
        r = self.client.get(self.url("/analytics/dashboard"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["campaigns"]["total"], 3)
        r = self.client.get(self.url("/analytics/timeseries?entity=companies&days=5"))
        self.assertEqual(len(r.json()["points"]), 5)
        other = str(uuid.uuid4())
        foreign = self.store.create_workspace(other, "F", "foreign-gtm")
        self.assertEqual(self.client.get(f"/api/v1/w/{foreign['id']}/analytics/dashboard").status_code, 404)


if __name__ == "__main__":
    unittest.main()
