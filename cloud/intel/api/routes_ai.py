"""AI providers, the AI scraper and the research agent.

    GET  /w/{ws}/ai/providers
    POST /w/{ws}/scraper/schema                      {"instruction"}
    POST /w/{ws}/scraper/runs                        JSON {"urls" (list or pasted text), "instruction", "schema"?,
                                                     "use_ai"?, "max_ai_calls"?} or multipart: file, column, urls, instruction, use_ai
    GET  /w/{ws}/scraper/runs[/{id}[/results]]       runs; one run (with live progress); per-URL result rows
    POST /w/{ws}/scraper/runs/{id}/cancel|retry
    GET  /w/{ws}/scraper/runs/{id}/records?view=all|companies|jobs
    GET  /w/{ws}/scraper/runs/{id}/files/{csv|xlsx|json}[?view=companies|jobs]
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


# --- scraper ------------------------------------------------------------------------


@router.post("/scraper/schema")
def scraper_schema(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    try:
        return platform.service("scraper").instruction_to_schema(ctx, str(body.get("instruction") or ""))
    except PlatformError as error:
        raise http_error(error) from error


def _flag(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


@router.post("/scraper/runs", status_code=status.HTTP_201_CREATED)
async def scraper_start(request: Request, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    service = platform.service("scraper")
    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file") or form.get("files")
            data, filename = None, None
            if upload is not None and hasattr(upload, "read"):
                try:
                    data = await upload.read(_MAX_UPLOAD + 1)
                finally:
                    await upload.close()
                if len(data) > _MAX_UPLOAD:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "files are limited to 20 MB")
                filename = upload.filename
            pasted = str(form.get("urls") or "")
            if data is None and not pasted.strip():
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "paste URLs or attach a CSV/XLSX file as 'file'")
            run = service.start(ctx, pasted or None, str(form.get("instruction") or ""), file_bytes=data,
                                filename=filename, column=(str(form.get("column")) if form.get("column") else None),
                                use_ai=_flag(form.get("use_ai")))
            return jsonable_encoder(run)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expected a JSON object")
        urls = body.get("urls") or []
        if not isinstance(urls, (list, str)):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "urls must be a list or pasted text")
        return idempotent(platform, ctx, request, body, lambda: jsonable_encoder(
            service.start(ctx, urls if isinstance(urls, str) else [str(u) for u in urls],
                          str(body.get("instruction") or ""),
                          schema=body.get("schema") if isinstance(body.get("schema"), dict) else None,
                          use_ai=_flag(body.get("use_ai")),
                          max_ai_calls=body.get("max_ai_calls") if isinstance(body.get("max_ai_calls"), int) else None)))
    except PlatformError as error:
        raise http_error(error) from error
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid JSON") from None


@router.get("/scraper/runs")
def scraper_runs(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "scrape_runs", list_filters(request), limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/scraper/runs/{run_id}")
def scraper_run(run_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.store.get(ctx, "scrape_runs", run_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/scraper/runs/{run_id}/results")
def scraper_results(run_id: str, limit: int = 100, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                    platform: Platform = Depends(get_platform)):
    try:
        platform.store.get(ctx, "scrape_runs", run_id)
        return page_response(platform.store.list(ctx, "scrape_results", {"run_id": run_id}, order="created_at",
                                                 limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/scraper/runs/{run_id}/cancel")
def scraper_cancel(run_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("scraper").cancel(ctx, run_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/scraper/runs/{run_id}/retry")
def scraper_retry(run_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("scraper").retry(ctx, run_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/scraper/runs/{run_id}/records")
def scraper_records(run_id: str, view: str = "all", limit: int = 500, offset: int = 0,
                    ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("scraper").records(ctx, run_id, view, limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/scraper/runs/{run_id}/files/{fmt}")
def scraper_file(run_id: str, fmt: str, view: str = "all", ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        run = platform.store.get(ctx, "scrape_runs", run_id)
    except PlatformError as error:
        raise http_error(error) from error
    files = run["stats"].get("files") or {}
    key = f"{view}.{fmt}" if fmt == "csv" and view in ("companies", "jobs") else fmt
    return _stream(platform, files.get(key))


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
