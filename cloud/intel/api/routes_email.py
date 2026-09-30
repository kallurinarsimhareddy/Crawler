"""Email validation jobs: upload a file (or pick contacts / a list), choose the email
column, run, watch progress, filter results, export, and — only when the user asks —
add the results to a list, a draft campaign or a sequence (pending approval).

The quick ``POST /email/validate`` endpoint for up to 25 addresses stays in
:mod:`cloud.intel.api.routes_sources`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response

from cloud.intel.api.crud import list_filters
from cloud.intel.api.deps import get_platform, http_error, page_response, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.email.jobs import MAX_BYTES
from cloud.intel.platform import Platform

router = APIRouter(prefix="/w/{workspace_id}", tags=["email validation"])


def _run(fn):
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error


def _statuses(value: Any) -> Optional[list]:
    if value is None:
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "statuses must be a list")


@router.get("/email/provider")
def provider_status(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("email_jobs").provider_status(ctx))


@router.post("/email/provider/test")
def provider_test(ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("email_jobs").test_provider(ctx))


@router.get("/email/jobs")
def list_jobs(request: Request, limit: int = 25, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
              platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "email_validation_jobs", list_filters(request),
                                                 limit=min(limit, 200), offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.post("/email/jobs/upload", status_code=status.HTTP_201_CREATED)
async def upload(files: List[UploadFile] = File(...), name: Optional[str] = Form(None),
                 ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    """One CSV or XLSX file in the multipart field ``files`` (the dashboard's upload helper)."""
    if len(files) != 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "upload exactly one file")
    file = files[0]
    data = await file.read(MAX_BYTES + 1)
    return _run(lambda: platform.service("email_jobs").create_upload(ctx, file.filename or "upload", data,
                                                                     name=name))


@router.post("/email/jobs", status_code=status.HTTP_201_CREATED)
def create_job(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
               platform: Platform = Depends(get_platform)):
    """``{"source": "contacts"|"list"|"rows", "name", "contact_ids"|"list_id"|"rows"+"email_field", "start"}``."""
    service = platform.service("email_jobs")
    source = body.get("source")
    name = str(body.get("name") or "").strip() or "Email validation"
    start = bool(body.get("start"))
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else None

    def go():
        if source in ("contacts", "list"):
            return service.create_from_contacts(ctx, name=name, contact_ids=body.get("contact_ids"),
                                                list_id=body.get("list_id"), start=start, settings=settings)
        if source == "rows":
            rows = body.get("rows")
            if not isinstance(rows, list):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "rows must be a list of objects")
            return service.create_from_rows(ctx, name=name, rows=rows, email_field=str(body.get("email_field") or
                                                                                        "email"),
                                            source_type=str(body.get("source_type") or "manual"),
                                            source_id=body.get("source_id"), start=start, settings=settings)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "source must be contacts, list or rows")

    return _run(go)


@router.get("/email/jobs/{job_id}")
def get_job(job_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    def go():
        service = platform.service("email_jobs")
        job = service.get(ctx, job_id)
        job["counts"] = {**(job.get("counts") or {}), **service.counts(ctx, job_id)}
        return job

    return _run(go)


@router.delete("/email/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    _run(lambda: platform.service("email_jobs").delete(ctx, job_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/email/jobs/{job_id}/column")
def set_column(job_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
               platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("email_jobs").set_email_column(ctx, job_id, str(body.get("column") or "")))


@router.post("/email/jobs/{job_id}/start")
def start_job(job_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
              platform: Platform = Depends(get_platform)):
    return _run(lambda: platform.service("email_jobs").start(ctx, job_id, settings=(body or {}).get("settings")))


@router.post("/email/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
               platform: Platform = Depends(get_platform)):
    service = platform.service("email_jobs")
    body = body or {}
    actions = {
        "pause": lambda: service.pause(ctx, job_id),
        "resume": lambda: service.resume(ctx, job_id),
        "cancel": lambda: service.cancel(ctx, job_id),
        "add-to-list": lambda: service.add_to_list(
            ctx, job_id, list_id=body.get("list_id"), list_name=body.get("list_name"),
            statuses=_statuses(body.get("statuses")), create_missing_contacts=bool(body.get("create_missing_contacts"))),
        "campaign": lambda: service.create_campaign(
            ctx, job_id, name=str(body.get("name") or ""), statuses=_statuses(body.get("statuses")),
            create_missing_contacts=bool(body.get("create_missing_contacts")), list_name=body.get("list_name")),
        "enroll": lambda: service.enroll(ctx, job_id, sequence_id=str(body.get("sequence_id") or ""),
                                         statuses=_statuses(body.get("statuses")), campaign_id=body.get("campaign_id")),
    }
    if action not in actions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown action")
    return _run(actions[action])


@router.get("/email/jobs/{job_id}/items")
def job_items(job_id: str, request: Request, limit: int = 50, offset: int = 0, order: Optional[str] = None,
              ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.service("email_jobs").items(ctx, job_id, list_filters(request), limit=limit,
                                                                  offset=offset, order=order))
    except PlatformError as error:
        raise http_error(error) from error


@router.get("/email/jobs/{job_id}/export")
def export_job(job_id: str, format: str = "csv", status_filter: Optional[str] = None,
               ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        filename, content, media = platform.service("email_jobs").export(ctx, job_id, format,
                                                                         _statuses(status_filter))
    except PlatformError as error:
        raise http_error(error) from error
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
