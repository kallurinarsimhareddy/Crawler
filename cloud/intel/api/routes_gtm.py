"""GTM API: campaigns, templates, sequences, enrollments, message events,
suppressions, workflows, and the public unsubscribe link.

Nothing here sends mail by itself. ``POST /sequences/process-due`` advances due
enrollments, and still sends only when the environment, the campaign and the
enrollment approval all allow it (see :mod:`cloud.intel.gtm.sequences`).

Two routes are deliberately outside the signed-in workspace API and are left
out of the OpenAPI document: ``/api/v1/unsubscribe/{token}`` (the recipient has
no account; the HMAC token is the credential) and provider webhooks posting to
``/w/{ws}/events/inbound`` with the shared ``X-Webhook-Secret`` header.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cloud.api.routes import current_user
from cloud.intel.api.crud import crud_router
from cloud.intel.api.deps import get_platform, http_error, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


# --- CRUD with validating hooks -------------------------------------------------------


def _create_template(platform: Platform, ctx: Ctx, values: Dict[str, Any]) -> Dict[str, Any]:
    return platform.service("sequences").create_template(
        ctx, name=values.get("name"), subject=values.get("subject"), body=values.get("body"),
        campaign_id=values.get("campaign_id"))


def _create_step(platform: Platform, ctx: Ctx, values: Dict[str, Any]) -> Dict[str, Any]:
    if not values.get("sequence_id"):
        from cloud.intel.core.context import ValidationError

        raise ValidationError("sequence_id is required")
    return platform.service("sequences").add_step(
        ctx, values["sequence_id"], channel=values.get("channel", "email"), delay_days=int(values.get("delay_days", 0)),
        template_id=values.get("template_id"), instructions=values.get("instructions"), position=values.get("position"))


def _create_suppression(platform: Platform, ctx: Ctx, values: Dict[str, Any]) -> Dict[str, Any]:
    return platform.service("sequences").add_suppression(
        ctx, str(values.get("value") or ""), kind=values.get("kind", "email"), reason=values.get("reason", "manual"),
        source=values.get("source") or "manual")


def _create_workflow(platform: Platform, ctx: Ctx, values: Dict[str, Any]) -> Dict[str, Any]:
    return platform.service("automation").save_workflow(ctx, values)


def _update_workflow(platform: Platform, ctx: Ctx, row_id: str, changes: Dict[str, Any], expected) -> Dict[str, Any]:
    return platform.service("automation").save_workflow(ctx, changes, workflow_id=row_id)


router.include_router(crud_router("campaigns", tag="campaigns"))
router.include_router(crud_router("email_templates", path="/templates", tag="templates", create=_create_template))
router.include_router(crud_router("sequences", tag="sequences"))
router.include_router(crud_router("sequence_steps", path="/sequence-steps", tag="sequences", create=_create_step))
router.include_router(crud_router("sequence_enrollments", path="/enrollments", tag="sequences",
                                  allow_create=False, allow_update=False, allow_delete=False))
router.include_router(crud_router("message_events", path="/message-events", tag="sequences", allow_create=False))
router.include_router(crud_router("suppressions", tag="suppressions", create=_create_suppression))
router.include_router(crud_router("workflows", tag="workflows", create=_create_workflow, update=_update_workflow))
router.include_router(crud_router("workflow_runs", path="/workflow-runs", tag="workflows"))

ws = APIRouter(prefix="/w/{workspace_id}", tags=["gtm"])


def _run(fn):
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error


# --- campaigns ------------------------------------------------------------------------------


@ws.post("/campaigns/{campaign_id}/match")
def match_campaign(campaign_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    def go():
        company_id = body.get("company_id")
        company = platform.store.get(ctx, "companies", company_id)
        signals = platform.store.all(ctx, "hiring_signals", {"company_id": company_id, "status": "active"}, cap=200)
        jobs = platform.store.all(ctx, "job_postings", {"company_id": company_id, "status": "open"}, cap=500)
        results = platform.service("campaigns").match_campaigns(ctx, company, signals, jobs)
        for result in results:
            if result["campaign"]["id"] == campaign_id:
                return result
        platform.store.get(ctx, "campaigns", campaign_id)  # 404 when it does not exist
        return {"campaign": None, "score": 0, "reasons": []}

    return _run(go)


@ws.post("/companies/{company_id}/campaign-mapping")
def campaign_mapping(company_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(workspace_ctx),
                     platform: Platform = Depends(get_platform)):
    body = body or {}
    create = bool(body.get("create"))
    if create and not ctx.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this workspace role is read-only")
    return _run(lambda: platform.service("campaigns").map_signal_to_opportunity(
        ctx, company_id, create=create, campaign_id=body.get("campaign_id"),
        min_score=float(body.get("min_score", 20.0))))


# --- templates & sequences -------------------------------------------------------------------


@ws.post("/templates/{template_id}/preview")
def preview_template(template_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                     platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("sequences").preview(ctx, template_id, body.get("contact_id"),
                                                              extra=body.get("variables")))


@ws.post("/sequences/{sequence_id}/enroll")
def enroll(sequence_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
           platform: Platform = Depends(get_platform)):
    ids = body.get("contact_ids") or []
    if not isinstance(ids, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "contact_ids must be a list")
    return _run(lambda: {"results": platform.service("sequences").enroll(
        ctx, sequence_id, ids, campaign_id=body.get("campaign_id"), opportunity_id=body.get("opportunity_id"),
        variables=body.get("variables"))})


@ws.post("/enrollments/approve")
def approve(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
            platform: Platform = Depends(get_platform)):
    return _run(lambda: {"approved": platform.service("sequences").approve_enrollments(
        ctx, body.get("enrollment_ids") or [])})


@ws.post("/enrollments/{enrollment_id}/stop")
def stop(enrollment_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("sequences").stop_enrollment(ctx, enrollment_id))


@ws.post("/sequences/process-due")
def process_due(ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    def go():
        ctx.require_admin()
        return platform.service("sequences").process_due(ctx)

    return _run(go)


# --- inbound events (reply / bounce / unsubscribe) --------------------------------------------


@ws.post("/events/inbound", include_in_schema=False)
def inbound_event(workspace_id: str, request: Request, body: Dict[str, Any] = Body(...),
                  credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
                  platform: Platform = Depends(get_platform)):
    """A provider webhook (``X-Webhook-Secret``) or a signed-in writer recording an event."""
    secret = os.environ.get("CAREERCLOUD_INBOUND_WEBHOOK_SECRET", "")
    supplied = request.headers.get("x-webhook-secret", "")
    if supplied:
        if not secret or not hmac.compare_digest(secret, supplied):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook secret")
        lookup = getattr(platform.store, "system_membership", None)
        if not callable(lookup) or lookup(workspace_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
        ctx = Ctx.for_system(workspace_id, actor_kind="system")
    else:
        principal = current_user(request, credentials)
        ctx = workspace_ctx(workspace_id, request, principal, platform)
        if not ctx.can_write:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "this workspace role is read-only")
    return _run(lambda: platform.service("sequences").handle_event(
        ctx, str(body.get("kind") or ""), email=body.get("email"),
        provider_message_id=body.get("provider_message_id"), data=body.get("data")))


# --- workflows ---------------------------------------------------------------------------------


@ws.post("/workflows/{workflow_id}/test")
def test_workflow(workflow_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(workspace_ctx),
                  platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("automation").dry_run(ctx, workflow_id, (body or {}).get("payload") or {}))


@ws.post("/workflows/emit")
def emit(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"runs": platform.service("automation").emit(
        ctx, str(body.get("trigger") or ""), str(body.get("event_key") or ""), body.get("payload") or {})})


router.include_router(ws)


# --- public unsubscribe --------------------------------------------------------------------------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Unsubscribe</title><style>body{{font-family:system-ui,
sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;line-height:1.5}}button{{font-size:1rem;padding:.6rem
1.2rem}}</style></head><body>{content}</body></html>"""


@router.get("/unsubscribe/{token}", include_in_schema=False, response_class=HTMLResponse)
def unsubscribe_page(token: str, platform: Platform = Depends(get_platform)):
    try:
        platform.service("sequences").verify_unsubscribe_token(token)
    except PlatformError:
        return HTMLResponse(_PAGE.format(content="<h1>Link not valid</h1><p>This unsubscribe link is not valid."
                                                 "</p>"), status_code=400)
    # GET never changes state (mail scanners follow links); the button POSTs.
    return HTMLResponse(_PAGE.format(content=(
        "<h1>Unsubscribe</h1><p>Stop receiving these emails?</p>"
        f'<form method="post" action="/api/v1/unsubscribe/{token}"><button type="submit">Unsubscribe</button>'
        "</form>")))


@router.post("/unsubscribe/{token}", include_in_schema=False)
def unsubscribe(token: str, request: Request, platform: Platform = Depends(get_platform)):
    try:
        result = platform.service("sequences").unsubscribe_by_token(token)
    except PlatformError as error:
        raise http_error(error) from error
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(_PAGE.format(content="<h1>You are unsubscribed</h1><p>You will not receive further "
                                                 "emails from this sender.</p>"))
    return result
