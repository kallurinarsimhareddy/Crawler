"""Self-hosted SANA GTM (deploy/selfhost): container start-up, health checks and the registry.

    python -m cloud.ops.selfhost prepare            # one-shot "migrate" service: wait for the DB,
                                                    # apply migrations, stamp a FRESH database
    python -m cloud.ops.selfhost registry           # (re)write CAREERCLOUD_RESOURCE_REGISTRY from env
    python -m cloud.ops.selfhost health api|worker  # container health checks (exit 0 = healthy)
    python -m cloud.ops.selfhost stamp-restored     # after a verified restore: stamp it for this env
    python -m cloud.ops.selfhost info               # one JSON line: schema, stamp, sizes (no secrets)

Everything reads the process environment (the compose ``env_file``). Nothing here prints a
connection string or a secret.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional
from urllib.parse import urlsplit

#: The worker writes its state file at least every 60 s; three missed beats = unhealthy.
WORKER_STALE_S = 180


class SelfHostError(RuntimeError):
    pass


def _url(env: Mapping[str, str]) -> str:
    url = (env.get("CAREERCLOUD_DATABASE_URL") or "").strip()
    if not url:
        raise SelfHostError("CAREERCLOUD_DATABASE_URL is not set")
    return url


def wait_for_database(url: str, *, timeout: float = 120.0, sleep=time.sleep) -> None:
    import psycopg

    deadline = time.monotonic() + timeout
    while True:
        try:
            with psycopg.connect(url, connect_timeout=5) as conn:
                conn.execute("select 1")
            return
        except psycopg.OperationalError as error:
            if time.monotonic() >= deadline:
                raise SelfHostError(f"database not reachable after {timeout:.0f}s ({type(error).__name__})")
            sleep(2)


def _fresh(url: str) -> bool:
    """No workspace rows yet: an install, not a restored copy of live data."""
    import psycopg

    with psycopg.connect(url) as conn:
        if not conn.execute("select to_regclass('careercloud.workspaces') is not null").fetchone()[0]:
            return True
        return conn.execute("select not exists (select 1 from careercloud.workspaces)").fetchone()[0]


def prepare(env: Mapping[str, str]) -> Dict[str, object]:
    """Migrations + (fresh only) the environment stamp + the resource registry. Idempotent."""
    from cloud.db.migrate import apply_migrations
    from cloud.ops.stamps import read_database_stamp, stamp_database

    url, environment = _url(env), env.get("CAREERCLOUD_ENV", "development")
    wait_for_database(url, timeout=float(env.get("SANAGTM_DB_WAIT_SECONDS", "120")))
    fresh_before = _fresh(url)
    # Decide BEFORE touching the schema: data without a stamp (a restored copy that nobody has
    # verified yet) or data stamped for another environment is never migrated.
    stamp = read_database_stamp(url)
    if stamp is None and not fresh_before:
        raise SelfHostError(
            "this database holds data but has no environment stamp (a restored copy?). Verify it, then run "
            "`sanagtm migrate stamp` (python -m cloud.ops.selfhost stamp-restored); nothing was changed")
    if stamp is not None and stamp != environment:
        raise SelfHostError(f"database is stamped {stamp!r} but this install is {environment!r}; nothing was changed")
    applied = apply_migrations(url)
    if stamp is None:
        stamp = stamp_database(url, environment)
    registry = write_registry(env) if env.get("CAREERCLOUD_RESOURCE_REGISTRY") else None
    return {"applied": applied, "stamp": stamp, "fresh": fresh_before, "registry": registry}


def stamp_restored(env: Mapping[str, str]) -> str:
    """Stamp a verified, restored (unstamped) database for this environment. Never restamps."""
    from cloud.ops.stamps import read_database_stamp, stamp_database

    url, environment = _url(env), env.get("CAREERCLOUD_ENV", "development")
    current = read_database_stamp(url)
    if current not in (None, environment):
        raise SelfHostError(f"database is stamped {current!r}; refusing to restamp it {environment!r}")
    return stamp_database(url, environment)


def registry_document(env: Mapping[str, str]) -> Dict[str, object]:
    """This machine's resources (and, as another environment, the legacy hosted database the
    install must never write to)."""
    from cloud.shared.environment import supabase_ref_from_host

    environment = env.get("CAREERCLOUD_ENV", "production")
    db_host = urlsplit(_url(env)).hostname or ""
    supabase_host = urlsplit(env.get("CAREERCLOUD_SUPABASE_URL", "")).hostname
    own: Dict[str, List[str]] = {
        "database_hosts": [db_host],
        "supabase_refs": [r for r in [supabase_ref_from_host(supabase_host)] if r],
        "api_origins": [o for o in [(env.get("CAREERCLOUD_API_ORIGIN") or "").rstrip("/")] if o],
        "dashboard_origins": [o.strip().rstrip("/") for o in (env.get("CAREERCLOUD_CORS_ORIGINS") or "").split(",")
                              if o.strip()],
    }
    legacy = [h.strip().lower() for h in (env.get("SANAGTM_LEGACY_DATABASE_HOSTS") or "").split(",") if h.strip()]
    doc: Dict[str, object] = {
        "_comment": ["Written by `python -m cloud.ops.selfhost registry` from /etc/sanagtm/sanagtm.env.",
                     "Identifiers only, never secrets. 'legacy-hosted' lists the old hosted database so this "
                     "install refuses to use it."],
        environment: own,
    }
    if legacy:
        doc["legacy-hosted"] = {"database_hosts": legacy}
    return doc


def write_registry(env: Mapping[str, str]) -> str:
    path = Path(env["CAREERCLOUD_RESOURCE_REGISTRY"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry_document(env), indent=1), encoding="utf-8")
    tmp.replace(path)
    return str(path)


def health_api(env: Mapping[str, str], *, port: Optional[int] = None) -> Dict[str, object]:
    port = port or int(env.get("SANAGTM_API_PORT", "8100"))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=5) as response:  # noqa: S310
        body = json.loads(response.read().decode("utf-8") or "{}")
        return {"ok": response.status == 200, "status": response.status, "body": body}


def health_worker(env: Mapping[str, str], *, now: Optional[float] = None) -> Dict[str, object]:
    import psycopg

    out: Dict[str, object] = {"ok": False, "database": False, "state_age_s": None, "state": None}
    path = env.get("CAREERCLOUD_WORKER_STATE_FILE")
    if path:
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
            updated = datetime.fromisoformat(state["updated_at"]).timestamp()
            out["state"] = state.get("state")
            out["state_age_s"] = round((now or time.time()) - updated, 1)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        with psycopg.connect(_url(env), connect_timeout=5) as conn:
            out["database"] = conn.execute("select 1").fetchone()[0] == 1
    except Exception as error:  # noqa: BLE001 - reported by class only
        out["database_error"] = type(error).__name__
    age = out["state_age_s"]
    out["ok"] = bool(out["database"]) and age is not None and float(age) < WORKER_STALE_S
    return out


def info(env: Mapping[str, str]) -> Dict[str, object]:
    import psycopg

    from cloud.ops.stamps import read_database_stamp

    url = _url(env)
    with psycopg.connect(url) as conn:
        cur = conn.cursor()
        cur.execute("set transaction read only")
        cur.execute("select current_setting('server_version'), pg_database_size(current_database())")
        version, size = cur.fetchone()
        migrations = 0
        if cur.execute("select to_regclass('careercloud.schema_migrations') is not null").fetchone()[0]:
            migrations = cur.execute("select count(*) from careercloud.schema_migrations").fetchone()[0]
        cur.execute("""select c.relname, pg_total_relation_size(c.oid) from pg_class c
                        join pg_namespace n on n.oid = c.relnamespace
                       where n.nspname = 'careercloud' and c.relkind = 'r'
                       order by 2 desc limit 8""")
        largest = [{"table": t, "mb": round(b / 1048576, 1)} for t, b in cur.fetchall()]
        conn.rollback()
    return {"server_version": version, "database_mb": round(size / 1048576, 1), "migrations": migrations,
            "stamp": read_database_stamp(url), "largest_tables": largest,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.ops.selfhost", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("prepare", "registry", "health", "stamp-restored", "info"))
    parser.add_argument("target", nargs="?", choices=("api", "worker"))
    args = parser.parse_args(argv)
    env = os.environ
    try:
        if args.command == "prepare":
            result: object = prepare(env)
        elif args.command == "registry":
            result = {"registry": write_registry(env)}
        elif args.command == "stamp-restored":
            result = {"stamp": stamp_restored(env)}
        elif args.command == "info":
            result = info(env)
        else:
            if args.target is None:
                parser.error("health needs api or worker")
            result = health_api(env) if args.target == "api" else health_worker(env)
            print(json.dumps(result, default=str))
            return 0 if result["ok"] else 1
        print(json.dumps(result, default=str))
        return 0
    except SelfHostError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - health probes must fail closed, without secrets
        print(f"error: {type(error).__name__}: {str(error)[:200]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
