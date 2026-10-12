"""Verified PostgreSQL backups for SANA GTM: dump, verify, copy off the machine, restore-test.

    backup        consistent pg_dump of one schema (default ``careercloud``) taken inside an exported
                  REPEATABLE READ snapshot, with exact per-table row counts and id/version checksums
                  from that same snapshot, a SHA-256, a TOC check, and a verified copy to an
                  off-machine folder on a different physical disk.
    restore-test  restores a dump into a throwaway PostgreSQL cluster (127.0.0.1, random port,
                  deleted afterwards), then checks counts, checksums, RLS and the schema migrations
                  against the manifest and this repository.
    selftest      the whole procedure end to end on two local throwaway clusters with synthetic data.

Read-only on the source: the dump session is ``READ ONLY`` and pg_dump never writes. The database
URL is read from an env file and handed to the PostgreSQL tools through PG* environment variables,
never on a command line and never printed.

    python deploy/ops/backup/sana_backup.py backup --env-file cloud/worker/.env.sana-cloud \
        --pg-bin E:/Tools/pgsql17/bin --out E:/SanaBackups --offsite F:/SanaBackups
    python deploy/ops/backup/sana_backup.py restore-test --dump E:/SanaBackups/<name>.dump \
        --pg-bin E:/Tools/pgsql17/bin
    python deploy/ops/backup/sana_backup.py selftest --pg-bin <a PostgreSQL bin directory>

    restore       (self-hosted) restore into the install's own database, owned by the app role;
                  refuses to overwrite existing tables without --replace --confirm <database>.
    verify        compare any database with a backup manifest (used after a migration).
Connection URLs for restore/verify come from environment variables (``--server-url-env``,
``--url-env``), never from the command line.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "cloud" / "db" / "migrations"
EXE = ".exe" if os.name == "nt" else ""


class BackupError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def read_database_url(env_file: Path, key: str = "CAREERCLOUD_DATABASE_URL") -> str:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(key + "="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise BackupError(f"{key} is not set in {env_file}")


def pg_env(url: str) -> Dict[str, str]:
    """PG* variables for the PostgreSQL tools (keeps the password off every command line)."""
    from psycopg.conninfo import conninfo_to_dict

    parts = conninfo_to_dict(url)
    names = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
             "dbname": "PGDATABASE", "sslmode": "PGSSLMODE"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update({names[k]: str(v) for k, v in parts.items() if k in names and v not in (None, "")})
    return env


def describe(url: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    parts = conninfo_to_dict(url)
    host = str(parts.get("host") or "local")
    return f"{host.split('.')[0]}…:{parts.get('port') or 5432}/{parts.get('dbname') or ''}"


def tool(pg_bin: Path, name: str) -> str:
    path = pg_bin / f"{name}{EXE}"
    if not path.exists():
        raise BackupError(f"{path} not found (pass --pg-bin with PostgreSQL client tools)")
    return str(path)


def tool_major(pg_bin: Path, name: str) -> int:
    out = subprocess.run([tool(pg_bin, name), "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"(\d+)(?:\.\d+)?", out.split(")")[-1])
    if not match:
        raise BackupError(f"cannot read the version of {name}: {out!r}")
    return int(match.group(1))


def run(cmd: List[str], *, env: Optional[Mapping[str, str]] = None, quiet: bool = False) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, env=dict(env) if env is not None else None)
    if done.returncode != 0:
        # pg tools never echo the password; stderr is safe to show
        raise BackupError(f"{Path(cmd[0]).name} failed ({done.returncode}): {done.stderr.strip()[-2000:]}")
    if not quiet and done.stderr.strip():
        print(done.stderr.strip())
    return done.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def physical_disk(path: Path) -> str:
    """An identifier of the physical disk holding ``path`` (Windows: disk number; else st_dev)."""
    path = path.resolve()
    if os.name == "nt":
        letter = path.drive.rstrip(":")
        if not letter:
            raise BackupError(f"cannot tell which disk holds {path} (use a drive-letter path)")
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-Partition -DriveLetter {letter}).DiskNumber"],
                             capture_output=True, text=True).stdout.strip()
        if not out.isdigit():
            raise BackupError(f"cannot tell which physical disk holds drive {letter}:")
        return f"disk{out}"
    return f"dev{os.stat(path).st_dev}"


def acl_roles(dump: Path, pg_bin: Path) -> List[str]:
    """Roles named by the dump's GRANT/REVOKE entries (on Supabase: anon, authenticated, service_role…).
    A restore target needs them to exist, or pg_restore stops at the first grant."""
    toc = run([tool(pg_bin, "pg_restore"), "--list", str(dump)], quiet=True)
    acl_lines = [line for line in toc.splitlines() if re.search(r"\b(ACL|DEFAULT ACL)\b", line)]
    if not acl_lines:
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False, encoding="utf-8") as handle:
        handle.write("\n".join(acl_lines) + "\n")
    try:
        sql = run([tool(pg_bin, "pg_restore"), "-L", handle.name, "-f", "-", str(dump)], quiet=True)
    finally:
        os.unlink(handle.name)
    roles = set(re.findall(r"\bTO\s+\"?([A-Za-z_][\w$]*)\"?", sql)) | set(
        re.findall(r"\bFROM\s+\"?([A-Za-z_][\w$]*)\"?", sql))
    return sorted(roles - {"PUBLIC"})


def table_stats(cur, schema: str) -> Dict[str, Dict[str, object]]:
    """Exact row counts, the RLS flag, and an id/version checksum for tables that have those columns."""
    cur.execute("""select c.relname,
                          bool_or(a.attname = 'id') as has_id, bool_or(a.attname = 'version') as has_version,
                          c.relrowsecurity
                     from pg_class c join pg_namespace n on n.oid = c.relnamespace
                     join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
                    where n.nspname = %s and c.relkind in ('r', 'p')
                    group by c.relname, c.relrowsecurity order by c.relname""", (schema,))
    stats: Dict[str, Dict[str, object]] = {}
    for name, has_id, has_version, rls in cur.fetchall():
        ident = f'"{schema}"."{name}"'
        cur.execute(f"select count(*) from {ident}")
        entry: Dict[str, object] = {"rows": cur.fetchone()[0], "rls": bool(rls)}
        if has_id:
            expr = "id::text || ':' || coalesce(version::text, '')" if has_version else "id::text"
            cur.execute(f"select md5(coalesce(string_agg({expr}, ',' order by id::text), '')) from {ident}")
            entry["checksum"] = cur.fetchone()[0]
        stats[name] = entry
    return stats


# --------------------------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------------------------

def backup(url: str, *, pg_bin: Path, out_dir: Path, offsite: Optional[Path], schema: str = "careercloud",
           label: str = "sanagtm", allow_same_disk: bool = False, use_snapshot: bool = True) -> Dict[str, object]:
    import psycopg

    out_dir.mkdir(parents=True, exist_ok=True)
    if offsite is None and not allow_same_disk:
        raise BackupError("--offsite is required: the only copy must not live on this machine")
    if offsite is not None:
        offsite.mkdir(parents=True, exist_ok=True)
        if physical_disk(offsite) == physical_disk(out_dir) and not allow_same_disk:
            raise BackupError(f"{offsite} is on the same physical disk as {out_dir}; use a USB/second disk "
                              "or a network share on another machine")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{label}_{schema}_{stamp}"
    dump = out_dir / f"{name}.dump"
    partial = dump.with_suffix(".dump.partial")
    env = pg_env(url)

    with psycopg.connect(url, autocommit=False) as conn:
        cur = conn.cursor()
        cur.execute("set transaction isolation level repeatable read, read only")
        cur.execute("show server_version_num")
        server_major = int(cur.fetchone()[0]) // 10000
        dump_major = tool_major(pg_bin, "pg_dump")
        if dump_major < server_major:
            raise BackupError(f"pg_dump {dump_major} cannot dump a PostgreSQL {server_major} server; "
                              f"use PostgreSQL {server_major}+ client tools")
        snapshot = None
        if use_snapshot:
            cur.execute("select pg_export_snapshot()")
            snapshot = cur.fetchone()[0]
        print(f"source {describe(url)} (PostgreSQL {server_major}); counting rows in the snapshot …")
        stats = table_stats(cur, schema)
        # Privileges are kept: the app's RLS path (SET ROLE authenticated) needs the schema/table grants.
        cmd = [tool(pg_bin, "pg_dump"), "--format=custom", "--compress=6", f"--schema={schema}", "--no-owner",
               f"--file={partial}"]
        if snapshot:
            cmd.append(f"--snapshot={snapshot}")
        started = time.monotonic()
        print(f"pg_dump {dump_major} → {dump.name} …")
        run(cmd, env=env)
        conn.rollback()
    partial.replace(dump)
    toc = run([tool(pg_bin, "pg_restore"), "--list", str(dump)], quiet=True)
    data_entries = set(re.findall(rf"TABLE DATA {re.escape(schema)} (\S+) ", toc))
    missing = sorted(t for t in stats if t not in data_entries)
    if missing:
        raise BackupError(f"the dump has no data entry for: {', '.join(missing)}")
    manifest: Dict[str, object] = {
        "file": dump.name, "bytes": dump.stat().st_size, "sha256": sha256(dump), "schema": schema,
        "grant_roles": acl_roles(dump, pg_bin),
        "created_at": stamp, "source": describe(url), "server_major": server_major, "pg_dump_major": dump_major,
        "snapshot": bool(snapshot), "seconds": round(time.monotonic() - started, 1),
        "toc_entries": toc.count("\n"), "tables": stats,
        "rows_total": sum(int(s["rows"]) for s in stats.values()),
    }
    manifest_path = dump.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    if offsite is not None:
        for source in (dump, manifest_path):
            shutil.copy2(source, offsite / source.name)
        copied = offsite / dump.name
        if sha256(copied) != manifest["sha256"]:
            raise BackupError(f"the off-site copy {copied} does not match the dump's SHA-256")
        manifest["offsite"] = str(copied)
    print(f"OK {dump} ({manifest['bytes'] / 1048576:.1f} MB, {manifest['rows_total']:,} rows in "
          f"{len(stats)} tables, sha256 {str(manifest['sha256'])[:16]}…)"
          + (f"; verified copy at {manifest['offsite']}" if offsite else "; NO off-site copy"))
    return manifest


# --------------------------------------------------------------------------------------------
# throwaway cluster + restore test
# --------------------------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Cluster:
    """A disposable PostgreSQL cluster bound to 127.0.0.1 (trust auth, deleted on stop)."""

    def __init__(self, pg_bin: Path, root: Path):
        self.pg_bin, self.data, self.port = pg_bin, root / f"pg-{uuid.uuid4().hex[:8]}", free_port()
        self.log = self.data.with_suffix(".log")

    def url(self, db: str = "postgres") -> str:
        return f"postgresql://postgres@127.0.0.1:{self.port}/{db}"

    def __enter__(self) -> "Cluster":
        run([tool(self.pg_bin, "initdb"), "-D", str(self.data), "-U", "postgres", "--auth=trust",
             "--encoding=UTF8", "--no-instructions"], quiet=True)
        # No pipes: the server inherits pg_ctl's handles, and a captured pipe would never reach EOF.
        started = subprocess.run([tool(self.pg_bin, "pg_ctl"), "-D", str(self.data), "-l", str(self.log), "-w",
                                  "-t", "60", "-o", f"-p {self.port} -c listen_addresses=127.0.0.1", "start"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if started.returncode != 0:
            raise BackupError(f"the throwaway PostgreSQL did not start (see {self.log})")
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run([tool(self.pg_bin, "pg_ctl"), "-D", str(self.data), "-m", "fast", "-w", "stop"],
                       capture_output=True)
        shutil.rmtree(self.data, ignore_errors=True)
        try:
            self.log.unlink()
        except OSError:
            pass


def compat_sql() -> str:
    """The Supabase-compatibility block of migration 0001 (role ``authenticated``, ``auth.uid()``)."""
    text = (MIGRATIONS / "0001_careercloud_core.sql").read_text(encoding="utf-8")
    match = re.search(r"do \$compat\$.*?\$compat\$;", text, re.S)
    if not match:
        raise BackupError("migration 0001 has no $compat$ block")
    return match.group(0)


def _load_manifest(dump: Path, manifest: Optional[Mapping[str, object]]) -> Mapping[str, object]:
    manifest = manifest or json.loads(dump.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if sha256(dump) != manifest["sha256"]:
        raise BackupError("the dump's SHA-256 does not match its manifest")
    return manifest


def _with_db(url: str, database: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    return make_conninfo(**{**conninfo_to_dict(url), "dbname": database})


def _prepare_target(url: str, dump: Path, manifest: Mapping[str, object], pg_bin: Path,
                    owner: Optional[str]) -> None:
    """Roles the dump's grants name, the ``authenticated`` role, and the 0001 compatibility block
    (``auth.uid()``) — run as ``owner`` when given, exactly like migrations on a fresh install."""
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        for role in ["authenticated", *(manifest.get("grant_roles") or acl_roles(dump, pg_bin))]:
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f'create role "{role}" nologin' + (" noinherit" if role == "authenticated" else ""))
        if owner:
            conn.execute(f'grant authenticated to "{owner}"')
            conn.execute(f'set role "{owner}"')
        conn.execute(compat_sql())


def _restore(url: str, dump: Path, pg_bin: Path, owner: Optional[str]) -> float:
    started = time.monotonic()
    cmd = [tool(pg_bin, "pg_restore"), "--exit-on-error", "--no-owner"]
    if owner:
        cmd.append(f"--role={owner}")
    env = pg_env(url)  # credentials via PG* variables: a URL in argv would show the password in `ps`
    run([*cmd, f"--dbname={env['PGDATABASE']}", str(dump)], env=env, quiet=True)
    return round(time.monotonic() - started, 1)


def verify_database(url: str, manifest: Mapping[str, object]) -> Dict[str, object]:
    """Rows, checksums and RLS per table against the manifest; RLS stranger probe; the app's boot
    schema check. Read-only."""
    import psycopg

    schema = str(manifest["schema"])
    problems: List[str] = []
    report: Dict[str, object] = {}
    with psycopg.connect(url, autocommit=False) as conn:
        cur = conn.cursor()
        restored = table_stats(cur, schema)
        for table, expected in dict(manifest["tables"]).items():
            got = restored.get(table)
            if got is None:
                problems.append(f"{table}: missing")
            elif got != expected:
                problems.append(f"{table}: expected {expected}, restored {got}")  # rows, checksum and RLS
        report["rls_tables"] = sum(1 for s in restored.values() if s.get("rls"))
        report["schema_migrations"] = (restored.get("schema_migrations") or {}).get("rows")
        if "workspaces" in restored:  # a stranger sees nothing through RLS
            cur.execute("select set_config('request.jwt.claims', %s, true)",
                        (json.dumps({"sub": str(uuid.uuid4()), "role": "authenticated"}),))
            cur.execute("set local role authenticated")
            cur.execute(f'select count(*) from "{schema}".workspaces')
            visible = cur.fetchone()[0]
            if visible:
                problems.append(f"RLS probe: a stranger sees {visible} workspaces")
            report["rls_probe_rows"] = visible
        conn.rollback()
    if schema == "careercloud":
        from psycopg_pool import ConnectionPool

        sys.path.insert(0, str(REPO))
        from cloud.intel.store.schema_check import require_schema, schema_report

        pool = ConnectionPool(url, min_size=1, max_size=1, open=True)
        try:
            schema = schema_report(pool)
            if schema["pending_migrations"] and not schema["changed_migrations"]:
                # A backup from an older release (e.g. the hosted database before an upgrade): the
                # install's "migrate" service applies these next; only edited migrations are an error.
                report["pending_migrations"] = list(schema["pending_migrations"])
                if schema["tables_without_rls"]:
                    problems.append("row-level security is off on: " + ", ".join(schema["tables_without_rls"]))
            else:
                check = require_schema(pool)
                report["schema_check"] = f"{check['tables_verified']}/{check['tables_expected']} tables"
        except Exception as error:  # noqa: BLE001 - reported, not raised
            problems.append(f"boot schema check: {error}")
        finally:
            pool.close()
    report["tables"] = len(restored)
    report["rows_total"] = sum(int(s["rows"]) for s in restored.values())
    report["problems"] = problems
    report["ok"] = not problems
    return report


def _print_report(title: str, report: Mapping[str, object]) -> None:
    print((f"{title} PASSED" if report["ok"] else f"{title} FAILED") + ": "
          + json.dumps({k: v for k, v in report.items() if k != "problems"}, default=str))
    for problem in report["problems"]:  # type: ignore[union-attr]
        print("  -", problem)


def restore_test(dump: Path, *, pg_bin: Path, manifest: Optional[Mapping[str, object]] = None,
                 workdir: Optional[Path] = None, server_url: Optional[str] = None) -> Dict[str, object]:
    """Restore into something disposable and verify it: a scratch database on ``server_url`` (an
    admin connection; the database is dropped afterwards), else a throwaway local cluster."""
    import psycopg

    manifest = _load_manifest(dump, manifest)
    restore_major = tool_major(pg_bin, "pg_restore")
    if restore_major < int(manifest["server_major"]):
        raise BackupError(f"restore with PostgreSQL {manifest['server_major']}+ tools (have {restore_major})")
    scratch = f"restore_test_{uuid.uuid4().hex[:10]}"

    def run_on(admin_url: str) -> Dict[str, object]:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'create database "{scratch}"')
        url = _with_db(admin_url, scratch)
        try:
            _prepare_target(url, dump, manifest, pg_bin, owner=None)
            return {"dump": dump.name, "restore_major": restore_major,
                    "restore_seconds": _restore(url, dump, pg_bin, owner=None), **verify_database(url, manifest)}
        finally:
            with psycopg.connect(admin_url, autocommit=True) as conn:
                conn.execute(f'drop database if exists "{scratch}" with (force)')

    if server_url:
        report = run_on(server_url)
    else:
        root = Path(workdir or tempfile.mkdtemp(prefix="sana-restore-"))
        root.mkdir(parents=True, exist_ok=True)
        with Cluster(pg_bin, root) as cluster:
            report = run_on(cluster.url())
        if workdir is None:
            shutil.rmtree(root, ignore_errors=True)
    _print_report("RESTORE TEST", report)
    return report


def restore_live(dump: Path, *, pg_bin: Path, server_url: str, database: str, owner: str,
                 replace: bool = False, confirm: Optional[str] = None,
                 manifest: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """Restore into the install's real database (``server_url`` = admin connection). An empty
    database (no application tables) is restored into; one with tables only with ``replace`` and
    ``confirm == database`` — it is then dropped and recreated owned by ``owner``. The caller
    stops the API and worker first and takes a backup of what is there."""
    import psycopg

    manifest = _load_manifest(dump, manifest)
    schema = str(manifest["schema"])
    url = _with_db(server_url, database)
    with psycopg.connect(server_url, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname = %s", (database,)).fetchone()
    has_tables = False
    if exists:
        with psycopg.connect(url) as conn:
            has_tables = conn.execute("select count(*) from pg_tables where schemaname = %s",
                                      (schema,)).fetchone()[0] > 0
    if has_tables:
        if not replace or confirm != database:
            raise BackupError(f"database {database!r} already holds {schema} tables; restoring replaces it — "
                              f"pass --replace --confirm {database} after taking a backup")
        with psycopg.connect(server_url, autocommit=True) as conn:
            conn.execute(f'drop database "{database}" with (force)')
        exists = None
    if not exists:
        with psycopg.connect(server_url, autocommit=True) as conn:
            conn.execute(f'create database "{database}" owner "{owner}"')
    _prepare_target(url, dump, manifest, pg_bin, owner=owner)
    report = {"dump": dump.name, "database": database, "replaced": has_tables,
              "restore_seconds": _restore(url, dump, pg_bin, owner=owner), **verify_database(url, manifest)}
    _print_report("RESTORE", report)
    return report


# --------------------------------------------------------------------------------------------
# selftest: the whole procedure on local throwaway clusters
# --------------------------------------------------------------------------------------------

def _seed(url: str) -> None:
    """Migrations + a realistic slice of data (workspace, monitor run, postings, history)."""
    sys.path.insert(0, str(REPO))
    from cloud.db.migrate import apply_migrations
    from cloud.intel.core.context import Ctx
    from cloud.intel.platform import Platform, PlatformConfig
    from cloud.intel.store.postgres import PostgresStore
    from cloud.intel.tasks.worker import run_task_inline
    from cloud.shared.storage import LocalFileStorage
    from cloud.tests.test_job_monitor import FAST, START, FakeSession, fake_resolver, job, site
    from cloud.intel.core.http import SafeFetcher

    apply_migrations(url)
    store = PostgresStore.from_url(url, max_size=2)
    try:
        user = str(uuid.uuid4())
        ws = store.create_workspace(user, "Backup selftest", f"bk-{uuid.uuid4().hex[:6]}")
        ctx = Ctx(ws["id"], user, "owner")
        pages = site([job(n) for n in range(1, 61)], per_page=6)
        with tempfile.TemporaryDirectory() as files:
            platform = Platform(store, storage=LocalFileStorage(Path(files)), config=PlatformConfig(extra={
                **FAST, "fetcher_factory": lambda: SafeFetcher(session=FakeSession(pages), resolver=fake_resolver(),
                                                               per_host_delay=0)}))
            svc = platform.service("job_monitors")
            monitor = svc.create_monitor(ctx, {"source_url": START, "name": "Selftest monitor"})
            run_ = svc.start_run(ctx, monitor["id"], mode="full")
            run_task_inline(platform, ctx.workspace_id, run_["task_id"])
            svc.evaluate_lifecycle(ctx, monitor["id"], notify=False)
    finally:
        store.close()


def selftest(pg_bin: Path, workdir: Path) -> bool:
    """Every mode on local throwaway clusters: backup (snapshot + manifest + verified copy),
    restore-test into a throwaway cluster and into a scratch database, live restore into an
    install-shaped database (owner role, refusal without --replace, replace with confirmation),
    and refusal of a tampered copy."""
    import psycopg

    workdir.mkdir(parents=True, exist_ok=True)
    out, offsite = workdir / "out", workdir / "offsite"
    checks: Dict[str, bool] = {}
    with Cluster(pg_bin, workdir) as source:
        with psycopg.connect(source.url(), autocommit=True) as conn:
            conn.execute("create database sana_source")
        url = source.url("sana_source")
        _seed(url)
        manifest = backup(url, pg_bin=pg_bin, out_dir=out, offsite=offsite, label="selftest", allow_same_disk=True)
    dump = offsite / str(manifest["file"])
    print(f"selftest dump: {manifest['rows_total']:,} rows, job_postings={manifest['tables']['job_postings']}")
    checks["restore-test (throwaway cluster)"] = bool(
        restore_test(dump, pg_bin=pg_bin, workdir=workdir / "restore")["ok"])
    with Cluster(pg_bin, workdir) as target:
        admin = target.url()
        checks["restore-test (scratch database)"] = bool(restore_test(dump, pg_bin=pg_bin, server_url=admin)["ok"])
        with psycopg.connect(admin, autocommit=True) as conn:   # what deploy/selfhost/postgres/initdb does
            conn.execute("create role sanagtm login password 'selftest-only'")
            if not conn.execute("select 1 from pg_roles where rolname = 'authenticated'").fetchone():
                conn.execute("create role authenticated nologin noinherit")
            conn.execute("grant authenticated to sanagtm")
            conn.execute("create database sanagtm owner sanagtm")
        live = restore_live(dump, pg_bin=pg_bin, server_url=admin, database="sanagtm", owner="sanagtm")
        with psycopg.connect(_with_db(admin, "sanagtm")) as conn:
            owners = {r[0] for r in conn.execute(
                "select distinct tableowner from pg_tables where schemaname = 'careercloud'").fetchall()}
        checks["restore (live, owned by the app role)"] = bool(live["ok"]) and owners == {"sanagtm"}
        try:
            restore_live(dump, pg_bin=pg_bin, server_url=admin, database="sanagtm", owner="sanagtm")
            checks["restore refuses to overwrite without --replace"] = False
        except BackupError:
            checks["restore refuses to overwrite without --replace"] = True
        again = restore_live(dump, pg_bin=pg_bin, server_url=admin, database="sanagtm", owner="sanagtm",
                             replace=True, confirm="sanagtm")
        checks["restore --replace --confirm"] = bool(again["ok"]) and bool(again["replaced"])
    bad = workdir / "tampered.dump"
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad.write_bytes(bytes(data))
    try:
        restore_test(bad, pg_bin=pg_bin, manifest=manifest)
        checks["tampered copy refused"] = False
    except BackupError as error:
        checks["tampered copy refused"] = "SHA-256" in str(error)
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    shutil.rmtree(workdir, ignore_errors=True)
    return all(checks.values())


def prune(directory: Path, *, daily: int, weekly: int, monthly: int,
          now: Optional[dt.datetime] = None) -> List[str]:
    """Delete old dumps (and their manifests): keep the newest per day for ``daily`` days, per ISO
    week for ``weekly`` weeks, per month for ``monthly`` months, the newest dump overall, and any
    ``pre-*`` safety dump younger than 30 days. Returns the deleted dump names."""
    now = now or dt.datetime.now(dt.timezone.utc)
    dumps = []
    for manifest_path in directory.glob("*.manifest.json"):
        try:
            created = dt.datetime.strptime(json.loads(manifest_path.read_text(encoding="utf-8"))["created_at"],
                                           "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        except (OSError, ValueError, KeyError):
            continue  # never delete what cannot be dated
        dumps.append((created, manifest_path.name[: -len(".manifest.json")]))
    dumps.sort(reverse=True)
    keep = {dumps[0][1]} if dumps else set()
    buckets = ((daily, lambda d: d.date()), (weekly, lambda d: tuple(d.isocalendar())[:2]),
               (monthly, lambda d: (d.year, d.month)))
    for limit, key in buckets:
        seen: List[object] = []
        for created, name in dumps:
            bucket = key(created)
            if bucket not in seen and len(seen) < limit:
                seen.append(bucket)
                keep.add(name)
    for created, name in dumps:
        if "-pre-" in name and now - created < dt.timedelta(days=30):
            keep.add(name)
    deleted = []
    for _created, name in dumps:
        if name not in keep:
            for suffix in (".dump", ".manifest.json"):
                (directory / f"{name}{suffix}").unlink(missing_ok=True)
            deleted.append(name)
    return deleted


# --------------------------------------------------------------------------------------------

def _env_url(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise BackupError(f"{name} is not set (connection URLs come from the environment, never the command line)")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--env-file", type=Path, help="read CAREERCLOUD_DATABASE_URL from this file")
    b.add_argument("--url-env", default="CAREERCLOUD_DATABASE_URL",
                   help="else from this environment variable (default CAREERCLOUD_DATABASE_URL)")
    b.add_argument("--pg-bin", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--offsite", type=Path)
    b.add_argument("--local-only", action="store_true",
                   help="no off-site copy now (self-hosted nightly job; the host copies backups off the machine)")
    b.add_argument("--schema", default="careercloud")
    b.add_argument("--label", default="sanagtm")
    b.add_argument("--no-snapshot", action="store_true", help="if the pooler refuses snapshot import")
    r = sub.add_parser("restore-test")
    r.add_argument("--dump", type=Path, required=True)
    r.add_argument("--pg-bin", type=Path, required=True)
    r.add_argument("--workdir", type=Path)
    r.add_argument("--server-url-env", help="restore into a scratch database on this server (admin URL variable)")
    lv = sub.add_parser("restore", help="restore into the install's database (stop the API and worker first)")
    lv.add_argument("--dump", type=Path, required=True)
    lv.add_argument("--pg-bin", type=Path, required=True)
    lv.add_argument("--server-url-env", required=True, help="admin connection URL variable")
    lv.add_argument("--database", default="sanagtm")
    lv.add_argument("--owner", default="sanagtm")
    lv.add_argument("--replace", action="store_true")
    lv.add_argument("--confirm")
    v = sub.add_parser("verify", help="compare a database with a backup manifest (read-only)")
    v.add_argument("--manifest", type=Path, required=True)
    v.add_argument("--url-env", default="CAREERCLOUD_DATABASE_URL")
    pr = sub.add_parser("prune", help="retention: keep daily/weekly/monthly dumps, delete the rest")
    pr.add_argument("--dir", type=Path, required=True)
    pr.add_argument("--daily", type=int, default=14)
    pr.add_argument("--weekly", type=int, default=8)
    pr.add_argument("--monthly", type=int, default=6)
    s = sub.add_parser("selftest")
    s.add_argument("--pg-bin", type=Path, required=True)
    s.add_argument("--workdir", type=Path, default=REPO / "cloud" / ".localdev" / "backup-selftest")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            url = read_database_url(args.env_file) if args.env_file else _env_url(args.url_env)
            backup(url, pg_bin=args.pg_bin, out_dir=args.out, offsite=args.offsite, schema=args.schema,
                   label=args.label, allow_same_disk=args.local_only, use_snapshot=not args.no_snapshot)
            return 0
        if args.command == "restore-test":
            server = _env_url(args.server_url_env) if args.server_url_env else None
            return 0 if restore_test(args.dump, pg_bin=args.pg_bin, workdir=args.workdir,
                                     server_url=server)["ok"] else 1
        if args.command == "restore":
            report = restore_live(args.dump, pg_bin=args.pg_bin, server_url=_env_url(args.server_url_env),
                                  database=args.database, owner=args.owner, replace=args.replace,
                                  confirm=args.confirm)
            return 0 if report["ok"] else 1
        if args.command == "verify":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            report = verify_database(_env_url(args.url_env), manifest)
            _print_report("VERIFY", report)
            return 0 if report["ok"] else 1
        if args.command == "prune":
            deleted = prune(args.dir, daily=args.daily, weekly=args.weekly, monthly=args.monthly)
            print(f"pruned {len(deleted)} old backup(s)" + (": " + ", ".join(deleted) if deleted else ""))
            return 0
        ok = selftest(args.pg_bin, args.workdir)
        print("SELFTEST PASSED" if ok else "SELFTEST FAILED")
        return 0 if ok else 1
    except BackupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
