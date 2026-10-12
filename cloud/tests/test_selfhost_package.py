"""deploy/selfhost: compose/Docker invariants, and the `sanagtm` CLI driven against a fake docker
(no Docker needed). The CLI tests need bash; they skip without it."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SELFHOST = REPO / "deploy" / "selfhost"
if not (SELFHOST / "compose.yaml").exists():   # inside the app image: deploy/selfhost is not copied
    raise unittest.SkipTest("deploy/selfhost is not present (repository-only tests)")
COMPOSE = yaml.safe_load((SELFHOST / "compose.yaml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]


class ComposeInvariants(unittest.TestCase):
    def test_nothing_is_published_on_the_host(self) -> None:
        for name, service in SERVICES.items():
            self.assertNotIn("ports", service, name)
            self.assertNotIn("network_mode", service, name)

    def test_the_database_is_only_on_the_private_network(self) -> None:
        self.assertEqual(SERVICES["postgres"]["networks"], ["backend"])
        self.assertEqual(SERVICES["cloudflared"]["networks"], ["edge"])
        self.assertEqual(set(SERVICES["web"]["networks"]), {"backend", "edge"})
        for name in ("api", "worker", "migrate"):
            self.assertEqual(SERVICES[name]["networks"], ["backend"], name)

    def test_no_redis_and_no_hosted_database(self) -> None:
        lines = (SELFHOST / "compose.yaml").read_text(encoding="utf-8").lower().splitlines()
        text = " ".join(line.split("#")[0] for line in lines)          # comments may say "no redis"
        self.assertNotIn("redis", text)
        self.assertNotIn("upstash", text)
        self.assertNotIn("pooler.supabase", text)

    def test_restart_policies_health_checks_and_order(self) -> None:
        for name in ("postgres", "api", "worker", "web", "cloudflared"):
            self.assertEqual(SERVICES[name]["restart"], "unless-stopped", name)
        for name in ("postgres", "api", "worker", "web"):
            self.assertIn("healthcheck", SERVICES[name], name)
        self.assertEqual(SERVICES["migrate"]["restart"], "no")
        self.assertEqual(SERVICES["migrate"]["command"], ["python", "-m", "cloud.ops.selfhost", "prepare"])
        for name in ("api", "worker"):
            self.assertEqual(SERVICES[name]["depends_on"], {"migrate": {"condition": "service_completed_successfully"}})
        self.assertEqual(SERVICES["migrate"]["depends_on"], {"postgres": {"condition": "service_healthy"}})
        self.assertEqual(SERVICES["cloudflared"]["profiles"], ["tunnel"])

    def test_persistent_data_lives_under_the_data_directory(self) -> None:
        self.assertIn("${SANAGTM_DATA:-/srv/sanagtm}/pgdata:/var/lib/postgresql/data", SERVICES["postgres"]["volumes"])
        for mount in ("files", "results", "run"):
            self.assertIn(f"${{SANAGTM_DATA:-/srv/sanagtm}}/{mount}:/srv/sanagtm/{mount}", SERVICES["api"]["volumes"])

    def test_app_containers_drop_privileges(self) -> None:
        for name in ("api", "worker", "migrate"):
            self.assertEqual(SERVICES[name]["cap_drop"], ["ALL"], name)
            self.assertIn("no-new-privileges:true", SERVICES[name]["security_opt"], name)

    def test_the_trusted_proxy_is_exactly_the_web_container(self) -> None:
        web_ip = SERVICES["web"]["networks"]["backend"]["ipv4_address"]
        self.assertEqual(SERVICES["api"]["environment"]["CAREERCLOUD_TRUSTED_PROXIES"], web_ip + "/32")

    def test_logs_are_rotated(self) -> None:
        for name, service in SERVICES.items():
            self.assertEqual(service["logging"]["options"]["max-size"], "10m", name)


class ImageInvariants(unittest.TestCase):
    def test_the_app_image_runs_as_a_user_and_skips_sheets_and_sqlite(self) -> None:
        text = (SELFHOST / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER sanagtm", text)
        self.assertIn("postgresql-client-17", text)
        copied = set(re.findall(r"^COPY (\S+) ", text, re.M))
        self.assertFalse({"sheets", "store", "."} & copied)
        for directory in ("adapters", "crawler", "cloud", "utils", "config", "exporters", "models"):
            self.assertIn(directory, copied)

    def test_runtime_requirements_are_pinned_and_lean(self) -> None:
        lines = [line.split("#")[0].strip() for line in (SELFHOST / "requirements.txt").read_text().splitlines()]
        packages = [line for line in lines if line]
        self.assertTrue(all("==" in line for line in packages), packages)
        names = {re.split(r"[\[=]", line)[0].lower() for line in packages}
        self.assertFalse(names & {"redis", "supabase", "google-api-python-client", "fakeredis", "pgserver", "boto3"})
        self.assertTrue({"fastapi", "psycopg", "dnspython", "cryptography", "pyjwt"} <= names)

    def test_no_secret_ever_enters_the_build_context(self) -> None:
        ignore = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for pattern in ("**/.env", "**/.env.*", "**/secrets/", "**/.venv/", "**/.localdev/", "**/*.dump"):
            self.assertIn(pattern, ignore)

    def test_the_web_image_bakes_in_public_values_only(self) -> None:
        text = (SELFHOST / "web" / "Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(sorted(re.findall(r"^ARG (\S+)", text, re.M)), ["VITE_SUPABASE_ANON_KEY", "VITE_SUPABASE_URL"])
        self.assertIn("VITE_API_URL=", text)

    def test_initdb_passes_the_password_as_a_psql_variable(self) -> None:
        text = (SELFHOST / "postgres" / "initdb" / "10-sanagtm.sh").read_text(encoding="utf-8")
        self.assertIn("-v app_password=\"$SANAGTM_DB_PASSWORD\"", text)
        self.assertIn("%L', :'app_password'", text)


def _bash():
    """bash on PATH, else Git for Windows (machine-wide or per-user install)."""
    found = shutil.which("bash")
    if found or os.name != "nt":
        return found
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "") + r"\Programs"]
    return next((str(p) for root in roots for p in [Path(root) / "Git" / "bin" / "bash.exe"] if p.exists()), None)


BASH = _bash()

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Records every call; answers the few queries the CLI makes.
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$*" in
  "compose version") exit 0 ;;
  *" ps --format"*) printf 'postgres healthy running\napi healthy running\nworker healthy running\nweb healthy running\n' ;;
  *" run "*" python -") cat > /dev/null; echo "${FAKE_WS_COUNT:-0}" ;;
esac
exit 0
"""


