"""Keep staging and production from ever touching each other's resources.

A deployed process (``CAREERCLOUD_ENV`` = ``staging`` or ``production``) must
pass every check here before it serves a request or claims a job. Any failure
stops the process at startup with every problem listed. There is no fallback.

**1. Resource registry: an allowlist, not a guess.** The host provides
``CAREERCLOUD_RESOURCE_REGISTRY``, a JSON file naming the identifiers of every
environment::

    {
      "staging":    {"database_hosts": ["db.abcd.supabase.co", "aws-0-eu-west-1.pooler.supabase.com"],
                     "supabase_refs": ["abcd"], "redis_hosts": ["x.upstash.io"],
                     "storage_buckets": ["careercloud-staging-results"], "api_origins": ["https://api-staging.example.com"],
                     "dashboard_origins": ["https://staging.example.com"]},
      "production": {"database_hosts": ["db.wxyz.supabase.co"], "supabase_refs": ["wxyz"], ...}
    }

A process may use only resources listed under its **own** environment. It
refuses any resource that appears under **another** environment, even if it is
also listed under its own, because a copy-paste into the wrong block must not
make a production database acceptable to staging.

**2. Naming rules.** The queue prefix must be ``careercloud:<env>``, and the
results namespace and storage bucket must contain ``<env>``. A staging identifier
may not contain ``prod``; a production identifier may not contain ``staging``,
``stage`` or ``dev``.

**3. Environment stamps.** The database (``careercloud.deployment``), Redis
(``<prefix>:environment``) and object storage (``_environment`` object) each
record the environment they belong to. A process refuses a resource stamped for
a different environment. See :mod:`cloud.ops.stamps`.

**4. Forbidden configuration.** These are refused outright: Google Sheets
settings, a SQLite or ``crawler.db`` path, any reference to the production
CareerCrawler checkout, its ``state/`` or ``secrets/``, or the Seamless worktree.

Development and test environments skip the registry and stamps. The local-host
and forbidden-configuration rules still apply there.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

__all__ = [
    "DEPLOYED_ENVIRONMENTS",
    "EnvironmentIsolationError",
    "ResourceIdentity",
    "ResourceRegistry",
    "check_forbidden_configuration",
    "check_identity",
    "enforce_isolation",
    "supabase_ref_from_host",
]

DEPLOYED_ENVIRONMENTS = ("staging", "production")

#: Tokens that mark an identifier as belonging to another environment.
_FOREIGN_TOKENS = {
    "staging": ("prod",),
    "production": ("staging", "stage", "dev", "test"),
}

#: Environment variables that would connect a process to Google Sheets.
_SHEETS_VARIABLES = re.compile(
    r"(SPREADSHEET|GOOGLE_APPLICATION_CREDENTIALS|GOOGLE_SERVICE_ACCOUNT|SHEETS?_(ID|KEY|CREDENTIALS|TOKEN))",
    re.IGNORECASE,
)
#: Values that point at CareerCrawler's production state or the Seamless worktree.
_FORBIDDEN_VALUE_PATTERNS = (
    (re.compile(r"crawler\.db", re.IGNORECASE), "the production CareerCrawler SQLite database"),
    (re.compile(r"\.(db|sqlite|sqlite3)([\"'\s]|$)", re.IGNORECASE), "a SQLite database file"),
    (re.compile(r"^sqlite:", re.IGNORECASE), "a SQLite URL"),
    (re.compile(r"careercrawler-seamless|[\\/]seamless([\\/]|$)", re.IGNORECASE), "the Seamless worktree"),
    (re.compile(r"[\\/]crawlers[\\/]careercrawler([\\/]|$)", re.IGNORECASE), "the production CareerCrawler checkout"),
    (re.compile(r"[\\/]secrets[\\/]|service_account.*\.json|client_secret.*\.json", re.IGNORECASE), "crawler Google credentials"),
)


class EnvironmentIsolationError(RuntimeError):
    """The process is configured to touch a resource outside its environment."""

    def __init__(self, environment: str, problems: Sequence[str]) -> None:
        self.environment = environment
        self.problems = list(problems)
        listed = "\n  - ".join(self.problems)
        super().__init__(f"refusing to start {environment!r}:\n  - {listed}")


@dataclass(frozen=True)
class ResourceIdentity:
    """What a process is about to connect to, reduced to comparable identifiers."""

    environment: str
    database_host: Optional[str] = None
    supabase_ref: Optional[str] = None
    redis_host: Optional[str] = None
    queue_prefix: Optional[str] = None
    storage_bucket: Optional[str] = None
    storage_endpoint_host: Optional[str] = None
    results_namespace: Optional[str] = None
    api_origin: Optional[str] = None
    dashboard_origins: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_urls(
        cls,
        environment: str,
        *,
        database_url: Optional[str] = None,
        supabase_url: Optional[str] = None,
        redis_url: Optional[str] = None,
        queue_prefix: Optional[str] = None,
        storage_bucket: Optional[str] = None,
        storage_endpoint: Optional[str] = None,
        results_namespace: Optional[str] = None,
        api_origin: Optional[str] = None,
        dashboard_origins: Sequence[str] = (),
    ) -> "ResourceIdentity":
        def host(url: Optional[str]) -> Optional[str]:
            return (urlsplit(url).hostname or "").lower() or None if url else None

        supabase_host = host(supabase_url)
        database_host = host(database_url)
        ref = supabase_ref_from_host(supabase_host) or _ref_from_database_url(database_url)
        storage_host = host(storage_endpoint)
        return cls(
            environment=environment,
            database_host=database_host,
            supabase_ref=ref,
            redis_host=host(redis_url),
            queue_prefix=queue_prefix,
            storage_bucket=storage_bucket,
            storage_endpoint_host=storage_host,
            results_namespace=results_namespace,
            api_origin=api_origin.rstrip("/") if api_origin else None,
            dashboard_origins=tuple(origin.rstrip("/") for origin in dashboard_origins),
        )


def supabase_ref_from_host(host: Optional[str]) -> Optional[str]:
    """``abcd`` from ``abcd.supabase.co``, ``db.abcd.supabase.co`` or ``abcd.storage.supabase.co``."""
    if not host:
        return None
    match = re.match(r"^(?:db\.)?([a-z0-9]{8,40})\.(?:storage\.)?supabase\.(?:co|in)$", host)
    return match.group(1) if match else None


def _ref_from_database_url(url: Optional[str]) -> Optional[str]:
    # Supavisor pooler URLs carry the project ref in the user name: postgres.<ref>
    if not url:
        return None
    user = urlsplit(url).username or ""
    match = re.match(r"^[a-z_]+\.([a-z0-9]{8,40})$", user)
    return match.group(1) if match else None


_REGISTRY_KEYS = {
    "database_hosts": "database_host",
    "supabase_refs": "supabase_ref",
    "redis_hosts": "redis_host",
    "storage_buckets": "storage_bucket",
    "storage_endpoint_hosts": "storage_endpoint_host",
    "api_origins": "api_origin",
}


@dataclass(frozen=True)
class ResourceRegistry:
    environments: Mapping[str, Mapping[str, Sequence[str]]]

    @classmethod
    def load(cls, path: Path) -> "ResourceRegistry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("resource registry must be a JSON object keyed by environment")
        normalised: Dict[str, Dict[str, List[str]]] = {}
        for environment, entries in data.items():
            if environment.startswith("_"):
                continue  # comments
            if not isinstance(entries, dict):
                raise ValueError(f"registry entry {environment!r} must be an object")
            normalised[environment] = {
                key: [str(v).strip().lower().rstrip("/") for v in (entries.get(key) or []) if str(v).strip()]
                for key in (*_REGISTRY_KEYS, "dashboard_origins")
            }
        return cls(normalised)

    def own(self, environment: str, key: str) -> List[str]:
        return list(self.environments.get(environment, {}).get(key, []))

    def others(self, environment: str, key: str) -> Dict[str, List[str]]:
        return {
            other: list(entries.get(key, []))
            for other, entries in self.environments.items()
            if other != environment
        }


def check_identity(identity: ResourceIdentity, registry: Optional[ResourceRegistry]) -> List[str]:
    """Every way ``identity`` strays outside its environment. Empty means safe."""
    env = identity.environment
    problems: List[str] = []
    if env not in DEPLOYED_ENVIRONMENTS:
        return problems

    if registry is None:
        return [f"{env} requires CAREERCLOUD_RESOURCE_REGISTRY (a JSON allowlist of this environment's resources)"]
    if env not in registry.environments:
        problems.append(f"the resource registry has no {env!r} entry")

    for registry_key, attribute in _REGISTRY_KEYS.items():
        value = getattr(identity, attribute)
        if value is None:
            continue
        value = value.lower().rstrip("/")
        allowed = registry.own(env, registry_key)
        label = registry_key[:-1].replace("_", " ")
        if value not in allowed:
            problems.append(f"{label} {value!r} is not registered for {env}")
        for other, values in registry.others(env, registry_key).items():
            if value in values:
                problems.append(f"{label} {value!r} belongs to {other}; {env} must never use it")

    for origin in identity.dashboard_origins:
        allowed = registry.own(env, "dashboard_origins")
        if origin.lower() not in allowed:
            problems.append(f"dashboard origin {origin!r} is not registered for {env}")
        for other, values in registry.others(env, "dashboard_origins").items():
            if origin.lower() in values:
                problems.append(f"dashboard origin {origin!r} belongs to {other}")

    expected_prefix = f"careercloud:{env}"
    if identity.queue_prefix is not None and not (
        identity.queue_prefix == expected_prefix or identity.queue_prefix.startswith(expected_prefix + ":")
    ):
        problems.append(f"queue prefix {identity.queue_prefix!r} must be {expected_prefix!r} (or start with it)")
    if identity.results_namespace is not None and env not in identity.results_namespace:
        problems.append(f"results namespace {identity.results_namespace!r} must contain {env!r}")
    if identity.storage_bucket is not None and env not in identity.storage_bucket.lower():
        problems.append(f"storage bucket {identity.storage_bucket!r} must contain {env!r}")

    for attribute in ("database_host", "redis_host", "queue_prefix", "storage_bucket", "results_namespace", "api_origin"):
        value = (getattr(identity, attribute) or "").lower()
        for token in _FOREIGN_TOKENS[env]:
            if token and re.search(rf"(^|[^a-z]){token}", value):
                problems.append(f"{attribute.replace('_', ' ')} {value!r} looks like another environment ({token!r})")
    for origin in identity.dashboard_origins:
        for token in _FOREIGN_TOKENS[env]:
            if re.search(rf"(^|[^a-z]){token}", origin.lower()):
                problems.append(f"dashboard origin {origin!r} looks like another environment ({token!r})")
    return problems


def check_forbidden_configuration(env: Mapping[str, str], *, loaded_modules: Iterable[str] = ()) -> List[str]:
    """Google Sheets, SQLite, production CareerCrawler state or Seamless in the configuration."""
    problems: List[str] = []
    for name, value in env.items():
        if _SHEETS_VARIABLES.search(name) and (value or "").strip():
            problems.append(f"{name} is set: CareerCloud never uses Google Sheets")
        if not name.startswith("CAREERCLOUD_"):
            continue
        for pattern, meaning in _FORBIDDEN_VALUE_PATTERNS:
            if value and pattern.search(value):
                problems.append(f"{name} points at {meaning}")
                break
    for module in loaded_modules:
        root = module.split(".")[0]
        if root in ("sheets", "store", "sqlite3", "googleapiclient") or module in (
            "crawler.weekly_run",
            "crawler.checkpoint",
        ):
            problems.append(f"module {module!r} is loaded: production crawler state must not be reachable")
    return sorted(set(problems))


def enforce_isolation(
    identity: ResourceIdentity,
    *,
    env: Optional[Mapping[str, str]] = None,
    registry_path: Optional[str] = None,
    loaded_modules: Iterable[str] = (),
) -> None:
    """Raise :class:`EnvironmentIsolationError` unless every check passes."""
    env = os.environ if env is None else env
    problems = check_forbidden_configuration(env, loaded_modules=loaded_modules)
    if identity.environment in DEPLOYED_ENVIRONMENTS:
        registry = None
        path = registry_path or env.get("CAREERCLOUD_RESOURCE_REGISTRY")
        if path:
            try:
                registry = ResourceRegistry.load(Path(path))
            except (OSError, ValueError) as error:
                problems.append(f"cannot read resource registry {path}: {error}")
        problems.extend(check_identity(identity, registry))
    if problems:
        raise EnvironmentIsolationError(identity.environment, sorted(set(problems)))
