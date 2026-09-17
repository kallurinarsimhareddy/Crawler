"""The HTTP API, end to end through FastAPI's TestClient. No network, no disk."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.worker.dispatcher import InlineDispatcher, NullDispatcher
from cloud.worker.fake_runner import FakeRunner


class _ApiTest(unittest.TestCase):
    """Jobs stay queued unless a test asks for a runner, so assertions are stable."""

    def make_client(self, **create_app_kwargs) -> TestClient:
        create_app_kwargs.setdefault("dispatcher", NullDispatcher())
        self.app = create_app(Settings(fake_step_seconds=0), **create_app_kwargs)
        client = TestClient(self.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def setUp(self) -> None:
        self.client = self.make_client()

    def create(self, payload: dict) -> str:
        response = self.client.post("/api/v1/jobs", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["job_id"]


class TestHealth(_ApiTest):
    def test_health(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["storage"], "memory")
        self.assertEqual(body["runner"], "none")

    def test_openapi_documents_every_endpoint(self) -> None:
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(
            set(paths),
            {
                "/api/v1/health",
                "/api/v1/jobs",
                "/api/v1/jobs/{job_id}",
                "/api/v1/jobs/{job_id}/cancel",
            },
        )


class TestCreateJob(_ApiTest):
    def test_the_documented_request_and_response(self) -> None:
        response = self.client.post(
            "/api/v1/jobs", json={"type": "single_company", "website": "https://example.com"}
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(set(body), {"job_id", "status"})
        self.assertEqual(body["status"], "queued")
        self.assertTrue(body["job_id"].startswith("job_"))

    def test_every_job_type_can_be_created(self) -> None:
        for payload in (
            {"type": "single_company", "website": "example.com"},
            {"type": "bulk_companies", "companies": [{"website": "a.com"}, {"company_name": "Bee"}]},
            {"type": "weekly_crawl"},
            {"type": "discovery", "company_name": "Acme"},
        ):
            with self.subTest(type=payload["type"]):
                job_id = self.create(payload)
                self.assertEqual(self.client.get(f"/api/v1/jobs/{job_id}").json()["type"], payload["type"])

    def test_invalid_requests_are_422_and_create_nothing(self) -> None:
        for payload in (
            {},
            {"type": "single_company"},
            {"type": "single_company", "website": "ftp://example.com"},
            {"type": "single_company", "website": "example.com", "priority": "high"},
            {"type": "weekly_crawl", "website": "example.com"},
            {"type": "bulk_companies", "companies": []},
            {"type": "teleport", "website": "example.com"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/api/v1/jobs", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/v1/jobs").json()["total"], 0)

    def test_a_non_json_body_is_422(self) -> None:
        response = self.client.post(
            "/api/v1/jobs", content="website=example.com", headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(response.status_code, 422)


class TestReadJobs(_ApiTest):
    def test_get_one_job(self) -> None:
        job_id = self.create({"type": "single_company", "website": "Example.com/"})
        body = self.client.get(f"/api/v1/jobs/{job_id}").json()
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["target"], "example.com")
        self.assertEqual(body["targets"], [{"website": "https://example.com", "company_name": None}])
        self.assertIsNone(body["completed_at"])
        self.assertIsNone(body["error"])
        self.assertEqual(body["progress"], {"completed": 0, "total": None, "message": None})

    def test_an_unknown_job_is_404(self) -> None:
        response = self.client.get("/api/v1/jobs/job_does_not_exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json()["detail"])

    def test_list_newest_first_with_counts(self) -> None:
        first = self.create({"type": "weekly_crawl"})
        second = self.create({"type": "discovery", "company_name": "Acme"})
        body = self.client.get("/api/v1/jobs").json()
        self.assertEqual([job["job_id"] for job in body["jobs"]], [second, first])
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            body["counts"],
            {"queued": 2, "running": 0, "completed": 0, "failed": 0, "cancelled": 0},
        )

    def test_list_filters_and_pages(self) -> None:
        ids = [self.create({"type": "weekly_crawl"}) for _ in range(3)]
        self.client.post(f"/api/v1/jobs/{ids[0]}/cancel")

        cancelled = self.client.get("/api/v1/jobs", params={"status": "cancelled"}).json()
        self.assertEqual([job["job_id"] for job in cancelled["jobs"]], [ids[0]])
        self.assertEqual(cancelled["total"], 1)
        self.assertEqual(cancelled["counts"]["queued"], 2)  # counts ignore the filter

        page = self.client.get("/api/v1/jobs", params={"limit": 1, "offset": 1}).json()
        self.assertEqual([job["job_id"] for job in page["jobs"]], [ids[1]])
        self.assertEqual(page["total"], 3)

    def test_bad_list_parameters_are_422(self) -> None:
        for params in ({"status": "sleeping"}, {"limit": 0}, {"limit": 201}, {"offset": -1}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/api/v1/jobs", params=params).status_code, 422)


class TestCancel(_ApiTest):
    def test_cancel_a_queued_job(self) -> None:
        job_id = self.create({"type": "weekly_crawl"})
        response = self.client.post(f"/api/v1/jobs/{job_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertIsNotNone(response.json()["completed_at"])

    def test_cancelling_twice_is_409(self) -> None:
        job_id = self.create({"type": "weekly_crawl"})
        self.client.post(f"/api/v1/jobs/{job_id}/cancel")
        self.assertEqual(self.client.post(f"/api/v1/jobs/{job_id}/cancel").status_code, 409)

    def test_cancelling_an_unknown_job_is_404(self) -> None:
        self.assertEqual(self.client.post("/api/v1/jobs/job_nope/cancel").status_code, 404)


class TestWithTheFakeRunner(_ApiTest):
    def setUp(self) -> None:
        self.client = self.make_client(dispatcher=None, runner=FakeRunner())

    def test_a_created_job_runs_to_completion(self) -> None:
        self.assertIsInstance(self.app.state.dispatcher, InlineDispatcher)
        self.assertEqual(self.client.get("/api/v1/health").json()["runner"], "fake")

        job_id = self.create({"type": "bulk_companies", "companies": [{"website": "a.com"}, {"website": "b.com"}]})
        self.assertTrue(self.app.state.dispatcher.wait_idle(timeout=10))

        body = self.client.get(f"/api/v1/jobs/{job_id}").json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["progress"]["completed"], 2)
        self.assertEqual(body["progress"]["total"], 2)
        self.assertIsNotNone(body["started_at"])
        self.assertIsNotNone(body["completed_at"])

    def test_a_failing_run_is_reported_with_its_error(self) -> None:
        self.client = self.make_client(dispatcher=None, runner=FakeRunner(fail_with="simulated 403"))
        job_id = self.create({"type": "single_company", "website": "example.com"})
        self.assertTrue(self.app.state.dispatcher.wait_idle(timeout=10))
        body = self.client.get(f"/api/v1/jobs/{job_id}").json()
        self.assertEqual((body["status"], body["error"]), ("failed", "simulated 403"))
        self.assertEqual(self.client.get("/api/v1/jobs").json()["counts"]["failed"], 1)


class TestCors(_ApiTest):
    def test_the_dev_frontend_origin_is_allowed_and_others_are_not(self) -> None:
        allowed = self.client.options(
            "/api/v1/jobs",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "http://localhost:5173")
        refused = self.client.options(
            "/api/v1/jobs",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        self.assertNotEqual(refused.headers.get("access-control-allow-origin"), "https://evil.example")


if __name__ == "__main__":
    unittest.main()
