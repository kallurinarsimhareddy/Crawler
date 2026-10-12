"""deploy/windows-selfhost: the package secret scanner, the pinned components and the scripts."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WIN = REPO / "deploy" / "windows-selfhost"
if not (WIN / "components.json").exists():           # inside an installed copy: repository-only tests
    raise unittest.SkipTest("deploy/windows-selfhost is not present")
spec = importlib.util.spec_from_file_location("scan_package", WIN / "tools" / "scan_package.py")
scan_package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan_package)


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / "app").mkdir()

    def write(self, rel: str, text: str = "x") -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def problems(self, values=()):
        return scan_package.scan(self.root, set(values))

    def test_clean_tree_passes(self) -> None:
        self.write("app/cloud/api/.env.example", "CAREERCLOUD_REDIS_URL=redis://127.0.0.1:6390/0\n")
        self.write("config/answers.example.env", "HOSTNAME=sanagtm.example.com\n")
        self.write("app/test.py", 'PASSWORD = "not-a-real-password-8891"\n')
        self.assertEqual(self.problems(), [])

    def test_secret_files_are_refused(self) -> None:
        for rel in ("app/cloud/api/.env", "app/cloud/api/.env.sana-cloud", "x/db.dump", "x/b.manifest.json",
                    "x/server.key", "config/sanagtm.env", "config/tunnel.token", "secrets/service_account_a.json"):
            with self.subTest(rel=rel):
                self.write(rel)
                self.assertTrue(self.problems(), rel)
                (self.root / rel).unlink()

    def test_credential_patterns_are_refused(self) -> None:
        fake = "postgresql://postgres.abcdefghij:" + "Sup3rS3cretPassw0rd" + "@aws-0.pooler.supabase.com/x"
        self.write("app/a.py", f'URL = "{fake}"\n')   # assembled here so this file itself stays clean
        self.assertTrue(any("database url" in p for p in self.problems()))

    def test_real_values_are_found_and_never_printed(self) -> None:
        self.write("app/cfg.py", "KEY = 'zq1-really-secret-value-77'\n")
        found = self.problems({"zq1-really-secret-value-77"})
        self.assertEqual(len(found), 1)
        self.assertNotIn("zq1-really", found[0])

    def test_only_password_bearing_urls_count_as_secret_values(self) -> None:
        env = self.root / "local.env"
        # assembled at run time so this file holds no credential-shaped literal
        db_url = "postgresql://u:" + "fakepw" + "123456" + "@db.example.com/x"
        local_redis = "redis://" + "localhost:6399/0"                 # no password: not a secret
        env.write_text(f"CAREERCLOUD_REDIS_URL={local_redis}\n"
                       f"CAREERCLOUD_DATABASE_URL={db_url}\n"
                       "CAREERCLOUD_PLATFORM_SECRETS_KEY=abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG=\n"
                       "CAREERCLOUD_CORS_ORIGINS=https://sanagtm.pages.dev\n", encoding="utf-8")
        values = scan_package.real_values([str(env)])
        self.assertEqual(values, {db_url, "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG="})


class ComponentPins(unittest.TestCase):
    def test_every_component_is_pinned_by_sha256_and_https(self) -> None:
        components = json.loads((WIN / "components.json").read_text(encoding="utf-8"))
        for name in ("python", "postgresql", "winsw", "cloudflared"):
            with self.subTest(name=name):
                c = components[name]
                self.assertRegex(c["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(c["url"].startswith("https://"))
                self.assertTrue(c["url"].endswith(c["file"]))
        self.assertTrue(components["postgresql"]["version"].startswith("17."))

    def test_lock_file_is_exact_and_has_no_redis_or_supabase_db_clients(self) -> None:
        lines = [l for l in (WIN / "requirements-lock.txt").read_text().splitlines() if l and not l.startswith("#")]
        self.assertTrue(all("==" in l for l in lines), lines)
        names = {re.split(r"==", l)[0].lower() for l in lines}
        self.assertFalse(names & {"redis", "supabase", "fakeredis", "pgserver"})
        self.assertTrue({"fastapi", "uvicorn", "psycopg", "dnspython", "cryptography"} <= names)


class ScriptChecks(unittest.TestCase):
    SCRIPTS = [WIN / "Install-SanaGtm.ps1", WIN / "Build-Package.ps1", WIN / "bin" / "sanagtm.ps1",
               WIN / "bin" / "lib" / "SanaGtm.psm1"]

    def test_scripts_are_ascii(self) -> None:          # Windows PowerShell 5.1 reads BOM-less files as ANSI
        for script in self.SCRIPTS:
            with self.subTest(script=script.name):
                script.read_bytes().decode("ascii")

    @unittest.skipUnless(subprocess.run(["where", "powershell"], capture_output=True, shell=True).returncode == 0,
                         "Windows PowerShell not available")
    def test_scripts_parse(self) -> None:
        for script in self.SCRIPTS:
            cmd = ("$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{script}',[ref]$null,[ref]$e);$e.Count")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "0", f"{script.name}: {out.stdout} {out.stderr}")

    def test_secrets_never_reach_a_command_line(self) -> None:
        module = (WIN / "bin" / "lib" / "SanaGtm.psm1").read_text(encoding="ascii")
        self.assertIn("--token-file", module)                       # tunnel token read from a file
        self.assertIn("\\getenv app_password SANAGTM_APP_PASSWORD", module)   # app password via env, not SQL text
        self.assertIn("listen_addresses = '127.0.0.1'", module)
        self.assertNotIn("0.0.0.0", module)


if __name__ == "__main__":
    unittest.main()
