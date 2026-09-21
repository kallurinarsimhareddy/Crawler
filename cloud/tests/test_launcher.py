"""The Windows launchers, and the promise that no cloud provider is required.

``run-worker.bat`` is the only thing standing between an operator and a worker
that writes to the wrong database, so its refusals are tested by running it, not
by reading it. The provider-neutrality tests exist because Phase 5C's
documentation assumed one specific cloud host; nothing may quietly require one
again.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUN_WORKER = REPO_ROOT / "run-worker.bat"
STOP_WORKER = REPO_ROOT / "stop-worker.bat"
DEFAULT_STOP_FILE = r"cloud\.localdev\worker.stop"

windows_only = unittest.skipUnless(sys.platform == "win32", "the launchers are cmd.exe scripts")


def run_launcher(script: Path, *args: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides or {})
    return subprocess.run(
        ["cmd.exe", "/c", str(script), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )


class TestLauncherFilesExist(unittest.TestCase):
    def test_both_scripts_are_at_the_repository_root(self) -> None:
        """Where an operator will look for them, not buried in cloud/."""
        self.assertTrue(RUN_WORKER.is_file(), f"{RUN_WORKER} is missing")
        self.assertTrue(STOP_WORKER.is_file(), f"{STOP_WORKER} is missing")

    def test_batch_files_use_crlf_throughout(self) -> None:
        """cmd.exe is unreliable with LF-only batch files, including mixed ones."""
        for script in (RUN_WORKER, STOP_WORKER):
            data = script.read_bytes()
            self.assertIn(b"\r\n", data, f"{script.name} has no CRLF line endings")
            # Every LF must be the tail of a CRLF: no bare newlines anywhere.
            self.assertEqual(
                data.count(b"\n"),
                data.count(b"\r\n"),
                f"{script.name} has bare LF line endings mixed in",
            )

    def test_gitattributes_pins_batch_files_to_crlf(self) -> None:
        """Otherwise a checkout on another machine can silently convert them."""
        attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertRegex(attributes, r"\*\.bat\s+text\s+eol=crlf")

    def test_the_two_scripts_agree_on_the_stop_file(self) -> None:
        """A mismatch here means stop-worker.bat silently does nothing."""
        for script in (RUN_WORKER, STOP_WORKER):
            self.assertIn(DEFAULT_STOP_FILE, script.read_text(encoding="utf-8"))

    def test_run_worker_starts_the_cloud_worker_from_its_own_venv(self) -> None:
        body = RUN_WORKER.read_text(encoding="utf-8")
        self.assertIn(r"cloud\.venv\Scripts\python.exe", body)
        self.assertIn("-m cloud.worker", body)

    def test_run_worker_never_invokes_the_production_crawler(self) -> None:
        """``main.py`` is the production weekly run. The launcher must not run it.

        Comments and the guard's own ``findstr`` pattern legitimately mention
        ``crawler.db``, so this inspects only the lines cmd.exe would execute.
        """
        executable = []
        for raw in RUN_WORKER.read_text(encoding="utf-8").splitlines():
            line = raw.strip().lower()
            if not line or line.startswith("rem") or line.startswith("echo") or line.startswith("::"):
                continue
            executable.append(line)
        body = "\n".join(executable)
        for forbidden in ("main.py", "weekly_run", "--preview", "sheets", "seamless"):
            self.assertNotIn(
                forbidden, body, f"run-worker.bat would run the production crawler: {forbidden}"
            )
        # crawler.db may appear only inside the refusal's search pattern.
        for line in executable:
            if "crawler.db" in line or "crawler\\.db" in line:
                self.assertIn("findstr", line, f"crawler.db used outside the guard: {line}")


@windows_only
class TestRunWorkerRefusals(unittest.TestCase):
    """Every guard, exercised by actually running the script."""

    def env_file(self, body: str) -> Path:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        path = Path(scratch.name) / "worker.env"
        path.write_text(body, encoding="ascii")
        return path

    def test_a_missing_environment_file_is_explained_not_crashed(self) -> None:
        missing = Path(tempfile.gettempdir()) / "careercloud-does-not-exist.env"
        result = run_launcher(RUN_WORKER, str(missing))
        self.assertEqual(result.returncode, 1)
        self.assertIn("No worker environment file", result.stdout)
        # It says what to do next.
        self.assertIn(".env.example", result.stdout)

    def test_production_is_refused(self) -> None:
        """A production worker belongs on a managed host, not a console window."""
        env_file = self.env_file("CAREERCLOUD_ENV=production\nCAREERCLOUD_DATABASE_URL=postgresql://h/d\n")
        result = run_launcher(RUN_WORKER, str(env_file))
        self.assertEqual(result.returncode, 1)
        self.assertIn("will not start a production worker", result.stdout)

    def test_production_is_refused_whatever_the_spacing_or_case(self) -> None:
        for line in (
            "CAREERCLOUD_ENV = production",
            "  CAREERCLOUD_ENV=production",
            "CAREERCLOUD_ENV=PRODUCTION",
        ):
            with self.subTest(line=line):
                result = run_launcher(RUN_WORKER, str(self.env_file(line + "\n")))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("production", result.stdout.lower())

    def test_a_sqlite_database_is_refused(self) -> None:
        """CareerCloud is PostgreSQL-only; state/crawler.db is production's."""
        env_file = self.env_file("CAREERCLOUD_ENV=development\nCAREERCLOUD_DATABASE_URL=sqlite:///x.db\n")
        result = run_launcher(RUN_WORKER, str(env_file))
        self.assertEqual(result.returncode, 1)
        self.assertIn("SQLite", result.stdout)

    def test_any_mention_of_the_production_crawler_database_is_refused(self) -> None:
        env_file = self.env_file(
            "CAREERCLOUD_ENV=development\nCAREERCLOUD_DATABASE_URL=postgresql://h/d\n"
            "# migrated from state/crawler.db\n"
        )
        result = run_launcher(RUN_WORKER, str(env_file))
        self.assertEqual(result.returncode, 1)
        self.assertIn("crawler.db", result.stdout)

    def test_staging_and_development_are_allowed_past_the_guards(self) -> None:
        """The refusals must not block the environments this machine is for.

        The worker still fails afterwards — there is no database here — but it
        must fail *in Python*, past the launcher's checks, not at them.
        """
        for environment in ("development", "staging"):
            with self.subTest(environment=environment):
                env_file = self.env_file(
                    f"CAREERCLOUD_ENV={environment}\n"
                    "CAREERCLOUD_DATABASE_URL=postgresql://127.0.0.1:1/none\n"
                    "CAREERCLOUD_REDIS_URL=redis://127.0.0.1:1/0\n"
                )
                result = run_launcher(RUN_WORKER, str(env_file))
                combined = result.stdout + result.stderr
                self.assertNotIn("will not start a production worker", combined)
                self.assertNotIn("No worker environment file", combined)
                self.assertNotEqual(result.returncode, 0)


