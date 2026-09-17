"""Unsafe inputs and unsafe configuration are refused before they can do harm."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cloud.api.settings import load_settings
from cloud.db.connection import ConfigurationError, resolve_database_url, resolve_redis_url
from cloud.shared.storage import InvalidKeyError, LocalFileStorage, validate_key
from cloud.shared.urls import UnsafeTargetError, check_public_host, is_public_address, resolve_public_addresses
from cloud.worker.settings import load_worker_settings


class TestTargetUrls(unittest.TestCase):
    def test_non_public_hosts_are_refused(self) -> None:
        for host in (
            "localhost", "LOCALHOST.", "127.0.0.1", "127.1", "0x7f000001", "2130706433", "0.0.0.0",
            "10.0.0.1", "172.16.5.5", "192.168.1.1", "169.254.169.254", "100.64.0.1",
            "::1", "[::1]", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1",
            "metadata.google.internal", "db.local", "printer.lan", "intranet", "router.home.arpa",
        ):
            with self.subTest(host=host), self.assertRaises(UnsafeTargetError):
                check_public_host(host)

    def test_public_hosts_and_web_ports_are_allowed(self) -> None:
        for host, port in (("example.com", None), ("jobs.lever.co", 443), ("93.184.216.34", 80), ("careers.acme.io", 8443)):
            with self.subTest(host=host):
                check_public_host(host, port)

    def test_non_web_ports_are_refused(self) -> None:
        for port in (22, 25, 3306, 5432, 6379, 11211):
            with self.subTest(port=port), self.assertRaises(UnsafeTargetError):
                check_public_host("example.com", port)

    def test_resolution_must_be_public_for_every_address(self) -> None:
        def resolver(addresses):
            return lambda host, port, type=None: [(2, 1, 6, "", (a, port)) for a in addresses]

        self.assertEqual(resolve_public_addresses("example.com", resolver=resolver(["93.184.216.34"])), ["93.184.216.34"])
        for addresses in (["10.0.0.5"], ["93.184.216.34", "127.0.0.1"], ["169.254.169.254"], []):
            with self.subTest(addresses=addresses), self.assertRaises(UnsafeTargetError):
                resolve_public_addresses("example.com", resolver=resolver(addresses))

        def failing(host, port, type=None):
            raise OSError("nxdomain")

        with self.assertRaises(UnsafeTargetError):
            resolve_public_addresses("does-not-exist.example", resolver=failing)

    def test_is_public_address(self) -> None:
        self.assertTrue(is_public_address("8.8.8.8"))
        self.assertFalse(is_public_address("not-an-ip"))


class TestStorageKeys(unittest.TestCase):
    def test_keys_are_strict(self) -> None:
        validate_key("results/0b8e-uuid/job_abc/jobs.csv")
        for key in ("../x", "results/../../etc/passwd", "/abs", "C:\\x", "results", "Results/x", "results//x", "results/x/../y", "a" * 600):
            with self.subTest(key=key), self.assertRaises(InvalidKeyError):
                validate_key(key)

    def test_local_storage_round_trip_stays_inside_its_root(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            source = Path(scratch) / "source.txt"
            source.write_text("hello", encoding="utf-8")
            storage = LocalFileStorage(Path(scratch) / "store")
            stored = storage.put_file("results/u/job_1/crawl.log", source, content_type="text/plain")
            self.assertEqual(stored.size_bytes, 5)
            self.assertEqual(b"".join(storage.iter_bytes("results/u/job_1/crawl.log")), b"hello")
            storage.delete("results/u/job_1/crawl.log")
            self.assertFalse(storage.exists("results/u/job_1/crawl.log"))
            with self.assertRaises(FileNotFoundError):
                storage.open("results/u/job_1/crawl.log")


class TestConnectionSafety(unittest.TestCase):
    def test_only_postgres_urls_are_accepted(self) -> None:
        for url in ("sqlite:///state/crawler.db", "state/crawler.db", "E:\\Crawlers\\CareerCrawler\\state\\crawler.db", "mysql://x"):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                resolve_database_url(url, environment="development")
        self.assertEqual(
            resolve_database_url("postgresql://u:p@127.0.0.1:5432/db", environment="development"),
            "postgresql://u:p@127.0.0.1:5432/db",
        )
        self.assertIsNone(resolve_database_url("", environment="production"))

    def test_development_never_reaches_remote_services_by_accident(self) -> None:
        remote_db = "postgresql://postgres:pw@db.abcdefgh.supabase.co:5432/postgres"
        with self.assertRaises(ConfigurationError):
            resolve_database_url(remote_db, environment="development")
        self.assertEqual(resolve_database_url(remote_db, environment="development", allow_remote=True), remote_db)
        self.assertEqual(resolve_database_url(remote_db, environment="production"), remote_db)
        with self.assertRaises(ConfigurationError):
            resolve_redis_url("rediss://default:pw@cache.upstash.io:6379", environment="development")
        with self.assertRaises(ConfigurationError):
            resolve_redis_url("http://127.0.0.1:6379", environment="development")

    def test_localdev_database_is_development_only(self) -> None:
        for environment in ("staging", "production"):
            with self.subTest(environment=environment), self.assertRaises(ConfigurationError):
                resolve_database_url("localdev", environment=environment)


class TestDeploymentSettings(unittest.TestCase):
    def test_production_requires_durable_backends(self) -> None:
        base = {"CAREERCLOUD_ENV": "production", "CAREERCLOUD_SUPABASE_URL": "https://abcdefgh.supabase.co"}
        for extra in ({}, {"CAREERCLOUD_STORAGE": "postgres", "CAREERCLOUD_DATABASE_URL": "postgresql://h/db"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                load_settings({**base, **extra})
        settings = load_settings(
            {
                **base,
                "CAREERCLOUD_STORAGE": "postgres",
                "CAREERCLOUD_DATABASE_URL": "postgresql://h/db",
                "CAREERCLOUD_QUEUE": "redis",
                "CAREERCLOUD_REDIS_URL": "rediss://h:6379",
            }
        )
        self.assertEqual((settings.storage, settings.queue, settings.auth_mode), ("postgres", "redis", "supabase"))

    def test_queue_redis_requires_postgres(self) -> None:
        with self.assertRaises(ValueError):
            load_settings({"CAREERCLOUD_QUEUE": "redis", "CAREERCLOUD_REDIS_URL": "redis://127.0.0.1:6390"})

    def test_worker_requires_database_and_redis_and_a_safe_runtime(self) -> None:
        with self.assertRaises(ValueError):
            load_worker_settings({})
        with self.assertRaises(ValueError):
            load_worker_settings({"CAREERCLOUD_DATABASE_URL": "localdev"})
        from cloud.worker.workspace import REPO_ROOT

        with self.assertRaises(ValueError):
            load_worker_settings(
                {
                    "CAREERCLOUD_DATABASE_URL": "localdev",
                    "CAREERCLOUD_REDIS_URL": "redis://127.0.0.1:6390",
                    "CAREERCLOUD_WORKER_RUNTIME_DIR": str(REPO_ROOT / "state"),
                }
            )
        settings = load_worker_settings(
            {"CAREERCLOUD_DATABASE_URL": "localdev", "CAREERCLOUD_REDIS_URL": "redis://127.0.0.1:6390"}
        )
        self.assertEqual((settings.runner, settings.browser_fallback), ("careercrawler", False))


if __name__ == "__main__":
    unittest.main()
