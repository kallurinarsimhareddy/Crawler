"""Email & Sending API: mailboxes, OAuth, outbox, provider events, suppression tools,
campaign operations and the sequence step editor.

Public (no sign-in, left out of OpenAPI):

* ``GET /api/v1/oauth/{provider}/callback`` — the OAuth redirect; the single-use
  state proves which workspace asked.
* ``POST /api/v1/w/{ws}/events/{provider}/webhook`` — provider delivery events,
  authenticated by ``X-Webhook-Secret`` or an ``X-Signature`` HMAC-SHA256 of the
  raw body, with ``CAREERCLOUD_WEBHOOK_SECRET_<PROVIDER>`` (falling back to
  ``CAREERCLOUD_INBOUND_WEBHOOK_SECRET``). Without a configured secret the
  webhook refuses everything.

Secrets never appear in a response: mailbox rows are returned without ciphertext.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from cloud.intel.api.crud import list_filters
from cloud.intel.api.deps import get_platform, http_error, page_response, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError, ValidationError
from cloud.intel.platform import Platform

router = APIRouter()
ws = APIRouter(prefix="/w/{workspace_id}", tags=["sending"])


def _run(fn):
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error


def _ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"not a date/time: {value!r}") from None


# --- providers & mailboxes ---------------------------------------------------------------------


@ws.get("/sending/status")
def sending_status(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    def go():
        mailboxes = platform.service("mailboxes")
        return {"providers": mailboxes.providers(), "outbox": platform.service("outbox").status(ctx),
                "capabilities": platform.service("events").capabilities(),
                "webhook_providers": ["sendgrid", "postmark", "generic"],
                "webhooks_configured": {p: bool(_secret_for(p)) for p in ("sendgrid", "postmark", "generic")},
                "secrets_key_configured": bool(platform.config.secrets_key),
                "unsubscribe_configured": bool(os.environ.get("CAREERCLOUD_UNSUBSCRIBE_SECRET")
                                               or platform.config.secrets_key)}

    return _run(go)


@ws.get("/mailboxes")
def list_mailboxes(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"items": platform.service("mailboxes").list(ctx)})


@ws.post("/mailboxes/oauth/{provider}/start")
def oauth_start(provider: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("mailboxes").start_oauth(ctx, provider,
                                                                  redirect_to=(body or {}).get("redirect_to")))


@ws.post("/mailboxes/api", status_code=status.HTTP_201_CREATED)
def connect_api(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    if "password" in body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mailbox passwords are never accepted")
    return _run(lambda: platform.service("mailboxes").connect_api(
        ctx, address=str(body.get("address") or ""), api_key=str(body.get("api_key") or ""),
        vendor=str(body.get("vendor") or "sendgrid"), display_name=body.get("display_name"),
        daily_limit=body.get("daily_limit")))


@ws.post("/mailboxes/smtp", status_code=status.HTTP_201_CREATED)
def connect_smtp(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                 platform: Platform = Depends(get_platform)):
    if "password" in body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "mailbox passwords are never accepted; the SMTP relay is configured on the server")
    return _run(lambda: platform.service("mailboxes").connect_smtp(
        ctx, address=str(body.get("address") or ""), display_name=body.get("display_name")))


@ws.patch("/mailboxes/{mailbox_id}")
def update_mailbox(mailbox_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                   platform: Platform = Depends(get_platform)):
    changes = body.get("changes") if isinstance(body.get("changes"), dict) else body
    return _run(lambda: platform.service("mailboxes").update(ctx, mailbox_id, changes))


@ws.delete("/mailboxes/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mailbox(mailbox_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    _run(lambda: platform.service("mailboxes").delete(ctx, mailbox_id))


@ws.post("/mailboxes/{mailbox_id}/{action}")
def mailbox_action(mailbox_id: str, action: str, body: Optional[Dict[str, Any]] = Body(None),
                   ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    service = platform.service("mailboxes")
    if action == "disconnect":
        return _run(lambda: service.disconnect(ctx, mailbox_id))
    if action == "default":
        return _run(lambda: service.set_default(ctx, mailbox_id))
    if action == "test":
        return _run(lambda: service.test(ctx, mailbox_id, to=(body or {}).get("to")))
    raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown mailbox action")


@router.get("/oauth/{provider}/callback", include_in_schema=False)
def oauth_callback(provider: str, request: Request, platform: Platform = Depends(get_platform)):
    params = request.query_params
    front = os.environ.get("CAREERCLOUD_PUBLIC_APP_URL", "").rstrip("/")
    if params.get("error"):
        target = f"{front}/settings/sending?oauth_error={quote(params.get('error', '')[:100])}"
        return RedirectResponse(target, status_code=302) if front else HTMLResponse(
            f"<p>Authorization was not granted ({params.get('error')[:100]}).</p>", status_code=400)
    try:
        result = platform.service("mailboxes").complete_oauth(provider, params.get("state", ""),
                                                              params.get("code", ""))
    except PlatformError as error:
        if front:
            return RedirectResponse(f"{front}/settings/sending?oauth_error={quote(str(error)[:200])}",
                                    status_code=302)
        return HTMLResponse(f"<p>Could not connect the mailbox: {str(error)[:200]}</p>", status_code=400)
    address = result["mailbox"]["address"]
    if front:
        return RedirectResponse(f"{front}{result['redirect_to']}?connected={quote(address)}", status_code=302)
    return HTMLResponse(f"<p>{address} is connected. You can close this window.</p>")


# --- outbox ------------------------------------------------------------------------------------------


@ws.get("/outbox")
def list_outbox(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                platform: Platform = Depends(get_platform)):
    try:
        page = platform.store.list(ctx, "outbound_messages", list_filters(request), order="-created_at",
                                   limit=limit, offset=offset)
    except PlatformError as error:
        raise http_error(error) from error
    return page_response(page)


@ws.post("/outbox/process")
def process_outbox(ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("outbox").process_now(ctx))


@ws.post("/outbox/{message_id}/cancel")
def cancel_outbox(message_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("outbox").cancel(ctx, message_id))


# --- provider events ---------------------------------------------------------------------------------


def _secret_for(provider: str) -> str:
    return (os.environ.get(f"CAREERCLOUD_WEBHOOK_SECRET_{provider.upper()}")
            or os.environ.get("CAREERCLOUD_INBOUND_WEBHOOK_SECRET") or "")


def verify_webhook(provider: str, headers: Any, raw: bytes) -> bool:
    secret = _secret_for(provider)
    if not secret:
        return False
    supplied = headers.get("x-webhook-secret", "")
    if supplied:
        return hmac.compare_digest(secret, supplied)
    signature = headers.get("x-signature", "")
    if signature:
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.removeprefix("sha256="))
    return False


@ws.post("/events/{provider}/webhook", include_in_schema=False)
async def provider_webhook(workspace_id: str, provider: str, request: Request,
                           platform: Platform = Depends(get_platform)):
    from cloud.intel.sending.events import WEBHOOK_PROVIDERS

    if provider not in WEBHOOK_PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no webhook for this provider")
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    if not verify_webhook(provider, request.headers, raw):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing webhook signature")
    lookup = getattr(platform.store, "system_membership", None)
    if not callable(lookup) or lookup(workspace_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    try:
        payload = json.loads(raw or b"null")
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "body must be JSON") from None
    ctx = Ctx.for_system(workspace_id, actor_kind="system")
    return _run(lambda: platform.service("events").ingest(ctx, provider, payload))


@ws.post("/events/manual")
def manual_event(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                 platform: Platform = Depends(get_platform)):
    kind = str(body.get("kind") or "")
    if kind not in ("reply", "bounce", "unsubscribe", "complaint"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "kind must be reply, bounce, unsubscribe or complaint")
    return _run(lambda: platform.service("events").record_manual(ctx, kind, email=str(body.get("email") or ""),
                                                                 note=body.get("note")))


@ws.get("/inbound-events")
def list_inbound(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        page = platform.store.list(ctx, "inbound_events", list_filters(request), order="-occurred_at",
                                   limit=limit, offset=offset)
    except PlatformError as error:
        raise http_error(error) from error
    return page_response(page)


# --- suppression tools (the plain CRUD stays at /suppressions) ------------------------------------------


@ws.get("/suppression/check")
def check_suppression(email: str, campaign_id: Optional[str] = None, ctx: Ctx = Depends(workspace_ctx),
                      platform: Platform = Depends(get_platform)):
    return _run(lambda: {"email": email, "suppressed": platform.service("suppression").check(
        ctx, email, campaign_id=campaign_id)})


@ws.get("/suppression/stats")
def suppression_stats(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("suppression").stats(ctx))


@ws.get("/suppression/global")
def suppression_global(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("suppression").global_list())


@ws.post("/suppression/add", status_code=status.HTTP_201_CREATED)
def add_suppression(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("suppression").add(
        ctx, str(body.get("value") or ""), kind=body.get("kind") or None, reason=body.get("reason") or "manual",
        scope=body.get("scope") or "workspace", campaign_id=body.get("campaign_id"),
        expires_at=_ts(body.get("expires_at")), note=body.get("note"), source="manual"))


@ws.post("/suppression/bulk")
def bulk_suppression(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                     platform: Platform = Depends(get_platform)):
    values = body.get("values")
    if isinstance(values, str):
        values = [v for chunk in values.splitlines() for v in chunk.replace(";", ",").split(",")]
    if not isinstance(values, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "values must be a list or text")
    if len(values) > 50_000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "at most 50,000 values at once")
    return _run(lambda: platform.service("suppression").bulk_add(
        ctx, values, reason=body.get("reason") or "manual", scope=body.get("scope") or "workspace",
        campaign_id=body.get("campaign_id"), expires_at=_ts(body.get("expires_at")), note=body.get("note")))


@ws.post("/suppression/remove")
def remove_suppression(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                       platform: Platform = Depends(get_platform)):
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ids must be a list")
    return _run(lambda: platform.service("suppression").bulk_remove(ctx, ids))


@ws.post("/suppression/import")
def import_suppression(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                       platform: Platform = Depends(get_platform)):
    text = str(body.get("text") or "")
    if len(text) > 5_000_000:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large (5 MB max)")
    return _run(lambda: platform.service("suppression").import_csv(
        ctx, text, reason=body.get("reason") or "manual", scope=body.get("scope") or "workspace",
        campaign_id=body.get("campaign_id")))


@ws.get("/suppression/export")
def export_suppression(request: Request, ctx: Ctx = Depends(workspace_ctx),
                       platform: Platform = Depends(get_platform)):
    try:
        text = platform.service("suppression").export_csv(ctx, list_filters(request))
    except PlatformError as error:
        raise http_error(error) from error
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="suppressions.csv"'})


# --- campaigns ---------------------------------------------------------------------------------------------


@ws.post("/campaigns/{campaign_id}/configure")
def configure_campaign(campaign_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                       platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("campaigns").configure(ctx, campaign_id, body))


@ws.get("/campaigns/{campaign_id}/audience")
def campaign_audience(campaign_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("campaigns").audience_preview(ctx, campaign_id))


@ws.get("/campaigns/{campaign_id}/readiness")
def campaign_readiness(campaign_id: str, ctx: Ctx = Depends(workspace_ctx),
                       platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("campaigns").readiness(ctx, platform.store.get(ctx, "campaigns",
                                                                                           campaign_id)))


@ws.get("/campaigns/{campaign_id}/performance")
def campaign_performance(campaign_id: str, ctx: Ctx = Depends(workspace_ctx),
                         platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("campaigns").performance(ctx, campaign_id))


@ws.post("/campaigns/{campaign_id}/launch")
def launch_campaign(campaign_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("campaigns").launch(ctx, campaign_id))


@ws.post("/campaigns/{campaign_id}/status/{action}")
def campaign_status(campaign_id: str, action: str, ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("campaigns").set_status(ctx, campaign_id, action))


# --- sequences -------------------------------------------------------------------------------------------


@ws.get("/sequences/{sequence_id}/overview")
def sequence_overview(sequence_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("sequences").overview(ctx, sequence_id))


@ws.put("/sequences/{sequence_id}/steps")
def save_steps(sequence_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
               platform: Platform = Depends(get_platform)):
    steps = body.get("steps")
    if not isinstance(steps, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "steps must be a list")
    return _run(lambda: {"steps": platform.service("sequences").save_steps(ctx, sequence_id, steps)})


@ws.post("/sequences/{sequence_id}/cadence")
def apply_cadence(sequence_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    def go():
        kwargs = {}
        if body.get("days"):
            kwargs["days"] = [int(d) for d in body["days"]]
        return {"steps": platform.service("sequences").apply_cadence(ctx, sequence_id,
                                                                     list(body.get("template_ids") or []), **kwargs)}

    return _run(go)


@ws.post("/sequences/{sequence_id}/stop-conditions")
def stop_conditions(sequence_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("sequences").update_stop_conditions(ctx, sequence_id, body))


@ws.post("/enrollments/{enrollment_id}/stop-with-reason")
def stop_with_reason(enrollment_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                     platform: Platform = Depends(get_platform)):
    reason = str((body or {}).get("reason") or "stopped by a user")
    if len(reason) > 300:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "reason is too long")
    return _run(lambda: platform.service("sequences").stop_enrollment(ctx, enrollment_id, "stopped", reason=reason))


router.include_router(ws)

__all__ = ["router", "verify_webhook", "ValidationError"]
