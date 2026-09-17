"""Multi-tenancy through the HTTP API: users see, cancel and download only their own.

Runs against the in-memory store and against PostgreSQL with row-level
security, with two real signed-in users. Another user's job answers exactly like
a job that does not exist (404), so ids cannot be probed.
"""

from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.shared.models import JobStatus
from cloud.shared.repository import InMemoryJobRepository, JobRepository
from cloud.shared.storage import LocalFileStorage
from cloud.tests._pg import PostgresTestCase
from cloud.worker.dispatcher import NullDispatcher
from cloud.worker.executor import JobExecutor
from cloud.worker.fake_runner import FakeRunner
from cloud.worker.results import ResultWriter

SECRET = "ownership-tests-development-secret-0123456789"


class ScopeSpy(JobRepository):
    """Wraps a repository and records any call the API makes in system scope."""

    USER_METHODS = ("get", "list", "count", "count_by_status", "list_targets", "list_events", "list_results", "get_result")

    def __init__(self, inner: JobRepository) -> None:
        self.inner = inner
        self.name = inner.name
        self.system_calls = []

    def _call(self, method, *args, **kwargs):
        if method in self.USER_METHODS + ("add", "add_event", "update_where") and kwargs.get("owner_id") is None:
            self.system_calls.append(method)
        return getattr(self.inner, method)(*args, **kwargs)

    def add(self, *a, **k): return self._call("add", *a, **k)
    def get(self, *a, **k): return self._call("get", *a, **k)
    def list(self, *a, **k): return self._call("list", *a, **k)
    def count(self, *a, **k): return self._call("count", *a, **k)
    def count_by_status(self, *a, **k): return self._call("count_by_status", *a, **k)
    def update_where(self, *a, **k): return self._call("update_where", *a, **k)
    def claim(self, *a, **k): return self.inner.claim(*a, **k)
    def heartbeat(self, *a, **k): return self.inner.heartbeat(*a, **k)
    def find_stale(self, *a, **k): return self.inner.find_stale(*a, **k)
    def find_orphaned(self, *a, **k): return self.inner.find_orphaned(*a, **k)
    def list_targets(self, *a, **k): return self._call("list_targets", *a, **k)
    def update_target(self, *a, **k): return self.inner.update_target(*a, **k)
    def add_event(self, *a, **k): return self._call("add_event", *a, **k)
    def list_events(self, *a, **k): return self._call("list_events", *a, **k)
    def upsert_result(self, *a, **k): return self.inner.upsert_result(*a, **k)
    def list_results(self, *a, **k): return self._call("list_results", *a, **k)
    def get_result(self, *a, **k): return self._call("get_result", *a, **k)


