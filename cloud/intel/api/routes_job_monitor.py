"""Job source monitors, the master job table, historical job imports, company review.

Reads need workspace membership; everything that changes data needs a writer.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder

from cloud.intel.api.deps import get_platform, http_error, page_response, workspace_ctx, write_ctx
from cloud.intel.core.context import Ctx, PlatformError
from cloud.intel.job_monitor.importer import MAX_IMPORT_BYTES
from cloud.intel.platform import Platform

router = APIRouter(tags=["job-monitors"])
W = "/w/{workspace_id}"
#: Free disk space an upload must leave untouched (bytes).
UPLOAD_DISK_RESERVE = 512 * 1024 * 1024


def _call(fn: Callable[[], Any]) -> Any:
    try:
        return jsonable_encoder(fn())
    except PlatformError as error:
        raise http_error(error) from error


def _svc(platform: Platform):
    return platform.service("job_monitors")


# --- monitors --------------------------------------------------------------------------------

@router.get(W + "/job-monitors")
def list_monitors(limit: int = 100, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                  platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "job_source_monitors", {}, order="name", limit=limit,
                                                 offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/job-monitors/plan")
def plan_monitor(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    """What a monitor for a URL would do; ``preview: true`` reads the first page (one request)."""
    return _call(lambda: _svc(platform).plan(ctx, str(body.get("source_url") or ""), name=body.get("name"),
                                             schedule=str(body.get("schedule") or "daily"),
                                             preview=bool(body.get("preview"))))


@router.post(W + "/job-monitors", status_code=status.HTTP_201_CREATED)
def create_monitor(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                   platform: Platform = Depends(get_platform)):
    return _call(lambda: _svc(platform).create_monitor(ctx, body))


@router.get(W + "/job-monitors/{monitor_id}")
def get_monitor(monitor_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: _svc(platform).monitor_detail(ctx, monitor_id))


@router.patch(W + "/job-monitors/{monitor_id}")
def update_monitor(monitor_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                   platform: Platform = Depends(get_platform)):
    return _call(lambda: _svc(platform).update_monitor(ctx, monitor_id, body))


@router.post(W + "/job-monitors/{monitor_id}/run", status_code=status.HTTP_201_CREATED)
def run_monitor(monitor_id: str, body: Optional[Dict[str, Any]] = Body(None), ctx: Ctx = Depends(write_ctx),
                platform: Platform = Depends(get_platform)):
    mode = str((body or {}).get("mode") or "incremental")
    since = (body or {}).get("since")
    return _call(lambda: _svc(platform).start_run(ctx, monitor_id, mode=mode, trigger="manual", since=since))


@router.post(W + "/job-monitors/{monitor_id}/lifecycle", status_code=status.HTTP_201_CREATED)
def run_lifecycle(monitor_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    """Queue the daily lifecycle evaluation (stale roles, closure candidates, signals) now."""
    def go():
        platform.store.get(ctx, "job_source_monitors", monitor_id)
        return platform.tasks.submit(ctx, "job_lifecycle", {"monitor_id": monitor_id}, max_attempts=3,
                                     entity_type="job_source_monitors", entity_id=monitor_id)
    return _call(go)


@router.get(W + "/job-monitor-runs")
def list_runs(monitor_id: Optional[str] = None, limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
              platform: Platform = Depends(get_platform)):
    filters = {"monitor_id": monitor_id} if monitor_id else {}
    try:
        return page_response(platform.store.list(ctx, "job_monitor_runs", filters, order="-created_at", limit=limit,
                                                 offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get(W + "/job-monitor-runs/{run_id}")
def get_run(run_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: platform.store.get(ctx, "job_monitor_runs", run_id))


@router.post(W + "/job-monitor-runs/{run_id}/cancel")
def cancel_run(run_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    def go():
        run = platform.store.get(ctx, "job_monitor_runs", run_id)
        if run.get("task_id"):
            platform.tasks.cancel(ctx, run["task_id"])
        if run["status"] == "queued":
            run = platform.store.update(ctx, "job_monitor_runs", run_id, {"status": "cancelled",
                                                                          "stop_reason": "cancelled before it started"})
        return run
    return _call(go)


# --- jobs ------------------------------------------------------------------------------------

_JOB_PARAMS = ("source", "source_board", "search_term", "relevance", "relevance_min", "category", "company", "title", "location", "country", "experience", "salary", "remote", "keyword",
               "status", "company_id", "import", "scraped_from", "scraped_to", "first_seen_from", "first_seen_to",
               "last_changed_from", "last_changed_to", "monitor", "run", "change", "since", "since_last_run", "q",
               "order", "limit", "offset")


@router.get(W + "/job-feed")
def job_feed(request: Request, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    """The Jobs table with the same simple filters a deep link carries (``/jobs?monitor=…&change=new``)."""
    params = {k: v for k, v in request.query_params.items() if k in _JOB_PARAMS}
    return _call(lambda: _svc(platform).search_jobs(ctx, params))


@router.post(W + "/job-feed/search")
def job_search(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
               platform: Platform = Depends(get_platform)):
    """Simple filters plus ``conditions``: ``{"all"|"any": [{"field","op","value"} | group, ...]}``."""
    params = {k: v for k, v in body.items() if k in _JOB_PARAMS or k == "conditions"}
    return _call(lambda: _svc(platform).search_jobs(ctx, params))


@router.get(W + "/job-feed/{job_id}")
def job_detail(job_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: _svc(platform).job_detail(ctx, job_id))


@router.get(W + "/companies/{company_id}/job-activity")
def company_job_activity(company_id: str, ctx: Ctx = Depends(workspace_ctx),
                         platform: Platform = Depends(get_platform)):
    return _call(lambda: _svc(platform).company_jobs(ctx, company_id))


@router.get(W + "/job-company-reviews")
def list_reviews(status_filter: str = "pending", limit: int = 100, offset: int = 0,
                 ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    filters = {"status": status_filter} if status_filter != "all" else {}
    try:
        return page_response(platform.store.list(ctx, "job_company_reviews", filters, order="-job_count",
                                                 limit=limit, offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.post(W + "/job-company-reviews/{review_id}/resolve")
def resolve_review(review_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                   platform: Platform = Depends(get_platform)):
    return _call(lambda: _svc(platform).resolve_review(ctx, review_id, action=str(body.get("action") or ""),
                                                       company_id=body.get("company_id")))


# --- historical imports ------------------------------------------------------------------------

@router.post(W + "/job-imports", status_code=status.HTTP_201_CREATED)
async def upload_import(file: UploadFile = File(...), sheet: Optional[str] = Form(None),
                        ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    """The body is streamed to a temp file in 1 MB chunks (never held in memory), then scanned.
    The copy is made next to the platform's file storage (not the system temp drive) and only
    when that disk has room for it, so a large upload can never fill the disk under the API."""
    import shutil
    import tempfile
    from pathlib import Path

    work = Path(platform.config.files_dir) / "upload-tmp"
    work.mkdir(parents=True, exist_ok=True)
    expected = file.size or 0
    free = shutil.disk_usage(work).free
    if free < max(expected, 0) * 2 + UPLOAD_DISK_RESERVE:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE,
                            "the server does not have enough free disk space for this upload right now")
    with tempfile.TemporaryDirectory(dir=work) as scratch:
        path = Path(scratch) / "upload"
        size = 0
        with path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_IMPORT_BYTES:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                        f"the file is larger than {MAX_IMPORT_BYTES // 2**20} MB")
                if size % (64 * 1024 * 1024) < len(chunk) and shutil.disk_usage(work).free < UPLOAD_DISK_RESERVE:
                    raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE,
                                        "the server ran out of free disk space during the upload")
                out.write(chunk)
        return _call(lambda: platform.service("job_imports").upload_file(ctx, file.filename or "jobs.csv", path,
                                                                         sheet=sheet))


@router.get(W + "/job-imports/{import_id}/report")
def import_report(import_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    """Summary + rejected rows of one import as CSV (only for members of its workspace)."""
    from fastapi.responses import Response

    from cloud.intel.job_monitor.importer import import_report_csv

    try:
        row = platform.store.get(ctx, "job_imports", import_id)
    except PlatformError as error:
        raise http_error(error) from error
    return Response("\ufeff" + import_report_csv(row), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="import-report-{import_id}.csv"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get(W + "/job-imports")
def list_imports(limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx),
                 platform: Platform = Depends(get_platform)):
    try:
        return page_response(platform.store.list(ctx, "job_imports", {}, order="-created_at", limit=limit,
                                                 offset=offset))
    except PlatformError as error:
        raise http_error(error) from error


@router.get(W + "/job-imports/{import_id}")
def get_import(import_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: platform.store.get(ctx, "job_imports", import_id))


@router.post(W + "/job-imports/{import_id}/validate")
def validate_import(import_id: str, body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                    platform: Platform = Depends(get_platform)):
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mapping must be an object of field -> column")
    return _call(lambda: platform.service("job_imports").validate(ctx, import_id, mapping,
                                                                   default_source=body.get("default_source")))


@router.post(W + "/job-imports/{import_id}/start")
def start_import(import_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: platform.service("job_imports").start(ctx, import_id))


# --- jobs CSV export ------------------------------------------------------------------------------

def _export_svc(platform: Platform):
    return platform.service("job_exports")


@router.get(W + "/job-exports/estimate")
def export_estimate(request: Request, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: _export_svc(platform).estimate(ctx, dict(request.query_params)))


@router.post(W + "/job-exports", status_code=status.HTTP_201_CREATED)
def create_export(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(write_ctx),
                  platform: Platform = Depends(get_platform)):
    """{scope: current|all, params: <the Jobs page filters>, page: {order, limit, offset} for current}"""
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "params must be an object of filters")
    return _call(lambda: _export_svc(platform).create(ctx, scope=str(body.get("scope") or "all"), params=params,
                                                      page=body.get("page") if isinstance(body.get("page"), dict)
                                                      else None))


@router.get(W + "/job-exports")
def list_exports(limit: int = 20, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: {"items": _export_svc(platform).history(ctx, limit=limit)})


@router.get(W + "/job-exports/{export_id}")
def get_job_export(export_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: _export_svc(platform).get(ctx, export_id))


@router.get(W + "/job-exports/{export_id}/download")
def download_job_export(export_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    from fastapi.responses import StreamingResponse

    try:
        record = _export_svc(platform).get(ctx, export_id)
    except PlatformError as error:
        raise http_error(error) from error
    if record["status"] != "completed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"export is {record['status']}")
    if not record["available"]:
        raise HTTPException(status.HTTP_410_GONE, "the export file is no longer available")
    return StreamingResponse(platform.storage.iter_bytes(record["storage_key"]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{record["filename"]}"',
                                      "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


# --- relevance keyword universe ------------------------------------------------------------------

@router.post(W + "/job-keyword-sets", status_code=status.HTTP_201_CREATED)
async def upload_keyword_set(file: UploadFile = File(...), name: Optional[str] = Form(None),
                             ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    data = await file.read(10 * 2**20 + 1)
    if len(data) > 10 * 2**20:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "the workbook is larger than 10 MB")
    return _call(lambda: _svc(platform).upload_keyword_set(ctx, file.filename or "keywords.xlsx", data, name=name))


@router.get(W + "/job-keyword-sets")
def list_keyword_sets(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    try:
        page = platform.store.list(ctx, "job_keyword_sets", {}, order="-created_at", limit=50)
    except PlatformError as error:
        raise http_error(error) from error
    return jsonable_encoder({"items": [{k: v for k, v in r.items() if k != "keywords"} for r in page.rows],
                             "total": page.total})


@router.get(W + "/job-keyword-sets/{set_id}")
def get_keyword_set(set_id: str, ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: platform.store.get(ctx, "job_keyword_sets", set_id))


@router.post(W + "/job-keyword-sets/{set_id}/activate")
def activate_keyword_set(set_id: str, ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: _svc(platform).activate_keyword_set(ctx, set_id))


@router.post(W + "/job-relevance/score")
def score_job_text(body: Dict[str, Any] = Body(...), ctx: Ctx = Depends(workspace_ctx),
                   platform: Platform = Depends(get_platform)):
    """Try the active keyword set on a title/description (nothing is stored)."""
    return _call(lambda: _svc(platform).score_text(ctx, title=str(body.get("title") or ""),
                                                   description=str(body.get("description") or ""),
                                                   search_term=body.get("search_term")))


@router.post(W + "/job-relevance/rescore")
def rescore_jobs(ctx: Ctx = Depends(write_ctx), platform: Platform = Depends(get_platform)):
    return _call(lambda: _svc(platform).rescore(ctx))


@router.get(W + "/job-sources")
def job_sources(ctx: Ctx = Depends(workspace_ctx), platform: Platform = Depends(get_platform)):
    """What a monitor can be built on: site profiles, the AI Scraper, and JobSpy boards (with
    which boards this deployment has enabled)."""
    from cloud.intel.job_monitor.jobspy_source import BOARD_LABELS, JOBSPY_BOARDS, enabled_boards
    from cloud.intel.job_monitor.profiles import PROFILES

    enabled = enabled_boards(platform)
    return {"site_profiles": [{"name": p.name, "source_name": p.source_name, "hosts": list(p.hosts)}
                              for p in PROFILES.values()],
            "jobspy": {"boards": [{"id": b, "label": BOARD_LABELS[b], "enabled": b in enabled,
                                   "note": None if b in enabled else "disabled until access is authorized"}
                                  for b in JOBSPY_BOARDS],
                       "defaults": {"boards": ["indeed"], "location": "United States", "hours_old": 24,
                                    "results_wanted": 25, "country": "USA"}}}
