"""Workspaces, members, background tasks and the audit log."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder

from cloud.api.auth import Principal
from cloud.api.routes import current_user
from cloud.intel.api.crud import list_filters
from cloud.intel.api.deps import get_platform, http_error, page_response, workspace_ctx, write_ctx
from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

router = APIRouter(tags=["workspaces"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    return slug if len(slug) >= 2 else f"ws-{slug or 'new'}"


@router.get("/workspaces")
def list_workspaces(principal: Principal = Depends(current_user), platform: Platform = Depends(get_platform)):
    return {"items": jsonable_encoder(platform.store.workspaces_for(principal.user_id))}


@router.post("/workspaces", status_code=status.HTTP_201_CREATED)
def create_workspace(body: Dict[str, Any] = Body(...), principal: Principal = Depends(current_user),
                     platform: Platform = Depends(get_platform)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")
    slug = str(body.get("slug") or _slugify(name))
    try:
        ws = platform.store.create_workspace(principal.user_id, name, slug)
        ctx = Ctx(ws["id"], principal.user_id, "owner")
        audit(platform.store, ctx, "workspace.create", summary=name)
        if body.get("seed", True):
            platform.service("crm").ensure_defaults(ctx)
            platform.service("campaigns").ensure_defaults(ctx)
        return jsonable_encoder(ws)
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/w/{workspace_id}")
def get_workspace(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    info = platform.store.membership(ctx.user_id, ctx.workspace_id)
    return jsonable_encoder(info)


@router.patch("/w/{workspace_id}")
def update_workspace(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                     platform: Platform = Depends(get_platform)):
    try:
        ctx.require_admin()
        row = platform.store.update_workspace(ctx, **body)
        audit(platform.store, ctx, "workspace.update", changes=body)
        return jsonable_encoder(row)
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/w/{workspace_id}/members")
def list_members(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return {"items": jsonable_encoder(platform.store.list_members(ctx))}


@router.post("/w/{workspace_id}/members", status_code=status.HTTP_201_CREATED)
def add_member(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
               platform: Platform = Depends(get_platform)):
    try:
        ctx.require_admin()
        platform.store.add_member(ctx, str(body.get("user_id")), str(body.get("role") or "member"))
        audit(platform.store, ctx, "workspace.member_add", changes={"user_id": body.get("user_id"),
                                                                    "role": body.get("role")})
        return {"ok": True}
    except (PlatformError, ValueError) as error:
        raise HTTPException(getattr(error, "status", 422), str(error)) from error


# --- background tasks ------------------------------------------------------------


@router.get("/w/{workspace_id}/tasks")
def list_tasks(request: Request, limit: int = 50, offset: int = 0, order: Optional[str] = None,
               ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "platform_tasks", list_filters(request), order=order,
                                                 limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/w/{workspace_id}/tasks/{task_id}")
def get_task(task_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.tasks.get(ctx, task_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/w/{workspace_id}/tasks/{task_id}/{action}")
def task_action(task_id: str, action: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    handlers = {"cancel": platform.tasks.cancel, "pause": platform.tasks.pause, "resume": platform.tasks.resume}
    if action not in handlers:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown task action")
    try:
        return jsonable_encoder(handlers[action](ctx, task_id))
    except PlatformError as error:
        raise http_error(error) from error


# --- audit -------------------------------------------------------------------------


@router.get("/w/{workspace_id}/audit")
def list_audit(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
               platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "audit_log", list_filters(request), limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/w/{workspace_id}/provenance/{entity_type}/{entity_id}")
def provenance_for(entity_type: str, entity_id: str, ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    rows = platform.store.list(ctx, "source_records", {"entity_type": entity_type, "entity_id": entity_id},
                               order="-observed_at", limit=200)
    return page_response(rows)
