"""Start, stop, kill and restart a LOCAL CareerCloud stack for rehearsals.

    python -m cloud.ops.local_stack start-api     --env-file <file> [--port 8000]
    python -m cloud.ops.local_stack start-worker  --env-file <file>
    python -m cloud.ops.local_stack kill-worker    # hard kill: a crash, no shutdown handler
    python -m cloud.ops.local_stack stop-worker    # graceful (SIGTERM / CTRL_BREAK)
    python -m cloud.ops.local_stack restart-worker --env-file <file>
    python -m cloud.ops.local_stack restart-api    --env-file <file>
    python -m cloud.ops.local_stack status | stop-all

It only ever signals processes it started itself (PIDs in
``cloud/.localdev/run``), each re-checked against its command line before being
signalled. Refuses any environment other than ``development``. On a staging VM,
use systemctl instead.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

REPO = Path(__file__).resolve().parent.parent.parent
RUN = REPO / "cloud" / ".localdev" / "run"
PYTHON = sys.executable


def _pidfile(name: str) -> Path:
    return RUN / f"{name}.pid"


def _ours(name: str):
    import psutil

    path = _pidfile(name)
    if not path.exists():
        return None
    try:
        proc = psutil.Process(int(path.read_text().strip()))
        marker = "cloud.worker" if name == "worker" else "cloud.api.main:app"
        if marker in " ".join(proc.cmdline()) and proc.is_running():
            return proc
    except (psutil.Error, ValueError):
        pass
    path.unlink(missing_ok=True)
    return None


def _check_env(env_file: Optional[str]) -> None:
    from dotenv import dotenv_values

    if not env_file:
        raise SystemExit("--env-file is required")
    environment = dotenv_values(env_file).get("CAREERCLOUD_ENV", "development")
    if environment != "development":
        raise SystemExit(f"refusing: local_stack runs development only (env file says {environment!r})")


def _stop_file(name: str) -> Path:
    return RUN / f"{name}.stop"


def _start(name: str, command: List[str], log: Path) -> None:
    if _ours(name):
        print(f"{name} already running")
        return
    RUN.mkdir(parents=True, exist_ok=True)
    _stop_file(name).unlink(missing_ok=True)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    handle = open(log, "ab")
    proc = subprocess.Popen(
        command, cwd=REPO, stdout=handle, stderr=subprocess.STDOUT, creationflags=flags,
        start_new_session=os.name != "nt",
        env={**os.environ, "CAREERCLOUD_STOP_FILE": str(_stop_file(name))},
    )
    _pidfile(name).write_text(str(proc.pid))
    print(f"started {name} pid {proc.pid} (log {log})")


def _stop(name: str, *, hard: bool, timeout: float = 240) -> None:
    proc = _ours(name)
    if proc is None:
        print(f"{name} not running")
        return
    if hard:
        # On Windows a venv python.exe is a launcher with the real interpreter as its
        # child: kill the whole tree, children first, like a crash would.
        for child in proc.children(recursive=True):
            child.kill()
        proc.kill()
        print(f"killed {name} pid {proc.pid}")
    else:
        if os.name != "nt":
            proc.send_signal(signal.SIGTERM)
        elif name == "worker":
            # Signals do not cross Windows consoles; the worker watches this file instead.
            _stop_file(name).write_text("stop")
        else:
            # The API holds no job state: terminating it is a clean restart.
            for child in proc.children(recursive=True):
                child.terminate()
            proc.terminate()
        try:
            proc.wait(timeout)
            print(f"stopped {name} pid {proc.pid}")
        except Exception:
            proc.kill()
            print(f"{name} did not stop in {timeout}s; killed")
    try:
        proc.wait(10)
    except Exception:
        pass
    _pidfile(name).unlink(missing_ok=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.ops.local_stack")
    parser.add_argument(
        "command",
        choices=["start-api", "start-worker", "kill-worker", "stop-worker", "restart-worker", "restart-api", "status", "stop-all"],
    )
    parser.add_argument("--env-file")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    logs = REPO / "cloud" / ".localdev" / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    api_cmd = lambda: [PYTHON, "-m", "uvicorn", "cloud.api.main:app", "--host", "127.0.0.1", "--port", str(args.port), "--env-file", args.env_file, "--no-access-log"]  # noqa: E731
    worker_cmd = lambda: [PYTHON, "-m", "cloud.worker", "--env-file", args.env_file]  # noqa: E731

    if args.command in ("start-api", "restart-api"):
        _check_env(args.env_file)
        if args.command == "restart-api":
            _stop("api", hard=False, timeout=30)
            time.sleep(1)
        _start("api", api_cmd(), logs / "api.log")
    elif args.command in ("start-worker", "restart-worker"):
        _check_env(args.env_file)
        if args.command == "restart-worker":
            _stop("worker", hard=False)
        _start("worker", worker_cmd(), logs / "worker.log")
    elif args.command == "kill-worker":
        _stop("worker", hard=True)
    elif args.command == "stop-worker":
        _stop("worker", hard=False)
    elif args.command == "stop-all":
        _stop("worker", hard=False)
        _stop("api", hard=False, timeout=30)
    else:
        for name in ("api", "worker"):
            proc = _ours(name)
            print(f"{name}: {'running pid ' + str(proc.pid) if proc else 'stopped'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
