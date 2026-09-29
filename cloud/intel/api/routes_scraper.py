"""The AI scraper's HTTP API (all V1 routes kept, same paths and shapes).

    POST /w/{ws}/scraper/schema                        {"instruction"}
    POST /w/{ws}/scraper/plan                          JSON {"urls", "instruction", "schema"?, "options"?, "template_id"?}
                                                       or multipart: file, column, urls, instruction, options (JSON)
    POST /w/{ws}/scraper/runs                          as plan, plus "confirm" (needed for high-volume runs),
                                                       "use_ai"?, "max_ai_calls"?
    GET  /w/{ws}/scraper/runs[/{id}]                   run history; one run with live progress and statistics
    GET  /w/{ws}/scraper/runs/{id}/results             per-input result rows
    GET  /w/{ws}/scraper/runs/{id}/records?view=all|companies|jobs
    GET  /w/{ws}/scraper/runs/{id}/pages|errors|evidence
    GET  /w/{ws}/scraper/runs/{id}/files/{csv|xlsx|json|ndjson}[?view=companies|jobs|pages|errors]
    POST /w/{ws}/scraper/runs/{id}/pause|resume|cancel|retry|restart
    GET  /w/{ws}/scraper/runs/{id}/crm/match           existing / new / possible duplicate / conflict
    POST /w/{ws}/scraper/runs/{id}/crm/propose         {"actions": ["company", "job", ...]} — creates proposals only
    GET  /w/{ws}/scraper/runs/{id}/proposals
    POST /w/{ws}/scraper/proposals/review              {"ids": [...], "decision": "approved"|"rejected"}
    POST /w/{ws}/scraper/proposals/apply               {"ids": [...]} — approved proposals only
    GET/POST /w/{ws}/scraper/templates, GET/PATCH/DELETE /w/{ws}/scraper/templates/{id},
    POST /w/{ws}/scraper/templates/{id}/duplicate
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.crud import list_filters
from cloud.intel.api.deps import get_platform, http_error, idempotent, page_response, workspace_ctx, write_ctx
from cloud.intel.api.routes_ai import _stream
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

router = APIRouter(prefix="/w/{workspace_id}", tags=["scraper"])

_MAX_UPLOAD = 20 * 1024 * 1024


def _flag(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


async def _request_args(request: Request) -> Dict[str, Any]:
    """Keyword arguments for ``plan``/``start`` from a JSON body or a multipart form."""
    content_type = request.headers.get("content-type", "")
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
        options: Dict[str, Any] = {}
        for key in ("options", "schema"):
            if form.get(key):
                try:
                    options[key] = json.loads(str(form.get(key)))
                except json.JSONDecodeError:
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{key} must be JSON") from None
        raw_options = dict(options.get("options") or {})
        if form.get("use_ai") is not None:
            raw_options["use_ai"] = _flag(form.get("use_ai"))
        return {"urls": pasted or None, "instruction": str(form.get("instruction") or ""), "file_bytes": data,
                "filename": filename, "column": str(form.get("column")) if form.get("column") else None,
                "schema": options.get("schema") if isinstance(options.get("schema"), dict) else None,
                "options": raw_options, "template_id": str(form.get("template_id")) if form.get("template_id") else None,
                "confirm": _flag(form.get("confirm"), False), "_body": None}
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expected a JSON object")
    urls = body.get("urls") or []
    if not isinstance(urls, (list, str)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "urls must be a list or pasted text")
    options = dict(body.get("options") or {}) if isinstance(body.get("options"), dict) else {}
    if "use_ai" in body:
        options["use_ai"] = _flag(body.get("use_ai"))
    if isinstance(body.get("max_ai_calls"), int):
        options["max_ai_calls"] = body["max_ai_calls"]
    return {"urls": urls if isinstance(urls, str) else [str(u) for u in urls],
            "instruction": str(body.get("instruction") or ""),
            "schema": body.get("schema") if isinstance(body.get("schema"), dict) else None, "options": options,
            "template_id": str(body["template_id"]) if body.get("template_id") else None,
            "confirm": _flag(body.get("confirm"), False), "_body": body}


def _guard(fn):
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error


# --- schema, plan, start ----------------------------------------------------------------------


@router.post("/scraper/schema")
def scraper_schema(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").instruction_to_schema(ctx, str(body.get("instruction") or "")))


@router.post("/scraper/plan")
async def scraper_plan(request: Request, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    args = await _request_args(request)
    args.pop("_body")
    args.pop("confirm")
    return _guard(lambda: platform.service("scraper").plan(ctx, args.pop("urls"), args.pop("instruction"), **args))


@router.post("/scraper/runs", status_code=status.HTTP_201_CREATED)
async def scraper_start(request: Request, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    args = await _request_args(request)
    body = args.pop("_body")
    service = platform.service("scraper")
    produce = lambda: jsonable_encoder(service.start(ctx, args.pop("urls"), args.pop("instruction"), **args))  # noqa: E731
    try:
        return idempotent(platform, ctx, request, body, produce) if body is not None else produce()
    except PlatformError as error:
        raise http_error(error) from error


# --- runs -------------------------------------------------------------------------------------


@router.get("/scraper/runs")
def scraper_runs(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "scrape_runs", list_filters(request), limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/scraper/runs/{run_id}")
def scraper_run(run_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.store.get(ctx, "scrape_runs", run_id))


@router.get("/scraper/runs/{run_id}/results")
def scraper_results(run_id: str, limit: int = 100, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                    platform: Platform = Depends(get_platform)):
    try:
        platform.store.get(ctx, "scrape_runs", run_id)
        return page_response(platform.store.list(ctx, "scrape_results", {"run_id": run_id}, order="created_at",
                                                 limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/scraper/runs/{run_id}/records")
def scraper_records(run_id: str, view: str = "all", limit: int = 500, offset: int = 0,
                    ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").records(ctx, run_id, view, limit=limit, offset=offset))


@router.get("/scraper/runs/{run_id}/pages")
def scraper_pages(run_id: str, limit: int = 500, offset: int = 0, outcome: Optional[str] = None,
                  ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").pages(ctx, run_id, limit=limit, offset=offset, outcome=outcome))


@router.get("/scraper/runs/{run_id}/errors")
def scraper_errors(run_id: str, limit: int = 1000, ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").errors(ctx, run_id, limit=limit))


@router.get("/scraper/runs/{run_id}/evidence")
def scraper_evidence(run_id: str, limit: int = 500, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                     platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").evidence(ctx, run_id, limit=limit, offset=offset))


@router.get("/scraper/runs/{run_id}/files/{fmt}")
def scraper_file(run_id: str, fmt: str, view: str = "all", ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        info = platform.service("scraper").file(ctx, run_id, fmt, view)
    except PlatformError as error:
        raise http_error(error) from error
    return _stream(platform, info)


def _action(name: str):
    def endpoint(run_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
        return _guard(lambda: getattr(platform.service("scraper"), name)(ctx, run_id))
    endpoint.__name__ = f"scraper_{name}"
    return endpoint


for _name in ("pause", "resume", "cancel", "retry", "restart"):
    router.add_api_route(f"/scraper/runs/{{run_id}}/{_name}", _action(_name), methods=["POST"],
                         status_code=status.HTTP_201_CREATED if _name == "restart" else status.HTTP_200_OK)


# --- CRM --------------------------------------------------------------------------------------


@router.get("/scraper/runs/{run_id}/crm/match")
def scraper_crm_match(run_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").match_crm(ctx, run_id))


@router.post("/scraper/runs/{run_id}/crm/propose", status_code=status.HTTP_201_CREATED)
def scraper_crm_propose(run_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                        platform: Platform = Depends(get_platform)):
    actions = (body or {}).get("actions") or ["company", "job"]
    return _guard(lambda: platform.service("scraper").propose(ctx, run_id, [str(a) for a in actions],
                                                              task_title=(body or {}).get("task_title")))


@router.get("/scraper/runs/{run_id}/proposals")
def scraper_proposals(run_id: str, status_filter: Optional[str] = None, ctx: Ctx = Depends(workspace_ctx),
                      platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").proposals(ctx, run_id, status=status_filter))


@router.post("/scraper/proposals/review")
def scraper_review(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                   platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").review(ctx, [str(i) for i in body.get("ids") or []],
                                                             str(body.get("decision") or "")))


@router.post("/scraper/proposals/apply")
def scraper_apply(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").apply_proposals(ctx, [str(i) for i in body.get("ids") or []]))


# --- templates --------------------------------------------------------------------------------


@router.get("/scraper/templates")
def scraper_templates(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: {"items": platform.service("scraper").templates.list(ctx)})


@router.post("/scraper/templates", status_code=status.HTTP_201_CREATED)
def scraper_template_create(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                            platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").templates.create(ctx, body))


@router.get("/scraper/templates/{template_id}")
def scraper_template(template_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").templates.get(ctx, template_id))


@router.patch("/scraper/templates/{template_id}")
def scraper_template_update(template_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                            platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").templates.update(ctx, template_id, body))


@router.post("/scraper/templates/{template_id}/duplicate", status_code=status.HTTP_201_CREATED)
def scraper_template_duplicate(template_id: str, body: Optional[Dict[str, Any]] = Body(None),
                               ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("scraper").templates.duplicate(ctx, template_id, (body or {}).get("name")))


@router.delete("/scraper/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def scraper_template_delete(template_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    try:
        platform.service("scraper").templates.delete(ctx, template_id)
    except PlatformError as error:
        raise http_error(error) from error
