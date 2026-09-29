"""SANA GTM health check (also used for the post-reboot test).

    runtime\\python.exe manager\\verify.py [--wait SECONDS] [--out FILE]

Checks: supervisor running, API local health, worker heartbeat, tunnel public
health, the sanagtm.pages.dev proxy reaching THIS PC's API, the startup task, and
that no SANA GTM process owns a visible window (no CMD / PowerShell windows).
Prints a report (and writes it to --out); exit code 0 = everything passed.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

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


def visible_windows():
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


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=0, help="wait up to N seconds for READY first")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    deadline = time.time() + args.wait
    while args.wait and time.time() < deadline and C.read_status(P).get("overall") != "READY":
        time.sleep(5)

    lines, ok = [], True

    def check(name, passed, detail=""):
        nonlocal ok
        ok = ok and bool(passed)
        lines.append(f"{'PASS' if passed else 'FAIL'}  {name}{('  -- ' + detail) if detail else ''}")

    st = C.read_status(P)
    procs = C.owned_processes(P)
    kinds = {}
    for p in procs:
        kinds.setdefault(C.classify(p), []).append(p.pid)
    settings = C.load_settings(P)
    lines.append(f"SANA GTM verify  {time.strftime('%Y-%m-%d %H:%M:%S')}  install {P.root}")
    up = ctypes.windll.kernel32.GetTickCount64() / 1000
    lines.append(f"Windows up {up / 60:.1f} min; status updated {time.time() - float(st.get('updated', 0) or 0):.0f} s ago")
    check("startup task registered", C.task_exists(), C.TASK_NAME)
    check("supervisor running (exactly one)", len(kinds.get("supervisor", [])) == 1, str(kinds.get("supervisor")))
    code, _ = get(f"http://127.0.0.1:{settings.get('api_port', 8100)}/api/v1/health", 10)
    check("API: ONLINE (local /api/v1/health)", code == 200, f"HTTP {code}, pids {kinds.get('api')}")
    w = st.get("worker", {})
    check("Worker: ONLINE (heartbeat)", w.get("online"), f"heartbeat {w.get('heartbeat_age_s')} s ago, pids {kinds.get('worker')}")
    url = st.get("tunnel", {}).get("url", "")
    code, _ = get(url + "/api/v1/health") if url else (None, "")
    check("Tunnel: ONLINE (public health)", code == 200, f"{url} HTTP {code}, pids {kinds.get('tunnel')}")
    check("API origin published for sanagtm.pages.dev", st.get("tunnel", {}).get("published"))
    front = (settings.get("frontend_url") or C.FRONTEND_URL).rstrip("/")
    code, _ = get(front + "/")
    check("sanagtm.pages.dev loads", code == 200, f"HTTP {code}")
    code, body = get(front + "/api/v1/health")
    check("sanagtm.pages.dev/api reaches this PC's API", code == 200, f"HTTP {code} {body[:120]}")
    for k in ("api", "worker", "tunnel", "supervisor"):
        n = len(kinds.get(k, []))
        if n > 1:
            check(f"no duplicate {k} processes", False, f"{n} running")
    check("Overall: READY", st.get("overall") == "READY", st.get("overall", "?"))

    ours = {p.pid for p in procs if C.classify(p) != "panel"}
    shown = [wdw for wdw in visible_windows() if wdw[0] in ours]
    check("no visible window from any SANA GTM process", not shown, repr(shown) if shown else "")
    consoles = [wdw for wdw in visible_windows()
                if wdw[1] in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS") and wdw[2].strip()]
    suspicious = [wdw for wdw in consoles if any(s in wdw[2].lower() for s in
                                                 ("python", "cloudflared", "uvicorn", "sana", "supervisor"))]
    check("no CMD / PowerShell / Terminal window running SANA GTM", not suspicious, repr(suspicious) if suspicious else "")
    lines.append("Console/terminal windows open (any program): " + (repr([c[2] for c in consoles]) or "none"))
    lines.append("RESULT: " + ("ALL PASS" if ok else "FAILED"))
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
