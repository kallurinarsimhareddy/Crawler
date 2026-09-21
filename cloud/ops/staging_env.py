"""Render the staging environment files from one secrets file.

The staging values appear in four places — the API, the worker, the dashboard
build and the resource registry — and typing them four times is how a
production URL ends up in a staging file. So they are written once, here, and
everything else is generated.

    python -m cloud.ops.staging_env template   # write the fill-in file
    python -m cloud.ops.staging_env render     # generate the env files
    python -m cloud.ops.staging_env check      # report what is set, masked

The secrets file (``cloud/.env.staging-secrets``) and every file this writes
are git-ignored. **Nothing here ever prints a secret**: values are reported as
"set" or "missing", and anything that looks like a credential is masked even in
error messages.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from urllib.parse import urlsplit

CLOUD = Path(__file__).resolve().parent.parent
REPO = CLOUD.parent
SECRETS_FILE = CLOUD / ".env.staging-secrets"

#: name -> (required?, one-line description of where to find it)
FIELDS: Dict[str, tuple] = {
    "SUPABASE_PROJECT_REF": (True, "Supabase > Project Settings > General > Reference ID (20 lowercase letters)"),
    "SUPABASE_REGION": (True, "Supabase > Project Settings > General > Region, e.g. ap-south-1"),
    "SUPABASE_DB_PASSWORD": (True, "the database password you set when creating the project"),
    "SUPABASE_ANON_KEY": (True, "Supabase > Project Settings > API Keys > anon / publishable"),
    "UPSTASH_REDIS_URL": (True, "Upstash > your database > Connect > redis-cli, the full rediss://... URL"),
    "SUPABASE_S3_ACCESS_KEY_ID": (False, "optional: Supabase > Storage > S3 access keys"),
    "SUPABASE_S3_SECRET_ACCESS_KEY": (False, "optional: the secret shown once when you create the S3 key"),
    "SUPABASE_STORAGE_BUCKET": (False, "optional: a private bucket name, e.g. careercloud-staging-results"),
}

TEMPLATE = """# CareerCloud STAGING secrets. This file is git-ignored; keep it that way.
#
# Fill in the values below, then run:
#     cloud\\.venv\\Scripts\\python -m cloud.ops.staging_env render
#
# Nothing in this file is ever printed back to you or written to a log.
# Do not paste these values into a chat, an issue, or a commit.
#
# --- required -----------------------------------------------------------
{required}
# --- optional: private result storage -----------------------------------
# Leave these blank to keep result files on this machine instead of in
# Supabase Storage. Downloads work either way; local files simply do not
# survive moving the worker to another host.
{optional}"""


def _mask(value: Optional[str]) -> str:
    """Describe a value without revealing it."""
    if not value:
        return "(not set)"
    return f"set, {len(value)} chars"


def mask_url(url: Optional[str]) -> str:
    """A URL with its credentials removed, safe to log."""
    if not url:
        return "(not set)"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unparsable)"
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    user = f"{parts.username}:***@" if parts.username else ""
    return f"{parts.scheme}://{user}{host}{port}"


def read_secrets(path: Path = SECRETS_FILE) -> Dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. Run: python -m cloud.ops.staging_env template"
        )
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value and not value.startswith("<"):
            values[name.strip()] = value
    return values


def write_template(path: Path = SECRETS_FILE) -> Path:
    if path.exists():
        raise FileExistsError(f"{path} already exists; not overwriting it")

    def block(required: bool) -> str:
        lines = []
        for name, (is_required, hint) in FIELDS.items():
            if is_required is not required:
                continue
            lines.append(f"# {hint}")
            lines.append(f"{name}=")
            lines.append("")
        return "\n".join(lines)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        TEMPLATE.format(required=block(True), optional=block(False)), encoding="utf-8"
    )
    return path


def missing(values: Dict[str, str]) -> List[str]:
    return [name for name, (required, _) in FIELDS.items() if required and not values.get(name)]


def _validate(values: Dict[str, str]) -> List[str]:
    """Catch the mistakes that otherwise fail much later, without echoing values."""
    problems: List[str] = []
    ref = values.get("SUPABASE_PROJECT_REF", "")
    if ref and not re.fullmatch(r"[a-z]{20}", ref):
        problems.append(
            "SUPABASE_PROJECT_REF should be 20 lowercase letters — it looks like a URL or key was pasted instead"
        )
    redis_url = values.get("UPSTASH_REDIS_URL", "")
    if redis_url:
        if not redis_url.startswith("rediss://"):
            problems.append("UPSTASH_REDIS_URL must start with rediss:// (TLS), not redis://")
        elif "@" not in redis_url:
            problems.append("UPSTASH_REDIS_URL has no password in it")
    key = values.get("SUPABASE_ANON_KEY", "")
    if key and ("service_role" in key or key.startswith("sbp_")):
        problems.append(
            "SUPABASE_ANON_KEY looks like a service-role or personal key. "
            "Use the anon/publishable key; the service-role key must never be used here"
        )
    password = values.get("SUPABASE_DB_PASSWORD", "")
    if password and password in redis_url:
        problems.append("SUPABASE_DB_PASSWORD and UPSTASH_REDIS_URL appear to share a value; check both")
    return problems


def database_url(values: Dict[str, str]) -> str:
    """Supabase's session pooler: needed because migrations take an advisory lock."""
    from urllib.parse import quote

    ref = values["SUPABASE_PROJECT_REF"]
    region = values["SUPABASE_REGION"]
    password = quote(values["SUPABASE_DB_PASSWORD"], safe="")
    return (
        f"postgresql://postgres.{ref}:{password}"
        f"@aws-0-{region}.pooler.supabase.com:5432/postgres?sslmode=require"
    )


