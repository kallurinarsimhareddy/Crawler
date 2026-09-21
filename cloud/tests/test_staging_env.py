"""The staging env generator: does it build usable config without leaking it?

Every assertion here uses invented values. The point of the module is that the
real values are written once and never echoed, so the tests that matter are the
ones checking what it *does not* print.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from cloud.api.settings import load_settings, validate_settings
from cloud.ops import staging_env
from cloud.worker.settings import load_worker_settings

FAKE = {
    "SUPABASE_PROJECT_REF": "abcdefghijklmnopqrst",
    "SUPABASE_REGION": "ap-south-1",
    "SUPABASE_DB_PASSWORD": "not-a-real-password-8891",
    "SUPABASE_ANON_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake.anon",
    "UPSTASH_REDIS_URL": "rediss://default:not-a-real-token-4417@example-12345.upstash.io:6379",
}


def env_of(text: str) -> dict:
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


class TestRendering(unittest.TestCase):
    def files(self, values=None) -> dict:
        """Keyed by which component the file is for — the basenames collide."""
        rendered = staging_env.render(dict(values or FAKE))
        return {path.parent.name: body for path, body in rendered.items()}

    def test_it_writes_api_worker_and_web_files(self) -> None:
        paths = staging_env.render(dict(FAKE))
        names = sorted(f"{p.parent.name}/{p.name}" for p in paths)
        self.assertEqual(names, ["api/.env.staging", "web/.env.staging", "worker/.env.staging"])

    def test_the_api_config_is_actually_valid(self) -> None:
        """Rendering something the settings loader then refuses helps nobody."""
        api = env_of(self.files()["api"])
        settings = load_settings(api)
        validate_settings(settings)
        self.assertEqual(settings.storage, "postgres")
        self.assertEqual(settings.queue, "redis")
        self.assertEqual(settings.auth_mode, "supabase")
        self.assertTrue(settings.allow_remote_services)

    def test_the_worker_config_is_actually_valid(self) -> None:
        rendered = staging_env.render(dict(FAKE))
        worker_text = next(body for path, body in rendered.items() if path.parent.name == "worker")
        settings = load_worker_settings(env_of(worker_text))
        self.assertEqual(settings.runner, "careercrawler")
        self.assertEqual(settings.queue_prefix, "careercloud:staging")
        self.assertTrue(settings.allow_remote_services)

    def test_api_and_worker_share_database_redis_and_prefix(self) -> None:
        """A mismatch here is the single most common way jobs never start."""
        rendered = staging_env.render(dict(FAKE))
        api = env_of(next(b for p, b in rendered.items() if p.parent.name == "api"))
        worker = env_of(next(b for p, b in rendered.items() if p.parent.name == "worker"))
        for key in ("CAREERCLOUD_DATABASE_URL", "CAREERCLOUD_REDIS_URL", "CAREERCLOUD_QUEUE_PREFIX"):
            self.assertEqual(api[key], worker[key], f"{key} differs between API and worker")

    def test_the_database_url_uses_tls_and_the_session_pooler(self) -> None:
        url = staging_env.database_url(dict(FAKE))
        self.assertIn("sslmode=require", url)
        self.assertIn("pooler.supabase.com:5432", url)

    def test_a_password_with_url_characters_is_escaped(self) -> None:
        values = dict(FAKE, SUPABASE_DB_PASSWORD="p@ss/word:with#chars")
        url = staging_env.database_url(values)
        self.assertNotIn("p@ss/word", url)
        self.assertIn("p%40ss%2Fword", url)

    def test_the_dashboard_origin_is_allowed_by_cors(self) -> None:
        api = env_of(self.files()["api"])
        self.assertIn("https://careercrawler-staging.pages.dev", api["CAREERCLOUD_CORS_ORIGINS"])
        self.assertNotIn("*", api["CAREERCLOUD_CORS_ORIGINS"])

    def test_results_stay_local_without_storage_keys(self) -> None:
        api = env_of(self.files()["api"])
        self.assertEqual(api["CAREERCLOUD_STORAGE_BACKEND"], "local")

    def test_storage_keys_switch_the_backend_to_s3(self) -> None:
        values = dict(
            FAKE,
            SUPABASE_S3_ACCESS_KEY_ID="fake-key-id",
            SUPABASE_S3_SECRET_ACCESS_KEY="fake-secret",
            SUPABASE_STORAGE_BUCKET="careercloud-staging-results",
        )
        api = env_of(self.files(values)["api"])
        self.assertEqual(api["CAREERCLOUD_STORAGE_BACKEND"], "s3")
        self.assertEqual(api["CAREERCLOUD_RESULTS_NAMESPACE"], "staging")


class TestNothingLeaks(unittest.TestCase):
    """The whole reason this module exists."""

    def test_the_web_config_carries_only_the_anon_key(self) -> None:
        rendered = staging_env.render(dict(FAKE))
        web = next(body for path, body in rendered.items() if path.parent.name == "web")
        self.assertIn(FAKE["SUPABASE_ANON_KEY"], web)
        # Anything that would be compiled into a public bundle:
        self.assertNotIn(FAKE["SUPABASE_DB_PASSWORD"], web)
        self.assertNotIn("not-a-real-token-4417", web)
        self.assertNotIn("CAREERCLOUD_DATABASE_URL", web)
        self.assertNotIn("CAREERCLOUD_REDIS_URL", web)

    def test_mask_url_hides_the_password(self) -> None:
        masked = staging_env.mask_url(FAKE["UPSTASH_REDIS_URL"])
        self.assertNotIn("not-a-real-token-4417", masked)
        self.assertIn("example-12345.upstash.io", masked)
        self.assertIn("6379", masked)

    def test_mask_url_survives_rubbish(self) -> None:
        for value in (None, "", "not a url"):
            self.assertNotIn("Traceback", staging_env.mask_url(value))

    def test_check_reports_presence_without_values(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / ".env.staging-secrets"
            path.write_text("\n".join(f"{k}={v}" for k, v in FAKE.items()), encoding="utf-8")
            values = staging_env.read_secrets(path)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            for name in FAKE:
                print(f"{name} {staging_env._mask(values.get(name))}")
        printed = buffer.getvalue()
        for secret in FAKE.values():
            self.assertNotIn(secret, printed)
        self.assertIn("set, ", printed)


class TestValidation(unittest.TestCase):
    """Catch the paste mistakes early, and say so without quoting the value."""

    def problems(self, **overrides) -> str:
        return " ".join(staging_env._validate(dict(FAKE, **overrides)))

    def test_a_clean_set_has_no_problems(self) -> None:
        self.assertEqual(staging_env._validate(dict(FAKE)), [])

    def test_a_url_pasted_as_the_project_ref_is_caught(self) -> None:
        self.assertIn("PROJECT_REF", self.problems(SUPABASE_PROJECT_REF="https://abc.supabase.co"))

    def test_a_plaintext_redis_url_is_refused(self) -> None:
        self.assertIn("rediss://", self.problems(UPSTASH_REDIS_URL="redis://example.upstash.io:6379"))

    def test_a_redis_url_without_a_password_is_caught(self) -> None:
        self.assertIn("no password", self.problems(UPSTASH_REDIS_URL="rediss://example.upstash.io:6379"))

    def test_a_service_role_key_is_refused(self) -> None:
        """The service-role key bypasses RLS. It must never reach the API or browser."""
        problems = self.problems(SUPABASE_ANON_KEY="eyJhbGci.service_role.xxx")
        self.assertIn("service-role", problems)

    def test_a_validation_message_never_quotes_the_value(self) -> None:
        problems = self.problems(SUPABASE_PROJECT_REF="https://supersecret.supabase.co")
        self.assertNotIn("supersecret", problems)

    def test_missing_required_fields_are_listed(self) -> None:
        absent = staging_env.missing({"SUPABASE_PROJECT_REF": "abcdefghijklmnopqrst"})
        self.assertIn("UPSTASH_REDIS_URL", absent)
        self.assertNotIn("SUPABASE_PROJECT_REF", absent)
        self.assertNotIn("SUPABASE_S3_ACCESS_KEY_ID", absent, "optional fields are not required")

    def test_placeholders_are_treated_as_unset(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "secrets"
            path.write_text("SUPABASE_PROJECT_REF=<staging-project-ref>\n", encoding="utf-8")
            self.assertEqual(staging_env.read_secrets(path), {})


class TestSecretsFileIsIgnored(unittest.TestCase):
    def test_the_generated_paths_are_all_git_ignored(self) -> None:
        """A rendered file must never become committable."""
        import subprocess

        paths = [staging_env.SECRETS_FILE, *staging_env.render(dict(FAKE))]
        for path in paths:
            result = subprocess.run(
                ["git", "check-ignore", "-q", str(path)],
                cwd=str(staging_env.REPO),
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, f"{path} is NOT git-ignored")


if __name__ == "__main__":
    unittest.main()
