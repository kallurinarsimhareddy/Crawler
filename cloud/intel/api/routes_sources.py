"""Sources, provider connections, credits, email validation and contact intelligence.

Secrets are write-only: connection endpoints accept them and never return them
(a 4-character hint at most). Setting credentials and adjusting credits needs a
workspace admin. Anything that could spend paid credits needs ``allow_paid``
in the request body, and still goes through the credit ledger.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.crud import list_filters
from cloud.intel.api.deps import get_platform, http_error, idempotent, page_response, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.platform import Platform
from cloud.intel.providers.base import PaidCallRefused, ProviderError
from cloud.intel.sources.base import SourceError, SourceUnavailable

router = APIRouter(prefix="/w/{workspace_id}", tags=["sources"])


def _fail(error: Exception) -> HTTPException:
    if isinstance(error, PlatformError):
        return http_error(error)
    if isinstance(error, PaidCallRefused):
        return HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(error))
    if isinstance(error, SourceUnavailable):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    if isinstance(error, (SourceError, ProviderError)):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(error))
    raise error


_HANDLED = (PlatformError, PaidCallRefused, SourceError, ProviderError)

# --- sources ---------------------------------------------------------------------------


@router.get("/sources")
def list_sources(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return {"items": jsonable_encoder(platform.service("sources").list_sources(ctx))}


@router.post("/sources/{source}/search", status_code=status.HTTP_202_ACCEPTED)
def search_source(source: str, request: Request, body: Dict[str, Any] = Body(default={}),
                  ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    """Runs as a ``source_search`` task; ``careercrawler`` becomes a ``crawl`` task."""
    params = {"source": source, "query": body.get("query") or {}, "allow_paid": bool(body.get("allow_paid")),
              "ingest": body.get("ingest", True)}

    def produce():
        if source == "careercrawler":
            return platform.service("sources").search(ctx, source, params["query"])
        task = platform.tasks.submit(ctx, "source_search", params)
        return {"task_id": task["id"], "status": task["status"]}

    try:
        return jsonable_encoder(idempotent(platform, ctx, request, params, produce))
    except _HANDLED as error:
        raise _fail(error) from error


# --- provider connections ----------------------------------------------------------------


@router.get("/providers")
def list_providers(kind: Optional[str] = None, ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    return {"items": jsonable_encoder(platform.service("providers").list_connections(ctx, kind=kind))}


@router.post("/providers/{provider}/credentials")
def set_credentials(provider: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    try:
        row = platform.service("providers").set_credentials(
            ctx, provider, body.get("secrets") or {}, settings=body.get("settings"), label=body.get("label"))
        return jsonable_encoder(row)
    except _HANDLED as error:
        raise _fail(error) from error


@router.delete("/providers/{provider}/credentials", status_code=status.HTTP_204_NO_CONTENT)
def clear_credentials(provider: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    try:
        platform.service("providers").clear_credentials(ctx, provider)
    except _HANDLED as error:
        raise _fail(error) from error


@router.patch("/providers/{provider}/settings")
def update_provider_settings(provider: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                             platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("providers").update_settings(ctx, provider, body.get("settings") or {}))
    except _HANDLED as error:
        raise _fail(error) from error


@router.post("/providers/{provider}/verify")
def verify_provider(provider: str, body: Dict[str, Any] = Body(default={}), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("providers").verify(ctx, provider,
                                                                     allow_paid=bool(body.get("allow_paid"))))
    except _HANDLED as error:
        raise _fail(error) from error


@router.post("/providers/zoominfo/company-search")
def zoominfo_company_search(body: Dict[str, Any] = Body(default={}), ctx: Ctx = Depends(write_ctx),
                            platform: Platform = Depends(get_platform)):
    """Credit-free ZoomInfo company search (CompanySearch attributes, e.g. companyName). Returns records only;
    nothing is saved — send them to Discovery to review and import."""
    registry = platform.service("providers")
    if not registry.configured(ctx, "zoominfo"):
        raise HTTPException(status.HTTP_409_CONFLICT, "ZoomInfo is not connected for this workspace")
    filters = body.get("filters") or {}
    if not isinstance(filters, dict) or not any(v not in (None, "", []) for v in filters.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "give at least one filter, e.g. companyName")
    try:
        connector = registry.enrichment(ctx, "zoominfo")
        rows = connector.search_companies(filters, limit=max(1, min(int(body.get("limit") or 25), 100)))
    except _HANDLED as error:
        raise _fail(error) from error
    return {"items": rows, "meta": connector.last_search, "credits_used": connector.credit_consuming_calls}


# --- credits ----------------------------------------------------------------------------------


@router.get("/credits")
def credit_balances(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    ledger = platform.service("credits")
    return {"items": jsonable_encoder(ledger.balances(ctx)), "usage": jsonable_encoder(ledger.usage(ctx))}


@router.get("/credits/ledger")
def credit_ledger(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                  platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "credit_ledger", list_filters(request), limit=limit,
                                                 offset=offset))
    except _HANDLED as error:
        raise _fail(error) from error


@router.post("/credits/{provider}/sync")
def credit_sync(provider: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    """Admin: record a balance read from the provider's own dashboard or API."""
    try:
        ctx.require_admin()
        return jsonable_encoder(platform.service("credits").sync(
            ctx, provider, body.get("total"), remaining=body.get("remaining"),
            source=str(body.get("source") or "manual entry")))
    except _HANDLED as error:
        raise _fail(error) from error


