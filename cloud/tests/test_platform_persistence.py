"""PostgreSQL is the platform's persistence: data survives an API restart.

Each test builds the real FastAPI app on a migrated PostgreSQL database, writes
through the HTTP API, shuts the app down (closing every pool), builds a brand-new
app on the same database, and reads everything back. Also covers the boot-time
schema check and the explicit in-memory fallback.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.db.connection import ConfigurationError
from cloud.intel.bootstrap import build_platform
from cloud.intel.core.http import FetchResult, SafeFetcher
from cloud.intel.store.memory import MemoryStore
from cloud.intel.store.postgres import PostgresStore
from cloud.intel.store.schema_check import SchemaError, expected_tables, require_schema
from cloud.shared.storage import LocalFileStorage
from cloud.tests._pg import drop_database, fresh_database

SECRET = "persistence-tests-secret-0123456789abcdef-xyz"
REQUEST = ("Find US manufacturing companies with SAP or JD Edwards hiring. Remove companies already in my CRM. "
           "Rank them and prepare a Cox-Little campaign list.")


class RestartPersistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.url = fresh_database()
        cls.scratch = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        drop_database(cls.url)
        cls.scratch.cleanup()

    def setUp(self) -> None:
        for patcher in (mock.patch("cloud.intel.email.providers.dns_has_mail", lambda d, *a, **k: True),
                        mock.patch.object(SafeFetcher, "fetch", lambda self, url, **kw: FetchResult(url, url, 404))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.issuer = DevTokenIssuer(SECRET)
        self.email = f"owner-{uuid.uuid4().hex[:6]}@example.com"

    def start_api(self) -> TestClient:
        """A fresh API process: new settings, new pools, new platform — only the database is shared."""
        root = Path(self.scratch.name)
        settings = Settings(environment="test", auth_mode="dev", storage="postgres", database_url=self.url,
                            queue="inline", runner="none", results_dir=root / "results")
        app = create_app(settings, storage=LocalFileStorage(root / "results"), token_verifier=self.issuer)
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {self.issuer.issue(self.email)['access_token']}"
        client.__enter__()
        return client

    def stop_api(self, client: TestClient) -> None:
        client.__exit__(None, None, None)   # runs the lifespan shutdown: pools closed, platform closed

    def test_everything_survives_an_api_restart(self) -> None:
        api = self.start_api()
        self.assertIsInstance(api.app.state.platform.store, PostgresStore)
        ws = api.post("/api/v1/workspaces", json={"name": f"Persist {uuid.uuid4().hex[:6]}"}).json()["id"]
        base = f"/api/v1/w/{ws}"
        company = api.post(base + "/companies", json={
            "name": "Alpha Mfg", "website": "alpha.example", "industry": "Manufacturing",
            "country": "United States", "technologies": ["SAP S/4HANA"]}).json()
        company = company.get("company") or company
        api.post(base + "/jobs/ingest", json={"company_id": company["id"], "postings": [
            {"title": "SAP S/4HANA Implementation Lead", "job_url": "https://alpha.example/jobs/1",
             "company_name": "Alpha Mfg", "description": "SAP S/4HANA ERP implementation"}]})
        contact = api.post(base + "/contacts", json={"full_name": "Pat Rivera", "title": "CIO",
                                                     "company_id": company["id"]}).json()
        api.post(base + "/signals/run", json={})
        memory = api.post(base + "/agent/memory", json={"text": "Whenever I say ERP, include SAP and JD Edwards"}).json()
        turn = api.post(base + "/agent/ask", json={"text": REQUEST}).json()
        run = api.post(f"{base}/agent/runs/{turn['run']['id']}/run").json()
        self.assertEqual(run["status"], "awaiting_approval")
        approvals = api.get(base + "/agent/approvals", params={"run_id": run["id"]}).json()["items"]
        self.assertTrue(approvals)
        results_before = api.get(f"{base}/agent/runs/{run['id']}/results").json()
        opportunity = api.post(base + "/opportunities", json={"company_id": company["id"], "title": "ERP team"}).json()
        workflow = api.post(base + "/workflows", json={"name": "Spike follow-up", "trigger": "hiring_spike",
                                                       "actions": [{"type": "create_task", "title": "Review"}]}).json()
        lst = api.post(base + "/lists", json={"name": "Keep me", "entity_type": "companies"}).json()
        crawl_job = api.post("/api/v1/jobs", json={"type": "single_company", "website": "https://alpha.example"}).json()
        self.stop_api(api)

        api = self.start_api()                                   # ---- restart ----
        try:
            self.assertIn(ws, [w["id"] for w in api.get("/api/v1/workspaces").json()["items"]])
            self.assertEqual(api.get(f"{base}/companies/{company['id']}").json()["name"], "Alpha Mfg")
            self.assertEqual(api.get(f"{base}/contacts/{contact['id']}").json()["full_name"], "Pat Rivera")
            self.assertEqual(api.get(f"{base}/opportunities/{opportunity['id']}").json()["title"], "ERP team")
            self.assertEqual(api.get(base + "/jobs", params={"company_id": company["id"]}).json()["total"], 1)
            self.assertGreater(api.get(base + "/hiring-signals", params={"company_id": company["id"]}).json()["total"], 0)
            self.assertEqual(api.get(f"{base}/workflows/{workflow['id']}").json()["name"], "Spike follow-up")
            self.assertEqual(api.get(f"{base}/lists/{lst['id']}").json()["name"], "Keep me")
            self.assertEqual({c["key"] for c in api.get(base + "/campaigns").json()["items"]},
                             {"cox-little", "riseit", "itech-us"})
            # research run, plan, approvals, results and AI memory
            again = api.get(f"{base}/agent/runs/{run['id']}").json()
            self.assertEqual(again["status"], "awaiting_approval")
            self.assertEqual([s["tool"] for s in again["plan"]], [s["tool"] for s in run["plan"]])
            pending = api.get(base + "/agent/approvals", params={"run_id": run["id"]}).json()["items"]
            self.assertEqual({a["id"] for a in pending}, {a["id"] for a in approvals})
            self.assertEqual(api.get(f"{base}/agent/runs/{run['id']}/results").json()["total"], results_before["total"])
            self.assertIn(memory["id"], [m["id"] for m in api.get(base + "/agent/memory").json()["items"]])
            session = api.get(f"{base}/agent/sessions/{turn['session']['id']}").json()
            self.assertGreaterEqual(len(session["messages"]), 2)
            # audit trail, activities and the CareerCloud crawl job
            self.assertGreater(api.get(base + "/audit").json()["total"], 5)
            self.assertEqual(api.get(f"/api/v1/jobs/{crawl_job['job_id']}").status_code, 200)
            # and the persisted approval still works after the restart
            list_approval = next(a for a in pending if a["impact"]["tool"] == "create_list")
            self.assertEqual(api.post(f"{base}/agent/approvals/{list_approval['id']}/approve").status_code, 200)
            self.assertEqual(api.get(base + "/lists", params={"q": "COX-LITTLE"}).json()["total"], 1)
        finally:
            self.stop_api(api)

    def test_workspace_isolation_holds_after_restart(self) -> None:
        api = self.start_api()
        ws = api.post("/api/v1/workspaces", json={"name": f"Private {uuid.uuid4().hex[:6]}"}).json()["id"]
        api.post(f"/api/v1/w/{ws}/companies", json={"name": "Secret Co", "website": "secret.example"})
        self.stop_api(api)
        self.email = f"intruder-{uuid.uuid4().hex[:6]}@example.com"
        api = self.start_api()
        try:
            self.assertEqual(api.get(f"/api/v1/w/{ws}/companies").status_code, 404)
            self.assertNotIn(ws, [w["id"] for w in api.get("/api/v1/workspaces").json()["items"]])
        finally:
            self.stop_api(api)


class SchemaChecks(unittest.TestCase):
    def test_a_migrated_database_passes_with_every_table_verified(self) -> None:
        url = fresh_database()
        try:
            store = PostgresStore.from_url(url, max_size=2)
            report = require_schema(store._pool)  # noqa: SLF001
            store.close()
            self.assertEqual(report["missing_tables"], [])
            self.assertEqual(report["tables_verified"], len(expected_tables()))
            self.assertEqual(report["tables_verified"], 2 + 47 + 9 + 1 + 3 + 14)   # tenancy + 0003 + 0004 + 0005 + 0006 + 0007
        finally:
            drop_database(url)

    def test_an_unmigrated_database_fails_clearly_at_boot(self) -> None:
        url = fresh_database(migrate=False)
        try:
            settings = Settings(environment="test", auth_mode="dev", storage="postgres", database_url=url)
            with self.assertRaises(SchemaError) as caught:
                build_platform(role="api", settings=settings)
            message = str(caught.exception)
            self.assertIn("migrations not applied", message)
            self.assertIn("python -m cloud.devtools.localpg start", message)
        finally:
            drop_database(url)

    def test_a_database_behind_the_code_names_the_missing_migration(self) -> None:
        from cloud.db.migrate import apply_migrations, load_migrations

        url = fresh_database(migrate=False)
        try:
            apply_migrations(url, [m for m in load_migrations() if not m.version.startswith("0004")])
            with self.assertRaises(SchemaError) as caught:
                build_platform(role="worker", env={"CAREERCLOUD_ENV": "test", "CAREERCLOUD_DATABASE_URL": url})
            self.assertIn("0004_ai_control_room", str(caught.exception))
            self.assertIn("agent_", str(caught.exception))
        finally:
            drop_database(url)


class Defaults(unittest.TestCase):
    def test_postgres_is_the_default_and_memory_is_explicit(self) -> None:
        # Nothing configured: the platform wants PostgreSQL (and says so), it never silently uses memory.
        with self.assertRaises(ConfigurationError):
            build_platform(role="worker", env={"CAREERCLOUD_ENV": "test"})
        platform = build_platform(role="worker", env={"CAREERCLOUD_STORAGE": "memory"})
        self.assertIsInstance(platform.store, MemoryStore)
        with self.assertRaises(ValueError):
            build_platform(role="worker", env={"CAREERCLOUD_STORAGE": "sqlite"})

    def test_the_api_platform_follows_the_api_storage_setting(self) -> None:
        platform = build_platform(role="api", settings=Settings(auth_mode="dev", storage="memory"))
        self.assertIsInstance(platform.store, MemoryStore)

    def test_the_dev_env_template_uses_local_postgres(self) -> None:
        template = (Path(__file__).resolve().parents[1] / "api" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("CAREERCLOUD_STORAGE=postgres", template)
        self.assertIn("CAREERCLOUD_DATABASE_URL=localdev", template)


if __name__ == "__main__":
    unittest.main()
