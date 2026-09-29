"""The events that drive workflows are actually emitted by the services that own them."""

from __future__ import annotations

import unittest

from cloud.intel.tasks.worker import run_task_inline
from cloud.tests.test_platform_email_jobs import make_platform


class TestWorkflowTriggers(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, _ = make_platform()
        self.automation = self.platform.service("automation")  # RecordingAutomation

    def triggers(self):
        return [event[0] for event in self.automation.events]

    def test_a_completed_validation_job_emits_validation_job_completed(self) -> None:
        jobs = self.platform.service("email_jobs")
        job = jobs.create_from_rows(self.ctx, name="t", rows=[{"email": "a@example.com"}], email_field="email",
                                    source_type="manual")
        job = jobs.start(self.ctx, job["id"])
        run_task_inline(self.platform, self.ctx.workspace_id, job["task_id"])
        self.assertIn("validation_job_completed", self.triggers())

    def test_a_recorded_reply_emits_reply_received(self) -> None:
        self.platform.store.insert(self.ctx, "contacts", {"full_name": "Ada Lovelace", "email": "ada@example.com"})
        self.platform.service("events").record_manual(self.ctx, "reply", email="ada@example.com")
        self.assertIn("reply_received", self.triggers())


if __name__ == "__main__":
    unittest.main()
