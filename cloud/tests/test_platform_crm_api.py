"""Track A over HTTP: CRM, imports and exports through the real FastAPI app,
including workspace isolation (another user's workspace is a 404)."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks.worker import run_task_inline
from cloud.shared.storage import LocalFileStorage
from cloud.worker.dispatcher import NullDispatcher

SECRET = "platform-track-a-tests-secret-0123456789abcdef"


class CrmApiTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.platform = Platform(MemoryStore(), storage=LocalFileStorage(root / "platform"),
                                 config=PlatformConfig(files_dir=root / "platform"))
        self.issuer = DevTokenIssuer(SECRET)
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=self.issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = self._client(app, "alice@example.com")
        self.mallory = self._client(app, "mallory@example.com")
        response = self.client.post("/api/v1/workspaces", json={"name": "Alice GTM", "seed": False})
        self.assertEqual(response.status_code, 201, response.text)
        self.ws = response.json()["id"]
        self.base = f"/api/v1/w/{self.ws}"
        from cloud.intel.core.context import Ctx

        self.ctx = Ctx(self.ws, self.issuer.user_id_for("alice@example.com"), "owner")
        self.platform.service("crm").ensure_defaults(self.ctx)

    def _client(self, app, email):
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {self.issuer.issue(email)['access_token']}"
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_company_contact_opportunity_flow(self) -> None:
        r = self.client.post(self.base + "/companies", json={"name": "Acme Corp", "website": "acme.com",
                                                             "industry": "Manufacturing"})
        self.assertEqual(r.status_code, 201, r.text)
        company = r.json()
        self.assertEqual(company["domain"], "acme.com")
        self.assertEqual(self.client.post(self.base + "/companies", json={"name": "Acme Corp"}).status_code, 409)
        r = self.client.get(self.base + "/companies", params={"industry": "Manufacturing", "q": "acme"})
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(self.client.get(self.base + "/companies", params={"bogus": "1"}).status_code, 422)
        r = self.client.post(self.base + "/contacts", json={"full_name": "Jane Doe", "email": "jane@acme.com",
                                                            "title": "CIO"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["company_id"], company["id"])
        r = self.client.get(f"{self.base}/companies/{company['id']}/contacts")
        self.assertEqual(r.json()["total"], 1)
        r = self.client.post(self.base + "/opportunities", json={"company_id": company["id"], "title": "ERP team",
                                                                 "score": 80, "reason": "SAP hiring"})
        self.assertEqual(r.status_code, 201, r.text)
        opp = r.json()
        pipeline = self.client.get(self.base + "/pipelines").json()["items"][0]
        stages = self.client.get(f"{self.base}/pipelines/{pipeline['id']}/stages").json()["items"]
        r = self.client.post(f"{self.base}/opportunities/{opp['id']}/stage", json={"stage_id": stages[2]["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        board = self.client.get(f"{self.base}/pipelines/{pipeline['id']}/board").json()
        self.assertEqual(board["columns"][2]["total"], 1)
        r = self.client.patch(f"{self.base}/opportunities/{opp['id']}", json={"changes": {"stage_id": "x"}})
        self.assertEqual(r.status_code, 422)
        timeline = self.client.get(f"{self.base}/companies/{company['id']}/timeline").json()["items"]
        self.assertTrue(any(i["kind"] == "stage_changed" for i in timeline))
        sources = self.client.get(f"{self.base}/companies/{company['id']}/sources").json()
        self.assertEqual(sources["items"][0]["source_kind"], "manual")
        r = self.client.post(self.base + "/lists", json={"name": "Hot", "entity_type": "companies"})
        list_id = r.json()["id"]
        r = self.client.post(f"{self.base}/lists/{list_id}/members", json={"ids": [company["id"]]})
        self.assertEqual(r.json(), {"added": 1})
        self.assertEqual(self.client.get(f"{self.base}/lists/{list_id}/members").json()["total"], 1)

    def test_idempotency_key(self) -> None:
        headers = {"Idempotency-Key": "abc-123"}
        a = self.client.post(self.base + "/tags", json={"name": "erp"}, headers=headers)
        b = self.client.post(self.base + "/tags", json={"name": "erp"}, headers=headers)
        self.assertEqual((a.status_code, b.status_code), (201, 201))
        self.assertEqual(a.json()["id"], b.json()["id"])
        c = self.client.post(self.base + "/tags", json={"name": "cloud"}, headers=headers)
        self.assertEqual(c.status_code, 409)

    def test_import_upload_map_merge_and_export_over_http(self) -> None:
        batch = self.client.post(self.base + "/imports", json={"name": "HTTP import"}).json()
        files = [("files", (f"f{i}.csv", f"Company,Website\nCo {i},co{i}.com\nShared,shared.com\n".encode(),
                            "text/csv")) for i in range(12)]
        r = self.client.post(f"{self.base}/imports/{batch['id']}/files", files=files)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len([f for f in r.json()["files"] if "id" in f]), 12)
        report = self.client.post(f"{self.base}/imports/{batch['id']}/validate").json()
        self.assertEqual(report["compatible"], 12)
        suggestions = self.client.get(f"{self.base}/imports/{batch['id']}/mapping-suggestions").json()
        self.assertFalse(suggestions["applied"])
        r = self.client.put(f"{self.base}/imports/{batch['id']}/mapping",
                            json={"mapping": {"Company": "company.name", "Website": "company.website"}})
        self.assertEqual(r.status_code, 200, r.text)
        task = self.client.post(f"{self.base}/imports/{batch['id']}/merge").json()
        run_task_inline(self.platform, self.ws, task["id"])
        self.assertEqual(self.client.get(f"{self.base}/tasks/{task['id']}").json()["status"], "completed")
        self.assertEqual(self.client.get(self.base + "/companies").json()["total"], 13)
        rows = self.client.get(f"{self.base}/imports/{batch['id']}/rows", params={"limit": 5}).json()
        self.assertEqual(rows["total"], 24)
        export = self.client.post(self.base + "/exports", json={"entity_type": "companies", "format": "csv"}).json()
        r = self.client.get(f"{self.base}/exports/{export['id']}/download")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("_source_names", r.content.decode("utf-8-sig").splitlines()[0])

    def test_workspace_isolation_over_http(self) -> None:
        company = self.client.post(self.base + "/companies", json={"name": "Private Co"}).json()
        for method, path in (("get", "/companies"), ("get", f"/companies/{company['id']}"),
                             ("get", "/imports"), ("get", "/exports"), ("get", "/pipelines")):
            r = getattr(self.mallory, method)(self.base + path)
            self.assertEqual(r.status_code, 404, (path, r.text))
        r = self.mallory.post(self.base + "/companies", json={"name": "Injected"})
        self.assertEqual(r.status_code, 404)
        r = self.mallory.patch(f"{self.base}/companies/{company['id']}", json={"changes": {"name": "pwned"}})
        self.assertEqual(r.status_code, 404)
        # mallory's own workspace cannot reach alice's records by id either
        own = self.mallory.post("/api/v1/workspaces", json={"name": "Mallory", "seed": False}).json()
        r = self.mallory.get(f"/api/v1/w/{own['id']}/companies/{company['id']}")
        self.assertEqual(r.status_code, 404)

    def test_viewer_cannot_write(self) -> None:
        viewer = self._client(self.client.app, "viewer@example.com")
        self.platform.store.add_member(self.ctx, self.issuer.user_id_for("viewer@example.com"), "viewer")
        self.assertEqual(viewer.get(self.base + "/companies").status_code, 200)
        self.assertEqual(viewer.post(self.base + "/companies", json={"name": "Nope"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
