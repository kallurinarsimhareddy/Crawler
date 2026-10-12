"""cloud/ops/webapp.py: one origin for the Windows install (dashboard + API in one process)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from cloud.ops.webapp import WebApp, runtime_config, security_headers

ENV = {"SANAGTM_PUBLIC_SUPABASE_URL": "https://abcdefghijklmnop.supabase.co",
       "SANAGTM_PUBLIC_SUPABASE_ANON_KEY": "public-anon-key-0123456789abcdef"}


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        from cloud.api.main import create_app
        from cloud.api.settings import Settings

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.dist = root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<html>SANA GTM</html>", encoding="utf-8")
        (self.dist / "assets" / "app-1234.js").write_text("console.log(1)", encoding="utf-8")
        (root / "secret.txt").write_text("do not serve", encoding="utf-8")
        api = create_app(Settings(auth_mode="dev", dev_jwt_secret="webapp-test-secret-0123456789abcdef",
                                  results_dir=root / "results"))
        self.client = TestClient(WebApp(self.dist, api, ENV))
        self.client.__enter__()                       # lifespan reaches the API
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_the_dashboard_is_served_with_browser_security_headers(self) -> None:
        r = self.client.get("/")
        self.assertEqual((r.status_code, r.text), (200, "<html>SANA GTM</html>"))
        csp = r.headers["content-security-policy"]
        self.assertIn("connect-src 'self' https://abcdefghijklmnop.supabase.co wss://abcdefghijklmnop.supabase.co", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertIn("max-age", r.headers["strict-transport-security"])
        self.assertEqual(r.headers["cache-control"], "no-cache")

    def test_spa_routes_assets_and_missing_files(self) -> None:
        self.assertEqual(self.client.get("/jobs/123").text, "<html>SANA GTM</html>")
        asset = self.client.get("/assets/app-1234.js")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("immutable", asset.headers["cache-control"])
        self.assertEqual(self.client.get("/assets/missing.js").status_code, 404)
        self.assertEqual(self.client.post("/").status_code, 405)

    def test_nothing_outside_the_build_is_reachable(self) -> None:
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt", "/..%5csecret.txt"):
            r = self.client.get(path)
            self.assertNotIn("do not serve", r.text, path)

    def test_runtime_config_carries_public_values_only(self) -> None:
        r = self.client.get("/runtime-config.js")
        self.assertEqual(r.headers["cache-control"], "no-store")
        config = json.loads(r.text.split("=", 1)[1].strip().rstrip(";"))
        self.assertEqual(config, {"VITE_API_URL": "", "VITE_AUTH_MODE": "supabase", "VITE_DEPLOY_ENV": "production",
                                  "VITE_SUPABASE_ANON_KEY": ENV["SANAGTM_PUBLIC_SUPABASE_ANON_KEY"],
                                  "VITE_SUPABASE_URL": ENV["SANAGTM_PUBLIC_SUPABASE_URL"]})
        for secret_key in ("sb_secret_abcdef", "eyJ...service_role..."):
            with self.subTest(key=secret_key), self.assertRaises(ValueError):
                runtime_config({**ENV, "SANAGTM_PUBLIC_SUPABASE_ANON_KEY": secret_key})

    def test_the_api_is_served_in_process_on_the_same_origin(self) -> None:
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(r.headers["content-security-policy"], "default-src 'none'; frame-ancestors 'none'")
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_healthz_needs_no_database(self) -> None:
        self.assertEqual(self.client.get("/healthz").text, "ok\n")

    def test_without_an_auth_url_the_csp_stays_self_only(self) -> None:
        self.assertIn("connect-src 'self';", security_headers({})["Content-Security-Policy"])

    def test_a_missing_build_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            WebApp(self.dist / "nope", None, ENV)


if __name__ == "__main__":
    unittest.main()
