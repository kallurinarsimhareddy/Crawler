"""Staging must never reach production, and nothing deployed may reach crawler state.

Covers the resource registry allowlist, the forbidden-configuration scan, the
database/Redis/storage environment stamps, and the deployment preflight of the
API and worker.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cloud.api.deployment import deployment_problems
from cloud.api.settings import Settings
from cloud.ops.stamps import (
    StampMismatchError,
    check_database_stamp,
    ensure_redis_stamp,
    ensure_storage_stamp,
    read_database_stamp,
    stamp_database,
)
from cloud.shared.environment import (
    EnvironmentIsolationError,
    ResourceIdentity,
    ResourceRegistry,
    check_forbidden_configuration,
    check_identity,
    enforce_isolation,
    supabase_ref_from_host,
)
from cloud.shared.storage import LocalFileStorage
from cloud.tests._pg import PostgresTestCase, drop_database, fresh_database
from cloud.worker.settings import WorkerSettings, worker_deployment_problems

REGISTRY = {
    "_comment": "test registry",
    "staging": {
        "supabase_refs": ["stagingref0001"],
        "database_hosts": ["aws-0-eu-west-1.pooler.supabase.com", "db.stagingref0001.supabase.co"],
        "redis_hosts": ["careercloud-stg.upstash.io"],
        "storage_endpoint_hosts": ["stagingref0001.storage.supabase.co"],
        "storage_buckets": ["careercloud-staging-results"],
        "api_origins": ["https://api-staging.example.com"],
        "dashboard_origins": ["https://careercloud-staging.pages.dev"],
    },
    "production": {
        "supabase_refs": ["liveref000009"],
        "database_hosts": ["db.liveref000009.supabase.co"],
        "redis_hosts": ["careercloud-live.upstash.io"],
        "storage_endpoint_hosts": ["liveref000009.storage.supabase.co"],
        "storage_buckets": ["careercloud-production-results"],
        "api_origins": ["https://api.example.com"],
        "dashboard_origins": ["https://app.example.com"],
    },
}

STAGING_URLS = dict(
    database_url="postgresql://postgres.stagingref0001:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?sslmode=require",
    supabase_url="https://stagingref0001.supabase.co",
    redis_url="rediss://default:pw@careercloud-stg.upstash.io:6379",
    queue_prefix="careercloud:staging",
    storage_bucket="careercloud-staging-results",
    storage_endpoint="https://stagingref0001.storage.supabase.co/storage/v1/s3",
    results_namespace="staging",
    api_origin="https://api-staging.example.com",
    dashboard_origins=["https://careercloud-staging.pages.dev"],
)


def registry() -> ResourceRegistry:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "resources.json"
        path.write_text(json.dumps(REGISTRY), encoding="utf-8")
        return ResourceRegistry.load(path)


class TestResourceRegistry(unittest.TestCase):
    def test_a_correct_staging_configuration_passes(self) -> None:
        identity = ResourceIdentity.from_urls("staging", **STAGING_URLS)
        self.assertEqual(identity.supabase_ref, "stagingref0001")
        self.assertEqual(check_identity(identity, registry()), [])

    def test_each_production_resource_is_refused_in_staging(self) -> None:
        swaps = {
            "database_url": "postgresql://postgres:pw@db.liveref000009.supabase.co:5432/postgres?sslmode=require",
            "supabase_url": "https://liveref000009.supabase.co",
            "redis_url": "rediss://default:pw@careercloud-live.upstash.io:6379",
            "storage_bucket": "careercloud-production-results",
            "storage_endpoint": "https://liveref000009.storage.supabase.co/storage/v1/s3",
            "api_origin": "https://api.example.com",
            "dashboard_origins": ["https://app.example.com"],
        }
        for key, value in swaps.items():
            with self.subTest(resource=key):
                identity = ResourceIdentity.from_urls("staging", **{**STAGING_URLS, key: value})
                problems = check_identity(identity, registry())
                self.assertTrue(problems, f"{key} pointing at production was accepted")

    def test_a_resource_listed_under_both_environments_is_still_refused(self) -> None:
        data = json.loads(json.dumps(REGISTRY))
        data["staging"]["redis_hosts"].append("careercloud-live.upstash.io")  # copy-paste mistake
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "r.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            identity = ResourceIdentity.from_urls("staging", **{**STAGING_URLS, "redis_url": "rediss://default:pw@careercloud-live.upstash.io:6379"})
            problems = check_identity(identity, ResourceRegistry.load(path))
        self.assertTrue(any("belongs to production" in p for p in problems))

    def test_unregistered_resources_are_refused(self) -> None:
        identity = ResourceIdentity.from_urls("staging", **{**STAGING_URLS, "redis_url": "rediss://default:pw@someone-else.upstash.io:6379"})
        self.assertTrue(any("not registered for staging" in p for p in check_identity(identity, registry())))

    def test_naming_rules(self) -> None:
        for key, value in (
            ("queue_prefix", "careercloud:production"),
            ("queue_prefix", "careercloud"),
            ("results_namespace", "results"),
            ("storage_bucket", "careercloud-results"),
        ):
            with self.subTest(key=key, value=value):
                identity = ResourceIdentity.from_urls("staging", **{**STAGING_URLS, key: value})
                self.assertTrue(check_identity(identity, registry()))

    def test_production_refuses_staging_looking_resources(self) -> None:
        identity = ResourceIdentity.from_urls(
            "production",
            database_url="postgresql://postgres:pw@db.liveref000009.supabase.co:5432/postgres",
            queue_prefix="careercloud:production",
            storage_bucket="careercloud-staging-results",
            results_namespace="production",
        )
        problems = check_identity(identity, registry())
        self.assertTrue(any("staging" in p for p in problems))

    def test_a_deployed_environment_without_a_registry_is_refused(self) -> None:
        with self.assertRaises(EnvironmentIsolationError):
            enforce_isolation(ResourceIdentity.from_urls("staging", **STAGING_URLS), env={})
        # development needs no registry
        enforce_isolation(ResourceIdentity.from_urls("development"), env={})

    def test_supabase_refs_are_extracted(self) -> None:
        for host in ("abcdefgh12.supabase.co", "db.abcdefgh12.supabase.co", "abcdefgh12.storage.supabase.co"):
            self.assertEqual(supabase_ref_from_host(host), "abcdefgh12")
        self.assertIsNone(supabase_ref_from_host("example.com"))


class TestForbiddenConfiguration(unittest.TestCase):
    def test_google_sheets_configuration_is_refused(self) -> None:
        for name in ("CAREERCRAWLER_SPREADSHEET_ID", "GOOGLE_APPLICATION_CREDENTIALS", "CAREERCLOUD_SHEETS_ID"):
            with self.subTest(name=name):
                self.assertTrue(check_forbidden_configuration({name: "x"}))

    def test_sqlite_and_production_paths_are_refused(self) -> None:
        for value in (
            "sqlite:///state/crawler.db",
            r"E:\Crawlers\CareerCrawler\state\crawler.db",
            "/srv/careercrawler/state/queue.sqlite",
            r"E:\Crawlers\CareerCrawler-seamless\output",
            r"E:\Crawlers\CareerCrawler\output",
            "/opt/crawler/secrets/service_account.json",
        ):
            with self.subTest(value=value):
                self.assertTrue(check_forbidden_configuration({"CAREERCLOUD_RESULTS_DIR": value}))

    def test_legitimate_cloud_paths_pass(self) -> None:
        self.assertEqual(
            check_forbidden_configuration(
                {
                    "CAREERCLOUD_RESULTS_DIR": r"E:\Crawlers\CareerCrawler-cloud\cloud\.localdev\results",
                    "CAREERCLOUD_WORKER_RUNTIME_DIR": "/var/lib/careercloud-staging/runtime",
                    "CAREERCLOUD_DATABASE_URL": "postgresql://u:p@db.stagingref0001.supabase.co:5432/postgres?sslmode=require",
                    "PATH": "/usr/bin",
                }
            ),
            [],
        )

    def test_loaded_production_modules_are_refused(self) -> None:
        for module in ("store.database", "sheets.auth", "crawler.weekly_run", "sqlite3"):
            with self.subTest(module=module):
                self.assertTrue(check_forbidden_configuration({}, loaded_modules=[module]))


class TestDeploymentPreflight(unittest.TestCase):
    def staging_settings(self, **overrides) -> Settings:
        base = Settings(
            environment="staging",
            storage="postgres",
            queue="redis",
            auth_mode="supabase",
            database_url=STAGING_URLS["database_url"],
            redis_url=STAGING_URLS["redis_url"],
            supabase_url=STAGING_URLS["supabase_url"],
            storage_backend="s3",
            s3_endpoint=STAGING_URLS["storage_endpoint"],
            s3_region="eu-west-1",
            s3_bucket="careercloud-staging-results",
            s3_access_key_id="key",
            s3_secret_access_key="secret",
            results_namespace="staging",
            api_origin="https://api-staging.example.com",
            cors_origins=("https://careercloud-staging.pages.dev",),
            queue_prefix="careercloud:staging",
            log_format="json",
            resource_registry="/etc/careercloud-staging/resources.json",
        )
        return replace(base, **overrides)

    def test_a_complete_staging_api_configuration_has_no_problems(self) -> None:
        self.assertEqual(deployment_problems(self.staging_settings()), [])

    def test_unsafe_staging_api_configurations_are_refused(self) -> None:
        cases = {
            "memory storage": dict(storage="memory"),
            "inline queue": dict(queue="inline"),
            "dev auth": dict(auth_mode="dev"),
            "service role key": dict(supabase_service_role_key="k"),
            "db without TLS": dict(database_url=STAGING_URLS["database_url"].replace("?sslmode=require", "")),
            "redis without TLS": dict(redis_url="redis://default:pw@careercloud-stg.upstash.io:6379"),
            "redis without password": dict(redis_url="rediss://careercloud-stg.upstash.io:6379"),
            "local results": dict(storage_backend="local"),
            "http CORS": dict(cors_origins=("http://careercloud-staging.pages.dev",)),
            "localhost CORS": dict(cors_origins=("https://localhost:5173",)),
            "text logs": dict(log_format="text"),
            "no registry": dict(resource_registry=None),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assertTrue(deployment_problems(self.staging_settings(**change)))

    def test_development_has_no_deployment_requirements(self) -> None:
        self.assertEqual(deployment_problems(Settings()), [])

    def test_create_app_refuses_to_start_a_misconfigured_staging_api(self) -> None:
        from cloud.api.main import create_app

        with self.assertRaises(EnvironmentIsolationError):
            create_app(self.staging_settings(queue="inline"))

    def test_worker_preflight(self) -> None:
        good = WorkerSettings(
            environment="staging",
            database_url=STAGING_URLS["database_url"],
            redis_url=STAGING_URLS["redis_url"],
            storage_backend="s3",
            s3_endpoint=STAGING_URLS["storage_endpoint"],
            s3_region="eu-west-1",
            s3_bucket="careercloud-staging-results",
            s3_access_key_id="k",
            s3_secret_access_key="s",
            results_namespace="staging",
            resource_registry="/etc/x.json",
            log_format="json",
        )
        self.assertEqual(worker_deployment_problems(good), [])
        for change in (
            dict(egress_guard=False),
            dict(runner="fake"),
            dict(browser_fallback=True),
            dict(redis_url="redis://default:pw@careercloud-stg.upstash.io:6379"),
            dict(storage_backend="local"),
        ):
            with self.subTest(change=change):
                self.assertTrue(worker_deployment_problems(replace(good, **change)))
        self.assertEqual(worker_deployment_problems(replace(good, browser_fallback=True, egress_firewall_confirmed=True)), [])


class TestRedisAndStorageStamps(unittest.TestCase):
    def test_redis_stamp_is_set_once_and_enforced(self) -> None:
        import fakeredis

        client = fakeredis.FakeRedis(decode_responses=True)
        ensure_redis_stamp(client, "careercloud:staging", "staging")
        ensure_redis_stamp(client, "careercloud:staging", "staging")
        with self.assertRaises(StampMismatchError):
            ensure_redis_stamp(client, "careercloud:staging", "production")

    def test_storage_stamp_is_set_once_and_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            storage = LocalFileStorage(Path(scratch))
            ensure_storage_stamp(storage, "staging")
            ensure_storage_stamp(storage, "staging")
            with self.assertRaises(StampMismatchError):
                ensure_storage_stamp(storage, "production")


class TestDatabaseStamp(PostgresTestCase):
    def test_database_stamp_lifecycle(self) -> None:
        import psycopg

        url = fresh_database()
        try:
            self.assertIsNone(read_database_stamp(url))
            with self.assertRaises(StampMismatchError):
                check_database_stamp(url, "staging")
            stamp_database(url, "staging")
            check_database_stamp(url, "staging")
            with self.assertRaises(StampMismatchError):
                check_database_stamp(url, "production")
            with self.assertRaises(StampMismatchError):
                stamp_database(url, "production")
            with psycopg.connect(url, autocommit=True) as conn:
                with self.assertRaises(psycopg.errors.CheckViolation):
                    conn.execute("update careercloud.deployment set environment = 'production'")
                with self.assertRaises(psycopg.errors.CheckViolation):
                    conn.execute("delete from careercloud.deployment")
                with self.assertRaises(psycopg.Error):
                    conn.execute("insert into careercloud.deployment (environment) values ('production')")
        finally:
            drop_database(url)

    def test_migrate_stamp_needs_confirmation(self) -> None:
        from cloud.db.migrate import main

        url = fresh_database()
        try:
            self.assertEqual(main(["stamp", "--database-url", url, "--environment", "test"]), 2)
            self.assertIsNone(read_database_stamp(url))
            self.assertEqual(main(["stamp", "--database-url", url, "--environment", "test", "--yes"]), 0)
            self.assertEqual(read_database_stamp(url), "test")
            self.assertEqual(main(["stamp", "--database-url", url, "--environment", "development", "--yes"]), 3)
        finally:
            drop_database(url)


if __name__ == "__main__":
    unittest.main()
