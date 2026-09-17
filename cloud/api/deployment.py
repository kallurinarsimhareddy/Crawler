"""What a deployed API (staging or production) must satisfy before it starts.

Development runs with local stand-ins. A deployed environment runs only on
durable, authenticated, encrypted, environment-isolated infrastructure, and says
exactly what is wrong when it is not.
"""

from __future__ import annotations

from typing import List
from urllib.parse import parse_qs, urlsplit

from cloud.api.settings import Settings
from cloud.shared.environment import ResourceIdentity

__all__ = ["deployment_problems", "identity_of"]


def identity_of(settings: Settings) -> ResourceIdentity:
    return ResourceIdentity.from_urls(
        settings.environment,
        database_url=settings.database_url,
        supabase_url=settings.supabase_url,
        redis_url=settings.redis_url,
        queue_prefix=settings.queue_prefix,
        storage_bucket=settings.s3_bucket,
        storage_endpoint=settings.s3_endpoint,
        results_namespace=settings.results_namespace,
        api_origin=settings.api_origin,
        dashboard_origins=settings.cors_origins,
    )


def deployment_problems(settings: Settings) -> List[str]:
    if not settings.deployed:
        return []
    env = settings.environment
    problems: List[str] = []

    def need(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    need(settings.storage == "postgres", f"{env} requires CAREERCLOUD_STORAGE=postgres")
    need(settings.queue == "redis", f"{env} requires CAREERCLOUD_QUEUE=redis")
    need(settings.auth_mode == "supabase", f"{env} requires CAREERCLOUD_AUTH_MODE=supabase")
    need(settings.dev_jwt_secret is None, f"{env} must not have CAREERCLOUD_DEV_JWT_SECRET")
    need(settings.supabase_service_role_key is None, "the API must never hold the Supabase service-role key")
    need(bool(settings.supabase_url), f"{env} requires CAREERCLOUD_SUPABASE_URL")

    if settings.database_url:
        query = parse_qs(urlsplit(settings.database_url).query)
        need(
            query.get("sslmode", [""])[0] in ("require", "verify-ca", "verify-full"),
            "CAREERCLOUD_DATABASE_URL must set sslmode=require (or verify-full)",
        )
    if settings.redis_url:
        parts = urlsplit(settings.redis_url)
        need(parts.scheme == "rediss", "CAREERCLOUD_REDIS_URL must use TLS (rediss://)")
        need(bool(parts.password), "CAREERCLOUD_REDIS_URL must authenticate (password in the URL)")

    need(settings.storage_backend == "s3", f"{env} requires CAREERCLOUD_STORAGE_BACKEND=s3 (private bucket)")
    if settings.storage_backend == "s3":
        need(bool(settings.s3_endpoint and settings.s3_endpoint.startswith("https://")), "CAREERCLOUD_S3_ENDPOINT must be https://")
        for name in ("s3_bucket", "s3_region", "s3_access_key_id", "s3_secret_access_key"):
            need(bool(getattr(settings, name)), f"CAREERCLOUD_{name.upper()} is required")
    need(bool(settings.results_namespace), "CAREERCLOUD_RESULTS_NAMESPACE is required")

    need(bool(settings.api_origin and settings.api_origin.startswith("https://")), "CAREERCLOUD_API_ORIGIN must be an https:// origin")
    need(bool(settings.cors_origins), "CAREERCLOUD_CORS_ORIGINS must name the dashboard origin")
    for origin in settings.cors_origins:
        need(origin.startswith("https://") and "localhost" not in origin and "127.0.0.1" not in origin,
             f"CORS origin {origin!r} must be a public https:// origin")
    need(settings.log_format == "json", f"{env} requires CAREERCLOUD_LOG_FORMAT=json")
    need(settings.resource_registry is not None, f"{env} requires CAREERCLOUD_RESOURCE_REGISTRY")
    return problems
