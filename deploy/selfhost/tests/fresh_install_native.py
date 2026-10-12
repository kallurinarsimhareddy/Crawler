"""Fresh-install test of the self-hosted package WITHOUT Docker (for machines that cannot run it).

It reproduces what `sanagtm install` + `docker compose up` do, step by step, with real processes:

  1. `sanagtm install --no-tunnel` (the real CLI, against a recording fake docker) writes the real
     /etc/sanagtm files: compose.env, postgres.env, sanagtm.env.
  2. A throwaway PostgreSQL (password auth, 127.0.0.1, random port) plays the "postgres" service;
     the real initdb script (postgres/initdb/10-sanagtm.sh) creates the app role and database with
     the passwords the installer generated.
  3. The app image's file set is staged by reading the Dockerfile's COPY lines.
  4. With the pinned self-host virtualenv (built from deploy/selfhost/requirements*.txt only):
     the "migrate" service command, then "api" (uvicorn) and "worker" exactly as compose runs them,
     with the env_file + compose `environment:` the containers get (host "postgres" -> 127.0.0.1).
  5. Functional checks over HTTP with Supabase-format access tokens: auth enforced, workspace,
     CSV job import and CSV export through the PostgreSQL task queue and worker, workspace
     isolation, /status, restart persistence, backup + restore test + verify.

    python deploy/selfhost/tests/fresh_install_native.py --pg-bin <PostgreSQL bin dir> \
        --python <selfhost venv python> [--workdir DIR] [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[3]
SELFHOST = REPO / "deploy" / "selfhost"
SUPABASE = "https://abcdefghijklmnop.supabase.co"
JWT_SECRET = "fresh-install-test-only-hs256-secret-0123456789"  # stands in for Supabase's signing key
EXE = ".exe" if os.name == "nt" else ""
CHECKS: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def find_bash() -> str:
    found = shutil.which("bash")
    if found:
        return found
    for root in (os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "") + r"\Programs"):
        candidate = Path(root) / "Git" / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    raise SystemExit("bash is required (Git for Windows or Linux)")


def posix(path: Path) -> str:
    text = str(path).replace("\\", "/")
    return "/" + text[0].lower() + text[2:] if os.name == "nt" and re.match(r"^[A-Za-z]:/", text) else text


def read_env(path: Path) -> Dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def stage_image(app: Path) -> List[str]:
    """Copy exactly what the Dockerfile COPYs (minus .dockerignore'd junk), then drop cloud/web."""
    copied = []
    for line in (SELFHOST / "Dockerfile").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^COPY (?!--)(\S+) \./(\S*)$", line.strip())
        if not match:
            continue
        source, target = REPO / match.group(1), app / (match.group(2) or Path(match.group(1)).name)
        junk = shutil.ignore_patterns(".venv", ".venv-*", "node_modules", "__pycache__", "*.pyc", ".localdev",
                                      ".env", ".env.*", "dist", ".wrangler")

        def ignore(directory, names):  # mirrors .dockerignore, including its `!**/.env.example`
            return {name for name in junk(directory, names) if name != ".env.example"}

        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        copied.append(match.group(1))
    shutil.rmtree(app / "cloud" / "web", ignore_errors=True)
    return copied


def token(sub: str, email: str) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode({"sub": sub, "email": email, "role": "authenticated", "aud": "authenticated",
                       "iss": f"{SUPABASE}/auth/v1", "iat": now, "exp": now + 3600, "is_anonymous": False},
                      JWT_SECRET, algorithm="HS256")


class Services:
    """The api and worker processes, started like compose starts them."""

    def __init__(self, python: str, app: Path, env: Dict[str, str], logs: Path, api_port: int):
        self.python, self.app, self.env, self.logs, self.api_port = python, app, env, logs, api_port
        self.procs: Dict[str, subprocess.Popen] = {}

    def start(self, name: str, args: List[str]) -> None:
        out = (self.logs / f"{name}.log").open("ab")
        self.procs[name] = subprocess.Popen([self.python, *args], cwd=self.app, env=self.env, stdin=subprocess.DEVNULL,
                                            stdout=out, stderr=subprocess.STDOUT)

    def start_all(self) -> None:
        self.start("api", ["-m", "uvicorn", "cloud.api.main:app", "--host", "127.0.0.1", "--port", str(self.api_port),
                           "--no-server-header"])
        self.start("worker", ["-m", "cloud.intel.tasks.worker", "--concurrency", "2", "--state-file",
                              self.env["CAREERCLOUD_WORKER_STATE_FILE"]])

    def stop_all(self) -> None:
        for proc in self.procs.values():
            proc.terminate()
        for proc in self.procs.values():
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.procs.clear()

    def tool(self, *args: str, timeout: int = 300, extra: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.python, *args], cwd=self.app, env={**self.env, **(extra or {})},
                              capture_output=True, text=True, timeout=timeout)

    def wait_healthy(self, target: str, seconds: int) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.tool("-m", "cloud.ops.selfhost", "health", target, timeout=30).returncode == 0:
                return True
            time.sleep(2)
        return False


