"""Advanced workflow API: metadata and templates, manual runs, approvals, run
control and the review queue for CRM changes workflows propose.

Workflow CRUD (``/workflows``), ``/workflows/{id}/test`` and ``/workflow-runs``
stay in :mod:`cloud.intel.api.routes_gtm`. Paths here avoid ``/workflows/<word>``
GETs, which the CRUD router would read as a workflow id.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.crud import crud_router
from cloud.intel.api.deps import get_platform, http_error, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform

router = APIRouter()
router.include_router(crud_router("workflow_proposals", path="/workflow-proposals", tag="workflows",
                                  allow_create=False, allow_update=False, allow_delete=False))

ws = APIRouter(prefix="/w/{workspace_id}", tags=["workflows"])


def _run(fn):
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error


def _ids(body: Dict[str, Any], key: str = "ids") -> list:
    ids = body.get(key) or []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{key} must be a list of ids")
    return ids


@ws.get("/workflow-meta")
def workflow_meta(ctx: Ctx = Depends(workspace_ctx)):
    from cloud.intel.automation.engine import ACTIONS, CRM_CHANGE_ACTIONS, TRIGGERS, _OPS
    from cloud.intel.automation.graph import NODE_TYPES

    return {"triggers": list(TRIGGERS), "actions": list(ACTIONS), "operators": list(_OPS),
            "node_types": list(NODE_TYPES), "crm_change_actions": list(CRM_CHANGE_ACTIONS),
            "failure_policies": ["stop", "continue", "retry"]}


@ws.get("/workflow-templates")
def workflow_templates(ctx: Ctx = Depends(workspace_ctx)):
    from cloud.intel.automation.templates import TEMPLATES

    return {"items": TEMPLATES}


@ws.post("/workflow-templates/{key}", status_code=status.HTTP_201_CREATED)
def create_from_template(key: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                         platform: Platform = Depends(get_platform)):
    from cloud.intel.automation.templates import template

    try:
        item = template(key)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown workflow template") from None
    values = {"name": (body or {}).get("name") or item["name"], "trigger": item["trigger"],
              "description": item["description"], "conditions": item.get("conditions") or [],
              "actions": [], "graph": item["graph"], "template_key": item["key"], "enabled": False}
    return _run(lambda: platform.service("automation").save_workflow(ctx, values))


@ws.post("/workflows/{workflow_id}/run")
def run_workflow(workflow_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                 platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("automation").run_now(ctx, workflow_id, (body or {}).get("payload") or {}))


@ws.post("/workflow-runs/{run_id}/approve")
def approve_run(run_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("automation").decide(ctx, run_id, approve=True,
                                                              note=(body or {}).get("note")))


@ws.post("/workflow-runs/{run_id}/reject")
def reject_run(run_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
               platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("automation").decide(ctx, run_id, approve=False,
                                                              note=(body or {}).get("note")))


@ws.post("/workflow-runs/{run_id}/cancel")
def cancel_run(run_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("automation").cancel_run(ctx, run_id))


@ws.post("/workflow-runs/{run_id}/resume")
def resume_run(run_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("automation").resume_now(ctx, run_id))


@ws.post("/workflow-proposals/review")
def review_proposals(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                     platform: Platform = Depends(get_platform)):
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "decision must be approve or reject")
    return _run(lambda: {"items": platform.service("automation").review_proposals(
        ctx, _ids(body), approve=decision == "approve")})


@ws.post("/workflow-proposals/apply")
def apply_proposals(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    return _run(lambda: {"items": platform.service("automation").apply_proposals(ctx, _ids(body))})


router.include_router(ws)
