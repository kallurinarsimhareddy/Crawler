"""Inspect and change a job source monitor's schedule settings with a snapshot first and an exact
rollback. Dry run unless --apply. Changes go through JobMonitorService.update_monitor (audited,
optimistic version check) — never a raw UPDATE.

    # read-only: print + save the monitor's schedule state
    python deploy/ops/prod-safety/monitor_settings.py snapshot --env-file cloud/worker/.env.sana-cloud \
        --monitor jmon_ec14fe0b… --out E:/SanaBackups/monitor-before.json
    # switch automatic full sweeps off (sets next_full_sweep_at NULL; nothing else)
    python deploy/ops/prod-safety/monitor_settings.py auto-sweep off --env-file … --workspace … --monitor … \
        --snapshot E:/SanaBackups/monitor-before.json [--apply]
    # rollback: put next_full_sweep_at back to the exact snapshot value
    python deploy/ops/prod-safety/monitor_settings.py restore --env-file … --workspace … --monitor … \
        --snapshot E:/SanaBackups/monitor-before.json [--apply]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

#: The settings this tool reports, snapshots and may restore.
FIELDS = ("name", "enabled", "schedule", "next_run_at", "next_full_sweep_at", "full_sweep_days", "stale_after_days",
          "visible_window_days", "gone_checks_per_run", "close_after_missed", "max_pages_incremental",
          "max_pages_full", "last_run_at", "last_run_status", "last_full_sweep_at", "version", "updated_at")


def load_env(env_file: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    env.pop("CAREERCLOUD_QUEUE", None)  # this tool never touches the queue (no LISTEN connection)
    env["CAREERCLOUD_DB_POOL_MAX"] = "1"
    return env


def connect(env_file: Path):
    from cloud.intel.bootstrap import build_platform

    return build_platform(role="ops", env=load_env(env_file))


def state(platform: Any, ctx: Any, monitor_id: str) -> Dict[str, Any]:
    row = platform.store.get(ctx, "job_source_monitors", monitor_id)
    out = {k: row.get(k) for k in FIELDS}
    out["id"], out["workspace_id"] = row["id"], row["workspace_id"]
    out["auto_full_sweep"] = row.get("next_full_sweep_at") is not None
    active = platform.service("job_monitors").active_run(ctx, monitor_id)
    out["active_run"] = None if active is None else {k: active.get(k) for k in ("id", "mode", "status", "pages",
                                                                                "found", "task_id")}
    return out


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v))


def snapshot(platform: Any, ctx: Any, monitor_id: str, out: Optional[Path]) -> Dict[str, Any]:
    current = state(platform, ctx, monitor_id)
    current["snapshot_at"] = datetime.now().astimezone().isoformat()
    if out is not None:
        if out.exists():
            raise SystemExit(f"{out} exists; snapshots are never overwritten")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json(current), encoding="utf-8")
    return current


def set_auto_sweep(platform: Any, ctx: Any, monitor_id: str, on: bool, *, snapshot_file: Path,
                   apply: bool) -> Dict[str, Any]:
    before = snapshot(platform, ctx, monitor_id, snapshot_file if apply else None)
    plan = {"auto_full_sweep": on}
    print(f"monitor {before['name']}: auto_full_sweep {before['auto_full_sweep']} -> {on}; "
          f"next_full_sweep_at {before['next_full_sweep_at']} -> {'now + full_sweep_days' if on else None}")
    if not apply:
        print("dry run: nothing changed (add --apply; a snapshot is written first)")
        return before
    platform.service("job_monitors").update_monitor(ctx, monitor_id, plan)
    after = state(platform, ctx, monitor_id)
    changed = sorted(k for k in FIELDS if k not in ("version", "updated_at") and before.get(k) != after.get(k))
    # switching on always recomputes the date; switching off changes it unless it was already off
    expected = ["next_full_sweep_at"] if (on or before["auto_full_sweep"]) else []
    if changed != expected:
        raise SystemExit(f"UNEXPECTED: changed {changed}, expected {expected}. Roll back with `restore`.")
    print(f"applied (version {before['version']} -> {after['version']}); snapshot kept at {snapshot_file}")
    return after


def restore(platform: Any, ctx: Any, monitor_id: str, *, snapshot_file: Path, apply: bool) -> Dict[str, Any]:
    from cloud.intel.job_monitor.service import audit

    saved = json.loads(snapshot_file.read_text(encoding="utf-8"))
    if saved["id"] != monitor_id:
        raise SystemExit("the snapshot belongs to another monitor")
    target = saved["next_full_sweep_at"]
    target_dt = datetime.fromisoformat(target) if target else None
    current = state(platform, ctx, monitor_id)
    print(f"restore next_full_sweep_at {current['next_full_sweep_at']} -> {target}")
    if not apply:
        print("dry run: nothing changed (add --apply)")
        return current
    platform.store.update(ctx, "job_source_monitors", monitor_id, {"next_full_sweep_at": target_dt},
                          expected_version=current["version"])
    audit(platform.store, ctx, "job_monitor.updated", entity_type="job_source_monitors", entity_id=monitor_id,
          summary=f"{current['name']}: next_full_sweep_at restored from snapshot")
    return state(platform, ctx, monitor_id)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("snapshot", "auto-sweep", "restore"))
    parser.add_argument("value", nargs="?", choices=("on", "off"))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--monitor", required=True)
    parser.add_argument("--workspace", required=True, help="the monitor's workspace id")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    from cloud.intel.core.context import Ctx

    platform = connect(args.env_file)
    try:
        ctx = Ctx.for_system(args.workspace, actor_kind="ops")
        if args.command == "snapshot":
            print(_json(snapshot(platform, ctx, args.monitor, args.out)))
        elif args.command == "auto-sweep":
            if args.value is None or args.snapshot is None:
                parser.error("auto-sweep needs on|off and --snapshot")
            set_auto_sweep(platform, ctx, args.monitor, args.value == "on", snapshot_file=args.snapshot,
                           apply=args.apply)
        else:
            if args.snapshot is None:
                parser.error("restore needs --snapshot")
            print(_json(restore(platform, ctx, args.monitor, snapshot_file=args.snapshot, apply=args.apply)))
    finally:
        platform.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