@windows_only
class TestStopWorker(unittest.TestCase):
    def test_it_writes_the_stop_file_the_worker_watches(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        stop_file = Path(scratch.name) / "worker.stop"
        result = run_launcher(STOP_WORKER, env_overrides={"CAREERCLOUD_STOP_FILE": str(stop_file)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(stop_file.is_file(), "stop-worker.bat did not create the stop file")

    def test_it_is_safe_to_run_with_no_worker_running(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        stop_file = Path(scratch.name) / "worker.stop"
        for _ in range(2):
            result = run_launcher(STOP_WORKER, env_overrides={"CAREERCLOUD_STOP_FILE": str(stop_file)})
            self.assertEqual(result.returncode, 0)


class TestNoCloudProviderIsRequired(unittest.TestCase):
    """Compute must stay provider-neutral.

    The worker's whole contract is outbound connections to Postgres, Redis and
    storage. Nothing in the deployment may assume a particular vendor's VM.
    """

    #: Hosting vendors. `oracle` is checked separately: the repository also has
    #: an Oracle Recruiting *ATS adapter*, which is crawler code and unrelated.
    VENDORS = ("oracle cloud", "always free", "ampere", "a1.flex", "oci ", "ec2", "droplet")

    def deployment_files(self):
        base = REPO_ROOT / "cloud" / "deploy"
        return [p for p in base.rglob("*") if p.is_file()]

    def test_no_deployment_file_names_a_hosting_vendor(self) -> None:
        for path in self.deployment_files():
            body = path.read_text(encoding="utf-8", errors="replace").lower()
            for vendor in self.VENDORS:
                self.assertNotIn(
                    vendor,
                    body,
                    f"{path.relative_to(REPO_ROOT)} ties the deployment to {vendor!r}",
                )

    def test_the_installer_only_requires_a_generic_linux_host(self) -> None:
        install = (REPO_ROOT / "cloud" / "deploy" / "staging" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("any Linux VM", install)
        self.assertNotIn("Oracle", install)

    def test_no_cloud_module_imports_a_provider_sdk(self) -> None:
        """boto3 is fine: S3 is a protocol every provider speaks, not a vendor.

        Tests are excluded — this file names the SDKs in order to forbid them.
        """
        forbidden = ("import oci", "from oci", "googleapiclient.discovery", "import azure")
        for path in (REPO_ROOT / "cloud").rglob("*.py"):
            parts = set(path.parts)
            if parts & {".venv", ".localdev", "tests"}:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            for name in forbidden:
                self.assertNotIn(name, body, f"{path.relative_to(REPO_ROOT)} imports {name!r}")

    def test_the_readme_presents_oracle_only_as_one_option(self) -> None:
        readme = (REPO_ROOT / "cloud" / "README.md").read_text(encoding="utf-8")
        self.assertIn("Running the worker on another host", readme)
        self.assertIn("nothing depends on it", readme)
        # The worker host must not be listed as a blocker any more.
        self.assertNotIn("| Worker/API host | Oracle", readme)

    def test_the_readme_documents_the_local_worker(self) -> None:
        readme = (REPO_ROOT / "cloud" / "README.md").read_text(encoding="utf-8")
        for expected in ("run-worker.bat", "stop-worker.bat", "Operator workflow", "Crawler worker offline"):
            self.assertIn(expected, readme)


if __name__ == "__main__":
    unittest.main()