@unittest.skipUnless(BASH, "bash is not available")
class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.app, self.etc, self.data = root / "app", root / "etc", root / "data"
        (self.app / "deploy" / "selfhost").mkdir(parents=True)
        shutil.copy(SELFHOST / "compose.yaml", self.app / "deploy" / "selfhost" / "compose.yaml")
        self.log = root / "docker.log"
        self.docker = root / "fake-docker"
        self.docker.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
        self.docker.chmod(self.docker.stat().st_mode | stat.S_IEXEC)
        self.answers = root / "answers.env"
        self.answers.write_text("SANAGTM_HOSTNAME=sanagtm.example.com\n"
                                "SANAGTM_SUPABASE_URL=https://abcdefghijklmnop.supabase.co\n"
                                "SANAGTM_SUPABASE_ANON_KEY=public-anon-key-0123456789abcdef\n"
                                "SANAGTM_PLATFORM_SECRETS_KEY=KEEP-THIS-EXISTING-KEY-0123456789abcdefghijk=\n"
                                "SANAGTM_TUNNEL_TOKEN=tunnel-token-SECRET-value\n", encoding="utf-8", newline="\n")

    def sanagtm(self, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
        full = {**os.environ, "SANAGTM_APP": self.posix(self.app), "SANAGTM_ETC": self.posix(self.etc),
                "SANAGTM_DATA": self.posix(self.data), "SANAGTM_DOCKER": self.posix(self.docker),
                "SANAGTM_ALLOW_NONROOT": "1", "FAKE_LOG": self.posix(self.log), **env}
        return subprocess.run([BASH, self.posix(SELFHOST / "bin" / "sanagtm"), *args], input=stdin, text=True,
                              capture_output=True, env=full, timeout=120)

    @staticmethod
    def posix(path: Path) -> str:
        text = str(path).replace("\\", "/")
        if os.name == "nt" and re.match(r"^[A-Za-z]:/", text):
            text = "/" + text[0].lower() + text[2:]
        return text

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def install(self, *extra: str) -> subprocess.CompletedProcess:
        return self.sanagtm("install", "--answers", self.posix(self.answers), *extra)

    def test_syntax(self) -> None:
        for script in (SELFHOST / "bin" / "sanagtm", SELFHOST / "bin" / "bootstrap-ubuntu.sh",
                       SELFHOST / "postgres" / "initdb" / "10-sanagtm.sh"):
            done = subprocess.run([BASH, "-n", self.posix(script)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{script.name}: {done.stderr}")

    def test_install_writes_configuration_and_never_passes_secrets_to_docker(self) -> None:
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        app = (self.etc / "sanagtm.env").read_text(encoding="utf-8")
        for line in ("CAREERCLOUD_ENV=production", "CAREERCLOUD_SELF_HOSTED=1", "CAREERCLOUD_QUEUE=postgres",
                     "CAREERCLOUD_STORAGE_BACKEND=local", "CAREERCLOUD_API_ORIGIN=https://sanagtm.example.com",
                     "CAREERCLOUD_PLATFORM_SECRETS_KEY=KEEP-THIS-EXISTING-KEY-0123456789abcdefghijk="):
            self.assertIn(line, app)
        self.assertRegex(app, r"CAREERCLOUD_DATABASE_URL=postgresql://sanagtm:[0-9a-f]{48}@postgres:5432/sanagtm")
        self.assertNotIn("REDIS", app)
        pg = (self.etc / "postgres.env").read_text(encoding="utf-8")
        db_pw = re.search(r"SANAGTM_DB_PASSWORD=(\w+)", pg).group(1)
        self.assertIn(db_pw, app)
        self.assertIn("TUNNEL_TOKEN=tunnel-token-SECRET-value", (self.etc / "tunnel.env").read_text(encoding="utf-8"))
        compose_env = (self.etc / "compose.env").read_text(encoding="utf-8")
        self.assertIn("COMPOSE_PROFILES=tunnel", compose_env)
        self.assertNotIn("SECRET", compose_env)
        calls = self.calls()
        self.assertIn("compose -p sanagtm", calls)
        self.assertIn(" build", calls)
        self.assertIn(" up -d", calls)
        for secret in ("tunnel-token-SECRET-value", "KEEP-THIS-EXISTING-KEY", db_pw):
            self.assertNotIn(secret, calls + done.stdout + done.stderr)
        for directory in ("pgdata", "files", "results", "run", "backups"):
            self.assertTrue((self.data / directory).is_dir(), directory)
        if os.name != "nt":
            for name in ("sanagtm.env", "postgres.env", "tunnel.env", "compose.env"):
                self.assertEqual(stat.S_IMODE((self.etc / name).stat().st_mode), 0o600, name)

    def test_install_refuses_twice_and_over_an_existing_database(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        again = self.install()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already installed", again.stderr)
        shutil.rmtree(self.etc)
        (self.data / "pgdata" / "PG_VERSION").write_text("17", encoding="utf-8")
        over = self.install()
        self.assertNotEqual(over.returncode, 0)
        self.assertIn("refusing to install over it", over.stderr)

    def test_install_refuses_a_secret_key_and_a_staging_hostname(self) -> None:
        bad = self.answers.read_text(encoding="utf-8").replace("public-anon-key", "sb_secret_abc")
        self.answers.write_text(bad, encoding="utf-8", newline="\n")
        self.assertIn("SECRET key", self.install().stderr)
        self.answers.write_text(bad.replace("sb_secret_abc", "public").replace("sanagtm.example.com",
                                                                                "sanagtm-staging.example.com"),
                                encoding="utf-8", newline="\n")
        self.assertIn("dev/test/staging", self.install().stderr)

    def test_a_new_secrets_key_is_generated_when_none_is_given(self) -> None:
        text = self.answers.read_text(encoding="utf-8")
        self.answers.write_text("\n".join(line for line in text.splitlines()
                                          if not line.startswith("SANAGTM_PLATFORM_SECRETS_KEY")) + "\n",
                                encoding="utf-8", newline="\n")
        done = self.install("--no-tunnel")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("generated a NEW platform secrets key", done.stderr)
        key = re.search(r"CAREERCLOUD_PLATFORM_SECRETS_KEY=(\S+)", (self.etc / "sanagtm.env").read_text()).group(1)
        from cryptography.fernet import Fernet

        Fernet(key.encode())                                         # a valid Fernet key
        self.assertNotIn(key, done.stdout + done.stderr + self.calls())

    def test_no_tunnel_mode(self) -> None:
        self.assertEqual(self.install("--no-tunnel").returncode, 0)
        self.assertIn("COMPOSE_PROFILES=\n", (self.etc / "compose.env").read_text(encoding="utf-8"))
        self.assertNotIn("TUNNEL_TOKEN=", (self.etc / "tunnel.env").read_text(encoding="utf-8"))

    def test_backup_runs_the_tested_backup_tool_with_no_url_on_the_command_line(self) -> None:
        self.install("--no-tunnel")
        done = self.sanagtm("backup", "--label", "check")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertIn("deploy/ops/backup/sana_backup.py backup --pg-bin /usr/lib/postgresql/17/bin --out /backups "
                      "--label sanagtm-check --local-only", calls)
        self.assertIn("sana_backup.py prune --dir /backups", calls)
        self.assertNotIn("postgresql://", calls)
        self.assertTrue(list((self.data / "backups").glob("files_*.tar.gz")))
        self.assertTrue(list((self.data / "backups" / "config").glob("config_*.tar.gz")))

    def test_restore_and_migrate_guards(self) -> None:
        self.install("--no-tunnel")
        outside = self.data.parent / "elsewhere.dump"
        outside.write_text("x", encoding="utf-8")
        (self.data.parent / "elsewhere.manifest.json").write_text("{}", encoding="utf-8")
        refused = self.sanagtm("restore", self.posix(outside))
        self.assertIn("copy the dump and its manifest into", refused.stderr)
        dump = self.data / "backups" / "x.dump"
        dump.write_text("x", encoding="utf-8")
        (self.data / "backups" / "x.manifest.json").write_text("{}", encoding="utf-8")
        test_restore = self.sanagtm("restore", self.posix(dump))
        self.assertEqual(test_restore.returncode, 0, test_restore.stderr)
        self.assertIn("restore-test --dump /backups/x.dump", self.calls())
        self.assertIn("-e SANAGTM_ADMIN_DATABASE_URL", self.calls())
        self.assertNotIn("postgresql://", self.calls())
        live = self.sanagtm("restore", self.posix(dump), "--live", stdin="wrong-host\n")
        self.assertIn("not confirmed", live.stderr)
        busy = self.sanagtm("migrate", "import", self.posix(dump), FAKE_WS_COUNT="3")
        self.assertIn("already has data", busy.stderr)

    def test_uninstall_keeps_data_and_purge_needs_a_fresh_backup(self) -> None:
        self.install("--no-tunnel")
        purge = self.sanagtm("uninstall", "--purge-data")
        self.assertIn("no backup exists", purge.stderr)
        self.assertTrue(self.data.exists())
        kept = self.sanagtm("uninstall")
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertIn("data kept", kept.stdout)
        self.assertTrue((self.data / "pgdata").exists() and (self.etc / "sanagtm.env").exists())


if __name__ == "__main__":
    unittest.main()
