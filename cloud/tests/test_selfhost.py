"""Self-hosted SANA GTM (deploy/selfhost): the production profile, the container helper
(cloud.ops.selfhost) and a production-mode API boot on a real, freshly migrated PostgreSQL."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cloud.api.deployment import deployment_problems, is_private_host
from cloud.api.settings import load_settings

SUPABASE = "https://abcdefghijklmnop.supabase.co"


def selfhosted_env(**over: str) -> dict:
    env = {
        "CAREERCLOUD_ENV": "production", "CAREERCLOUD_SELF_HOSTED": "1",
        "CAREERCLOUD_STORAGE": "postgres", "CAREERCLOUD_QUEUE": "postgres",
        "CAREERCLOUD_DATABASE_URL": "postgresql://sanagtm:pw@postgres:5432/sanagtm",
        "CAREERCLOUD_AUTH_MODE": "supabase", "CAREERCLOUD_SUPABASE_URL": SUPABASE,
        "CAREERCLOUD_API_ORIGIN": "https://sanagtm.example.com",
        "CAREERCLOUD_CORS_ORIGINS": "https://sanagtm.example.com",
        "CAREERCLOUD_TRUST_PROXY": "cloudflare", "CAREERCLOUD_TRUSTED_PROXIES": "172.29.40.10/32",
        "CAREERCLOUD_LOG_FORMAT": "json", "CAREERCLOUD_STORAGE_BACKEND": "local",
        "CAREERCLOUD_RESULTS_DIR": str(Path(tempfile.gettempdir()).resolve() / "sanagtm-results"),
        "CAREERCLOUD_RESOURCE_REGISTRY": "/srv/sanagtm/run/resources.json",
    }
    env.update(over)
    return env


class ProfileTests(unittest.TestCase):
    def test_a_complete_self_hosted_production_config_has_no_problems(self) -> None:
        settings = load_settings(selfhosted_env())
        self.assertEqual((settings.self_hosted, settings.queue, settings.storage_backend), (True, "postgres", "local"))
        self.assertEqual(settings.trusted_proxies, ("172.29.40.10/32",))
        self.assertEqual(deployment_problems(settings), [])

    def test_without_the_profile_production_still_demands_s3_and_tls(self) -> None:
        problems = deployment_problems(load_settings(selfhosted_env(CAREERCLOUD_SELF_HOSTED="0")))
        self.assertTrue(any("sslmode" in p for p in problems))
        self.assertTrue(any("STORAGE_BACKEND=s3" in p for p in problems))

    def test_the_profile_never_relaxes_tls_for_a_public_database(self) -> None:
        problems = deployment_problems(load_settings(selfhosted_env(
            CAREERCLOUD_DATABASE_URL="postgresql://u:p@aws-0-ap-south-1.pooler.supabase.com:5432/postgres")))
        self.assertTrue(any("sslmode" in p for p in problems))

    def test_every_other_production_rule_still_applies(self) -> None:
        for over, expected in (({"CAREERCLOUD_LOG_FORMAT": "text"}, "LOG_FORMAT=json"),
                               ({"CAREERCLOUD_API_ORIGIN": "http://sanagtm.example.com"}, "API_ORIGIN"),
                               ({"CAREERCLOUD_RESOURCE_REGISTRY": ""}, "RESOURCE_REGISTRY")):
            with self.subTest(over=over):
                self.assertTrue(any(expected in p for p in deployment_problems(load_settings(selfhosted_env(**over)))))
        with self.assertRaises(ValueError):
            load_settings(selfhosted_env(CAREERCLOUD_AUTH_MODE="dev", CAREERCLOUD_DEV_JWT_SECRET="x" * 40))

    def test_private_hosts(self) -> None:
        for host in ("postgres", "localhost", "127.0.0.1", "10.0.0.5", "172.29.40.2", "192.168.1.9", "::1"):
            self.assertTrue(is_private_host(host), host)
        for host in (None, "", "db.example.com", "aws-0-ap-south-1.pooler.supabase.com", "8.8.8.8"):
            self.assertFalse(is_private_host(host), host)

    def test_trusted_proxies_must_be_networks(self) -> None:
        with self.assertRaisesRegex(ValueError, "TRUSTED_PROXIES"):
            load_settings({"CAREERCLOUD_TRUSTED_PROXIES": "web-container"})


class ClientIpTests(unittest.TestCase):
    def request(self, peer: str, header: str = "203.0.113.7"):
        from starlette.requests import Request

        return Request({"type": "http", "client": (peer, 1234), "headers": [(b"cf-connecting-ip", header.encode())]})

    def test_the_web_container_is_believed_only_when_listed(self) -> None:
        from cloud.api.middleware import client_ip

        self.assertEqual(client_ip(self.request("172.29.40.10"), "cloudflare", ("172.29.40.10/32",)), "203.0.113.7")
        self.assertEqual(client_ip(self.request("172.29.40.11"), "cloudflare", ("172.29.40.10/32",)), "172.29.40.11")
        self.assertEqual(client_ip(self.request("172.29.40.10"), "none", ("172.29.40.10/32",)), "172.29.40.10")
        self.assertEqual(client_ip(self.request("127.0.0.1"), "cloudflare"), "203.0.113.7")


class RegistryTests(unittest.TestCase):
    def test_document_lists_this_install_and_the_legacy_database(self) -> None:
        from cloud.ops.selfhost import registry_document

        doc = registry_document(selfhosted_env(SANAGTM_LEGACY_DATABASE_HOSTS="aws-0-ap-south-1.pooler.supabase.com"))
        own = doc["production"]
        self.assertEqual(own["database_hosts"], ["postgres"])
        self.assertEqual(own["supabase_refs"], ["abcdefghijklmnop"])
        self.assertEqual(own["api_origins"], ["https://sanagtm.example.com"])
        self.assertEqual(doc["legacy-hosted"]["database_hosts"], ["aws-0-ap-south-1.pooler.supabase.com"])

    def test_the_legacy_database_is_refused(self) -> None:
        from cloud.ops.selfhost import registry_document
        from cloud.shared.environment import ResourceIdentity, ResourceRegistry, check_identity

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(registry_document(selfhosted_env(
                SANAGTM_LEGACY_DATABASE_HOSTS="aws-0-ap-south-1.pooler.supabase.com"))), encoding="utf-8")
            registry = ResourceRegistry.load(path)
        ok = ResourceIdentity.from_urls("production", database_url="postgresql://u@postgres/sanagtm",
                                        supabase_url=SUPABASE, api_origin="https://sanagtm.example.com",
                                        dashboard_origins=("https://sanagtm.example.com",))
        self.assertEqual(check_identity(ok, registry), [])
        legacy = ResourceIdentity.from_urls("production",
                                            database_url="postgresql://u@aws-0-ap-south-1.pooler.supabase.com/x")
        self.assertTrue(any("belongs to legacy-hosted" in p for p in check_identity(legacy, registry)))


class WorkerHealthTests(unittest.TestCase):
    def test_fresh_state_and_database_are_healthy(self) -> None:
        from cloud.ops import selfhost

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "worker-state.json"
            state.write_text(json.dumps({"state": "ok", "updated_at": datetime.now(timezone.utc).isoformat()}))
            env = {"CAREERCLOUD_WORKER_STATE_FILE": str(state), "CAREERCLOUD_DATABASE_URL": "postgresql://x/y"}
            connect = mock.MagicMock()
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (1,)
            with mock.patch("psycopg.connect", connect):
                self.assertTrue(selfhost.health_worker(env)["ok"])
                self.assertFalse(selfhost.health_worker(env, now=time.time() + 600)["ok"])     # stale
            env.pop("CAREERCLOUD_WORKER_STATE_FILE")
            with mock.patch("psycopg.connect", connect):
                self.assertFalse(selfhost.health_worker(env)["ok"])                           # no state


class PostgresSelfHostTests(unittest.TestCase):
    """prepare() and a production-mode API boot on a real PostgreSQL (embedded, or
    CAREERCLOUD_TEST_DATABASE_URL)."""

    def setUp(self) -> None:
        from cloud.tests._pg import drop_database, fresh_database

        self.url = fresh_database(migrate=False)
        self.addCleanup(drop_database, self.url)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.registry = self.root / "run" / "resources.json"

    def env(self, **over: str) -> dict:
        return selfhosted_env(CAREERCLOUD_DATABASE_URL=self.url, CAREERCLOUD_RESOURCE_REGISTRY=str(self.registry),
                              CAREERCLOUD_RESULTS_DIR=str(self.root / "results"), SANAGTM_DB_WAIT_SECONDS="10",
                              **over)

    def test_prepare_migrates_stamps_a_fresh_database_and_is_idempotent(self) -> None:
        from cloud.ops import selfhost
        from cloud.ops.stamps import read_database_stamp

        first = selfhost.prepare(self.env())
        self.assertTrue(first["fresh"])
        self.assertGreaterEqual(len(first["applied"]), 13)
        self.assertEqual(read_database_stamp(self.url), "production")
        self.assertTrue(self.registry.exists())
        second = selfhost.prepare(self.env())
        self.assertEqual((second["applied"], second["stamp"]), ([], "production"))
        with self.assertRaisesRegex(selfhost.SelfHostError, "stamped 'production'"):
            selfhost.prepare(self.env(CAREERCLOUD_ENV="staging"))

    def test_a_restored_unstamped_database_needs_an_explicit_stamp(self) -> None:
        import psycopg

        from cloud.db.migrate import apply_migrations, load_migrations
        from cloud.intel.store.postgres import PostgresStore
        from cloud.ops import selfhost

        migrations = load_migrations()
        apply_migrations(self.url, migrations[:-2])    # a restored dump from an OLDER release
        store = PostgresStore.from_url(self.url, max_size=1)
        try:
            store.create_workspace(str(uuid.uuid4()), "Restored", f"r-{uuid.uuid4().hex[:6]}")
        finally:
            store.close()
        count = lambda: psycopg.connect(self.url).execute(  # noqa: E731
            "select count(*) from careercloud.schema_migrations").fetchone()[0]
        with self.assertRaisesRegex(selfhost.SelfHostError, "no environment stamp"):
            selfhost.prepare(self.env())
        self.assertEqual(count(), len(migrations) - 2)                   # refused BEFORE migrating anything
        self.assertEqual(selfhost.stamp_restored(self.env()), "production")
        result = selfhost.prepare(self.env())
        self.assertEqual((result["stamp"], result["applied"]),
                         ("production", [m.version for m in migrations[-2:]]))

    def test_the_api_boots_in_self_hosted_production_mode(self) -> None:
        from fastapi.testclient import TestClient

        from cloud.api.main import create_app
        from cloud.ops import selfhost

        env = self.env()
        selfhost.prepare(env)
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict("sys.modules", {"redis": None}):
            app = create_app(load_settings(env))
            with TestClient(app, base_url="https://sanagtm.example.com") as client:
                health = client.get("/api/v1/health")
                self.assertEqual(health.status_code, 200, health.text)
                self.assertIn("Strict-Transport-Security", health.headers)
                self.assertEqual(client.get("/api/v1/me").status_code, 401)        # Supabase auth enforced
                status = client.get("/api/v1/status")
                self.assertIn(status.status_code, (200, 401))
        self.assertTrue((self.root / "results" / "meta" / "environment").exists())   # storage stamped

    def test_provider_credentials_move_only_with_the_same_secrets_key(self) -> None:
        from cryptography.fernet import Fernet

        from cloud.intel.core.context import Ctx
        from cloud.intel.platform import Platform, PlatformConfig
        from cloud.intel.providers.registry import SecretsUnavailable
        from cloud.intel.store.postgres import PostgresStore
        from cloud.ops import selfhost

        selfhost.prepare(self.env())
        key, other = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        user = str(uuid.uuid4())
        store = PostgresStore.from_url(self.url, max_size=2)
        self.addCleanup(store.close)
        ws = store.create_workspace(user, "Keys", f"k-{uuid.uuid4().hex[:6]}")
        ctx = Ctx(ws["id"], user, "owner")
        Platform(store, config=PlatformConfig(secrets_key=key)).service("providers").set_credentials(
            ctx, "zoominfo", {"client_id": "cid-12345678", "client_secret": "s3cret-value-0123"})
        # a different process on the (restored) database, configured with the COPIED key
        fresh = PostgresStore.from_url(self.url, max_size=2)
        self.addCleanup(fresh.close)
        same = Platform(fresh, config=PlatformConfig(secrets_key=key)).service("providers")
        self.assertEqual(same.get_secrets(ctx, "zoominfo")["client_secret"], "s3cret-value-0123")
        # a newly generated key cannot read them, and says so
        with self.assertRaisesRegex(SecretsUnavailable, "cannot be decrypted"):
            Platform(fresh, config=PlatformConfig(secrets_key=other)).service("providers").get_secrets(ctx, "zoominfo")

    def test_the_api_refuses_a_database_stamped_for_another_environment(self) -> None:
        from cloud.api.main import create_app
        from cloud.ops import selfhost
        from cloud.shared.environment import EnvironmentIsolationError

        selfhost.prepare(self.env(CAREERCLOUD_ENV="staging", CAREERCLOUD_API_ORIGIN="https://sanagtm.example.com"))
        env = self.env()
        selfhost.write_registry(env)
        with mock.patch.dict(os.environ, env, clear=False), self.assertRaises(EnvironmentIsolationError):
            create_app(load_settings(env))


if __name__ == "__main__":
    unittest.main()
