"""Import batches (CSV/XLSX/JSON, up to 30 files) and exports (CSV/XLSX/JSON)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from cloud.intel.api.crud import list_filters
from cloud.intel.api.deps import get_platform, http_error, page_response, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.imports.service import MAX_FILE_BYTES, MAX_FILES
from cloud.intel.platform import Platform

router = APIRouter(tags=["imports"])
W = "/w/{workspace_id}"


def _imports(platform: Platform):
    return platform.service("imports")


@router.get(W + "/imports")
def list_batches(request: Request, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "import_batches", list_filters(request), limit=limit,
                                                 offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/imports", status_code=status.HTTP_201_CREATED)
def create_batch(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(_imports(platform).create_batch(ctx, str(body.get("name") or "Import"),
                                                                str(body.get("target") or "companies")))
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/imports/{batch_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_files(batch_id: str, files: List[UploadFile] = File(...), sheet: Optional[str] = None,
                       ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    if len(files) > MAX_FILES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"at most {MAX_FILES} files per batch")
    results = []
    for upload in files:
        data = await upload.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            results.append({"filename": upload.filename, "error": f"larger than {MAX_FILE_BYTES // 2**20} MB"})
            continue
        try:
            row = _imports(platform).add_file(ctx, batch_id, upload.filename or "upload", data, sheet=sheet)
            results.append(jsonable_encoder(row))
        except PlatformError as error:
            if getattr(error, "status", 400) == 404:
                raise http_error(error) from error
            results.append({"filename": upload.filename, "error": str(error), "status": error.status})
    return {"files": results}


@router.get(W + "/imports/{batch_id}")
def get_batch(batch_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        batch = platform.store.get(ctx, "import_batches", batch_id)
        return jsonable_encoder({**batch, "files": _imports(platform).files(ctx, batch_id)})
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/imports/{batch_id}/validate")
def validate_batch(batch_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(_imports(platform).validate(ctx, batch_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.get(W + "/imports/{batch_id}/mapping-suggestions")
def mapping_suggestions(batch_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(_imports(platform).suggest_mapping(ctx, batch_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.put(W + "/imports/{batch_id}/mapping")
def set_mapping(batch_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'body must be {"mapping": {column: target}}')
    try:
        return jsonable_encoder(_imports(platform).set_mapping(ctx, batch_id, mapping))
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/imports/{batch_id}/merge", status_code=status.HTTP_202_ACCEPTED)
def merge_batch(batch_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(_imports(platform).merge(ctx, batch_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.get(W + "/imports/{batch_id}/rows")
def batch_rows(batch_id: str, row_status: Optional[str] = None, limit: int = 50, offset: int = 0,
               ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return page_response(_imports(platform).rows(ctx, batch_id, status=row_status, limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/imports/{batch_id}/rows/{row_id}/resolve")
def resolve_row(batch_id: str, row_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(_imports(platform).resolve_row(ctx, batch_id, row_id, str(body.get("action") or ""),
                                                               body.get("company_id")))
    except PlatformError as error:
        raise http_error(error) from error


# --- exports ------------------------------------------------------------------------------


@router.get(W + "/exports", tags=["exports"])
def list_exports(limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    return page_response(platform.store.list(ctx, "exports", limit=limit, offset=offset))


@router.post(W + "/exports", status_code=status.HTTP_201_CREATED, tags=["exports"])
def create_export(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.service("exports").export_entity(
            ctx, str(body.get("entity_type") or ""), body.get("filters") or {}, str(body.get("format") or "csv"),
            async_=bool(body.get("async"))))
    except PlatformError as error:
        raise http_error(error) from error


@router.get(W + "/exports/{export_id}", tags=["exports"])
def get_export(export_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return jsonable_encoder(platform.store.get(ctx, "exports", export_id))
    except PlatformError as error:
        raise http_error(error) from error


@router.get(W + "/exports/{export_id}/download", tags=["exports"])
def download_export(export_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        record = platform.store.get(ctx, "exports", export_id)
    except PlatformError as error:
        raise http_error(error) from error
    if record["status"] != "completed" or not record["storage_key"]:
        raise HTTPException(status.HTTP_409_CONFLICT, f"export is {record['status']}")
    if not platform.storage.exists(record["storage_key"]):
        raise HTTPException(status.HTTP_410_GONE, "the export file is no longer available")
    return StreamingResponse(
        platform.storage.iter_bytes(record["storage_key"]),
        media_type=platform.service("exports").content_type(record["format"]),
        headers={"Content-Disposition": f'attachment; filename="{record["filename"]}"',
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
