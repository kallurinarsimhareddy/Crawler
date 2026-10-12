"""Migration rehearsal: hosted database -> self-hosted install, on real data, without touching
production. A stand-in for the hosted database is restored from any existing pg_dump; then the
exact Phase 2 procedure runs against it:

   1. backup of the stand-in (consistent snapshot, manifest: rows, checksums, RLS per table)
   2. a self-hosted-shaped target: PostgreSQL with password auth + the real initdb script
   3. restore-test into a scratch database on the target
   4. restore into the install's database, owned by the app role; verified against the manifest
   5. explicit production stamp (stamp-restored), then the "migrate" service (pending migrations)
   6. the API boots in self-hosted production mode; an EXISTING member signs in with their
      original user id (Supabase identity preserved) and sees their workspace and jobs; a
      stranger sees nothing
   7. rollback check: the stand-in (= the hosted database) is unchanged

    python deploy/selfhost/tests/migration_rehearsal.py --source-dump X.dump --pg-bin <bin> --python <venv python>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "deploy" / "ops" / "backup"))

from fresh_install_native import (CHECKS, JWT_SECRET, SUPABASE, Services, check, find_bash,  # noqa: E402
                                  free_port, posix, token)
import sana_backup  # noqa: E402

EXE = ".exe" if os.name == "nt" else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dump", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--workdir", type=Path, default=REPO / "cloud" / ".localdev" / "migration-rehearsal")
    args = parser.parse_args()
    import psycopg
    import requests

    work = args.workdir.resolve()
    shutil.rmtree(work, ignore_errors=True)
    (work / "logs").mkdir(parents=True)
    pg = lambda name: str(args.pg_bin / f"{name}{EXE}")  # noqa: E731
    timings = {}

    with sana_backup.Cluster(args.pg_bin, work) as hosted:
        # --- the stand-in for the hosted database -------------------------------------------
        with psycopg.connect(hosted.url(), autocommit=True) as conn:
            conn.execute("create role authenticated nologin noinherit")
            conn.execute("create database hosted")
        started = time.monotonic()
        loaded = subprocess.run([pg("pg_restore"), "--no-owner", "--exit-on-error", f"--dbname={hosted.url('hosted')}",
                                 str(args.source_dump)], capture_output=True, text=True)
        timings["load stand-in"] = round(time.monotonic() - started, 1)
        check("stand-in for the hosted database loaded", loaded.returncode == 0, loaded.stderr[-300:])
        with psycopg.connect(hosted.url("hosted"), autocommit=True) as conn:
            # production is unstamped (it runs as "development"); mirror that
            conn.execute("delete from careercloud.deployment")
            owner = conn.execute("select m.workspace_id, m.user_id from careercloud.workspace_members m "
                                 "where m.role = 'owner' order by (select count(*) from careercloud.job_postings j "
                                 "where j.workspace_id = m.workspace_id) desc limit 1").fetchone()
            jobs = conn.execute("select count(*) from careercloud.job_postings where workspace_id = %s",
                                (owner[0],)).fetchone()[0]
        print(f"  stand-in: {jobs:,} job postings; owner {str(owner[1])[:8]}… of workspace {str(owner[0])[:8]}…")

        # --- 1. backup ------------------------------------------------------------------------
        started = time.monotonic()
        manifest = sana_backup.backup(hosted.url("hosted"), pg_bin=args.pg_bin, out_dir=work / "backups",
                                      offsite=None, label="rehearsal", allow_same_disk=True)
        timings["backup"] = round(time.monotonic() - started, 1)
        dump = work / "backups" / str(manifest["file"])
        check("backup with manifest", dump.exists(), f"{manifest['bytes'] / 1048576:.1f} MB, "
              f"{manifest['rows_total']:,} rows, {len(manifest['tables'])} tables")

        # --- 2. the self-hosted-shaped target ---------------------------------------------------
        su_pw, app_pw = uuid.uuid4().hex, uuid.uuid4().hex
        pgdata, port = work / "target", free_port()
        pwfile = work / "su.pw"
        pwfile.write_text(su_pw, encoding="utf-8")
        subprocess.run([pg("initdb"), "-D", str(pgdata), "-U", "postgres", "--auth=scram-sha-256",
                        f"--pwfile={pwfile}", "--encoding=UTF8", "--no-instructions"], check=True, capture_output=True)
        pwfile.unlink()
        subprocess.run([pg("pg_ctl"), "-D", str(pgdata), "-l", str(work / "logs" / "target.log"), "-w", "-t", "60",
                        "-o", f"-p {port} -c listen_addresses=127.0.0.1", "start"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        services = None
        try:
            init = subprocess.run([find_bash(), posix(REPO / "deploy" / "selfhost" / "postgres" / "initdb" /
                                                      "10-sanagtm.sh")], capture_output=True, text=True,
                                  env={**os.environ, "PATH": posix(args.pg_bin) + ":" + os.environ.get("PATH", ""),
                                       "POSTGRES_USER": "postgres", "PGHOST": "127.0.0.1", "PGPORT": str(port),
                                       "PGPASSWORD": su_pw, "SANAGTM_DB_PASSWORD": app_pw})
            check("target initialised by the real initdb script", init.returncode == 0, init.stderr[-200:])
            admin = f"postgresql://postgres:{su_pw}@127.0.0.1:{port}/postgres"
            app_url = f"postgresql://sanagtm:{app_pw}@127.0.0.1:{port}/sanagtm"

            # --- 3. restore test, 4. restore into the install's database ------------------------
            check("restore test (scratch database on the target)",
                  sana_backup.restore_test(dump, pg_bin=args.pg_bin, server_url=admin)["ok"])
            started = time.monotonic()
            live = sana_backup.restore_live(dump, pg_bin=args.pg_bin, server_url=admin, database="sanagtm",
                                            owner="sanagtm", replace=True, confirm="sanagtm")
            timings["restore + verify"] = round(time.monotonic() - started, 1)
            check("restored into the install's database; rows, checksums, RLS match the manifest", live["ok"],
                  "; ".join(live["problems"][:3]) or f"pending migrations: {live.get('pending_migrations')}")

            # --- 5. stamp + the migrate service -----------------------------------------------
            env = {k: v for k, v in os.environ.items() if not k.startswith(("CAREERCLOUD_", "SANAGTM_", "PG"))}
            data = work / "data"
            for sub in ("files/tmp", "results", "run"):
                (data / sub).mkdir(parents=True, exist_ok=True)
            env.update({
                "CAREERCLOUD_ENV": "production", "CAREERCLOUD_SELF_HOSTED": "1", "CAREERCLOUD_STORAGE": "postgres",
                "CAREERCLOUD_QUEUE": "postgres", "CAREERCLOUD_DATABASE_URL": app_url,
                "CAREERCLOUD_DB_USER_ROLE": "authenticated", "CAREERCLOUD_AUTH_MODE": "supabase",
                "CAREERCLOUD_SUPABASE_URL": SUPABASE, "CAREERCLOUD_SUPABASE_JWT_SECRET": JWT_SECRET,
                "CAREERCLOUD_API_ORIGIN": "https://sanagtm.example.com",
                "CAREERCLOUD_CORS_ORIGINS": "https://sanagtm.example.com", "CAREERCLOUD_TRUST_PROXY": "cloudflare",
                "CAREERCLOUD_LOG_FORMAT": "json", "CAREERCLOUD_STORAGE_BACKEND": "local",
                "CAREERCLOUD_PLATFORM_FILES_DIR": str(data / "files"), "CAREERCLOUD_RESULTS_DIR": str(data / "results"),
                "CAREERCLOUD_WORKER_STATE_FILE": str(data / "run" / "worker-state.json"),
                "CAREERCLOUD_RESOURCE_REGISTRY": str(data / "run" / "resources.json"),
                "SANAGTM_LEGACY_DATABASE_HOSTS": "aws-0-ap-south-1.pooler.supabase.com",
                "TMP": str(data / "files" / "tmp"), "TEMP": str(data / "files" / "tmp"),
                "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1",
            })
            api_port = free_port()
            env["SANAGTM_API_PORT"] = str(api_port)
            services = Services(args.python, REPO, env, work / "logs", api_port)
            refused = services.tool("-m", "cloud.ops.selfhost", "prepare")
            check("unstamped restored data is refused until stamped explicitly", refused.returncode != 0
                  and "no environment stamp" in refused.stderr)
            stamped = services.tool("-m", "cloud.ops.selfhost", "stamp-restored")
            check("stamp-restored", stamped.returncode == 0 and "production" in stamped.stdout, stamped.stderr[-200:])
            started = time.monotonic()
            prepared = services.tool("-m", "cloud.ops.selfhost", "prepare", timeout=1800)
            timings["pending migrations"] = round(time.monotonic() - started, 1)
            applied = json.loads(prepared.stdout.strip().splitlines()[-1])["applied"] if prepared.returncode == 0 else None
            check("migrate service applied the pending migrations to the real data",
                  prepared.returncode == 0 and applied == live.get("pending_migrations"),
                  f"applied {applied}" if applied is not None else prepared.stderr[-300:])

            # --- 6. the API on the migrated data, as an existing member -------------------------
            services.start("api", ["-m", "uvicorn", "cloud.api.main:app", "--host", "127.0.0.1", "--port",
                                   str(api_port), "--no-server-header"])
            check("api healthy (self-hosted production mode)", services.wait_healthy("api", 120))
            base = f"http://127.0.0.1:{api_port}/api/v1"
            member = {"Authorization": "Bearer " + token(str(owner[1]), "owner@example.com")}
            stranger = {"Authorization": "Bearer " + token(str(uuid.uuid4()), "stranger@example.com")}
            workspaces = requests.get(base + "/workspaces", headers=member, timeout=60)
            ids = {w["id"] for w in workspaces.json().get("items", [])} if workspaces.ok else set()
            check("an existing member signs in with their original user id and sees their workspace",
                  str(owner[0]) in {str(i) for i in ids}, f"HTTP {workspaces.status_code}")
            feed = requests.get(f"{base}/w/{owner[0]}/job-feed?limit=1", headers=member, timeout=300)
            total = feed.json().get("total") if feed.ok else None
            check("the member's jobs are all there", total == jobs, f"{total} of {jobs:,}")
            other = requests.get(f"{base}/w/{owner[0]}/job-feed?limit=1", headers=stranger, timeout=60)
            check("a stranger sees nothing", other.status_code in (403, 404), str(other.status_code))

            # --- 7. rollback: the hosted database was never written ----------------------------
            unchanged = sana_backup.verify_database(hosted.url("hosted"), manifest)
            check("rollback path intact: the hosted stand-in still matches its backup", unchanged["ok"])
        finally:
            if services:
                services.stop_all()
            subprocess.run([pg("pg_ctl"), "-D", str(pgdata), "-m", "fast", "-w", "stop"], capture_output=True)
    print("timings (s):", json.dumps(timings))
    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    if not failed:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
