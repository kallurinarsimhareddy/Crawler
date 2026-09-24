"""The AI Control Room API: ask, plan, run, approve, results, memory, insights, audit.

Everything is under ``/api/v1/w/{workspace_id}/agent`` and runs server-side with
the caller's workspace role. No provider key, model key or secret is ever
returned; the browser only sees plans, results, evidence and approvals.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.agent.tools import MODES, TOOLS, catalogue
from cloud.intel.api.crud import list_filters
from cloud.intel.api.deps import get_platform, http_error, idempotent, page_response, workspace_ctx, write_ctx
from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

router = APIRouter(prefix="/w/{workspace_id}/agent", tags=["ai-control-room"])


def _agent(platform: Platform):
    return platform.service("agent")


def _run_view(platform: Platform, ctx: Ctx, run: Dict[str, Any]) -> Dict[str, Any]:
    approvals = platform.store.all(ctx, "agent_approvals", {"run_id": run["id"]})
    return jsonable_encoder({**run, "approvals": approvals})


def _guard(fn):
    try:
        return fn()
    except PlatformError as error:
        raise http_error(error) from error


# --- catalogue ---------------------------------------------------------------------------

@router.get("/modes")
def modes(ctx: Ctx = Depends(workspace_ctx)):
    return {"items": [{"key": k, **v, "tools": [t["name"] for t in catalogue(k)]} for k, v in MODES.items()]}


@router.get("/tools")
def tools(mode: str = "auto", ctx: Ctx = Depends(workspace_ctx)):
    if mode not in MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown mode")
    items = catalogue(mode)
    for item in items:
        item["allowed_for_you"] = TOOLS[item["name"]].allowed_for(ctx.role)
    return {"items": items}


# --- ask / plan / run ----------------------------------------------------------------------

@router.post("/ask")
def ask(request: Request, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
        platform: Platform = Depends(get_platform)):
    """Chat turn. ``execute`` = the Run button; otherwise the Research button (plan only)."""
    def produce():
        turn = _agent(platform).ask(ctx, str(body.get("text") or ""), session_id=body.get("session_id"),
                                    mode=str(body.get("mode") or "auto"), execute=bool(body.get("execute")))
        return jsonable_encoder({**turn, "run": _run_view(platform, ctx, turn["run"]) if turn.get("run") else None})
    return _guard(lambda: idempotent(platform, ctx, request, body, produce))


@router.get("/runs")
def list_runs(request: Request, limit: int = 30, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
              platform: Platform = Depends(get_platform)):
    return _guard(lambda: page_response(platform.store.list(ctx, "agent_runs", list_filters(request),
                                                            limit=limit, offset=offset)))


@router.get("/runs/{run_id}")
def get_run(run_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: _run_view(platform, ctx, platform.store.get(ctx, "agent_runs", run_id)))


@router.post("/runs/{run_id}/run")
def run(run_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: _run_view(platform, ctx, _agent(platform).run(ctx, run_id)))


@router.put("/runs/{run_id}/plan")
def edit_plan(run_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
              platform: Platform = Depends(get_platform)):
    steps = body.get("steps")
    if not isinstance(steps, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "steps must be a list")
    return _guard(lambda: _run_view(platform, ctx, _agent(platform).edit_plan(ctx, run_id, steps)))


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: _run_view(platform, ctx, _agent(platform).cancel(ctx, run_id)))


@router.get("/runs/{run_id}/results")
def results(run_id: str, view: str = "companies", limit: int = 100, offset: int = 0, order: Optional[str] = None,
            ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: jsonable_encoder(_agent(platform).results(ctx, run_id, view=view, limit=min(limit, 500),
                                                                    offset=offset, order=order)))


@router.post("/runs/{run_id}/actions")
def result_action(run_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    """Act on selected results: add_to_list, create_list, create_opportunity, create_task,
    campaign_proposal, export, start_monitor, validate_email, find_contacts. High-impact ones wait for approval."""
    return _guard(lambda: _run_view(platform, ctx, _agent(platform).act_on_results(
        ctx, run_id, str(body.get("action") or ""), company_ids=list(body.get("company_ids") or []),
        params=body.get("params") or {})))


@router.get("/runs/{run_id}/trail")
def trail(run_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    """Execution history for admins: request, plan, tool calls (redacted), credits, approvals, audit."""
    if not ctx.can_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "execution history is visible to workspace admins")
    return _guard(lambda: jsonable_encoder(_agent(platform).trail(ctx, run_id)))


# --- approvals ------------------------------------------------------------------------------

@router.get("/approvals")
def approvals(status_: str = Query("pending", alias="status"), run_id: Optional[str] = None, ctx: Ctx = Depends(workspace_ctx),
              platform: Platform = Depends(get_platform)):
    return {"items": jsonable_encoder(_agent(platform).approvals(ctx, run_id, status_))}


@router.post("/approvals/{approval_id}/{decision}")
def decide(approval_id: str, decision: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    if decision not in ("approve", "reject"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "decision must be approve or reject")
    return _guard(lambda: _run_view(platform, ctx, _agent(platform).decide(ctx, approval_id,
                                                                            approve=decision == "approve")))


# --- sessions -------------------------------------------------------------------------------

@router.get("/sessions")
def sessions(limit: int = 30, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return page_response(platform.store.list(ctx, "agent_sessions", {"status": "active"}, limit=limit))


@router.get("/sessions/{session_id}")
def session(session_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    def produce():
        row = platform.store.get(ctx, "agent_sessions", session_id)
        messages = platform.store.all(ctx, "agent_messages", {"session_id": session_id}, order="created_at", cap=500)
        return jsonable_encoder({**row, "messages": messages})
    return _guard(produce)


@router.post("/sessions/{session_id}/clear")
def clear_session(session_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    """Clear history: archive this conversation (kept for audit) and forget its working set."""
    return _guard(lambda: jsonable_encoder(platform.store.update(ctx, "agent_sessions", session_id,
                                                                 {"status": "archived", "working_set": {}})))


# --- memory -----------------------------------------------------------------------------------

@router.get("/memory")
def memory(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return {"items": jsonable_encoder(platform.store.all(ctx, "ai_memory", {}, cap=1000))}


@router.post("/memory", status_code=status.HTTP_201_CREATED)
def remember(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    service = platform.service("agent_memory")
    if body.get("text"):
        return _guard(lambda: jsonable_encoder(service.remember(ctx, str(body["text"]))))
    return _guard(lambda: jsonable_encoder(service.save(ctx, str(body.get("kind") or ""), str(body.get("key") or ""),
                                                        body.get("value") or {}, text=body.get("note"))))


@router.delete("/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def forget(memory_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    _guard(lambda: platform.service("agent_memory").forget(ctx, memory_id))


# --- insights ---------------------------------------------------------------------------------

@router.get("/insights")
def insights(status_: Optional[str] = Query(None, alias="status"), ctx: Ctx = Depends(workspace_ctx),
             platform: Platform = Depends(get_platform)):
    return page_response(platform.service("insights").list(ctx, status=status_))


@router.post("/insights/refresh")
def refresh_insights(ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return {"created": jsonable_encoder(_guard(lambda: platform.service("insights").generate(ctx)))}


@router.get("/insights/settings")
def insight_settings(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return platform.service("insights").settings(ctx)


@router.put("/insights/settings")
def set_insight_settings(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                         platform: Platform = Depends(get_platform)):
    return _guard(lambda: platform.service("insights").configure(ctx, body))


@router.post("/insights/{insight_id}/{state}")
def mark_insight(insight_id: str, state: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _guard(lambda: jsonable_encoder(platform.service("insights").mark(ctx, insight_id, state)))


# --- saved requests and views -------------------------------------------------------------------

@router.get("/saved")
def saved(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return page_response(platform.store.list(ctx, "saved_requests", {}, limit=200))


@router.post("/saved", status_code=status.HTTP_201_CREATED)
def save_request(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    def produce():
        row = platform.store.insert(ctx, "saved_requests", {
            "name": str(body.get("name") or body.get("request") or "Saved")[:200], "request": str(body.get("request") or ""),
            "mode": body.get("mode"), "kind": body.get("kind") or "request", "config": body.get("config") or {}})
        audit(platform.store, ctx, "agent.save_request", entity_type="saved_requests", entity_id=row["id"])
        return jsonable_encoder(row)
    return _guard(produce)


@router.delete("/saved/{saved_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved(saved_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    _guard(lambda: platform.store.delete(ctx, "saved_requests", saved_id))


# --- per-workspace AI provider --------------------------------------------------------------------

@router.get("/ai-config")
def ai_config(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    registry = platform.service("ai")
    return {"workspace": registry.workspace_config(ctx), "platform_default": registry.configured,
            "in_use": registry.for_ctx(ctx, "describe").describe(),
            "external_allowed": ctx.ai_external_allowed,
            "providers": ["rules", "claude", "gemini", "openai_compatible"]}


@router.put("/ai-config")
def set_ai_config(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    """Workspace provider/model and explicit fallbacks. Keys are never accepted here — they stay server-side."""
    if not ctx.can_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "workspace admin rights required")
    allowed = {"rules", "claude", "gemini", "openai_compatible"}
    provider = body.get("provider")
    if provider is not None and provider not in allowed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"provider must be one of {sorted(allowed)}")
    fallbacks = body.get("fallbacks") or []
    if not isinstance(fallbacks, list) or any(not isinstance(f, dict) or f.get("provider") not in allowed for f in fallbacks):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "fallbacks must be a list of {provider, model}")
    if any(k in body for k in ("api_key", "key", "secret", "token")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "API keys are configured on the server, never here")

    def produce():
        info = platform.store.membership(ctx.user_id, ctx.workspace_id)
        settings = dict(info.get("settings") or {})
        settings["ai"] = {**(settings.get("ai") or {}), "provider": provider, "model": body.get("model"),
                          "fallbacks": [{"provider": f["provider"], "model": f.get("model")} for f in fallbacks]}
        platform.store.update_workspace(ctx, settings=settings)
        audit(platform.store, ctx, "agent.ai_config", changes=settings["ai"])
        return platform.service("ai").workspace_config(ctx)
    return _guard(produce)
