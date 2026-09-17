"""API rate limits, security headers, structured logs and client-IP trust."""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.middleware import RateLimiter
from cloud.api.settings import Settings
from cloud.shared.logs import JsonFormatter, redact
from cloud.shared.storage import LocalFileStorage
from cloud.worker.dispatcher import NullDispatcher

SECRET = "hardening-tests-development-secret-0123456789"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestRateLimiter(unittest.TestCase):
    def test_bucket_refills(self) -> None:
        clock = Clock()
        limiter = RateLimiter(capacity=2, refill_per_second=1, clock=clock)
        self.assertTrue(limiter.allow("a")[0])
        self.assertTrue(limiter.allow("a")[0])
        allowed, wait = limiter.allow("a")
        self.assertFalse(allowed)
        self.assertAlmostEqual(wait, 1.0)
        self.assertTrue(limiter.allow("b")[0], "keys are independent")
        clock.now += 1
        self.assertTrue(limiter.allow("a")[0])


class ApiCase(unittest.TestCase):
    def app(self, **settings):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        issuer = DevTokenIssuer(SECRET)
        app = create_app(
            Settings(auth_mode="dev", results_dir=Path(scratch.name), **settings),
            dispatcher=NullDispatcher(),
            storage=LocalFileStorage(Path(scratch.name)),
            token_verifier=issuer,
        )
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client, {"Authorization": f"Bearer {issuer.issue('rate@example.com')['access_token']}"}


class TestLimitsThroughTheApi(ApiCase):
    def test_per_ip_limit_returns_429_with_retry_after(self) -> None:
        client, auth = self.app(rate_limit_per_minute=3)
        codes = [client.get("/api/v1/jobs", headers=auth).status_code for _ in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)
        response = client.get("/api/v1/jobs", headers=auth)
        self.assertIn("retry-after", response.headers)
        self.assertEqual(client.get("/api/v1/health").status_code, 200, "health is exempt")

    def test_per_user_job_creation_limit(self) -> None:
        client, auth = self.app(job_create_per_hour=2, max_active_jobs_per_user=100)
        payload = {"type": "single_company", "website": "example.com"}
        codes = [client.post("/api/v1/jobs", json=payload, headers=auth).status_code for _ in range(3)]
        self.assertEqual(codes, [201, 201, 429])

    def test_cf_connecting_ip_is_only_trusted_behind_the_tunnel(self) -> None:
        client, auth = self.app(rate_limit_per_minute=1, trust_proxy="cloudflare")
        # TestClient's peer is "testclient", not loopback: the header must be ignored,
        # so two different claimed IPs still share one bucket.
        self.assertEqual(client.get("/api/v1/jobs", headers={**auth, "CF-Connecting-IP": "1.1.1.1"}).status_code, 200)
        self.assertEqual(client.get("/api/v1/jobs", headers={**auth, "CF-Connecting-IP": "2.2.2.2"}).status_code, 429)


class TestHeadersAndDocs(ApiCase):
    def test_security_headers(self) -> None:
        client, auth = self.app()
        response = client.get("/api/v1/jobs", headers=auth)
        for header, value in (
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            ("referrer-policy", "no-referrer"),
            ("cache-control", "no-store"),
        ):
            self.assertEqual(response.headers.get(header), value)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertNotIn("strict-transport-security", response.headers, "HSTS only when deployed")

    def test_docs_exist_in_development(self) -> None:
        client, _ = self.app()
        self.assertEqual(client.get("/docs").status_code, 200)


class TestStructuredLogs(unittest.TestCase):
    def test_json_lines_with_fields_and_redaction(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter(service="careercloud-api", environment="staging"))
        logger = logging.getLogger("test.structured")
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.warning(
            "connecting to postgresql://postgres:hunter2@db.x.supabase.co/db with Bearer abc.def.ghi",
            extra={"fields": {"job_id": "job_1", "status": 429}},
        )
        record = json.loads(stream.getvalue())
        self.assertEqual((record["service"], record["env"], record["level"]), ("careercloud-api", "staging", "warning"))
        self.assertEqual((record["job_id"], record["status"]), ("job_1", 429))
        self.assertNotIn("hunter2", record["message"])
        self.assertNotIn("abc.def.ghi", record["message"])
        self.assertEqual(redact("rediss://default:pw@h:6379"), "rediss://default:[redacted]@h:6379")


if __name__ == "__main__":
    unittest.main()
