"""Request dependencies: the platform, the caller, and the workspace context.

Every workspace route is mounted under ``/api/v1/w/{workspace_id}/...`` and
depends on :func:`workspace_ctx`, which:

1. authenticates the bearer token exactly like CareerCloud's job routes
   (:func:`cloud.api.routes.current_user`);
2. looks up the caller's membership **in the store** (RLS-scoped in
   PostgreSQL) — a workspace the caller does not belong to is a 404,
   indistinguishable from one that does not exist;
3. returns a :class:`~cloud.intel.core.context.Ctx` with the stored role.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, status

from cloud.api.auth import Principal
from cloud.api.routes import current_user
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

__all__ = ["get_platform", "workspace_ctx", "write_ctx", "http_error", "idempotent"]


def get_platform(request: Request) -> Platform:
    platform = getattr(request.app.state, "platform", None)
    if platform is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the platform is not configured")
    return platform


def workspace_ctx(workspace_id: str, request: Request, principal: Principal = Depends(current_user),
                  platform: Platform = Depends(get_platform)) -> Ctx:
    membership = platform.store.membership(principal.user_id, workspace_id)
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    return Ctx(workspace_id=membership["workspace_id"], user_id=principal.user_id, role=membership["role"],
               request_id=request.headers.get("x-request-id"),
               ai_external_allowed=bool(membership.get("ai_external_allowed")),
               actor_label=(principal.email or None) and str(principal.email)[:320])


def write_ctx(ctx: Ctx = Depends(workspace_ctx)) -> Ctx:
    if not ctx.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this workspace role is read-only")
    return ctx


def http_error(error: PlatformError) -> HTTPException:
    return HTTPException(getattr(error, "status", 400), str(error))


def _hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def idempotent(platform: Platform, ctx: Ctx, request: Request, body: Any, produce) -> Any:
    """Honour an ``Idempotency-Key`` header on a POST.

    The first request with a key runs ``produce()`` and stores its response; a
    retry with the same key and body gets the stored response; the same key
    with a different body is a 409. Without the header, ``produce()`` just runs.
    """
    key = request.headers.get("idempotency-key")
    if not key:
        return produce()
    if len(key) > 200:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key is too long")
    digest = _hash(body)
    existing = platform.store.first(ctx, "idempotency_keys", {"key": key})
    if existing is not None:
        if existing["request_hash"] != digest or existing["path"] != request.url.path[:500]:
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency-Key was already used for a different request")
        return existing["response"].get("body")
    result = produce()
    from fastapi.encoders import jsonable_encoder

    try:
        platform.store.insert(ctx, "idempotency_keys", {
            "key": key, "method": request.method, "path": request.url.path[:500], "request_hash": digest,
            "status_code": 200, "response": {"body": jsonable_encoder(result)}})
    except PlatformError:
        pass  # a concurrent twin stored it first; both produced the same logical result
    return result


def page_response(page, *, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    body = {"items": jsonable_encoder(page.rows), "total": page.total, "limit": page.limit, "offset": page.offset,
            "has_more": page.has_more}
    if extra:
        body.update(extra)
    return body
