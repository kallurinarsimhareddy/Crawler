"""Uninstall SANA GTM (Settings > Apps, or the control panel's Uninstall button).

    pythonw uninstall.py [--quiet] [--confirmed] [--keep-config]

Stops the supervisor and services, removes the startup task, shortcuts and the
Apps & features entry, withdraws this PC's API address from the sanagtm.pages.dev
proxy, then deletes the installation folder (after this process exits, by a
windowless cmd.exe). --keep-config first copies config\\ (settings + DPAPI
secrets) to %LOCALAPPDATA%\\SANA GTM Saved Configuration.
Log: %TEMP%\\SANA-GTM-Uninstall.log
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sanagtm_common as C  # noqa: E402

P = C.installed_paths()
LOG_FILE = Path(os.environ.get("TEMP", str(P.root.parent))) / "SANA-GTM-Uninstall.log"


def log(msg: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def unpublish() -> None:
    try:
        import redis

        settings = C.load_settings(P)
        secrets = C.load_secrets(P)
        r = redis.Redis.from_url(secrets["CAREERCLOUD_REDIS_URL"], socket_timeout=10, decode_responses=True)
        url = P.tunnel_url.read_text(encoding="ascii").strip() if P.tunnel_url.exists() else ""
        key = C.origin_key(settings)
        if url and r.get(key) == url:
            r.delete(key)
            log("withdrew this PC's API address from the proxy")
    except Exception as e:  # noqa: BLE001
        log(f"could not withdraw the API address ({type(e).__name__}); it expires within 5 minutes")


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--confirmed", action="store_true")
    ap.add_argument("--keep-config", action="store_true")
    args = ap.parse_args(argv)

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

    log(f"uninstalling {P.root}")
    C.stop_everything(P)
    unpublish()
    me = os.getpid()
    for p in C.owned_processes(P):
        if p.pid != me:
            C.kill_tree(p)
    log("services stopped")
    C.unregister_task()
    C.remove_shortcuts()
    C.unregister_uninstall()
    log("startup task, shortcuts and Apps entry removed")
    if args.keep_config and P.config.exists():
        dest = Path(os.environ["LOCALAPPDATA"]) / "SANA GTM Saved Configuration"
        shutil.copytree(P.config, dest, dirs_exist_ok=True)
        log(f"configuration kept in {dest}")

    # Everything except the running interpreter can go now; cmd.exe removes the rest
    # once this process has exited.
    for name in ("app", "bin", "config", "data", "logs", "state", "version.json"):
        target = P.root / name
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError as e:
            log(f"will retry {name}: {type(e).__name__}")
    root = str(P.root)
    cmd = (f'for /l %i in (1,1,15) do @(if exist "{root}" (ping -n 2 127.0.0.1 >nul & rd /s /q "{root}" 2>nul))')
    C.spawn_hidden(["cmd.exe", "/d", "/c", cmd], cwd=os.environ.get("TEMP", "C:\\"))
    log("folder removal scheduled")

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