@router.post("/credits/{provider}/grant")
def credit_grant(provider: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("credits").grant(ctx, provider, float(body.get("amount") or 0),
                                                                  reason=str(body.get("reason") or "manual grant")))
    except _HANDLED as error:
        raise _fail(error) from error


@router.post("/credits/{provider}/limit")
def credit_limit(provider: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        limit = body.get("hard_limit")
        return jsonable_encoder(platform.service("credits").set_hard_limit(
            ctx, provider, None if limit is None else float(limit)))
    except _HANDLED as error:
        raise _fail(error) from error


# --- email validation ---------------------------------------------------------------------------


@router.post("/email/validate")
def validate_emails(request: Request, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    """Up to 25 addresses are validated synchronously; larger requests become a task."""
    emails = [str(e) for e in (body.get("emails") or []) if e]
    allow_paid = bool(body.get("allow_paid"))
    targets = body.get("contact_ids") or body.get("list_id")
    try:
        if emails and len(emails) <= 25 and not targets:
            results = platform.service("email").validate(ctx, emails, allow_paid=allow_paid,
                                                         max_age_days=int(body.get("max_age_days") or 30))
            return {"items": jsonable_encoder(results)}
        if not emails and not targets:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "give emails, contact_ids or list_id")
        params = {"emails": emails, "contact_ids": body.get("contact_ids") or [], "list_id": body.get("list_id"),
                  "allow_paid": allow_paid, "max_age_days": body.get("max_age_days") or 30}
        task = idempotent(platform, ctx, request, params, lambda: jsonable_encoder(
            platform.tasks.submit(ctx, "validation", params)))
        return {"task_id": task["id"], "status": task["status"]}
    except _HANDLED as error:
        raise _fail(error) from error


@router.get("/email/validations")
def list_validations(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                     platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "email_validations", list_filters(request), limit=limit,
                                                 offset=offset))
    except _HANDLED as error:
        raise _fail(error) from error


# --- contact intelligence ------------------------------------------------------------------------


@router.get("/companies/{company_id}/contact-gaps")
def contact_gaps(company_id: str, functions: Optional[str] = None, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    wanted = [f for f in (functions or "hr,recruiting,it,cio,cto,vp,director,c_level").split(",") if f]
    try:
        return jsonable_encoder(platform.service("contacts").gap_analysis(ctx, company_id, wanted))
    except _HANDLED as error:
        raise _fail(error) from error


@router.post("/contacts/find", status_code=status.HTTP_202_ACCEPTED)
def find_contacts(request: Request, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    params = {"company_ids": list(body.get("company_ids") or []), "list_id": body.get("list_id"),
              "functions": body.get("functions") or ["hr", "it", "executive"],
              "allow_paid": bool(body.get("allow_paid")), "providers": body.get("providers")}
    if not params["company_ids"] and not params["list_id"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "give company_ids or list_id")
    try:
        task = idempotent(platform, ctx, request, params, lambda: jsonable_encoder(
            platform.tasks.submit(ctx, "enrichment", params)))
        return {"task_id": task["id"], "status": task["status"]}
    except _HANDLED as error:
        raise _fail(error) from error


@router.get("/enrichment/plan")
def enrichment_plan(company_ids: str, needs: Optional[str] = None, functions: Optional[str] = None,
                    ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    from cloud.intel.providers.routing import plan_enrichment

    try:
        return jsonable_encoder(plan_enrichment(platform, ctx, {
            "company_ids": [c for c in company_ids.split(",") if c],
            "needs": [n for n in (needs or "").split(",") if n] or None,
            "functions": [f for f in (functions or "").split(",") if f] or None}))
    except _HANDLED as error:
        raise _fail(error) from error
