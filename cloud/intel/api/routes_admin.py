"""Workspace administration API: members/roles, invitations, teams, assignment,
the searchable audit log (with CSV export) and in-app notifications.

Rights are enforced in :mod:`cloud.intel.admin.service` (``require_admin`` /
``require_manager``) and, in PostgreSQL, again by RLS.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime, time, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder

from cloud.api.auth import Principal
from cloud.api.middleware import RateLimiter, client_ip
from cloud.api.routes import current_user
from cloud.intel.api.deps import get_platform, http_error, page_response, workspace_ctx, write_ctx
from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, PlatformError, ValidationError
from cloud.intel.platform import Platform

router = APIRouter(tags=["admin"])
ws = APIRouter(prefix="/w/{workspace_id}", tags=["admin"])


def _run(fn):
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error
    except ValueError as error:  # bad uuid and similar
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error


def _admin(platform: Platform):
    return platform.service("admin")


def _app_url(request: Request) -> Optional[str]:
    """Where invite links point: the calling dashboard when it is an allowed origin,
    else ``CAREERCLOUD_PUBLIC_APP_URL``. Never an arbitrary Origin header."""
    settings = getattr(request.app.state, "settings", None)
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin and settings is not None and origin in tuple(settings.cors_origins):
        return origin
    configured = os.environ.get("CAREERCLOUD_PUBLIC_APP_URL", "").strip().rstrip("/")
    return configured or None


def _throttle_invites(request: Request) -> None:
    """Invitation preview/accept are reachable with nothing but a token: 20 tries per
    client IP per 10 minutes, on top of the global per-IP limit."""
    limiter = getattr(request.app.state, "invite_limiter", None)
    if limiter is None:
        limiter = request.app.state.invite_limiter = RateLimiter(capacity=20, refill_per_second=20 / 600)
    settings = getattr(request.app.state, "settings", None)
    allowed, wait = limiter.allow(f"invite:{client_ip(request, getattr(settings, 'trust_proxy', 'none'))}")
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many invitation attempts; try again later",
                            headers={"Retry-After": str(max(1, int(wait + 0.999)))})


def _token(body: Dict[str, Any]) -> str:
    return str(body.get("token") or "").strip()


def _text(body: Dict[str, Any], key: str) -> Optional[str]:
    value = body.get(key)
    return str(value) if value not in (None, "") else None


# --- members & permissions -----------------------------------------------------------


@ws.get("/admin/overview")
def overview(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: _admin(platform).overview(ctx))


@ws.get("/admin/members")
def members(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"items": _admin(platform).members(ctx)})


@ws.patch("/admin/members/{user_id}")
@ws.patch("/admin/members/{user_id}/role")
def change_role(user_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                platform: Platform = Depends(get_platform)):
    changes = body.get("changes") if isinstance(body.get("changes"), dict) else body
    return _run(lambda: _admin(platform).change_role(ctx, user_id, str(changes.get("role") or "")))


@ws.patch("/admin/members/{user_id}/teams")
def set_member_teams(user_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                     platform: Platform = Depends(get_platform)):
    changes = body.get("changes") if isinstance(body.get("changes"), dict) else body
    team_ids = changes.get("team_ids")
    if not isinstance(team_ids, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "team_ids must be a list")
    return _run(lambda: _admin(platform).set_member_teams(ctx, user_id, team_ids))


@ws.delete("/admin/members/{user_id}")
def remove_member(user_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: _admin(platform).remove_member(ctx, user_id))


# --- invitations -------------------------------------------------------------------------


@ws.get("/admin/invitations")
def invitations(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"items": _admin(platform).invitations(ctx)})


@ws.post("/admin/invitations", status_code=status.HTTP_201_CREATED)
def invite(request: Request, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
           platform: Platform = Depends(get_platform)):
    return _run(lambda: _admin(platform).invite(
        ctx, str(body.get("email") or ""), str(body.get("role") or "member"), first_name=_text(body, "first_name"),
        last_name=_text(body, "last_name"), team_id=_text(body, "team_id"), days=int(body.get("days") or 7),
        app_url=_app_url(request)))


@ws.post("/admin/invitations/{invitation_id}/resend")
def resend(invitation_id: str, request: Request, ctx: Ctx = Depends(workspace_ctx),
           platform: Platform = Depends(get_platform)):
    return _run(lambda: _admin(platform).resend_invitation(ctx, invitation_id, app_url=_app_url(request)))


@ws.post("/admin/invitations/{invitation_id}/link")
def invitation_link(invitation_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    """A new copyable invite link (the previous one stops working; tokens are stored hashed)."""
    return _run(lambda: _admin(platform).invitation_link(ctx, invitation_id))


@ws.post("/admin/invitations/{invitation_id}/revoke")
def revoke(invitation_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: _admin(platform).revoke_invitation(ctx, invitation_id))


@ws.post("/invitations/accept")
def accept(workspace_id: str, request: Request, body: Dict[str, Any] = Body(...),
           principal: Principal = Depends(current_user), platform: Platform = Depends(get_platform)):
    """The signed-in invitee (not yet a member, so no workspace context) redeems their token.
    The token names its workspace; it must match the path."""
    _throttle_invites(request)
    token = _token(body)
    if token.partition(".")[0] != workspace_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "this invitation is not valid")
    return _run(lambda: _admin(platform).accept_invitation(token, user_id=principal.user_id, email=principal.email))


# --- the invite page (no workspace in the path: the token names it) ---------------------


@router.post("/invitations/preview")
def preview_invitation(request: Request, body: Dict[str, Any] = Body(...), platform: Platform = Depends(get_platform)):
    """Shown before sign-in, so it needs no session: the token is the secret. Only a
    matching token gets an answer; every other input gets the same 422."""
    _throttle_invites(request)
    return _run(lambda: _admin(platform).preview_invitation(_token(body)))


@router.post("/invitations/accept")
def accept_invitation(request: Request, body: Dict[str, Any] = Body(...), principal: Principal = Depends(current_user),
                      platform: Platform = Depends(get_platform)):
    _throttle_invites(request)
    return _run(lambda: _admin(platform).accept_invitation(_token(body), user_id=principal.user_id,
                                                           email=principal.email))


# --- teams ----------------------------------------------------------------------------------


@ws.get("/admin/teams")
def teams(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"items": _admin(platform).teams(ctx)})


@ws.post("/admin/teams", status_code=status.HTTP_201_CREATED)
def create_team(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    return _run(lambda: _admin(platform).save_team(ctx, body))


@ws.patch("/admin/teams/{team_id}")
def update_team(team_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    changes = body.get("changes") if isinstance(body.get("changes"), dict) else body
    return _run(lambda: _admin(platform).save_team(ctx, changes, team_id=team_id))


@ws.delete("/admin/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(team_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    _run(lambda: _admin(platform).delete_team(ctx, team_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@ws.post("/admin/teams/{team_id}/members", status_code=status.HTTP_201_CREATED)
def add_team_member(team_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    return _run(lambda: _admin(platform).add_team_member(ctx, team_id, str(body.get("user_id") or ""),
                                                         str(body.get("role") or "member")))


@ws.delete("/admin/teams/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_team_member(team_id: str, user_id: str, ctx: Ctx = Depends(write_ctx),
                       platform: Platform = Depends(get_platform)):
    _run(lambda: _admin(platform).remove_team_member(ctx, team_id, user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@ws.post("/admin/assign")
def assign(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ids must be a list")
    return _run(lambda: _admin(platform).assign(ctx, str(body.get("entity") or ""), ids, body.get("owner_id") or None))


# --- audit log --------------------------------------------------------------------------------


@ws.post("/audit/session")
def session_event(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                  platform: Platform = Depends(get_platform)):
    """Recorded by the web app on sign-in and sign-out (any role, including read-only)."""
    event = str(body.get("event") or "")
    if event not in ("login", "logout"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "event must be login or logout")
    audit(platform.store, ctx.as_system("user") if not ctx.can_write else ctx, f"session.{event}",
          summary=f"{ctx.actor_label or 'user'} signed {'in' if event == 'login' else 'out'}")
    return {"ok": True}


def _day(value: Optional[str], end: bool = False) -> Optional[datetime]:
    if not value:
        return None
    try:
        if len(value) == 10:
            d = datetime.strptime(value, "%Y-%m-%d").date()
            return datetime.combine(d, time.max if end else time.min, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValidationError(f"not a date: {value!r}") from None


def audit_filters(request: Request) -> Dict[str, Any]:
    p = request.query_params
    filters: Dict[str, Any] = {}
    if p.get("action"):
        filters["action__ilike"] = p["action"]
    if p.get("actor"):
        filters["actor_label__ilike"] = p["actor"]
    if p.get("actor_id"):
        filters["actor_id"] = p["actor_id"]
    if p.get("actor_kind"):
        filters["actor_kind"] = p["actor_kind"]
    if p.get("entity_type"):
        filters["entity_type"] = p["entity_type"]
    if p.get("entity_id"):
        filters["entity_id"] = p["entity_id"]
    if p.get("q"):
        filters["summary__ilike"] = p["q"]
    start, end = _day(p.get("from")), _day(p.get("to"), end=True)
    if start:
        filters["created_at__gte"] = start
    if end:
        filters["created_at__lte"] = end
    return filters


@ws.get("/audit/search")
def search_audit(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        page = platform.store.list(ctx, "audit_log", audit_filters(request), order="-created_at",
                                   limit=max(1, min(limit, 200)), offset=max(0, offset))
        return page_response(page)
    except PlatformError as error:
        raise http_error(error) from error


@ws.get("/audit/export.csv")
def export_audit(request: Request, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        ctx.require_manager()
        rows = platform.store.all(ctx, "audit_log", audit_filters(request), order="-created_at", cap=50_000)
    except PlatformError as error:
        raise http_error(error) from error
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["created_at", "actor", "actor_id", "actor_kind", "action", "entity_type", "entity_id",
                     "summary", "changes"])
    import json

    for r in rows:
        # Changes were redacted when written; exported as stored.
        cells = [r.get("created_at"), r.get("actor_label"), r.get("actor_id"), r.get("actor_kind"), r.get("action"),
                 r.get("entity_type"), r.get("entity_id"), r.get("summary"),
                 json.dumps(r.get("changes") or {}, sort_keys=True, default=str)]
        # Neutralise spreadsheet formulas in exported text.
        writer.writerow(["'" + c if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in
                         [x.isoformat() if isinstance(x, datetime) else x for x in cells]])
    audit(platform.store, ctx, "audit.export", summary=f"{len(rows)} audit row(s) exported")
    return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'})


# --- notifications ------------------------------------------------------------------------------


@ws.get("/notifications")
def list_notifications(unread: bool = False, limit: int = 50, ctx: Ctx = Depends(workspace_ctx),
                       platform: Platform = Depends(get_platform)):
    service = platform.service("notifications")
    return _run(lambda: {"items": service.list(ctx, unread_only=unread, limit=limit),
                         "unread": service.unread_count(ctx)})


@ws.get("/notifications/count")
def notification_count(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"unread": platform.service("notifications").unread_count(ctx)})


@ws.post("/notifications/read-all")
def read_all(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"marked": platform.service("notifications").mark_all_read(ctx)})


@ws.post("/notifications/{notification_id}/read")
def read_one(notification_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("notifications").mark_read(ctx, notification_id))


router.include_router(ws)
