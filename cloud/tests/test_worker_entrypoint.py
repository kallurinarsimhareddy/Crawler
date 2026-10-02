"""The worker's ``python -m`` entry point must use the canonical module, so the task-control
exceptions handlers raise (TaskPaused, TaskCancelled, PermanentTaskError) are the classes the
worker loop catches."""

from __future__ import annotations

import runpy
import sys
import unittest
from unittest import mock


class WorkerEntrypointTests(unittest.TestCase):
    def test_python_m_runs_the_canonical_module(self) -> None:
        import cloud.intel.tasks.worker as canonical

        with mock.patch.object(canonical, "main", return_value=0) as main,                 mock.patch.object(sys, "argv", ["worker"]):
            with self.assertRaises(SystemExit) as stop:
                runpy.run_module("cloud.intel.tasks.worker", run_name="__main__")
        self.assertEqual(stop.exception.code, 0)
        main.assert_called_once_with()          # the canonical main ran, not a __main__ copy

    def test_handler_exceptions_are_the_ones_the_loop_catches(self) -> None:
        import cloud.intel.tasks.worker as canonical
        from cloud.intel.job_monitor import runner

        self.assertIs(runner.__dict__.get("TaskPaused", canonical.TaskPaused), canonical.TaskPaused)
        from cloud.intel.tasks.worker import PermanentTaskError, TaskCancelled, TaskPaused

        self.assertIs(TaskPaused, canonical.TaskPaused)
        self.assertIs(TaskCancelled, canonical.TaskCancelled)
        self.assertIs(PermanentTaskError, canonical.PermanentTaskError)


if __name__ == "__main__":
    unittest.main()