def render(values: Dict[str, str]) -> Dict[Path, str]:
    """Build every staging file. Returns path -> contents."""
    ref = values["SUPABASE_PROJECT_REF"]
    region = values["SUPABASE_REGION"]
    db_url = database_url(values)
    redis_url = values["UPSTASH_REDIS_URL"]
    supabase_url = f"https://{ref}.supabase.co"
    bucket = values.get("SUPABASE_STORAGE_BUCKET")
    s3_key = values.get("SUPABASE_S3_ACCESS_KEY_ID")
    s3_secret = values.get("SUPABASE_S3_SECRET_ACCESS_KEY")
    use_s3 = bool(bucket and s3_key and s3_secret)

    header = (
        "# GENERATED by `python -m cloud.ops.staging_env render`. Do not edit:\n"
        "# change cloud/.env.staging-secrets and render again. Git-ignored.\n"
        "#\n"
        "# Local staging: real Supabase and real Upstash, but the API and worker\n"
        "# run on this machine. CAREERCLOUD_ENV stays `development` so the\n"
        "# deployed-only guards (resource registry, JSON logs, stamped\n"
        "# resources) do not fire for a laptop; the remote services are still\n"
        "# real, which is why ALLOW_REMOTE_SERVICES must be set explicitly.\n\n"
    )

    storage_lines = (
        f"CAREERCLOUD_STORAGE_BACKEND=s3\n"
        f"CAREERCLOUD_S3_ENDPOINT=https://{ref}.storage.supabase.co/storage/v1/s3\n"
        f"CAREERCLOUD_S3_REGION={region}\n"
        f"CAREERCLOUD_S3_BUCKET={bucket}\n"
        f"CAREERCLOUD_S3_ACCESS_KEY_ID={s3_key}\n"
        f"CAREERCLOUD_S3_SECRET_ACCESS_KEY={s3_secret}\n"
        f"CAREERCLOUD_RESULTS_NAMESPACE=staging\n"
        if use_s3
        else "# Result files stay on this machine (no Supabase Storage keys given).\nCAREERCLOUD_STORAGE_BACKEND=local\n"
    )

    api = header + f"""CAREERCLOUD_ENV=development
CAREERCLOUD_ALLOW_REMOTE_SERVICES=1

CAREERCLOUD_STORAGE=postgres
CAREERCLOUD_DATABASE_URL={db_url}
CAREERCLOUD_DB_USER_ROLE=authenticated
CAREERCLOUD_DB_POOL_MAX=5

CAREERCLOUD_QUEUE=redis
CAREERCLOUD_REDIS_URL={redis_url}
CAREERCLOUD_QUEUE_PREFIX=careercloud:staging
CAREERCLOUD_RUNNABLE_TYPES=single_company,bulk_companies

# Real Supabase Auth: tokens are verified against the project's JWKS endpoint.
CAREERCLOUD_AUTH_MODE=supabase
CAREERCLOUD_SUPABASE_URL={supabase_url}

# The dashboard is already deployed; it must be allowed to call this API.
CAREERCLOUD_CORS_ORIGINS=https://careercrawler-staging.pages.dev,http://localhost:5173,http://127.0.0.1:5173

{storage_lines}
CAREERCLOUD_MAX_ATTEMPTS=3
CAREERCLOUD_MAX_ACTIVE_JOBS_PER_USER=3
CAREERCLOUD_RATE_LIMIT_PER_MINUTE=120
CAREERCLOUD_JOB_CREATE_PER_HOUR=20
CAREERCLOUD_WORKER_STALE_AFTER_SECONDS=90
"""

    worker = header + f"""CAREERCLOUD_ENV=development
CAREERCLOUD_ALLOW_REMOTE_SERVICES=1

CAREERCLOUD_DATABASE_URL={db_url}
CAREERCLOUD_DB_POOL_MAX=4

CAREERCLOUD_REDIS_URL={redis_url}
CAREERCLOUD_QUEUE_PREFIX=careercloud:staging

{storage_lines}
# The real engine, through cloud/worker/careercrawler_runner.py.
CAREERCLOUD_WORKER_RUNNER=careercrawler
CAREERCLOUD_WORKER_CONCURRENCY=1
CAREERCLOUD_CRAWLER_COMPANY_CONCURRENCY=3
CAREERCLOUD_CRAWLER_MAX_RUNTIME_SECONDS=1800
# A browser bypasses the in-process egress guard, so it stays off.
CAREERCLOUD_CRAWLER_BROWSER_FALLBACK=false
CAREERCLOUD_EGRESS_GUARD=true

CAREERCLOUD_LEASE_SECONDS=120
CAREERCLOUD_VISIBILITY_TIMEOUT=600
# Upstash bills per command. A 10 s poll is ~260K reserve calls a month per
# worker, which fits the free tier; 1-2 s would not.
CAREERCLOUD_POLL_INTERVAL=10
CAREERCLOUD_REAP_INTERVAL=30
CAREERCLOUD_ORPHAN_AFTER_SECONDS=600
CAREERCLOUD_MAX_ATTEMPTS=3
CAREERCLOUD_RETRY_BASE_DELAY=30
CAREERCLOUD_RETRY_MAX_DELAY=900
"""

    # Only the anon key belongs in a VITE_ variable: everything VITE_ is
    # compiled into the bundle and served to every visitor.
    web = (
        "# GENERATED. Git-ignored. Only the anon key may appear here — every\n"
        "# VITE_ value is compiled into the bundle and served publicly.\n\n"
        "VITE_AUTH_MODE=supabase\n"
        f"VITE_SUPABASE_URL={supabase_url}\n"
        f"VITE_SUPABASE_ANON_KEY={values['SUPABASE_ANON_KEY']}\n"
        "VITE_DEPLOY_ENV=staging\n"
        "# Set once the API has a public HTTPS URL (Cloudflare Tunnel).\n"
        "VITE_API_URL=\n"
    )

    return {
        CLOUD / "api" / ".env.staging": api,
        CLOUD / "worker" / ".env.staging": worker,
        CLOUD / "web" / ".env.staging": web,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.ops.staging_env")
    parser.add_argument("command", choices=["template", "render", "check"])
    args = parser.parse_args(argv)

    if args.command == "template":
        try:
            path = write_template()
        except FileExistsError as error:
            print(error)
            return 1
        print(f"wrote {path.relative_to(REPO)}")
        print("Fill it in, then run: python -m cloud.ops.staging_env render")
        return 0

    try:
        values = read_secrets()
    except FileNotFoundError as error:
        print(error)
        return 1

    absent = missing(values)
    if args.command == "check":
        for name, (required, hint) in FIELDS.items():
            flag = "required" if required else "optional"
            print(f"  {name:<32} {_mask(values.get(name)):<20} ({flag})")
        for problem in _validate(values):
            print(f"  ! {problem}")
        return 1 if absent else 0

    if absent:
        print("Cannot render; these are still empty:")
        for name in absent:
            print(f"  {name:<32} {FIELDS[name][1]}")
        return 1
    problems = _validate(values)
    if problems:
        print("Cannot render:")
        for problem in problems:
            print(f"  ! {problem}")
        return 1

    for path, body in render(values).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO)}")
    print()
    print(f"  database : {mask_url(database_url(values))}")
    print(f"  redis    : {mask_url(values['UPSTASH_REDIS_URL'])}")
    print(f"  supabase : https://{values['SUPABASE_PROJECT_REF']}.supabase.co")
    print(f"  storage  : {'Supabase Storage (S3)' if values.get('SUPABASE_STORAGE_BUCKET') else 'local files'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
