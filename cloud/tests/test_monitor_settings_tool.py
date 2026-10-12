"""deploy/ops/prod-safety/monitor_settings.py: snapshot → switch auto sweeps off → exact rollback."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

from cloud.intel.core.context import Ctx
from cloud.tests.test_job_monitor import _Base

TOOL = Path(__file__).resolve().parents[2] / "deploy" / "ops" / "prod-safety" / "monitor_settings.py"
spec = importlib.util.spec_from_file_location("monitor_settings", TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class MonitorSettingsTool(_Base):
    def setUp(self) -> None:
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.snap = Path(scratch.name) / "before.json"
        self.mon = self.monitor()
        self.ops = Ctx.for_system(self.ctx.workspace_id, actor_kind="ops")

    def test_dry_run_changes_nothing_and_writes_no_snapshot(self) -> None:
        tool.set_auto_sweep(self.platform, self.ops, self.mon["id"], False, snapshot_file=self.snap, apply=False)
        row = self.store.get(self.ctx, "job_source_monitors", self.mon["id"])
        self.assertEqual((row["next_full_sweep_at"], row["version"]), (self.mon["next_full_sweep_at"], self.mon["version"]))
        self.assertFalse(self.snap.exists())

    def test_off_then_exact_restore(self) -> None:
        original = self.mon["next_full_sweep_at"]
        after = tool.set_auto_sweep(self.platform, self.ops, self.mon["id"], False, snapshot_file=self.snap, apply=True)
        self.assertIsNone(after["next_full_sweep_at"])
        self.assertFalse(after["auto_full_sweep"])
        saved = json.loads(self.snap.read_text(encoding="utf-8"))
        self.assertTrue(saved["auto_full_sweep"])
        self.assertEqual(saved["stale_after_days"], self.mon["stale_after_days"])
        audits = self.store.all(self.ctx, "audit_log", {"action": "job_monitor.updated"})
        self.assertTrue(any("next_full_sweep_at" in (a.get("summary") or "") for a in audits))
        with self.assertRaises(SystemExit):                         # snapshots are never overwritten
            tool.snapshot(self.platform, self.ops, self.mon["id"], self.snap)
        restored = tool.restore(self.platform, self.ops, self.mon["id"], snapshot_file=self.snap, apply=True)
        self.assertEqual(restored["next_full_sweep_at"], original)

    def test_switching_off_twice_is_a_no_op(self) -> None:
        tool.set_auto_sweep(self.platform, self.ops, self.mon["id"], False, snapshot_file=self.snap, apply=True)
        again = tool.set_auto_sweep(self.platform, self.ops, self.mon["id"], False,
                                    snapshot_file=self.snap.with_name("again.json"), apply=True)
        self.assertIsNone(again["next_full_sweep_at"])
