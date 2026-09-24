"""Analytics API: the dashboard and daily time series for one workspace."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.deps import get_platform, http_error, workspace_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

router = APIRouter(prefix="/w/{workspace_id}/analytics", tags=["analytics"])


@router.get("/dashboard")
def dashboard(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("analytics").dashboard(ctx))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/timeseries")
def timeseries(entity: str = "companies", days: int = 30, ctx: Ctx = Depends(workspace_ctx),
               platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("analytics").timeseries(ctx, entity, days))
    except PlatformError as error:
        raise http_error(error) from error
