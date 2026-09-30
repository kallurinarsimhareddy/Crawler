r"""Headless logic of the SANA GTM control panel (control_panel.pyw is only the Tk view).

* :func:`compute_view` turns state\status.json + "is the supervisor alive" into the
  labels, colours and detail text the panel shows. Pure; unit-tested.
* :class:`PanelActions` maps every button (Start, Stop, Restart, Status, Open SANA GTM,
  View Logs, Update, Uninstall) to the manager functions in sanagtm_common. Its
  side effects (process start, browser, Explorer) are injectable so tests run headless.

Update looks for a newer SANA-GTM-Setup*.exe in <install>\updates\ (or the folder in
settings "update_source"); it never downloads anything by itself.
"""

from __future__ import annotations

import json
import os
import re
import time
import webbrowser
from pathlib import Path
from typing import Callable, Dict, List, Optional

import sanagtm_common as C

__all__ = ["GREEN", "RED", "AMBER", "GREY", "compute_view", "PanelActions", "BUTTONS", "find_update"]

GREEN, RED, AMBER, GREY = "#1a7f37", "#cf222e", "#9a6700", "#57606a"
#: (label, action name) in the order the panel shows them.
BUTTONS = (("Start", "start"), ("Stop", "stop"), ("Restart", "restart"), ("Open SANA GTM", "open_site"),
           ("View Logs", "view_logs"), ("Update", "update"), ("Uninstall", "uninstall"))
COMPONENTS = (("API", "api"), ("Worker", "worker"), ("Tunnel", "tunnel"))


def compute_view(status: Dict, supervisor_alive: bool, stop_requested: bool, now: Optional[float] = None) -> Dict:
    """``{"rows": {"API": (text, colour), ...,"Overall": ...}, "detail": str}``."""
    now = time.time() if now is None else now
    rows: Dict[str, tuple] = {}
    stale = now - float(status.get("updated", 0) or 0) > 60
    if not supervisor_alive:
        for name, _ in COMPONENTS:
            rows[name] = ("OFFLINE", RED)
        if status.get("overall") == "NOT CONFIGURED":
            rows["Overall"] = ("NOT CONFIGURED", RED)
        else:
            stopped = stop_requested or status.get("overall") == "STOPPED"
            rows["Overall"] = ("STOPPED" if stopped else "NOT RUNNING", RED)
        detail = status.get("error") or "Press Start to start SANA GTM."
        return {"rows": rows, "detail": detail}
    if stale or not status.get("api"):
        for name, _ in COMPONENTS:
            rows[name] = ("STARTING", AMBER)
        rows["Overall"] = ("STARTING", AMBER)
        return {"rows": rows, "detail": "The supervisor is starting the services..."}
    for name, key in COMPONENTS:
        comp = status.get(key) or {}
        if comp.get("online"):
            rows[name] = ("ONLINE", GREEN)
        elif comp.get("running"):
            rows[name] = ("STARTING", AMBER)
        else:
            rows[name] = ("OFFLINE", RED)
    ready = status.get("overall") == "READY"
    rows["Overall"] = ("READY", GREEN) if ready else ("NOT READY", AMBER)
    notes = []
    if status.get("database") is False:
        notes.append("Database (Supabase) not reachable.")
    if status.get("queue") is False:
        notes.append("Queue (Upstash) not reachable.")
    url = (status.get("tunnel") or {}).get("url")
    if url:
        notes.append("Tunnel: " + url)
    return {"rows": rows, "detail": "\n".join(notes)}


def _version_key(path: Path):
    nums = re.findall(r"\d+", path.stem)
    return tuple(int(n) for n in nums) or (0,)


def find_update(paths: "C.Paths", settings: Dict) -> Optional[Path]:
    """The newest setup program offered for an update, if its version is newer."""
    folders = [paths.root / "updates"]
    if settings.get("update_source"):
        folders.insert(0, Path(settings["update_source"]))
    candidates: List[Path] = []
    for folder in folders:
        if folder.is_dir():
            candidates += [p for p in folder.glob("SANA-GTM-Setup*.exe") if p.is_file()]
    if not candidates:
        return None
    best = max(candidates, key=_version_key)
    try:
        current = json.loads(paths.version.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        current = ""
    cur = tuple(int(n) for n in re.findall(r"\d+", current))
    if cur and re.findall(r"\d+", best.stem) and _version_key(best) <= cur:
        return None
    return best


class PanelActions:
    def __init__(self, paths: "C.Paths", *, common=C, open_url: Callable[[str], object] = webbrowser.open,
                 open_folder: Optional[Callable[[str], object]] = None, sleep: Callable[[float], None] = time.sleep):
        self.paths = paths
        self.C = common
        self.open_url = open_url
        self.open_folder = open_folder or (lambda p: os.startfile(p))  # type: ignore[attr-defined]
        self.sleep = sleep

    def run(self, name: str) -> str:
        if name not in {a for _, a in BUTTONS} | {"status"}:
            raise ValueError(f"unknown panel action {name!r}")
        return getattr(self, name)() or ""

    def supervisor_alive(self) -> bool:
        return any(self.C.classify(p) == "supervisor" for p in self.C.owned_processes(self.paths))

    def status(self) -> str:
        view = compute_view(self.C.read_status(self.paths), self.supervisor_alive(),
                            self.C.stop_requested(self.paths))
        return view["rows"]["Overall"][0]

    def start(self) -> str:
        self.C.start_supervisor(self.paths)
        return "starting"

    def stop(self) -> str:
        self.C.stop_everything(self.paths)
        return "stopped"

    def restart(self) -> str:
        self.C.stop_everything(self.paths)
        self.sleep(1)
        self.C.start_supervisor(self.paths)
        return "restarting"

    def open_site(self) -> str:
        url = self.C.load_settings(self.paths).get("frontend_url") or self.C.FRONTEND_URL
        self.open_url(url)
        return url

    def view_logs(self) -> str:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        self.open_folder(str(self.paths.logs))
        return str(self.paths.logs)

    def update(self) -> str:
        setup = find_update(self.paths, self.C.load_settings(self.paths))
        if setup is None:
            return "No newer SANA GTM setup found in " + str(self.paths.root / "updates")
        self.C.spawn_hidden([str(setup)], cwd=str(setup.parent))
        return "running " + setup.name

    def uninstall(self) -> str:
        self.C.spawn_hidden([str(self.paths.pythonw), str(self.paths.manager / "uninstall.py"), "--confirmed"],
                            cwd=os.environ.get("TEMP"))
        return "uninstalling"
