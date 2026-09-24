"""CRM endpoints: companies, contacts, opportunities, pipelines, tasks, notes,
activities, tags, lists, segments, custom fields and relationships.

Simple resources use :func:`cloud.intel.api.crud.crud_router`; creation and
updates of companies, contacts and opportunities go through
:class:`cloud.intel.crm.service.CrmService` so normalisation, dedupe,
provenance and audit always apply.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.crud import crud_router, list_filters
from cloud.intel.api.deps import get_platform, http_error, page_response, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError, ValidationError
from cloud.intel.platform import Platform

router = APIRouter()
W = "/w/{workspace_id}"


def _crm(platform: Platform):
    return platform.service("crm")


# --- companies ---------------------------------------------------------------------


def _create_company(platform: Platform, ctx: Ctx, values: Dict[str, Any]) -> Dict[str, Any]:
    values = dict(values)
    force = bool(values.pop("force", False))
    return _crm(platform).create_company(ctx, values, force=force)["company"]


def _update_company(platform, ctx, row_id, changes, expected):
    return _crm(platform).update_company(ctx, row_id, changes, expected)


router.include_router(crud_router("companies", create=_create_company, update=_update_company, tag="companies"))


def _company_children(entity: str, order: Optional[str] = None):
    def handler(company_id: str, request: Request, limit: int = 50, offset: int = 0,
                ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
        try:
            platform.store.get(ctx, "companies", company_id)
            filters = {**list_filters(request), "company_id": company_id}
            return page_response(platform.store.list(ctx, entity, filters, order=order, limit=limit, offset=offset))
        except PlatformError as error:
            raise http_error(error) from error

    return handler


for _path, _entity, _order in (("contacts", "contacts", None), ("jobs", "job_postings", None),
                               ("signals", "hiring_signals", None), ("technologies", "company_technologies", None),
                               ("opportunities", "opportunities", None), ("activities", "activities", None),
                               ("changes", "change_events", None)):
    router.add_api_route(W + "/companies/{company_id}/" + _path, _company_children(_entity, _order),
                         methods=["GET"], tags=["companies"], name=f"company_{_path}")


@router.get(W + "/companies/{company_id}/timeline", tags=["companies"])
def company_timeline(company_id: str, limit: int = 100, ctx: Ctx = Depends(workspace_ctx),
                     platform: Platform = Depends(get_platform)):
    try:
        return {"items": jsonable_encoder(_crm(platform).company_timeline(ctx, company_id, limit=min(limit, 500)))}
    except PlatformError as error:
        raise http_error(error) from error


@router.get(W + "/companies/{company_id}/sources", tags=["companies"])
def company_sources(company_id: str, limit: int = 100, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                    platform: Platform = Depends(get_platform)):
    return page_response(platform.store.list(ctx, "source_records",
                                             {"entity_type": "companies", "entity_id": company_id},
                                             order="-observed_at", limit=limit, offset=offset))


@router.get(W + "/companies/{company_id}/relationships", tags=["companies"])
def company_relationships(company_id: str, ctx: Ctx = Depends(workspace_ctx),
                          platform: Platform = Depends(get_platform)):
    return {"items": jsonable_encoder(_crm(platform).relationships(ctx, company_id))}


@router.post(W + "/companies/{company_id}/relationships", status_code=201, tags=["companies"])
def add_relationship(company_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                     platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(_crm(platform).add_relationship(
            ctx, company_id, str(body.get("related_company_id") or ""), str(body.get("relationship") or ""),
            source=str(body.get("source") or "manual"), confidence=body.get("confidence"),
            evidence=body.get("evidence")))
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/companies/merge", tags=["companies"])
def merge_companies(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    keep = body.get("keep_id")
    merge_ids = body.get("merge_ids") or []
    if not keep or not isinstance(merge_ids, list) or not merge_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "send keep_id and a non-empty merge_ids list")
    try:
        return jsonable_encoder(_crm(platform).merge_companies(ctx, keep, merge_ids))
    except PlatformError as error:
        raise http_error(error) from error


# --- contacts ------------------------------------------------------------------------


def _create_contact(platform, ctx, values):
    return _crm(platform).upsert_contact(ctx, values, source_kind="manual", source_name="user")["contact"]


def _update_contact(platform, ctx, row_id, changes, expected):
    return _crm(platform).update_contact(ctx, row_id, changes, expected)


router.include_router(crud_router("contacts", create=_create_contact, update=_update_contact, tag="contacts"))


# --- opportunities and pipelines ----------------------------------------------------------


def _create_opportunity(platform, ctx, values):
    values = dict(values)
    company_id = values.pop("company_id", None)
    title = values.pop("title", None)
    if not company_id or not title:
        raise ValidationError("an opportunity needs company_id and title")
    allowed = {"signal_ids", "signal_types", "score", "score_breakdown", "reason", "campaign_id", "contact_id",
               "evidence", "source", "pipeline_id", "stage_id", "owner_id", "amount"}
    unknown = set(values) - allowed
    if unknown:
        raise ValidationError(f"unknown opportunity field(s): {', '.join(sorted(unknown))}")
    return _crm(platform).create_opportunity(ctx, company_id, title, **values)


def _update_opportunity(platform, ctx, row_id, changes, expected):
    if "stage_id" in changes or "status" in changes:
        raise ValidationError("change the stage with POST /opportunities/{id}/stage")
    return platform.store.update(ctx, "opportunities", row_id, changes, expected_version=expected)


router.include_router(crud_router("opportunities", create=_create_opportunity, update=_update_opportunity,
                                  tag="opportunities"))


@router.post(W + "/opportunities/{opportunity_id}/stage", tags=["opportunities"])
def move_stage(opportunity_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
               platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(_crm(platform).move_stage(ctx, opportunity_id, str(body.get("stage_id") or "")))
    except PlatformError as error:
        raise http_error(error) from error


def _create_pipeline(platform, ctx, values):
    values = dict(values)
    stages = values.pop("stages", None)
    if stages:
        return _crm(platform).create_pipeline(ctx, values.get("name") or "", list(stages),
                                              description=values.get("description"))
    return platform.store.insert(ctx, "pipelines", values)


router.include_router(crud_router("pipelines", create=_create_pipeline, tag="pipelines"))
router.include_router(crud_router("pipeline_stages", path="/pipeline-stages", tag="pipelines"))


@router.get(W + "/pipelines/{pipeline_id}/stages", tags=["pipelines"])
def pipeline_stages(pipeline_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return {"items": jsonable_encoder(_crm(platform).stages(ctx, pipeline_id))}


@router.get(W + "/pipelines/{pipeline_id}/board", tags=["pipelines"])
def pipeline_board(pipeline_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    """Opportunities grouped by stage, for a kanban view."""
    try:
        stages = _crm(platform).stages(ctx, pipeline_id)
        columns = []
        for stage in stages:
            page = platform.store.list(ctx, "opportunities", {"stage_id": stage["id"]}, order="-score", limit=100)
            columns.append({"stage": stage, "total": page.total, "items": page.rows})
        return jsonable_encoder({"pipeline_id": pipeline_id, "columns": columns})
    except PlatformError as error:
        raise http_error(error) from error


# --- tasks, notes, activities, tags, custom fields ------------------------------------------


router.include_router(crud_router("crm_tasks", path="/crm-tasks", tag="tasks",
                                  create=lambda p, c, v: _crm(p).create_task(c, v)))
router.include_router(crud_router("notes", tag="notes",
                                  create=lambda p, c, v: _crm(p).add_note(c, **v)))
router.include_router(crud_router("activities", tag="activities",
                                  create=lambda p, c, v: _crm(p).log_activity(c, **v)))
router.include_router(crud_router("tags", tag="tags"))
router.include_router(crud_router("custom_field_defs", path="/custom-fields", tag="custom fields"))


# --- lists and segments ------------------------------------------------------------------------


def _create_list(platform, ctx, values):
    return _crm(platform).create_list(ctx, values.get("name") or "", values.get("entity_type") or "companies",
                                      description=values.get("description"), source=values.get("source"))


router.include_router(crud_router("lists", create=_create_list, tag="lists"))


@router.get(W + "/lists/{list_id}/members", tags=["lists"])
def list_members(list_id: str, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(_crm(platform).list_members(ctx, list_id, limit=min(limit, 500), offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/lists/{list_id}/members", tags=["lists"])
def add_list_members(list_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                     platform: Platform = Depends(get_platform)):
    ids: List[str] = body.get("ids") or []
    try:
        target = platform.store.get(ctx, "lists", list_id)
        added = _crm(platform).add_to_list(ctx, list_id, body.get("entity_type") or target["entity_type"], ids,
                                           reason=body.get("reason"))
        return {"added": added}
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/lists/{list_id}/members/remove", tags=["lists"])
def remove_list_members(list_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                        platform: Platform = Depends(get_platform)):
    try:
        return {"removed": _crm(platform).remove_from_list(ctx, list_id, body.get("ids") or [])}
    except PlatformError as error:
        raise http_error(error) from error


def _create_segment(platform, ctx, values):
    return _crm(platform).create_segment(ctx, values.get("name") or "", values.get("entity_type") or "companies",
                                         values.get("filters") or {}, values.get("description"))


router.include_router(crud_router("segments", create=_create_segment, tag="segments"))


@router.get(W + "/segments/{segment_id}/results", tags=["segments"])
def segment_results(segment_id: str, limit: int = 50, offset: int = 0, order: Optional[str] = None,
                    ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return page_response(_crm(platform).evaluate_segment(ctx, segment_id, limit=limit, offset=offset,
                                                             order=order))
    except PlatformError as error:
        raise http_error(error) from error
