"""SANA GTM health check (also used for the post-reboot test).

    runtime\\python.exe manager\\verify.py [--wait SECONDS] [--out FILE] [--json]

Checks, as a structured report (``{"ok", "checks": [{"name", "passed", "detail",
"category"}], ...}``):

* install:  private runtime present, app present, config decryptable (secrets names
  only -- never values), startup task registered;
* processes: supervisor running (exactly one), no duplicates;
* network:  API port listening, API local /api/v1/health, worker heartbeat, tunnel
  public health, API origin published, sanagtm.pages.dev loads and its /api proxy
  reaches THIS PC's API;
* windows:  no SANA GTM process owns a visible window (no CMD / PowerShell windows).

Exit code 0 = everything passed. Every probe is injectable (:class:`Probes`) so the
report logic is tested without services, network or Windows APIs.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sanagtm_common as C  # noqa: E402

P = C.installed_paths()


def get(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sana-gtm-verify", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def visible_windows():
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            out.append((pid.value, cls.value, title.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


class Probes:
    """Everything verify.py observes. Tests pass fakes."""

    def __init__(self, *, http_get: Callable = get, port_open: Callable = port_listening,
                 processes: Optional[Callable] = None, task_exists: Callable = C.task_exists,
                 windows: Callable = visible_windows, status: Optional[Callable] = None,
                 secrets_status: Callable = C.secrets_status, settings: Optional[Callable] = None):
        self.http_get = http_get
        self.port_open = port_open
        self.processes = processes or C.owned_processes
        self.task_exists = task_exists
        self.windows = windows
        self.status = status or C.read_status
        self.secrets_status = secrets_status
        self.settings = settings or C.load_settings


def run_checks(paths: "C.Paths", probes: Optional[Probes] = None) -> Dict:
    probes = probes or Probes()
    checks: List[Dict] = []

    def check(category, name, passed, detail=""):
        checks.append({"category": category, "name": name, "passed": bool(passed), "detail": detail})

    st = probes.status(paths)
    settings = probes.settings(paths)
    procs = probes.processes(paths)
    kinds: Dict[str, List[int]] = {}
    for p in procs:
        kinds.setdefault(C.classify(p), []).append(p.pid)

    # install
    check("install", "private Python runtime present", paths.python.exists() and paths.pythonw.exists(),
          str(paths.runtime))
    check("install", "SANA GTM app present", (paths.app / "cloud").exists(), str(paths.app))
    sec = probes.secrets_status(paths)
    if not sec["decryptable"]:
        check("install", "configuration decryptable (DPAPI, this Windows user)", False, sec.get("error", ""))
    else:
        check("install", "configuration decryptable (DPAPI, this Windows user)", not sec["missing"],
              "missing: " + ", ".join(sec["missing"]) if sec["missing"] else f"{len(sec['present'])} secrets present")
    check("install", "startup task registered", probes.task_exists(), C.TASK_NAME)

    # processes
    check("processes", "supervisor running (exactly one)", len(kinds.get("supervisor", [])) == 1,
          str(kinds.get("supervisor")))
    for k in ("api", "worker", "tunnel", "supervisor"):
        if len(kinds.get(k, [])) > 1:
            check("processes", f"no duplicate {k} processes", False, f"{len(kinds[k])} running")

    # network
    port = int(settings.get("api_port", 8100))
    check("network", f"API port {port} listening", probes.port_open(port), f"127.0.0.1:{port}")
    code, _ = probes.http_get(f"http://127.0.0.1:{port}/api/v1/health", 10)
    check("network", "API: ONLINE (local /api/v1/health)", code == 200, f"HTTP {code}, pids {kinds.get('api')}")
    w = st.get("worker", {})
    check("network", "Worker: ONLINE (heartbeat)", w.get("online"),
          f"heartbeat {w.get('heartbeat_age_s')} s ago, pids {kinds.get('worker')}")
    url = (st.get("tunnel") or {}).get("url", "")
    code, _ = probes.http_get(url + "/api/v1/health") if url else (None, "")
    check("network", "Tunnel: ONLINE (public health)", code == 200, f"{url or 'no tunnel URL'} HTTP {code}")
    check("network", "API origin published for sanagtm.pages.dev", (st.get("tunnel") or {}).get("published"))
    front = (settings.get("frontend_url") or C.FRONTEND_URL).rstrip("/")
    code, _ = probes.http_get(front + "/")
    check("network", "sanagtm.pages.dev loads", code == 200, f"HTTP {code}")
    code, body = probes.http_get(front + "/api/v1/health")
    check("network", "sanagtm.pages.dev/api reaches this PC's API", code == 200, f"HTTP {code} {body[:120]}")
    check("network", "Overall: READY", st.get("overall") == "READY", st.get("overall", "?"))

    # windows
    all_windows = probes.windows()
    ours = {p.pid for p in procs if C.classify(p) != "panel"}
    shown = [wdw for wdw in all_windows if wdw[0] in ours]
    check("windows", "no visible window from any SANA GTM process", not shown, repr(shown) if shown else "")
    consoles = [wdw for wdw in all_windows
                if wdw[1] in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS") and wdw[2].strip()]
    suspicious = [wdw for wdw in consoles if any(s in wdw[2].lower() for s in
                                                 ("python", "cloudflared", "uvicorn", "sana", "supervisor"))]
    check("windows", "no CMD / PowerShell / Terminal window running SANA GTM", not suspicious,
          repr(suspicious) if suspicious else "")

    return {"ok": all(c["passed"] for c in checks), "install": str(paths.root),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": checks,
            "failed": [c["name"] for c in checks if not c["passed"]]}


def format_report(report: Dict) -> str:
    lines = [f"SANA GTM verify  {report['time']}  install {report['install']}"]
    for c in report["checks"]:
        lines.append(f"{'PASS' if c['passed'] else 'FAIL'}  {c['name']}{('  -- ' + c['detail']) if c['detail'] else ''}")
    lines.append("RESULT: " + ("ALL PASS" if report["ok"] else "FAILED"))
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=0, help="wait up to N seconds for READY first")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    deadline = time.time() + args.wait
    while args.wait and time.time() < deadline and C.read_status(P).get("overall") != "READY":
        time.sleep(5)
    report = run_checks(P)
    text = json.dumps(report, indent=2) if args.json else format_report(report)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
