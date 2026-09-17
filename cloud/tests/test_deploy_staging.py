"""Static checks on the staging deployment files.

They run on Linux; these tests make sure they stay LF, hardened, staging-only,
and never reference the production crawler service, checkout, Sheets or Seamless.
"""

from __future__ import annotations

import configparser
import json
import re
import unittest
from pathlib import Path

from cloud.shared.environment import ResourceIdentity, ResourceRegistry, check_identity

STAGING = Path(__file__).resolve().parent.parent / "deploy" / "staging"
UNITS = sorted((STAGING / "systemd").glob("*.service"))
ALL_FILES = [p for p in STAGING.rglob("*") if p.is_file()]


def unit(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    text = re.sub(r"\\\n\s*", " ", path.read_text(encoding="utf-8"))
    parser.read_string(text)
    return parser


class TestFiles(unittest.TestCase):
    def test_everything_is_lf_and_present(self) -> None:
        names = {p.name for p in ALL_FILES}
        for expected in (
            "careercloud-staging-api.service",
            "careercloud-staging-worker.service",
            "careercloud-staging-tunnel.service",
            "careercloud-staging-egress.nft",
            "install.sh",
            "rollback.sh",
            "api.env.example",
            "worker.env.example",
            "resources.json.example",
            "verify.sql",
            "storage_bucket.sql",
        ):
            self.assertIn(expected, names)
        for path in ALL_FILES:
            with self.subTest(path=path.name):
                self.assertNotIn(b"\r\n", path.read_bytes())

    def test_nothing_references_production_crawler_sheets_or_seamless(self) -> None:
        forbidden = re.compile(
            r"careercrawler\.service|state/crawler\.db|CAREERCRAWLER_SPREADSHEET|GOOGLE_APPLICATION_CREDENTIALS|seamless|queue-recovery",
            re.IGNORECASE,
        )
        for path in ALL_FILES:
            if path.suffix == ".md":  # the safety report documents these names on purpose
                continue
            text = path.read_text(encoding="utf-8")
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            with self.subTest(path=path.name):
                self.assertIsNone(forbidden.search(code), f"{path.name}: {forbidden.search(code)}")


class TestUnits(unittest.TestCase):
    def test_units_are_hardened_and_run_as_dedicated_users(self) -> None:
        for path in UNITS:
            service = unit(path)["Service"]
            with self.subTest(unit=path.name):
                self.assertTrue(service["User"].startswith("ccstg-"))
                self.assertEqual(service["Restart"], "always")
                self.assertEqual(service["KillSignal"], "SIGTERM")
                self.assertEqual(service["NoNewPrivileges"], "yes")
                self.assertEqual(service["ProtectSystem"], "strict")
                self.assertEqual(service["StandardOutput"], "journal")
                self.assertIn("MemoryMax", service)
                self.assertEqual(service.get("CapabilityBoundingSet", "x"), "")

    def test_api_binds_loopback_only(self) -> None:
        exec_start = unit(STAGING / "systemd" / "careercloud-staging-api.service")["Service"]["ExecStart"]
        self.assertIn("--host 127.0.0.1", exec_start)
        self.assertNotIn("--reload", exec_start)
        self.assertNotIn("0.0.0.0", exec_start)

    def test_worker_requires_the_firewall_and_safety_report_and_can_stop_gracefully(self) -> None:
        text = (STAGING / "systemd" / "careercloud-staging-worker.service").read_text(encoding="utf-8")
        service = unit(STAGING / "systemd" / "careercloud-staging-worker.service")["Service"]
        self.assertIn("nft list table inet careercloud_staging_egress", text)
        self.assertIn("cloud.ops.safety_report --component worker", text)
        self.assertTrue(service["ExecStart"].endswith("-m cloud.worker"))
        self.assertGreaterEqual(int(service["TimeoutStopSec"]), 120)
        self.assertEqual(service["ReadWritePaths"], "/var/lib/careercloud-staging/runtime")


class TestEgressPolicy(unittest.TestCase):
    def test_blocks_every_unsafe_range_for_the_worker_user_only(self) -> None:
        text = (STAGING / "nftables" / "careercloud-staging-egress.nft").read_text(encoding="utf-8")
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10",
                     "::1/128", "fc00::/7", "fe80::/10", "::ffff:0:0/96"):
            self.assertIn(cidr, text)
        self.assertIn('meta skuid != "ccstg-worker" accept', text)
        self.assertIn("ip daddr 127.0.0.53 udp dport 53 accept", text)


class TestTemplates(unittest.TestCase):
    def test_env_templates_are_staging_only(self) -> None:
        for name in ("api.env.example", "worker.env.example"):
            raw = (STAGING / "env" / name).read_text(encoding="utf-8")
            text = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
            with self.subTest(name=name):
                self.assertIn("CAREERCLOUD_ENV=staging", text)
                self.assertIn("CAREERCLOUD_QUEUE_PREFIX=careercloud:staging", text)
                self.assertIn("CAREERCLOUD_RESULTS_NAMESPACE=staging", text)
                self.assertIn("rediss://", text)
                self.assertIn("sslmode=require", text)
                self.assertNotRegex(text, r"(?m)^CAREERCLOUD_SUPABASE_SERVICE_ROLE_KEY=")
                self.assertNotRegex(text, r"(?m)^CAREERCLOUD_DEV_JWT_SECRET=")
                self.assertNotRegex(text, r"(?i)production")

    def test_registry_template_parses_and_separates_environments(self) -> None:
        registry = ResourceRegistry.load(STAGING / "env" / "resources.json.example")
        self.assertEqual(set(registry.environments), {"staging", "production"})
        staging_bucket = registry.own("staging", "storage_buckets")[0]
        self.assertIn("staging", staging_bucket)
        identity = ResourceIdentity(environment="staging", storage_bucket=registry.own("production", "storage_buckets")[0])
        self.assertTrue(check_identity(identity, registry))

    def test_install_script_guards(self) -> None:
        text = (STAGING / "install.sh").read_text(encoding="utf-8")
        self.assertIn("--i-am-deploying-staging", text)
        self.assertIn("*prod*", text)
        self.assertIn("CAREERCLOUD_ENV=staging", text)
        self.assertIn("safety_report", text)
        self.assertNotRegex(text, r"env \$\(grep", "secrets must never be passed on a command line")
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", text)

    def test_dashboard_staging_template(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "web" / ".env.staging.example").read_text(encoding="utf-8")
        self.assertIn("VITE_DEPLOY_ENV=staging", text)
        self.assertIn("VITE_AUTH_MODE=supabase", text)
        self.assertNotIn("SERVICE_ROLE", text.upper().replace("# ", ""))
        json.dumps(text)  # plain text only


if __name__ == "__main__":
    unittest.main()
