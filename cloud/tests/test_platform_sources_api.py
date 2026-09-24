"""The sources/providers/credits/email/contacts API end to end (TestClient, no network)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.intel.core.context import Ctx
from cloud.intel.email.providers import LocalValidator
from cloud.intel.email.service import EmailValidationService
from cloud.shared.storage import LocalFileStorage
from cloud.worker.dispatcher import NullDispatcher
from cloud.tests.test_platform_sources_support import make_platform

SECRET = "api-tests-secret-0123456789abcdef-sources"
KEY = "seamless-api-key-TOPSECRET-4321"


class SourcesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.platform, _, _, _ = make_platform()
        self.platform.override("email", EmailValidationService(
            self.platform, local=LocalValidator(resolver=lambda d: True)))
        self.issuer = DevTokenIssuer(SECRET)
        app = create_app(Settings(auth_mode="dev", results_dir=Path(scratch.name) / "r"),
                         storage=LocalFileStorage(Path(scratch.name) / "r"), token_verifier=self.issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = self._client(app, "owner@example.com")
        self.stranger = self._client(app, "stranger@example.com")
        response = self.client.post("/api/v1/workspaces", json={"name": "Acme", "slug": "acme-api", "seed": False})
        self.assertEqual(response.status_code, 201, response.text)
        self.base = f"/api/v1/w/{response.json()['id']}"

    def _client(self, app, email):
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {self.issuer.issue(email)['access_token']}"
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_sources_list(self) -> None:
        items = {s["name"]: s for s in self.client.get(f"{self.base}/sources").json()["items"]}
        self.assertEqual(items["ats_public"]["health"]["status"], "ok")
        self.assertEqual(items["indeed"]["health"]["status"], "not_configured")

    def test_credentials_are_write_only_and_private(self) -> None:
        response = self.client.post(f"{self.base}/providers/seamless/credentials", json={"secrets": {"api_key": KEY}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(KEY, response.text)
        listing = self.client.get(f"{self.base}/providers").text
        self.assertNotIn(KEY, listing)
        self.assertIn("…4321", listing)
        self.assertNotIn(KEY, self.client.get(f"{self.base}/audit").text)
        self.assertEqual(self.stranger.get(f"{self.base}/providers").status_code, 404)
        self.assertEqual(self.stranger.post(f"{self.base}/providers/seamless/credentials",
                                            json={"secrets": {"api_key": "x" * 20}}).status_code, 404)

    def test_credits_endpoints(self) -> None:
        response = self.client.post(f"{self.base}/credits/seamless/sync", json={"remaining": 25, "source": "dashboard"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["remaining"], 25)
        balances = self.client.get(f"{self.base}/credits").json()["items"]
        self.assertEqual(balances[0]["provider"], "seamless")
        self.assertEqual(self.client.get(f"{self.base}/credits/ledger").json()["total"], 1)

    def test_email_validation_sync_and_task(self) -> None:
        response = self.client.post(f"{self.base}/email/validate", json={"emails": ["info@acme.com", "x@yopmail.com"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r["status"] for r in response.json()["items"]], ["ROLE", "DISPOSABLE"])
        many = [f"user{i}@acme.com" for i in range(30)]
        response = self.client.post(f"{self.base}/email/validate", json={"emails": many})
        self.assertIn("task_id", response.json())
        self.assertEqual(self.client.get(f"{self.base}/email/validations").json()["total"], 2)

    def test_contact_gaps_and_plan(self) -> None:
        company = self.platform.store.insert(
            Ctx.for_system(self.base.rsplit("/", 1)[1]),
            "companies", {"name": "Acme", "website": "https://acme.com", "domain": "acme.com"})
        gaps = self.client.get(f"{self.base}/companies/{company['id']}/contact-gaps?functions=hr,it").json()
        self.assertEqual(gaps["summary"]["MISSING"], 2)
        plan = self.client.get(f"{self.base}/enrichment/plan?company_ids={company['id']}&needs=contacts").json()
        self.assertEqual(plan["paid_steps"], 0)
        response = self.client.post(f"{self.base}/contacts/find", json={"company_ids": [company["id"]]})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertIn("task_id", response.json())

    def test_source_search_becomes_a_task_and_is_idempotent(self) -> None:
        body = {"query": {"board_url": "https://boards.greenhouse.io/acme"}}
        first = self.client.post(f"{self.base}/sources/ats_public/search", json=body,
                                 headers={"Idempotency-Key": "k-1"})
        second = self.client.post(f"{self.base}/sources/ats_public/search", json=body,
                                  headers={"Idempotency-Key": "k-1"})
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["task_id"], second.json()["task_id"])


if __name__ == "__main__":
    unittest.main()
