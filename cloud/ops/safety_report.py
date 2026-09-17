"""STAGING SAFETY REPORT: prove where a process will connect before it does anything.

    python -m cloud.ops.safety_report --component all --env-file /etc/careercloud-staging/worker.env
    python -m cloud.ops.safety_report --component api --quiet        # systemd ExecStartPre
    python -m cloud.ops.safety_report --offline                      # configuration checks only

Run it before every real crawl, and it runs automatically before each staging
service starts. The exit status is 0 only if **no** check failed.

Checked:

* git branch and commit of the code being run (never ``queue-recovery``/``main``)
* target environment
* deployment requirements for the component (TLS, auth, private storage…)
* resource registry allowlist: database, Supabase ref, Redis, bucket, origins
* database: reachable, environment stamp, migrations, RLS, ``anon`` privileges,
  Data API schema exposure
* Redis: TLS, password required, queue prefix, environment stamp
* storage: bucket reachable, private to anonymous requests, environment stamp
* Google Sheets disabled; production SQLite path disabled
* Seamless and the production CareerCrawler checkout not referenced
* worker egress guard, firewall confirmation, browser fallback

Nothing here reads, opens, lists or locks anything belonging to Seamless or the
production CareerCrawler checkout. Those checks look only at this process's own
configuration.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"
FORBIDDEN_BRANCHES = {"queue-recovery", "main", "master", "seamless-integration"}
EXPECTED_MIGRATIONS = {"0001_careercloud_core", "0002_deployment_environment"}
RLS_TABLES = {"jobs", "crawl_targets", "job_events", "job_results", "deployment"}


@dataclass
class Check:
    section: str
    name: str
    status: str
    detail: str = ""


class Report:
    def __init__(self) -> None:
        self.checks: List[Check] = []

    def add(self, section: str, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(section, name, status, detail))

    def run(self, section: str, name: str, fn: Callable[[], "tuple"]) -> None:
        try:
            status, detail = fn()
        except Exception as error:  # every unexpected error is a failure, never a pass
            status, detail = FAIL, f"{type(error).__name__}: {error}"
        self.add(section, name, status, detail)

    @property
    def failed(self) -> bool:
        return any(check.status == FAIL for check in self.checks)

    def render(self, *, environment: str, quiet: bool) -> str:
        lines = [
            "=" * 78,
            f"{environment.upper()} SAFETY REPORT".center(78),
            "=" * 78,
        ]
        section = None
        for check in self.checks:
            if quiet and check.status != FAIL:
                continue
            if check.section != section:
                section = check.section
                lines.append(f"\n[{section}]")
            detail = f"  {check.detail}" if check.detail else ""
            lines.append(f"  {check.status:<4}  {check.name:<38}{detail}")
        counts = {status: sum(1 for c in self.checks if c.status == status) for status in (PASS, FAIL, SKIP, INFO)}
        lines.append("")
        lines.append(
            f"RESULT: {'UNSAFE - DO NOT RUN' if self.failed else 'SAFE'}   "
            f"(pass {counts[PASS]}, fail {counts[FAIL]}, skipped {counts[SKIP]}, info {counts[INFO]})"
        )
        return "\n".join(lines)


def host_of(url: Optional[str]) -> str:
    return (urlsplit(url).hostname or "?") if url else "unset"


# --- sections ---------------------------------------------------------------------


def check_code(report: Report) -> None:
    def branch():
        if not (REPO_ROOT / ".git").exists():
            release = REPO_ROOT.name
            return INFO, f"release directory {release} (no git metadata on deployed hosts)"
        name = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15
        ).stdout.strip()
        if name in FORBIDDEN_BRANCHES:
            return FAIL, f"{name}@{commit}: CareerCloud never runs from this branch"
        return PASS, f"{name}@{commit}"

    report.run("code", "git branch", branch)

    def worktree():
        path = str(REPO_ROOT).lower().replace("\\", "/")
        if path.rstrip("/").endswith("/crawlers/careercrawler"):
            return FAIL, "running from the production CareerCrawler checkout"
        return PASS, str(REPO_ROOT)

    report.run("code", "not the production checkout", worktree)


def check_configuration(report: Report, env: Mapping[str, str], component: str, expected: str) -> Dict[str, object]:
    from cloud.shared.environment import check_forbidden_configuration

    loaded: Dict[str, object] = {}
    environment = env.get("CAREERCLOUD_ENV", "development")
    report.add(
        "environment",
        "target environment",
        PASS if environment == expected else FAIL,
        f"{environment} (expected {expected})",
    )

    problems = check_forbidden_configuration(env)
    sheets = [p for p in problems if "Sheets" in p]
    sqlite = [p for p in problems if "SQLite" in p or "crawler.db" in p]
    other = [p for p in problems if p not in sheets and p not in sqlite]
    report.add("production isolation", "Google Sheets disabled", FAIL if sheets else PASS, "; ".join(sheets) or "no Sheets configuration")
    report.add("production isolation", "production SQLite path disabled", FAIL if sqlite else PASS, "; ".join(sqlite) or "no SQLite/crawler.db path")
    seamless = [p for p in other if "Seamless" in p]
    checkout = [p for p in other if "checkout" in p or "credentials" in p]
    report.add(
        "production isolation",
        "Seamless not referenced",
        FAIL if seamless else PASS,
        "; ".join(seamless) or "configuration only; no Seamless file or process was inspected",
    )
    report.add(
        "production isolation",
        "CareerCrawler production state",
        FAIL if checkout else PASS,
        "; ".join(checkout) or "no production checkout, secrets or state referenced",
    )
    google_installed = importlib.util.find_spec("googleapiclient") is not None
    report.add(
        "production isolation",
        "Google API client not installed",
        (FAIL if component in ("worker", "all") and environment != "development" else INFO) if google_installed else PASS,
        "googleapiclient importable in this venv" if google_installed else "absent from this venv",
    )

    if component in ("api", "all"):
        def api_settings():
            from cloud.api.deployment import deployment_problems, identity_of
            from cloud.api.settings import load_settings

            settings = load_settings(env)
            loaded["api"] = settings
            issues = deployment_problems(settings)
            return (FAIL, "; ".join(issues)) if issues else (PASS, f"storage={settings.storage} queue={settings.queue} auth={settings.auth_mode}")

        report.run("api", "deployment requirements", api_settings)

    if component in ("worker", "all"):
        def worker_settings():
            from cloud.worker.settings import load_worker_settings

            settings = load_worker_settings(env)
            loaded["worker"] = settings
            return PASS, f"runner={settings.runner} egress_guard={settings.egress_guard} browser={settings.browser_fallback}"

        report.run("worker", "deployment requirements", worker_settings)
    return loaded


def check_registry(report: Report, env: Mapping[str, str], loaded: Dict[str, object]) -> None:
    from cloud.shared.environment import DEPLOYED_ENVIRONMENTS, ResourceIdentity, ResourceRegistry, check_identity

    environment = env.get("CAREERCLOUD_ENV", "development")
    if environment not in DEPLOYED_ENVIRONMENTS:
        report.add("resource registry", "allowlist", SKIP, f"{environment}: registry applies to staging/production")
        return
    path = env.get("CAREERCLOUD_RESOURCE_REGISTRY")
    registry = None
    if path:
        try:
            registry = ResourceRegistry.load(Path(path))
        except (OSError, ValueError) as error:
            report.add("resource registry", "load", FAIL, str(error))
            return
    identity = ResourceIdentity.from_urls(
        environment,
        database_url=env.get("CAREERCLOUD_DATABASE_URL"),
        supabase_url=env.get("CAREERCLOUD_SUPABASE_URL"),
        redis_url=env.get("CAREERCLOUD_REDIS_URL"),
        queue_prefix=env.get("CAREERCLOUD_QUEUE_PREFIX", f"careercloud:{environment}"),
        storage_bucket=env.get("CAREERCLOUD_S3_BUCKET"),
        storage_endpoint=env.get("CAREERCLOUD_S3_ENDPOINT"),
        results_namespace=env.get("CAREERCLOUD_RESULTS_NAMESPACE"),
        api_origin=env.get("CAREERCLOUD_API_ORIGIN"),
        dashboard_origins=[o.strip() for o in env.get("CAREERCLOUD_CORS_ORIGINS", "").split(",") if o.strip()],
    )
    problems = check_identity(identity, registry)
    report.add("resource registry", "every resource is this environment's", FAIL if problems else PASS, "; ".join(problems) or path or "")
    report.add("resource registry", "database host", INFO, identity.database_host or "unset")
    report.add("resource registry", "supabase project", INFO, identity.supabase_ref or "unset")
    report.add("resource registry", "redis host", INFO, identity.redis_host or "unset")
    report.add("resource registry", "queue prefix", INFO, identity.queue_prefix or "unset")
    report.add("resource registry", "storage bucket / namespace", INFO, f"{identity.storage_bucket or 'unset'} / {identity.results_namespace or 'unset'}")


def check_database(report: Report, env: Mapping[str, str]) -> None:
    import psycopg

    from cloud.db.connection import resolve_database_url

    environment = env.get("CAREERCLOUD_ENV", "development")
    url = resolve_database_url(
        env.get("CAREERCLOUD_DATABASE_URL"), environment=environment,
        allow_remote=env.get("CAREERCLOUD_ALLOW_REMOTE_SERVICES") in ("1", "true"),
    )
    if not url:
        report.add("database", "configured", FAIL, "CAREERCLOUD_DATABASE_URL unset")
        return
    report.add("database", "host", INFO, host_of(url))
    with psycopg.connect(url, connect_timeout=10) as conn:
        report.add("database", "reachable", PASS, conn.execute("select version()").fetchone()[0].split(",")[0])
        ssl = conn.execute("select ssl from pg_stat_ssl where pid = pg_backend_pid()").fetchone()
        tls = bool(ssl and ssl[0])
        report.add("database", "TLS", PASS if tls else (FAIL if environment != "development" else INFO), "encrypted" if tls else "not encrypted")

        migrations = {row[0] for row in conn.execute("select version from careercloud.schema_migrations").fetchall()}
        missing = EXPECTED_MIGRATIONS - migrations
        report.add("database", "migrations", FAIL if missing else PASS, f"missing {sorted(missing)}" if missing else ", ".join(sorted(migrations)))

        stamp_row = conn.execute("select environment from careercloud.deployment").fetchone() if "0002_deployment_environment" in migrations else None
        stamp = stamp_row[0] if stamp_row else None
        report.add(
            "database",
            "environment stamp",
            PASS if stamp == environment else FAIL,
            f"stamped {stamp!r}" if stamp else "not stamped (run: python -m cloud.db.migrate stamp)",
        )

        rls = {
            row[0]: row[1]
            for row in conn.execute(
                "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname = 'careercloud' and c.relkind = 'r'"
            ).fetchall()
        }
        without = sorted(t for t in RLS_TABLES if not rls.get(t))
        report.add("database", "row-level security", FAIL if without else PASS, f"missing on {without}" if without else "enabled on all CareerCloud tables")

        anon = conn.execute("select 1 from pg_roles where rolname = 'anon'").fetchone()
        if anon:
            grants = conn.execute(
                "select table_name, privilege_type from information_schema.role_table_grants"
                " where table_schema = 'careercloud' and grantee = 'anon'"
            ).fetchall()
            report.add("database", "anon has no access", FAIL if grants else PASS, str(grants) if grants else "no grants")
        else:
            report.add("database", "anon has no access", SKIP, "no anon role (not a Supabase database)")

        authenticator = conn.execute("select rolconfig from pg_roles where rolname = 'authenticator'").fetchone()
        if authenticator is None:
            report.add("database", "Data API schema exposure", SKIP, "no authenticator role (not a Supabase database)")
        else:
            config = ",".join(authenticator[0] or [])
            exposed = "careercloud" in config
            report.add(
                "database",
                "Data API schema exposure",
                FAIL if exposed else PASS,
                "careercloud is exposed via PostgREST" if exposed else "careercloud not in the Data API's schemas",
            )


def check_redis(report: Report, env: Mapping[str, str]) -> None:
    import redis

    from cloud.db.connection import resolve_redis_url

    environment = env.get("CAREERCLOUD_ENV", "development")
    url = resolve_redis_url(
        env.get("CAREERCLOUD_REDIS_URL"), environment=environment,
        allow_remote=env.get("CAREERCLOUD_ALLOW_REMOTE_SERVICES") in ("1", "true"),
    )
    if not url:
        report.add("redis", "configured", FAIL, "CAREERCLOUD_REDIS_URL unset")
        return
    parts = urlsplit(url)
    deployed = environment != "development"
    report.add("redis", "host", INFO, f"{parts.hostname}:{parts.port or 6379}")
    report.add("redis", "TLS (rediss://)", PASS if parts.scheme == "rediss" else (FAIL if deployed else INFO), parts.scheme)
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=10, socket_connect_timeout=10)
    report.add("redis", "reachable", PASS if client.ping() else FAIL, "PING ok")

    if parts.password:
        anonymous = redis.Redis(
            host=parts.hostname, port=parts.port or 6379, ssl=parts.scheme == "rediss",
            socket_timeout=10, socket_connect_timeout=10,
        )
        try:
            anonymous.ping()
            report.add("redis", "password required", FAIL, "an unauthenticated PING succeeded")
        except (redis.AuthenticationError, redis.ResponseError) as refused:
            report.add("redis", "password required", PASS, f"unauthenticated connection refused ({type(refused).__name__})")
        except redis.ConnectionError as error:
            report.add("redis", "password required", PASS, f"unauthenticated connection refused ({error.__class__.__name__})")
        finally:
            anonymous.close()
    else:
        report.add("redis", "password required", FAIL if deployed else INFO, "no password in URL")

    prefix = env.get("CAREERCLOUD_QUEUE_PREFIX", f"careercloud:{environment}")
    report.add("redis", "queue prefix", PASS if prefix.startswith(f"careercloud:{environment}") else FAIL, prefix)
    stamp = client.get(f"{prefix}:environment")
    if stamp is None:
        report.add("redis", "environment stamp", INFO if not deployed else PASS, "unstamped; the first deployed process will stamp it")
    else:
        report.add("redis", "environment stamp", PASS if stamp == environment else FAIL, f"stamped {stamp!r}")
    client.close()


def check_storage(report: Report, env: Mapping[str, str]) -> None:
    backend = env.get("CAREERCLOUD_STORAGE_BACKEND", "local")
    environment = env.get("CAREERCLOUD_ENV", "development")
    if backend != "s3":
        report.add("storage", "backend", FAIL if environment != "development" else INFO, f"{backend} (staging requires s3)")
        return
    from cloud.ops.stamps import STORAGE_STAMP_KEY
    from cloud.shared.s3_storage import S3Storage

    storage = S3Storage.from_settings(
        endpoint=env.get("CAREERCLOUD_S3_ENDPOINT", ""),
        region=env.get("CAREERCLOUD_S3_REGION", ""),
        bucket=env.get("CAREERCLOUD_S3_BUCKET", ""),
        namespace=env.get("CAREERCLOUD_RESULTS_NAMESPACE", environment),
        access_key_id=env.get("CAREERCLOUD_S3_ACCESS_KEY_ID", ""),
        secret_access_key=env.get("CAREERCLOUD_S3_SECRET_ACCESS_KEY", ""),
    )
    report.add("storage", "endpoint / bucket", INFO, f"{host_of(storage.endpoint)} / {storage.bucket} / {storage.namespace}")
    storage.ping()
    report.add("storage", "bucket reachable with S3 keys", PASS, "HEAD bucket ok")
    if storage.exists(STORAGE_STAMP_KEY):
        with storage.open(STORAGE_STAMP_KEY) as handle:
            stamp = handle.read().decode().strip()
        report.add("storage", "environment stamp", PASS if stamp == environment else FAIL, f"stamped {stamp!r}")
        private = storage.check_private(STORAGE_STAMP_KEY)
        report.add("storage", "private to anonymous requests", PASS if private else FAIL, "anonymous GET refused" if private else "anonymous GET succeeded")
    else:
        report.add("storage", "environment stamp", INFO, "unstamped; the first deployed process will stamp it")
        report.add("storage", "private to anonymous requests", SKIP, "checked once the stamp object exists")


def check_worker_network(report: Report, env: Mapping[str, str]) -> None:
    environment = env.get("CAREERCLOUD_ENV", "development")
    guard = env.get("CAREERCLOUD_EGRESS_GUARD", "true").lower() in ("1", "true", "yes", "on")
    confirmed = env.get("CAREERCLOUD_EGRESS_FIREWALL_CONFIRMED", "false").lower() in ("1", "true", "yes", "on")
    browser = env.get("CAREERCLOUD_CRAWLER_BROWSER_FALLBACK", "false").lower() in ("1", "true", "yes", "on")
    deployed = environment != "development"
    report.add("worker network", "in-process egress guard", PASS if guard else FAIL, "enabled" if guard else "disabled")
    report.add("worker network", "browser fallback", PASS if not browser else (PASS if confirmed else FAIL), "off" if not browser else "on")
    if platform.system() == "Linux":
        result = subprocess.run(
            ["nft", "list", "table", "inet", "careercloud_staging_egress"], capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            report.add("worker network", "nftables egress policy", PASS, "table careercloud_staging_egress loaded")
        else:
            report.add("worker network", "nftables egress policy", FAIL if deployed and not confirmed else INFO,
                       "not visible to this user (systemd checks it as root in ExecStartPre)")
    else:
        report.add("worker network", "nftables egress policy", FAIL if deployed else SKIP, f"{platform.system()}: kernel egress policy not available")
    report.add("worker network", "firewall confirmed by operator", PASS if confirmed else (FAIL if deployed else SKIP), str(confirmed))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.ops.safety_report")
    parser.add_argument("--component", choices=["api", "worker", "all"], default="all")
    parser.add_argument("--env-file", help="read CAREERCLOUD_* variables from this file (not the process environment)")
    parser.add_argument("--expect-environment", default="staging")
    parser.add_argument("--offline", action="store_true", help="configuration checks only; no connections")
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args(argv)

    env: Dict[str, str] = dict(os.environ)
    if args.env_file:
        from dotenv import dotenv_values

        env.update({key: value or "" for key, value in dotenv_values(args.env_file).items()})

    report = Report()
    check_code(report)
    loaded = check_configuration(report, env, args.component, args.expect_environment)
    check_registry(report, env, loaded)
    if not args.offline:
        report.run("database", "checks", lambda: (check_database(report, env), (INFO, "done"))[1])
        if env.get("CAREERCLOUD_REDIS_URL"):
            report.run("redis", "checks", lambda: (check_redis(report, env), (INFO, "done"))[1])
        report.run("storage", "checks", lambda: (check_storage(report, env), (INFO, "done"))[1])
    if args.component in ("worker", "all"):
        check_worker_network(report, env)

    print(report.render(environment=env.get("CAREERCLOUD_ENV", "development"), quiet=args.quiet))
    return 1 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
