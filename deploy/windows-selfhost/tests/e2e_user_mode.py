"""End-to-end test of SANA-GTM-Windows-Installer.zip on this Windows machine, WITHOUT administrator
rights (User mode: same programs, no Windows services). Everything is real: the ZIP is unpacked,
Install-SanaGtm.ps1 downloads nothing (pinned components from --components) but verifies their
SHA-256, installs Python packages, creates PostgreSQL 17, migrates, starts the web server and the
worker; then the sanagtm CLI is exercised and the application is used over HTTP.

    python e2e_user_mode.py --zip SANA-GTM-Windows-Installer.zip --components <dir with the 4 pinned files>

Phases: install -> use (auth, workspace, provider credential, CSV import + export through the
PostgreSQL queue, isolation) -> operate (status, health, backup, restore test, verify, logs,
capacity, restart, update, rollback-update) -> uninstall keeping data -> reinstall -ReuseData
-> uninstall -PurgeData -> fresh install with the SAME secrets key -> migrate import of the first
install's backup (credentials readable, worker off) -> uninstall -PurgeData.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

CHECKS = []
SUPABASE = "https://abcdefghijklmnop.supabase.co"
JWT_SECRET = "windows-e2e-test-only-hs256-secret-0123456789"


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok)))
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""), flush=True)
    return bool(ok)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def token(sub, email):
    import jwt

    now = int(time.time())
    return jwt.encode({"sub": sub, "email": email, "role": "authenticated", "aud": "authenticated",
                       "iss": f"{SUPABASE}/auth/v1", "iat": now, "exp": now + 3600}, JWT_SECRET, algorithm="HS256")


class Env:
    def __init__(self, work: Path, pkg: Path, components: Path, key: str):
        self.work, self.pkg, self.components, self.key = work, pkg, components, key
        self.install, self.data = work / "install", work / "data"
        self.web_port, self.pg_port = free_port(), free_port()
        self.answers = work / "answers.env"
        self.answers.write_text(
            "HOSTNAME=sanagtm.example.com\n"
            f"SUPABASE_URL={SUPABASE}\n"
            "SUPABASE_ANON_KEY=public-anon-key-0123456789abcdef\n"
            f"PLATFORM_SECRETS_KEY_FILE={work / 'key.txt'}\n"
            "LEGACY_DATABASE_HOSTS=aws-0-ap-south-1.pooler.supabase.com\n"
            "EXTRA_CORS_ORIGINS=https://sanagtm.pages.dev\n"
            # test hook: lets this harness mint Supabase-format tokens (the VM uses Supabase's JWKS)
            f"ENV_CAREERCLOUD_SUPABASE_JWT_SECRET={JWT_SECRET}\n", encoding="utf-8")
        (work / "key.txt").write_text(key, encoding="utf-8")

    def ps(self, script: Path, *args: str, timeout: int = 2400) -> subprocess.CompletedProcess:
        # Output goes to FILES, not pipes: the long-running programs the installer starts inherit
        # handles, and a captured pipe would then never reach end-of-file.
        out, err = self.work / "ps.out.txt", self.work / "ps.err.txt"
        with out.open("wb") as o, err.open("wb") as e:
            done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                                  stdout=o, stderr=e, timeout=timeout, stdin=subprocess.DEVNULL)
        read = lambda p: p.read_bytes().decode("utf-8", errors="replace")  # noqa: E731
        return subprocess.CompletedProcess(done.args, done.returncode, read(out), read(err))

    def install_cmd(self, *extra: str) -> subprocess.CompletedProcess:
        return self.ps(self.pkg / "Install-SanaGtm.ps1", "-Mode", "User", "-InstallDir", str(self.install), "-DataDir",
                       str(self.data), "-ComponentsDir", str(self.components), "-AnswersFile", str(self.answers),
                       "-WebPort", str(self.web_port), "-PgPort", str(self.pg_port), "-NoTunnel", "-NoAutoStart", *extra)

    def cli(self, *args: str, timeout: int = 2400) -> subprocess.CompletedProcess:
        return self.ps(self.install / "bin" / "sanagtm.ps1", *args, timeout=timeout)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.web_port}"


def feed_total(e, wsid, headers):
    import requests

    try:
        return requests.get(f"{e.base}/api/v1/w/{wsid}/job-feed", headers=headers, timeout=30).json().get("total")
    except Exception as error:  # noqa: BLE001 - an unreachable app is a failed check, not a crash
        return f"unreachable ({type(error).__name__})"


def show(r):
    return (r.stdout + r.stderr).strip().replace("\r", "")[-600:]


def use_the_app(e: Env, alice: str) -> dict:
    import requests

    ah = {"Authorization": "Bearer " + token(alice, "alice@example.com")}
    mh = {"Authorization": "Bearer " + token(str(uuid.uuid4()), "mallory@example.com")}
    index = requests.get(e.base + "/", timeout=30)
    check("dashboard served with security headers", index.ok and "<div id=\"root\">" in index.text
          and "frame-ancestors 'none'" in index.headers.get("content-security-policy", ""))
    cfg = requests.get(e.base + "/runtime-config.js", timeout=30).text
    check("runtime config carries the auth URL + public key only", SUPABASE in cfg and "public-anon-key" in cfg
          and JWT_SECRET not in cfg and e.key not in cfg)
    check("api health on the same origin", requests.get(e.base + "/api/v1/health", timeout=30).json().get("status") == "ok")
    check("auth enforced", requests.get(e.base + "/api/v1/me", timeout=30).status_code == 401)

    def preflight(origin):
        return requests.options(e.base + "/api/v1/me", timeout=30, headers={
            "Origin": origin, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization"}).headers.get("access-control-allow-origin")
    check("CORS allows the hostname and EXTRA_CORS_ORIGINS (Pages dashboard) only",
          preflight("https://sanagtm.example.com") == "https://sanagtm.example.com"
          and preflight("https://sanagtm.pages.dev") == "https://sanagtm.pages.dev"
          and preflight("https://evil.example.net") is None)
    ws = requests.post(e.base + "/api/v1/workspaces", json={"name": "Windows E2E", "seed": False}, headers=ah, timeout=60)
    check("workspace created by a Supabase-format identity", ws.status_code in (200, 201), ws.text[:200])
    w = f"{e.base}/api/v1/w/{ws.json()['id']}"
    cred = requests.post(w + "/providers/zoominfo/credentials", headers=ah, timeout=60,
                         json={"secrets": {"client_id": "cid-12345678", "client_secret": "e2e-provider-secret-0001"}})
    check("provider credential stored (encrypted with the platform key)", cred.ok and "e2e-provider-secret" not in cred.text)
    csv = ("Job URL,Job Title,Company,Location\nhttps://jobs.example.com/1,SAP Basis Engineer,Acme,Austin TX\n"
           "https://jobs.example.com/2,NetSuite Developer,Globex,Remote\nhttps://jobs.example.com/3,Oracle DBA,Initech,Dallas TX\n")
    up = requests.post(w + "/job-imports", files={"file": ("jobs.csv", csv.encode(), "text/csv")}, headers=ah, timeout=60).json()
    requests.post(f"{w}/job-imports/{up['id']}/validate", json={"mapping": up["mapping"]}, headers=ah, timeout=60)
    requests.post(f"{w}/job-imports/{up['id']}/start", headers=ah, timeout=60)
    deadline = time.time() + 180
    total = None
    while time.time() < deadline:
        total = requests.get(w + "/job-feed", headers=ah, timeout=30).json().get("total")
        if total == 3:
            break
        time.sleep(2)
    check("worker ran the CSV import through the PostgreSQL queue", total == 3, f"total={total}")
    ex = requests.post(w + "/job-exports", json={"scope": "all", "params": {}}, headers=ah, timeout=60).json()
    deadline = time.time() + 180
    while time.time() < deadline and requests.get(f"{w}/job-exports/{ex['id']}", headers=ah, timeout=30).json()["status"] != "completed":
        time.sleep(2)
    dl = requests.get(f"{w}/job-exports/{ex['id']}/download", headers=ah, timeout=60)
    check("worker ran the export; CSV downloads", dl.ok and dl.text.count("jobs.example.com") == 3)
    check("another account cannot read the workspace", requests.get(w + "/job-feed", headers=mh, timeout=30).status_code in (403, 404))
    return {"w": w, "ah": ah}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=Path(os.environ.get("TEMP", ".")) / "sanagtm-win-e2e")
    args = parser.parse_args()
    import requests

    work = args.workdir.resolve()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with zipfile.ZipFile(args.zip) as z:
        z.extractall(work / "pkg")
    pkg = work / "pkg" / "SANA-GTM-Windows-Installer"
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    e = Env(work, pkg, args.components.resolve(), key)
    alice = str(uuid.uuid4())
    started = time.time()

    print("phase 1: install (User mode, no administrator)")
    pre = e.ps(pkg / "Install-SanaGtm.ps1", "-Mode", "User", "-InstallDir", str(e.install), "-DataDir", str(e.data),
               "-WebPort", str(e.web_port), "-PgPort", str(e.pg_port), "-NoTunnel", "-PreflightOnly")
    check("preflight passes and changes nothing", pre.returncode == 0 and not e.data.exists(), show(pre))
    r = e.install_cmd()
    ok = check("Install-SanaGtm.ps1 completed", r.returncode == 0 and "SANA GTM is installed" in r.stdout, show(r))
    if not ok:
        return report(started)
    check("no secret printed during install", key not in r.stdout + r.stderr and JWT_SECRET not in r.stdout + r.stderr)
    env_file = e.data / "config" / "sanagtm.env"
    acl = subprocess.run(["icacls", str(env_file)], capture_output=True, text=True).stdout
    check("sanagtm.env is not readable by Users/Everyone", "BUILTIN\\Users" not in acl and "Everyone" not in acl, acl[:300])
    check("data is outside the program folder", e.data / "pgdata" != e.install and (e.data / "pgdata" / "PG_VERSION").exists()
          and not (e.install / "pgdata").exists())
    hba = (e.data / "pgdata" / "pg_hba.conf").read_text()
    check("PostgreSQL accepts only local SCRAM logins", "0.0.0.0" not in hba and "trust" not in hba and "127.0.0.1/32" in hba)

    print("phase 2: use the application")
    use_the_app(e, alice)

    print("phase 3: operate with sanagtm")
    check("status", e.cli("status").returncode == 0)
    b = e.cli("backup", "-Label", "e2e")
    check("backup (dump + manifest + files + config)", b.returncode == 0 and list((e.data / "backups").glob("sanagtm-e2e_*.dump")), show(b))
    dump = sorted((e.data / "backups").glob("sanagtm-e2e_*.dump"))[-1]
    keep = work / "keep"; keep.mkdir()
    for f in (dump, dump.with_suffix(".manifest.json")):
        shutil.copy2(f, keep / f.name)
    check("config backup is restricted", "BUILTIN\\Users" not in subprocess.run(
        ["icacls", str(e.data / "backups" / "config")], capture_output=True, text=True).stdout)
    h = e.cli("health")
    check("health: all checks ok", h.returncode == 0, show(h))
    t = e.cli("restore", str(dump))
    check("restore test into a scratch database", t.returncode == 0 and "RESTORE TEST PASSED" in t.stdout, show(t))
    v = e.cli("migrate", "verify", str(dump.with_suffix(".manifest.json")))
    check("live database matches the backup manifest", v.returncode == 0 and "VERIFY PASSED" in v.stdout, show(v))
    s = e.cli("migrate", "verify-secrets")
    check("stored provider credential readable with the platform key", s.returncode == 0 and '"readable": 1' in s.stdout, show(s))
    check("logs", e.cli("logs", "web").returncode == 0)
    check("capacity", e.cli("capacity").returncode == 0)
    rs = e.cli("restart")
    check("restart", rs.returncode == 0 and requests.get(e.base + "/healthz", timeout=30).ok, show(rs))
    up = e.cli("update", "-Package", str(args.zip))
    check("update -Package (backup, swap, pip, migrations, restart)", up.returncode == 0 and (e.install / "app.previous").exists(), show(up))
    rb = e.cli("rollback-update")
    check("rollback-update", rb.returncode == 0 and requests.get(e.base + "/healthz", timeout=30).ok, show(rb))
    ah = {"Authorization": "Bearer " + token(alice, "alice@example.com")}
    wsid = requests.get(e.base + "/api/v1/workspaces", headers=ah, timeout=30).json()["items"][0]["id"]
    check("data intact after update + rollback",
          feed_total(e, wsid, ah) == 3)

    print("phase 4: uninstall keeps data; reinstall -ReuseData")
    u = e.cli("uninstall")
    check("uninstall keeps the data folder", u.returncode == 0 and not e.install.exists() and (e.data / "pgdata").exists(), show(u))
    r = e.install_cmd("-ReuseData")
    check("reinstall -ReuseData", r.returncode == 0, show(r))
    check("data back after reinstall",
          feed_total(e, wsid, ah) == 3)
    p = e.cli("uninstall", "-PurgeData", "-ConfirmHostname", "wrong.example.com")
    check("purge refused without the right confirmation", p.returncode != 0 and e.data.exists())
    p = e.cli("uninstall", "-PurgeData", "-ConfirmHostname", "sanagtm.example.com")
    check("uninstall -PurgeData (fresh backup + confirmation)", p.returncode == 0 and not e.data.exists(), show(p))

    print("phase 5: fresh install with the SAME secrets key, then migrate import")
    r = e.install_cmd()
    check("fresh install", r.returncode == 0, show(r))
    target = e.data / "backups"
    for f in keep.iterdir():
        shutil.copy2(f, target / f.name)
    m = e.cli("migrate", "import", str(target / dump.name), "-ConfirmHostname", "sanagtm.example.com")
    check("migrate import (restore test, verified restore, stamp)", m.returncode == 0 and "RESTORE PASSED" in m.stdout, show(m))
    check("provider credentials readable after migration (same key)", '"readable": 1' in m.stdout, show(m))
    check("worker stays OFF after import",
          not (e.data / "run" / "worker.pid").exists() or "worker   stopped" in e.cli("status").stdout)
    check("migrated data visible to the original identity",
          feed_total(e, wsid, ah) == 3)
    p = e.cli("uninstall", "-PurgeData", "-ConfirmHostname", "sanagtm.example.com")
    if p.returncode != 0:            # no backup in the last 24 h on this fresh install: take one, then purge
        e.cli("backup", "-Label", "final")
        p = e.cli("uninstall", "-PurgeData", "-ConfirmHostname", "sanagtm.example.com")
    check("cleanup", p.returncode == 0 and not e.data.exists(), show(p))
    return report(started)


def report(started):
    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed in {time.time() - started:.0f} s"
          + (f"; FAILED: {failed}" if failed else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