class OwnershipContract:
    repository: JobRepository

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.storage = LocalFileStorage(self.scratch / "results")
        self.issuer = DevTokenIssuer(SECRET)
        self.spy = ScopeSpy(self.repository)
        self.app = create_app(
            Settings(auth_mode="dev", results_dir=self.scratch / "results", max_active_jobs_per_user=50),
            repository=self.spy,
            dispatcher=NullDispatcher(),
            storage=self.storage,
            token_verifier=self.issuer,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.alice = self.headers("alice@example.com")
        self.bob = self.headers("bob@example.com")

    def headers(self, email: str) -> dict:
        return {"Authorization": f"Bearer {self.issuer.issue(email)['access_token']}"}

    def create(self, headers: dict, website: str = "example.com") -> str:
        response = self.client.post("/api/v1/jobs", json={"type": "single_company", "website": website}, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["job_id"]

    def complete_with_results(self, job_id: str) -> None:
        """Do what a worker does: claim, run, store results, finish."""
        service = self.app.state.job_service
        executor = JobExecutor(
            service, FakeRunner(), result_writer=ResultWriter(self.storage), runtime_root=self.scratch / "runtime"
        )
        self.assertIs(executor.execute(job_id).status, JobStatus.COMPLETED)

    # --- authentication -------------------------------------------------------

    def test_anonymous_and_invalid_callers_are_rejected(self) -> None:
        job_id = self.create(self.alice)
        for headers in (
            {},
            {"Authorization": "Bearer "},
            {"Authorization": "Basic YWxpY2U6cGFzcw=="},
            {"Authorization": "Bearer not.a.jwt"},
            {"Authorization": "Bearer " + DevTokenIssuer("some-other-secret-that-is-long-enough-xx").issue("alice@example.com")["access_token"]},
            {"Authorization": "Bearer " + self.issuer.issue("alice@example.com", now=time.time() - 30 * 3600)["access_token"]},
        ):
            for method, path in (
                ("get", "/api/v1/jobs"),
                ("post", "/api/v1/jobs"),
                ("get", f"/api/v1/jobs/{job_id}"),
                ("post", f"/api/v1/jobs/{job_id}/cancel"),
                ("get", f"/api/v1/jobs/{job_id}/results"),
                ("get", "/api/v1/me"),
            ):
                with self.subTest(headers=str(headers)[:40], path=path):
                    if method == "post":
                        response = self.client.post(path, headers=headers, json={"type": "weekly_crawl"})
                    else:
                        response = self.client.get(path, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertIn("Bearer", response.headers.get("www-authenticate", ""))

    def test_health_is_public_and_reveals_no_data(self) -> None:
        self.create(self.alice)
        body = self.client.get("/api/v1/health").json()
        self.assertNotIn("jobs", body)
        self.assertEqual(body["auth"], "dev")

    def test_me(self) -> None:
        body = self.client.get("/api/v1/me", headers=self.alice).json()
        self.assertEqual(body["user_id"], self.issuer.user_id_for("alice@example.com"))
        self.assertEqual(body["email"], "alice@example.com")

    # --- isolation ------------------------------------------------------------

    def test_a_user_cannot_read_another_users_job(self) -> None:
        bobs = self.create(self.bob)
        for path in (
            f"/api/v1/jobs/{bobs}",
            f"/api/v1/jobs/{bobs}/targets",
            f"/api/v1/jobs/{bobs}/events",
            f"/api/v1/jobs/{bobs}/results",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.alice)
                self.assertEqual(response.status_code, 404)
                missing = self.client.get(path.replace(bobs, f"job_{uuid.uuid4().hex}"), headers=self.alice)
                self.assertEqual(response.json(), {"detail": response.json()["detail"]})
                self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/jobs/{bobs}", headers=self.bob).status_code, 200)

    def test_lists_and_counts_contain_only_the_callers_jobs(self) -> None:
        mine = {self.create(self.alice), self.create(self.alice)}
        self.create(self.bob)
        body = self.client.get("/api/v1/jobs", headers=self.alice).json()
        self.assertEqual({job["job_id"] for job in body["jobs"]}, mine)
        self.assertEqual(body["total"], 2)
        self.assertEqual(sum(body["counts"].values()), 2)
        self.assertEqual(self.client.get("/api/v1/jobs", headers=self.bob).json()["total"], 1)

    def test_a_user_cannot_cancel_another_users_job(self) -> None:
        bobs = self.create(self.bob)
        response = self.client.post(f"/api/v1/jobs/{bobs}/cancel", headers=self.alice)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/jobs/{bobs}", headers=self.bob).json()["status"], "queued")
        self.assertEqual(self.client.post(f"/api/v1/jobs/{bobs}/cancel", headers=self.bob).json()["status"], "cancelled")

    def test_downloads_enforce_ownership(self) -> None:
        bobs = self.create(self.bob)
        self.complete_with_results(bobs)
        results = self.client.get(f"/api/v1/jobs/{bobs}/results", headers=self.bob).json()["results"]
        self.assertEqual({r["kind"] for r in results}, {"summary_json", "jobs_csv"})
        csv_result = next(r for r in results if r["kind"] == "jobs_csv")

        own = self.client.get(csv_result["download_url"], headers=self.bob)
        self.assertEqual(own.status_code, 200)
        self.assertTrue(own.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("attachment", own.headers["content-disposition"])
        self.assertEqual(own.headers["x-content-type-options"], "nosniff")
        self.assertEqual(int(own.headers["content-length"]), csv_result["size_bytes"])

        self.assertEqual(self.client.get(csv_result["download_url"], headers=self.alice).status_code, 404)
        self.assertEqual(self.client.get(csv_result["download_url"]).status_code, 401)
        # Alice's own job id with Bob's result id: still not found.
        alices = self.create(self.alice)
        crossed = csv_result["download_url"].replace(bobs, alices)
        self.assertEqual(self.client.get(crossed, headers=self.alice).status_code, 404)

    def test_a_result_whose_file_vanished_is_gone_not_a_crash(self) -> None:
        job_id = self.create(self.alice)
        self.complete_with_results(job_id)
        result = self.client.get(f"/api/v1/jobs/{job_id}/results", headers=self.alice).json()["results"][0]
        stored = self.app.state.job_service.get_result(job_id, result["result_id"])
        self.storage.delete(stored.storage_key)
        self.assertEqual(self.client.get(result["download_url"], headers=self.alice).status_code, 410)

    def test_the_api_never_uses_system_scope(self) -> None:
        job_id = self.create(self.alice)
        for method, path in (
            ("get", "/api/v1/jobs"),
            ("get", f"/api/v1/jobs/{job_id}"),
            ("get", f"/api/v1/jobs/{job_id}/targets"),
            ("get", f"/api/v1/jobs/{job_id}/events"),
            ("get", f"/api/v1/jobs/{job_id}/results"),
            ("post", f"/api/v1/jobs/{job_id}/cancel"),
        ):
            getattr(self.client, method)(path, headers=self.alice)
        self.assertEqual(self.spy.system_calls, [])

    def test_active_job_limit_is_per_user(self) -> None:
        self.app.state.settings = Settings(auth_mode="dev", max_active_jobs_per_user=2)
        self.create(self.alice)
        self.create(self.alice)
        self.assertEqual(
            self.client.post("/api/v1/jobs", json={"type": "single_company", "website": "x.com"}, headers=self.alice).status_code,
            429,
        )
        self.create(self.bob)  # Bob is unaffected

    def test_unsafe_websites_are_rejected_at_the_api(self) -> None:
        for website in ("http://127.0.0.1", "http://169.254.169.254/latest/meta-data", "http://10.1.2.3", "localhost:8080", "file:///etc/passwd", "redis.internal"):
            with self.subTest(website=website):
                response = self.client.post(
                    "/api/v1/jobs", json={"type": "single_company", "website": website}, headers=self.alice
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_unsupported_types_are_recorded_but_say_they_will_not_start(self) -> None:
        self.app.state.runnable_types = frozenset()
        job_id = self.client.post("/api/v1/jobs", json={"type": "weekly_crawl"}, headers=self.alice).json()["job_id"]
        body = self.client.get(f"/api/v1/jobs/{job_id}", headers=self.alice).json()
        self.assertEqual(body["status"], "queued")
        self.assertFalse(body["runnable"])
        self.assertEqual(body["progress"]["current_phase"], "unsupported")
        self.assertIn("will not start", body["progress"]["message"])


class TestOwnershipInMemory(OwnershipContract, unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryJobRepository()
        super().setUp()


class TestOwnershipPostgres(OwnershipContract, PostgresTestCase):
    def setUp(self) -> None:
        self.truncate()
        self.repository = self.pg_repository
        super().setUp()


class TestUnconfiguredAuth(unittest.TestCase):
    def test_job_endpoints_fail_closed_when_auth_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            app = create_app(
                Settings(results_dir=Path(scratch)), dispatcher=NullDispatcher(), storage=LocalFileStorage(Path(scratch))
            )
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/jobs").status_code, 503)
                self.assertEqual(client.get("/api/v1/health").json()["auth"], "unconfigured")

    def test_dev_session_endpoint_exists_only_in_dev_mode(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            app = create_app(
                Settings(results_dir=Path(scratch)), dispatcher=NullDispatcher(), storage=LocalFileStorage(Path(scratch))
            )
            with TestClient(app) as client:
                self.assertEqual(
                    client.post("/api/v1/auth/dev-session", json={"email": "a@example.com"}).status_code, 404
                )


if __name__ == "__main__":
    unittest.main()
