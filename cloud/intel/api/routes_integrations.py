"""Integrations API: Slack, signed webhooks, Google Workspace, Microsoft 365, calendars.

Secrets go in, never out: responses carry only a hint. Configure, test and
disconnect need workspace admin rights (enforced in the service).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.deps import get_platform, http_error, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.integrations.service import EVENTS
from cloud.intel.platform import Platform

router = APIRouter(prefix="/w/{workspace_id}", tags=["integrations"])


def _run(fn):
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/integrations")
def list_integrations(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    service = platform.service("integrations")
    return _run(lambda: {"items": service.list(ctx), "events": list(EVENTS), "mock_mode": service.mock_mode})


@router.get("/integrations/deliveries")
def deliveries(provider: Optional[str] = None, limit: int = 50, ctx: Ctx = Depends(workspace_ctx),
               platform: Platform = Depends(get_platform)):
    return _run(lambda: {"items": platform.service("integrations").deliveries(ctx, provider=provider, limit=limit)})


@router.post("/integrations/deliveries/{delivery_id}/retry")
def retry(delivery_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("integrations").retry(ctx, delivery_id))


@router.get("/integrations/{provider}")
def get_integration(provider: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("integrations").status(ctx, provider))


@router.put("/integrations/{provider}")
def configure(provider: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
              platform: Platform = Depends(get_platform)):
    secrets, settings = body.get("secrets") or {}, body.get("settings") or {}
    if not isinstance(secrets, dict) or not isinstance(settings, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "secrets and settings must be objects")
    return _run(lambda: platform.service("integrations").configure(ctx, provider, secrets=secrets, settings=settings))


@router.post("/integrations/{provider}/test")
def test(provider: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("integrations").test(ctx, provider))


@router.delete("/integrations/{provider}")
def disconnect(provider: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("integrations").disconnect(ctx, provider))
