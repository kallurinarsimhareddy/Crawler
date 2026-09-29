"""Uninstall SANA GTM (Settings > Apps, or the control panel's Uninstall button).

    pythonw uninstall.py [--quiet] [--confirmed] [--keep-config] [--keep-data] [--dry-run] [--json]

Stops the supervisor and services, removes the startup task, shortcuts and the
Apps & features entry, withdraws this PC's API address from the sanagtm.pages.dev
proxy, then deletes the installation folder (after this process exits, by a
windowless cmd.exe).

--keep-config  copies config\\ (settings + DPAPI secrets) to
               %LOCALAPPDATA%\\SANA GTM Saved Configuration first.
--keep-data    leaves data\\ and config\\ in place (the program, logs, state, task,
               shortcuts and Apps entry are still removed).
--dry-run      prints the plan and changes NOTHING (no process is stopped, no task
               or file is removed). With --json the plan is printed as JSON.
Log: %TEMP%\\SANA-GTM-Uninstall.log
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sanagtm_common as C  # noqa: E402

P = C.installed_paths()
LOG_FILE = Path(os.environ.get("TEMP", str(P.root.parent))) / "SANA-GTM-Uninstall.log"
REMOVABLE = ("app", "bin", "config", "data", "logs", "state", "version.json", "staging", "versions", "updates")


def log(msg: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def saved_config_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SANA GTM Saved Configuration"


def build_plan(paths: "C.Paths", *, keep_config: bool = False, keep_data: bool = False) -> List[Dict[str, str]]:
    """Every step the uninstaller would take, in order. Pure: looks, never changes."""
    plan: List[Dict[str, str]] = [
        {"action": "stop_services", "target": str(paths.root)},
        {"action": "unpublish_api_origin", "target": "sanagtm.pages.dev proxy"},
        {"action": "remove_task", "target": C.TASK_NAME},
        {"action": "remove_shortcuts", "target": "Start menu + desktop"},
        {"action": "remove_apps_entry", "target": C.REG_UNINSTALL_KEY},
    ]
    if keep_config and paths.config.exists() and not keep_data:
        plan.append({"action": "copy_config", "target": str(saved_config_dir())})
    keep = {"data", "config"} if keep_data else set()
    for name in REMOVABLE:
        target = paths.root / name
        if name in keep or not target.exists():
            continue
        plan.append({"action": "delete", "target": str(target)})
    # The running interpreter (runtime\) and this script (manager\) go after this process exits.
    if keep_data:
        for name in ("runtime", "manager"):
            if (paths.root / name).exists():
                plan.append({"action": "delete_later", "target": str(paths.root / name)})
    else:
        plan.append({"action": "delete_later", "target": str(paths.root)})
    return plan


def unpublish(paths: "C.Paths") -> None:
    try:
        import redis

        settings = C.load_settings(paths)
        secrets = C.load_secrets(paths)
        r = redis.Redis.from_url(secrets["CAREERCLOUD_REDIS_URL"], socket_timeout=10, decode_responses=True)
        url = paths.tunnel_url.read_text(encoding="ascii").strip() if paths.tunnel_url.exists() else ""
        key = C.origin_key(settings)
        if url and r.get(key) == url:
            r.delete(key)
            log("withdrew this PC's API address from the proxy")
    except Exception as e:  # noqa: BLE001
        log(f"could not withdraw the API address ({type(e).__name__}); it expires within 5 minutes")


def execute(paths: "C.Paths", plan: List[Dict[str, str]], *, common=C) -> List[str]:
    """Carry out a plan from :func:`build_plan`. Returns what was done."""
    done = []
    me = os.getpid()
    for step in plan:
        action, target = step["action"], step["target"]
        try:
            if action == "stop_services":
                common.stop_everything(paths)
                for p in common.owned_processes(paths):
                    if p.pid != me:
                        common.kill_tree(p)
            elif action == "unpublish_api_origin":
                unpublish(paths)
            elif action == "remove_task":
                common.unregister_task()
            elif action == "remove_shortcuts":
                common.remove_shortcuts()
            elif action == "remove_apps_entry":
                common.unregister_uninstall()
            elif action == "copy_config":
                shutil.copytree(paths.config, target, dirs_exist_ok=True)
            elif action == "delete":
                t = Path(target)
                if t.is_dir():
                    shutil.rmtree(t)
                elif t.exists():
                    t.unlink()
            elif action == "delete_later":
                cmd = (f'for /l %i in (1,1,15) do @(if exist "{target}" (ping -n 2 127.0.0.1 >nul & '
                       f'rd /s /q "{target}" 2>nul))')
                common.spawn_hidden(["cmd.exe", "/d", "/c", cmd], cwd=os.environ.get("TEMP", "C:\\"))
            done.append(f"{action} {target}")
            log(f"ok {action} {target}")
        except OSError as e:
            log(f"will retry {action} {target}: {type(e).__name__}")
    return done


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--confirmed", action="store_true")
    ap.add_argument("--keep-config", action="store_true")
    ap.add_argument("--keep-data", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    plan = build_plan(P, keep_config=args.keep_config, keep_data=args.keep_data)
    if args.dry_run:
        if args.json:
            print(json.dumps({"dry_run": True, "plan": plan}, indent=2))
        else:
            print("SANA GTM uninstall -- dry run (nothing will be changed):")
            for step in plan:
                print(f"  {step['action']:<22} {step['target']}")
        return 0

    if not args.quiet and not args.confirmed:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        ok = messagebox.askyesno("Uninstall SANA GTM", "Remove SANA GTM from this PC?\n\nThis stops the services "
                                 "and deletes the program, its configuration and its logs.", icon="warning")
        root.destroy()
        if not ok:
            return 1

    log(f"uninstalling {P.root} (keep_config={args.keep_config}, keep_data={args.keep_data})")
    execute(P, plan)

    if not args.quiet:
        import tkinter as tk
        from tkinter import messagebox

        root_w = tk.Tk()
        root_w.withdraw()
        messagebox.showinfo("SANA GTM", "SANA GTM was removed from this PC.")
        root_w.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
