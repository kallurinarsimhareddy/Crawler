"""AI providers and the research agent.

    GET  /w/{ws}/ai/providers
    (scraper routes: routes_scraper.py)
    POST /w/{ws}/research/plan                       {"question"}
    GET  /w/{ws}/research/runs[/{id}[/results|/export]]
    POST /w/{ws}/research/runs/{id}/approve          {"allow_paid": false}
    POST /w/{ws}/research/runs/{id}/actions          {"action_ids": [...]}
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from cloud.intel.api.crud import list_filters
from cloud.intel.api.deps import get_platform, http_error, idempotent, page_response, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

router = APIRouter(prefix="/w/{workspace_id}", tags=["ai"])

_MAX_UPLOAD = 20 * 1024 * 1024


def _stream(platform: Platform, info: Optional[Dict[str, Any]]) -> StreamingResponse:
    if not info or not info.get("storage_key"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    try:
        handle = platform.storage.open(info["storage_key"])
    except (FileNotFoundError, KeyError):
        raise HTTPException(status.HTTP_410_GONE, "file is no longer available") from None

    def body():
        with handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(body(), media_type=info.get("content_type") or "application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{info.get("filename") or "download"}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.get("/ai/providers")
def ai_providers(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return platform.service("ai").describe(ctx)


# --- research ------------------------------------------------------------------------


@router.post("/research/plan", status_code=status.HTTP_201_CREATED)
def research_plan(request: Request, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    try:
        return idempotent(platform, ctx, request, body, lambda: jsonable_encoder(
            platform.service("research").plan(ctx, str(body.get("question") or ""))))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/research/runs")
def research_runs(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                  platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "research_runs", list_filters(request), limit=limit,
                                                 offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/research/runs/{run_id}")
def research_run(run_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.store.get(ctx, "research_runs", run_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/research/runs/{run_id}/results")
def research_results(run_id: str, limit: int = 100, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                     platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.service("research").results(ctx, run_id, limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/research/runs/{run_id}/export")
def research_export(run_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        run = platform.store.get(ctx, "research_runs", run_id)
    except PlatformError as error:
        raise http_error(error) from error
    return _stream(platform, run["progress"].get("files"))


@router.post("/research/runs/{run_id}/approve")
def research_approve(run_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                     platform: Platform = Depends(get_platform)):
    try:
        allow_paid = bool((body or {}).get("allow_paid", False))
        return jsonable_encoder(platform.service("research").approve(ctx, run_id, allow_paid=allow_paid))
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/research/runs/{run_id}/actions")
def research_actions(run_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                     platform: Platform = Depends(get_platform)):
    ids = body.get("action_ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "action_ids must be a non-empty list")
    try:
        return jsonable_encoder(platform.service("research").apply_actions(ctx, run_id, [str(i) for i in ids]))
    except PlatformError as error:
        raise http_error(error) from error
