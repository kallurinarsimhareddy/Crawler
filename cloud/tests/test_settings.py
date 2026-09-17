"""API settings come from the environment, and bad values fail at startup."""

from __future__ import annotations

import unittest

from cloud.api.settings import Settings, load_settings


class TestLoadSettings(unittest.TestCase):
    def test_an_empty_environment_gives_the_defaults(self) -> None:
        self.assertEqual(load_settings({}), Settings())

    def test_values_are_read(self) -> None:
        settings = load_settings(
            {
                "CAREERCLOUD_ENV": "staging",
                "CAREERCLOUD_RUNNER": "NONE",
                "CAREERCLOUD_CORS_ORIGINS": "https://app.example.com, https://admin.example.com",
                "CAREERCLOUD_FAKE_STEP_SECONDS": "0.5",
                "CAREERCLOUD_MAX_CONCURRENT_JOBS": "4",
            }
        )
        self.assertEqual(settings.environment, "staging")
        self.assertEqual(settings.runner, "none")
        self.assertEqual(settings.cors_origins, ("https://app.example.com", "https://admin.example.com"))
        self.assertEqual(settings.fake_step_seconds, 0.5)
        self.assertEqual(settings.max_concurrent_jobs, 4)

    def test_unusable_values_are_refused(self) -> None:
        for env in (
            {"CAREERCLOUD_RUNNER": "careercrawler"},
            {"CAREERCLOUD_CORS_ORIGINS": "*"},
            {"CAREERCLOUD_FAKE_STEP_SECONDS": "-1"},
            {"CAREERCLOUD_FAKE_STEP_SECONDS": "soon"},
            {"CAREERCLOUD_MAX_CONCURRENT_JOBS": "0"},
        ):
            with self.subTest(env=env), self.assertRaises(ValueError):
                load_settings(env)

    def test_phase_5b_placeholders_are_flagged_as_having_no_effect(self) -> None:
        with self.assertLogs("cloud.api.settings", level="WARNING") as logs:
            settings = load_settings(
                {"CAREERCLOUD_DATABASE_URL": "postgresql://u:secret@db/x", "CAREERCLOUD_REDIS_URL": " "}
            )
        self.assertEqual(settings.unused_placeholders(), ["CAREERCLOUD_DATABASE_URL"])
        self.assertIn("no effect", logs.output[0])

    def test_secrets_are_kept_out_of_repr(self) -> None:
        settings = Settings(database_url="postgresql://u:secret@db/x", supabase_service_role_key="sk")
        self.assertNotIn("secret", repr(settings))
        self.assertNotIn("'sk'", repr(settings))


if __name__ == "__main__":
    unittest.main()
