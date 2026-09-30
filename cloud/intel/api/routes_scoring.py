"""Scoring and analytics-report API.

* ``GET  /w/{ws}/scores/model`` — the scoring rules, stated in words.
* ``GET  /w/{ws}/scores/{company|contact}/{id}`` — the live explanation (read-only).
* ``POST /w/{ws}/scores/{company|contact}/{id}`` — recompute and save (writers).
* ``GET  /w/{ws}/scores/{company|contact}/{id}/history?kind=`` — saved snapshots.
* ``POST /w/{ws}/scores/rescore`` — background rescoring task (``kind: scoring``).
* ``GET  /w/{ws}/analytics/reports`` — the report catalogue.
* ``GET  /w/{ws}/analytics/reports/{report}?start=&end=&<filter>=`` — one report.
* ``GET  /w/{ws}/analytics/reports/{report}/export?format=csv|xlsx`` — a download.
* ``/w/{ws}/analytics/saved-reports`` — saved report views (CRUD).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response

from cloud.intel.api.crud import crud_router
from cloud.intel.api.deps import get_platform, http_error, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

router = APIRouter()
ws = APIRouter(prefix="/w/{workspace_id}", tags=["scoring", "analytics"])

_RESERVED = {"start", "end", "format"}


def _run(fn):
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error


def _filters(request: Request) -> Dict[str, Any]:
    return {k: v for k, v in request.query_params.items() if k not in _RESERVED and v != ""}


@ws.get("/scores/model")
def score_model(ctx: Ctx = Depends(workspace_ctx)):
    from cloud.intel.scoring.service import model_description

    return model_description()


@ws.post("/scores/rescore")
def rescore_all(body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    body = body or {}
    params = {k: body[k] for k in ("company_ids", "contact_ids", "all") if k in body}
    return _run(lambda: platform.tasks.submit(ctx, "scoring", params))


@ws.get("/scores/{entity_type}/{entity_id}")
def explain(entity_type: str, entity_id: str, ctx: Ctx = Depends(workspace_ctx),
            platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("scoring").explain(ctx, entity_type, entity_id))


@ws.post("/scores/{entity_type}/{entity_id}")
def rescore(entity_type: str, entity_id: str, ctx: Ctx = Depends(write_ctx),
            platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("scoring").rescore(ctx, entity_type, entity_id))


@ws.get("/scores/{entity_type}/{entity_id}/history")
def history(entity_type: str, entity_id: str, kind: Optional[str] = None, limit: int = 50,
            ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"items": platform.service("scoring").history(ctx, entity_type, entity_id, kind=kind,
                                                                     limit=limit)})


@ws.get("/analytics/reports")
def catalog(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return {"items": platform.service("reports").catalog()}


@ws.get("/analytics/reports/{report}")
def run_report(report: str, request: Request, start: Optional[str] = None, end: Optional[str] = None,
               ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("reports").run(ctx, report, start=start, end=end,
                                                        filters=_filters(request)))


@ws.get("/analytics/reports/{report}/export")
def export_report(report: str, request: Request, format: str = "csv", start: Optional[str] = None,
                  end: Optional[str] = None, ctx: Ctx = Depends(workspace_ctx),
                  platform: Platform = Depends(get_platform)):
    try:
        data, filename, content_type = platform.service("reports").export(
            ctx, report, format, start=start, end=end, filters=_filters(request))
    except PlatformError as error:
        raise http_error(error) from error
    return Response(data, media_type=content_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _create_view(platform: Platform, ctx: Ctx, values: Dict[str, Any]) -> Dict[str, Any]:
    return platform.service("reports").save_view(ctx, values)


router.include_router(ws)
router.include_router(crud_router("saved_reports", path="/analytics/saved-reports", tag="analytics",
                                  create=_create_view))