def poll(fn, seconds: int = 120, every: float = 1.5):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable, help="the self-host venv python (pinned requirements)")
    parser.add_argument("--workdir", type=Path, default=REPO / "cloud" / ".localdev" / "fresh-install")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    import psycopg
    import requests

    work = args.workdir.resolve()
    shutil.rmtree(work, ignore_errors=True)
    etc, data, app, logs = work / "etc", work / "data", work / "app", work / "logs"
    for directory in (app, logs):
        directory.mkdir(parents=True)
    bash = find_bash()
    pg = lambda name: str(args.pg_bin / f"{name}{EXE}")  # noqa: E731
    print(f"fresh install in {work}")

    # 1. the real installer writes the configuration (fake docker records what it would run)
    fake = work / "fake-docker"
    fake.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
                    "case \"$*\" in *' ps --format'*) printf 'postgres healthy running\\napi healthy running\\n';; esac\n",
                    encoding="utf-8", newline="\n")
    answers = work / "answers.env"
    answers.write_text("SANAGTM_HOSTNAME=sanagtm.example.com\nSANAGTM_SUPABASE_URL=" + SUPABASE + "\n"
                       "SANAGTM_SUPABASE_ANON_KEY=public-anon-key-0123456789abcdef\n"
                       "SANAGTM_LEGACY_DATABASE_HOSTS=aws-0-ap-south-1.pooler.supabase.com\n",
                       encoding="utf-8", newline="\n")
    (work / "cli-app" / "deploy" / "selfhost").mkdir(parents=True)
    shutil.copy(SELFHOST / "compose.yaml", work / "cli-app" / "deploy" / "selfhost" / "compose.yaml")
    installed = subprocess.run([bash, posix(SELFHOST / "bin" / "sanagtm"), "install", "--no-tunnel", "--answers",
                                posix(answers)], capture_output=True, text=True, timeout=120,
                               env={**os.environ, "SANAGTM_APP": posix(work / "cli-app"), "SANAGTM_ETC": posix(etc),
                                    "SANAGTM_DATA": posix(data), "SANAGTM_DOCKER": posix(fake),
                                    "SANAGTM_ALLOW_NONROOT": "1", "FAKE_LOG": posix(work / "docker.log")})
    check("sanagtm install wrote the configuration", installed.returncode == 0 and (etc / "sanagtm.env").exists(),
          installed.stderr.strip()[-300:])
    app_env, pg_env = read_env(etc / "sanagtm.env"), read_env(etc / "postgres.env")

    # 2. PostgreSQL + the real init script
    pgdata, port = data / "pgdata" / "pgdata", free_port()
    pwfile = work / "su.pw"
    pwfile.write_text(pg_env["POSTGRES_PASSWORD"], encoding="utf-8")
    subprocess.run([pg("initdb"), "-D", str(pgdata), "-U", "postgres", "--auth=scram-sha-256", f"--pwfile={pwfile}",
                    "--encoding=UTF8", "--no-instructions"], check=True, capture_output=True)
    pwfile.unlink()
    started = subprocess.run([pg("pg_ctl"), "-D", str(pgdata), "-l", str(logs / "postgres.log"), "-w", "-t", "60",
                              "-o", f"-p {port} -c listen_addresses=127.0.0.1", "start"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    services: Optional[Services] = None
    try:
        check("postgres started (password auth)", started.returncode == 0)
        init = subprocess.run([bash, posix(SELFHOST / "postgres" / "initdb" / "10-sanagtm.sh")], capture_output=True,
                              text=True, timeout=120,
                              env={**os.environ, "PATH": posix(args.pg_bin) + ":" + os.environ.get("PATH", ""),
                                   "POSTGRES_USER": "postgres", "PGHOST": "127.0.0.1", "PGPORT": str(port),
                                   "PGPASSWORD": pg_env["POSTGRES_PASSWORD"],
                                   "SANAGTM_DB_PASSWORD": pg_env["SANAGTM_DB_PASSWORD"]})
        check("initdb script created role + database", init.returncode == 0, init.stderr.strip()[-300:])

        # 3. the image's files
        copied = stage_image(app)
        check("image file set staged from the Dockerfile", {"cloud", "crawler", "adapters"} <= set(copied)
              and not (app / "sheets").exists() and not (app / "store").exists(), ", ".join(copied))

        # 4. the containers' environment: env_file + compose `environment:` (paths -> this machine)
        root = data
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CAREERCLOUD_", "SANAGTM_", "PG"))}
        env.update(app_env)
        env.update({
            "CAREERCLOUD_DATABASE_URL": app_env["CAREERCLOUD_DATABASE_URL"].replace("@postgres:5432/", f"@127.0.0.1:{port}/"),
            "CAREERCLOUD_PLATFORM_FILES_DIR": str(root / "files"), "CAREERCLOUD_RESULTS_DIR": str(root / "results"),
            "CAREERCLOUD_WORKER_STATE_FILE": str(root / "run" / "worker-state.json"),
            "CAREERCLOUD_RESOURCE_REGISTRY": str(root / "run" / "resources.json"),
            "CAREERCLOUD_TRUSTED_PROXIES": "172.29.40.10/32", "TMP": str(root / "files" / "tmp"),
            "TEMP": str(root / "files" / "tmp"), "TMPDIR": str(root / "files" / "tmp"),
            "CAREERCLOUD_SUPABASE_JWT_SECRET": JWT_SECRET, "PYTHONPATH": str(app), "PYTHONDONTWRITEBYTECODE": "1",
        })
        api_port = free_port()
        env["SANAGTM_API_PORT"] = str(api_port)
        services = Services(args.python, app, env, logs, api_port)

        prepared = services.tool("-m", "cloud.ops.selfhost", "prepare")
        result = json.loads(prepared.stdout.strip().splitlines()[-1]) if prepared.returncode == 0 else {}
        check("migrate service: migrations applied + stamped production", len(result.get("applied", [])) >= 13
              and result.get("stamp") == "production" and result.get("fresh") is True,
              (prepared.stderr or prepared.stdout).strip()[-300:])
        again = services.tool("-m", "cloud.ops.selfhost", "prepare")
        check("migrate service is idempotent", again.returncode == 0 and '"applied": []' in again.stdout)
        registry = json.loads((root / "run" / "resources.json").read_text(encoding="utf-8"))
        check("resource registry written (own + legacy)", registry["production"]["database_hosts"] == ["127.0.0.1"]
              and "legacy-hosted" in registry)

        services.start_all()
        check("api healthy", services.wait_healthy("api", 90))
        check("worker healthy (heartbeat + database)", services.wait_healthy("worker", 150))

        base = f"http://127.0.0.1:{api_port}/api/v1"
        alice, mallory = str(uuid.uuid4()), str(uuid.uuid4())
        ah = {"Authorization": "Bearer " + token(alice, "alice@example.com")}
        mh = {"Authorization": "Bearer " + token(mallory, "mallory@example.com")}
        check("auth enforced (no token -> 401)", requests.get(base + "/me", timeout=30).status_code == 401)
        forged = {"Authorization": "Bearer " + token(alice, "alice@example.com")[:-4] + "AAAA"}
        check("forged token refused", requests.get(base + "/me", headers=forged, timeout=30).status_code == 401)
        me = requests.get(base + "/me", headers=ah, timeout=30)
        check("Supabase-format token accepted", me.status_code == 200, me.text[:200])
        ws = requests.post(base + "/workspaces", json={"name": "Fresh install", "seed": False}, headers=ah, timeout=60)
        check("workspace created", ws.status_code in (200, 201), ws.text[:200])
        w = f"{base}/w/{ws.json()['id']}"

        csv = ("Job URL,Job Title,Company,Location\n"
               "https://jobs.example.com/1,SAP Basis Engineer,Acme,Austin TX\n"
               "https://jobs.example.com/2,NetSuite Developer,Globex,Remote\n"
               "https://jobs.example.com/3,Oracle DBA,Initech,Dallas TX\n").encode()
        up = requests.post(w + "/job-imports", files={"file": ("jobs.csv", csv, "text/csv")}, headers=ah, timeout=60)
        check("CSV upload accepted", up.status_code == 201, up.text[:200])
        imp = up.json()
        val = requests.post(f"{w}/job-imports/{imp['id']}/validate", json={"mapping": imp["mapping"]}, headers=ah,
                            timeout=60)
        check("import validated", val.status_code == 200 and val.json()["validation"]["valid"] == 3, val.text[:200])
        start = requests.post(f"{w}/job-imports/{imp['id']}/start", headers=ah, timeout=60)
        check("import queued on the PostgreSQL queue", start.status_code in (200, 201), start.text[:200])
        total = poll(lambda: requests.get(w + "/job-feed", headers=ah, timeout=30).json().get("total") == 3, 120)
        check("worker ran the import (3 jobs in the feed)", bool(total))

        export = requests.post(w + "/job-exports", json={"scope": "all", "params": {}}, headers=ah, timeout=60)
        check("export queued", export.status_code == 201, export.text[:200])
        done = poll(lambda: requests.get(f"{w}/job-exports/{export.json()['id']}", headers=ah, timeout=30)
                    .json().get("status") == "completed", 120)
        download = requests.get(f"{w}/job-exports/{export.json()['id']}/download", headers=ah, timeout=60)
        check("worker ran the export; CSV downloads", bool(done) and download.status_code == 200
              and download.text.count("jobs.example.com") == 3)

        stranger = requests.get(w + "/job-feed", headers=mh, timeout=30)
        check("another account cannot read the workspace", stranger.status_code in (403, 404), str(stranger.status_code))
        status = requests.get(base + "/status", headers=ah, timeout=30)
        check("/status answers on the PostgreSQL queue", status.status_code == 200 and "postgres" in status.text,
              status.text[:200])

        services.stop_all()
        services.start_all()
        check("restart: api healthy again", services.wait_healthy("api", 90))
        check("restart: worker healthy again", services.wait_healthy("worker", 150))
        check("restart: data persisted", requests.get(w + "/job-feed", headers=ah, timeout=30).json().get("total") == 3)

        # backup -> restore test (scratch database on the same server) -> verify the live database
        backup_dir = root / "backups"
        backup = services.tool("deploy/ops/backup/sana_backup.py", "backup", "--pg-bin", str(args.pg_bin), "--out",
                               str(backup_dir), "--label", "sanagtm-fresh", "--local-only")
        check("backup (app role, consistent snapshot)", backup.returncode == 0, (backup.stderr or "")[-300:])
        dump = sorted(backup_dir.glob("*.dump"))[-1]
        admin = f"postgresql://postgres:{pg_env['POSTGRES_PASSWORD']}@127.0.0.1:{port}/postgres"
        restore = services.tool("deploy/ops/backup/sana_backup.py", "restore-test", "--dump", str(dump), "--pg-bin",
                                str(args.pg_bin), "--server-url-env", "SANAGTM_ADMIN_DATABASE_URL",
                                extra={"SANAGTM_ADMIN_DATABASE_URL": admin})
        check("restore test into a scratch database", restore.returncode == 0 and "PASSED" in restore.stdout,
              (restore.stdout + restore.stderr)[-300:])
        verify = services.tool("deploy/ops/backup/sana_backup.py", "verify", "--manifest",
                               str(dump.with_suffix(".manifest.json")))
        check("live database matches the backup manifest", verify.returncode == 0, verify.stdout[-300:])
        with psycopg.connect(admin.rsplit("/", 1)[0] + "/sanagtm") as conn:
            owners = {r[0] for r in conn.execute("select distinct tableowner from pg_tables "
                                                 "where schemaname = 'careercloud'").fetchall()}
        check("every table is owned by the app role (not the superuser)", owners == {"sanagtm"}, str(owners))
    finally:
        if services:
            services.stop_all()
        subprocess.run([pg("pg_ctl"), "-D", str(pgdata), "-m", "fast", "-w", "stop"], capture_output=True)
        if not args.keep and all(ok for _, ok, _ in CHECKS):
            shutil.rmtree(work, ignore_errors=True)
    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    if failed:
        print(f"logs kept in {logs}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
