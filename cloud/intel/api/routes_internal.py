"""Internal data batches, company enrichment, and the scraper/research → GTM bridge.

Internal data (``/internal-data/...``) adds schema comparison, ambiguity-aware
mapping review, per-file mapping, conflict review and history on top of the
import engine (``/imports``). The bridge (``/gtm-bridge/{scrape|research}/{id}``)
never writes CRM records, sends mail or spends credits; every action returns
something a person reviews next.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.deps import get_platform, http_error, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.imports.service import MAX_FILE_BYTES, MAX_FILES
from cloud.intel.platform import Platform
from cloud.intel.providers.base import PaidCallRefused, ProviderError

router = APIRouter(tags=["internal data"])
W = "/w/{workspace_id}"


def _run(fn: Callable[[], Any]) -> Any:
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error
    except PaidCallRefused as error:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(error)) from error
    except ProviderError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error


def _svc(platform: Platform):
    return platform.service("internal_data")


# --- internal data ------------------------------------------------------------------------


@router.get(W + "/internal-data/batches")
def history(limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
            platform: Platform = Depends(get_platform)):
    return _run(lambda: _svc(platform).history(ctx, limit=limit, offset=offset))


@router.post(W + "/internal-data/batches", status_code=status.HTTP_201_CREATED)
def create_batch(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                 platform: Platform = Depends(get_platform)):
    return _run(lambda: _svc(platform).create_batch(ctx, str(body.get("name") or "Internal data"),
                                                    str(body.get("target") or "companies_and_contacts")))


@router.post(W + "/internal-data/batches/{batch_id}/files", status_code=status.HTTP_201_CREATED)
async def upload(batch_id: str, files: List[UploadFile] = File(...), ctx: Ctx = Depends(write_ctx),
                 platform: Platform = Depends(get_platform)):
    if len(files) > MAX_FILES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"at most {MAX_FILES} files per batch")
    payload, too_big = [], []
    for item in files:
        data = await item.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            too_big.append({"filename": item.filename, "error": f"larger than {MAX_FILE_BYTES // 2**20} MB"})
            continue
        payload.append((item.filename or "upload", data))
    result = _run(lambda: _svc(platform).add_files(ctx, batch_id, payload))
    result["failed"] = list(result.get("failed") or []) + too_big
    return result


@router.get(W + "/internal-data/batches/{batch_id}")
def overview(batch_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: _svc(platform).overview(ctx, batch_id))


@router.post(W + "/internal-data/batches/{batch_id}/schema")
def compare_schema(batch_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    """Compare now, or ``{"background": true}`` to run it as an ``internal_data`` task."""
    if (body or {}).get("background"):
        if not ctx.can_write:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "this workspace role is read-only")
        return _run(lambda: platform.tasks.submit(ctx, "internal_data", {"batch_id": batch_id, "action": "schema"},
                                                  entity_type="import_batches", entity_id=batch_id))
    return _run(lambda: _svc(platform).compare_schema(ctx, batch_id))


@router.get(W + "/internal-data/batches/{batch_id}/mapping-review")
def mapping_review(batch_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: _svc(platform).mapping_review(ctx, batch_id))


@router.put(W + "/internal-data/batches/{batch_id}/mapping")
def apply_mapping(batch_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'body must be {"mapping": {column: field|null}}')
    return _run(lambda: _svc(platform).apply_mapping(ctx, batch_id, mapping,
                                                     file_mappings=body.get("file_mappings") or None))


@router.post(W + "/internal-data/batches/{batch_id}/merge", status_code=status.HTTP_202_ACCEPTED)
def merge(batch_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: _svc(platform).merge(ctx, batch_id))


@router.get(W + "/internal-data/batches/{batch_id}/conflicts")
def conflicts(batch_id: str, limit: int = 100, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
              platform: Platform = Depends(get_platform)):
    return _run(lambda: _svc(platform).conflicts(ctx, batch_id, limit=limit, offset=offset))


@router.post(W + "/internal-data/batches/{batch_id}/rows/{row_id}/resolve")
def resolve(batch_id: str, row_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
            platform: Platform = Depends(get_platform)):
    decisions = body.get("decisions")
    if not isinstance(decisions, dict) or not decisions:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'body must be {"decisions": {field: decision}}')
    return _run(lambda: _svc(platform).resolve_conflicts(ctx, batch_id, row_id, decisions))


# --- enrichment ------------------------------------------------------------------------------


@router.get(W + "/enrichment/sources")
def enrichment_sources(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: {"items": platform.service("enrichment").sources(ctx)})


@router.put(W + "/enrichment/priority")
def enrichment_priority(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                        platform: Platform = Depends(get_platform)):
    order = body.get("order")
    if not isinstance(order, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'body must be {"order": [source, ...]}')
    return _run(lambda: {"priority": platform.service("enrichment").set_priority(ctx, order)})


@router.post(W + "/companies/{company_id}/enrich")
def enrich_company(company_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                   platform: Platform = Depends(get_platform)):
    body = body or {}
    return _run(lambda: platform.service("enrichment").enrich_company(
        ctx, company_id, fields=body.get("fields"), allow_paid=bool(body.get("allow_paid")),
        providers=body.get("providers")))


# --- scraper / research → GTM ------------------------------------------------------------------

_ACTIONS = {"add-to-list", "validate-emails", "create-campaign", "create-crm-proposal", "research-these", "pipeline"}


@router.get(W + "/gtm-bridge/{source_type}/{source_id}")
def bridge_prepare(source_type: str, source_id: str, ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("gtm_bridge").prepare(ctx, source_type, source_id))


@router.post(W + "/gtm-bridge/{source_type}/{source_id}/{action}")
def bridge_action(source_type: str, source_id: str, action: str, body: Optional[Dict[str, Any]] = Body(None),
                  ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    if action not in _ACTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown bridge action")
    body = body or {}
    bridge = platform.service("gtm_bridge")

    def go():
        if action == "add-to-list":
            return bridge.add_to_list(ctx, source_type, source_id, entity_type=body.get("entity_type") or "companies",
                                      list_id=body.get("list_id"), list_name=body.get("list_name"))
        if action == "validate-emails":
            return bridge.validate_emails(ctx, source_type, source_id, name=body.get("name"),
                                          start=bool(body.get("start")))
        if action == "create-campaign":
            return bridge.create_campaign(ctx, source_type, source_id, name=body.get("name"),
                                          list_id=body.get("list_id"))
        if action == "create-crm-proposal":
            return bridge.create_crm_proposal(ctx, source_type, source_id,
                                              actions=body.get("actions") or ("company", "contact"))
        if action == "research-these":
            return bridge.research_these(ctx, source_type, source_id, question=body.get("question"))
        return bridge.pipeline(ctx, source_type, source_id, name=body.get("name"),
                               steps=body.get("steps") or ("add_to_list", "validate_emails", "create_campaign"))

    return _run(go)
