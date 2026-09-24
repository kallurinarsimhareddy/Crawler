"""Intelligence routes: jobs, crawls, hiring signals, scores, technology, discovery, monitoring."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.crud import crud_router
from cloud.intel.api.deps import get_platform, http_error, idempotent, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError, ValidationError
from cloud.intel.platform import Platform

router = APIRouter()

# --- read-mostly resources ------------------------------------------------------------

router.include_router(crud_router("job_postings", path="/jobs", tag="jobs", allow_create=False,
                                  allow_update=False, allow_delete=False))
router.include_router(crud_router("hiring_signals", path="/hiring-signals", tag="hiring-intelligence",
                                  allow_create=False, allow_update=False, allow_delete=False))
router.include_router(crud_router("change_events", path="/change-events", tag="monitoring", allow_create=False))
router.include_router(crud_router("discovery_candidates", path="/discovery/candidates", tag="discovery",
                                  allow_create=False, allow_update=False, allow_delete=False))


def _record_technology(platform: Platform, ctx: Ctx, values: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("company_id", "technology"):
        if not values.get(key):
            raise ValidationError(f"{key} is required")
    return platform.service("technology").record(
        ctx, values["company_id"], values["technology"], source=values.get("source") or "manual",
        category=values.get("category"), evidence_url=values.get("evidence_url"),
        evidence_text=values.get("evidence_text"), confidence=values.get("confidence"))


router.include_router(crud_router("company_technologies", path="/company-technologies", tag="technology",
                                  create=_record_technology, allow_update=False))


def _create_monitor(platform: Platform, ctx: Ctx, values: Dict[str, Any]) -> Dict[str, Any]:
    return platform.service("monitoring").create_monitor(
        ctx, name=str(values.get("name") or "Monitor"), target_type=str(values.get("target_type") or ""),
        target_id=str(values.get("target_id") or ""), frequency=str(values.get("frequency") or "weekly"),
        watch=values.get("watch") or [], enabled=bool(values.get("enabled", True)))


router.include_router(crud_router("monitors", tag="monitoring", create=_create_monitor))


def _task(platform: Platform, ctx: Ctx, request: Request, kind: str, params: Dict[str, Any], **kw: Any):
    return idempotent(platform, ctx, request, {"kind": kind, **params},
                      lambda: jsonable_encoder(platform.tasks.submit(ctx, kind, params, **kw)))


# --- jobs and crawls ----------------------------------------------------------------------


@router.post("/w/{workspace_id}/jobs/ingest", tags=["jobs"])
def ingest_jobs(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    postings = body.get("postings")
    if not isinstance(postings, list) or not postings:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "postings must be a non-empty list")
    if len(postings) > 5000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "at most 5000 postings per request")
    try:
        return platform.service("jobs").ingest_postings(
            ctx, postings, source_kind="manual" if body.get("source_kind") in (None, "manual") else "api",
            source_name=str(body.get("source_name") or "manual"), company_id=body.get("company_id"))
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/w/{workspace_id}/crawl", status_code=status.HTTP_201_CREATED, tags=["jobs"])
def start_crawl(request: Request, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    params = {k: body[k] for k in ("company_ids", "filters", "list_id", "browser_fallback", "detect_signals")
              if k in body}
    if not any(params.get(k) for k in ("company_ids", "filters", "list_id")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "give company_ids, filters or list_id")
    try:
        return _task(platform, ctx, request, "crawl", params)
    except PlatformError as error:
        raise http_error(error) from error


# --- hiring intelligence ---------------------------------------------------------------------


@router.post("/w/{workspace_id}/hiring-signals/{signal_id}/dismiss", tags=["hiring-intelligence"])
def dismiss_signal(signal_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("signals").dismiss(ctx, signal_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/w/{workspace_id}/signals/run", status_code=status.HTTP_201_CREATED, tags=["hiring-intelligence"])
def run_signals(request: Request, body: Dict[str, Any] = Body(default={}), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    try:
        return _task(platform, ctx, request, "signals", {"company_ids": body.get("company_ids") or []})
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/w/{workspace_id}/companies/{company_id}/scores", tags=["hiring-intelligence"])
def company_scores(company_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        company = platform.store.get(ctx, "companies", company_id)
        return jsonable_encoder({
            "company_id": company_id, "account_score": company.get("account_score"),
            "hiring_score": company.get("hiring_score"), "opportunity_score": company.get("opportunity_score"),
            "breakdown": company.get("score_breakdown") or {},
            "hiring": platform.service("signals").aggregate_company(ctx, company_id),
            "signals": platform.store.all(ctx, "hiring_signals", {"company_id": company_id, "status": "active"}),
        })
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/w/{workspace_id}/companies/{company_id}/scores", tags=["hiring-intelligence"])
def recompute_scores(company_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    try:
        signals = platform.service("signals")
        detected = signals.detect_for_company(ctx, company_id)
        company = platform.store.get(ctx, "companies", company_id)
        return jsonable_encoder({"signals": detected, "account_score": company["account_score"],
                                 "hiring_score": company["hiring_score"],
                                 "opportunity_score": company["opportunity_score"],
                                 "breakdown": company["score_breakdown"]})
    except PlatformError as error:
        raise http_error(error) from error


# --- technology --------------------------------------------------------------------------------


@router.get("/w/{workspace_id}/technology/taxonomy", tags=["technology"])
def technology_taxonomy(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return platform.service("technology").taxonomy()


@router.post("/w/{workspace_id}/technology/detect", tags=["technology"])
def technology_detect(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                      platform: Platform = Depends(get_platform)):
    text = str(body.get("text") or "")
    if len(text) > 200_000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "text is too long (200k characters max)")
    return {"items": platform.service("technology").detect_in_text(text)}


# --- discovery ---------------------------------------------------------------------------------


@router.post("/w/{workspace_id}/discovery/candidates", status_code=status.HTTP_201_CREATED, tags=["discovery"])
def submit_candidates(request: Request, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                      platform: Platform = Depends(get_platform)):
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "candidates must be a non-empty list")
    if len(candidates) > 5000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "at most 5000 candidates per request")
    try:
        return idempotent(platform, ctx, request, body, lambda: {"items": jsonable_encoder(
            platform.service("discovery").submit_candidates(
                ctx, candidates, source_kind="manual", source_name=str(body.get("source_name") or "manual")))})
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/w/{workspace_id}/discovery/run", status_code=status.HTTP_201_CREATED, tags=["discovery"])
def run_discovery(request: Request, body: Dict[str, Any] = Body(default={}), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    params = {k: body[k] for k in ("candidate_ids", "source", "urls", "limit") if k in body}
    if params.get("source") not in (None, "job_postings", "urls"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "source must be job_postings or urls")
    try:
        return _task(platform, ctx, request, "discovery", params)
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/w/{workspace_id}/discovery/candidates/{candidate_id}/{decision}", tags=["discovery"])
def decide_candidate(candidate_id: str, decision: str, body: Dict[str, Any] = Body(default={}),
                     ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    service = platform.service("discovery")
    try:
        if decision == "approve":
            return jsonable_encoder(service.approve(ctx, candidate_id))
        if decision == "reject":
            return jsonable_encoder(service.reject(ctx, candidate_id, body.get("reason")))
    except PlatformError as error:
        raise http_error(error) from error
    raise HTTPException(status.HTTP_404_NOT_FOUND, "decision must be approve or reject")


# --- monitoring ---------------------------------------------------------------------------------


@router.post("/w/{workspace_id}/monitors/{monitor_id}/run", status_code=status.HTTP_201_CREATED, tags=["monitoring"])
def run_monitor(monitor_id: str, request: Request, ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    try:
        platform.store.get(ctx, "monitors", monitor_id)
        return _task(platform, ctx, request, "monitor", {"monitor_id": monitor_id}, entity_type="monitors",
                     entity_id=monitor_id)
    except PlatformError as error:
        raise http_error(error) from error
