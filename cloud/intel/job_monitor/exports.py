"""Jobs CSV export: exactly what the Jobs page filters select, at any size.

* ``current`` — the rows the Jobs page shows right now (same filters, order, page).
* ``all`` — every job matching the filters, read in id-ordered chunks (keyset paging),
  written row by row to a temp file and uploaded to object storage: memory stays bounded
  whatever the row count. Up to :data:`SYNC_ROWS` rows are written in the request; more
  run as a ``job_export`` worker task with progress, retried from scratch on failure (the
  file is rewritten, never appended twice).

The same user asking for the same scope + filters while an export is still running gets
that export back instead of a second one (``dedupe_key``). A file is downloadable only by
the person who created it (or a workspace admin), and only inside its workspace. Cells a
spreadsheet would treat as a formula (``= + - @``, tab, CR) are neutralised.
"""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ForbiddenError, NotFoundError, ValidationError, utcnow
from cloud.intel.exports.service import neutralise_cell
from cloud.intel.job_monitor.service import STATUS_LABELS

__all__ = ["JobExportService", "EXPORT_COLUMNS", "run_job_export_task", "SYNC_ROWS", "CHUNK"]

#: (CSV header, job_postings column) in export order. Nothing secret lives on job rows; the
#: list is explicit so a future internal column is never exported by accident.
EXPORT_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("Job URL", "job_url"), ("Job Title", "title"), ("Company Name", "company_name"), ("Location", "location"),
    ("Experience Level", "experience_level"), ("Salary Budget", "salary_budget"),
    ("Keyword 1", "keyword_1"), ("Keyword 2", "keyword_2"), ("Keyword 3", "keyword_3"),
    ("Keyword 4", "keyword_4"), ("Keyword 5", "keyword_5"), ("Remote", "remote"), ("Source", "source"),
    ("Source Board", "source_board"), ("Search Term", "search_term"), ("Scraped Date", "scraped_date"),
    ("First Seen", "first_seen_at"), ("Last Seen", "last_seen_at"), ("Last Changed", "last_changed_at"),
    ("Stale Date", "stale_at"), ("Closed Date", "closed_at"), ("Status", "status"),
    ("Relevance", "relevance_class"), ("Relevance Score", "relevance_score"), ("Relevance Reason", "relevance_reason"),
    ("Matched Keywords", "matched_keywords"), ("Matched Categories", "matched_categories"),
    ("Company ID", "company_id"), ("Monitor ID", "source_monitor_id"),
)
SYNC_ROWS = 2_000
CHUNK = 2_000
CURRENT_MAX = 500
SCOPES = ("current", "all")
#: Feed parameters that select or order jobs (anything else in the query string is ignored).
FILTER_PARAMS = ("q", "source", "company", "title", "location", "country", "experience", "salary", "remote",
                 "keyword", "status", "relevance", "relevance_min", "category", "source_board", "search_term",
                 "scraped_from", "scraped_to", "first_seen_from", "first_seen_to", "last_changed_from",
                 "last_changed_to", "change", "monitor", "run", "import", "since_last_run", "since", "company_id",
                 "conditions")


def _cell(column: str, value: Any) -> Any:
    if value is None:
        return ""
    if column == "status":
        return STATUS_LABELS.get(value, value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    if isinstance(value, float):
        return f"{value:g}"
    return value


def csv_row(job: Mapping[str, Any]) -> List[Any]:
    return [neutralise_cell(_cell(column, job.get(column))) for _, column in EXPORT_COLUMNS]


def clean_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: params[k] for k in FILTER_PARAMS if params.get(k) not in (None, "", [])}


class JobExportService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    @property
    def jobs(self):
        return self.platform.service("job_monitors")

    # --- what would be exported ------------------------------------------------------

    def estimate(self, ctx: Ctx, params: Mapping[str, Any]) -> Dict[str, Any]:
        filters = self.jobs.query_filters(ctx, clean_params(params))
        return {"count": self.store.count(ctx, "job_postings", filters), "sync_limit": SYNC_ROWS}

    def _dedupe_key(self, ctx: Ctx, scope: str, params: Mapping[str, Any], page: Mapping[str, Any]) -> str:
        payload = json.dumps([ctx.user_id, scope, params, page], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # --- create ----------------------------------------------------------------------

    def create(self, ctx: Ctx, *, scope: str, params: Mapping[str, Any],
               page: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        ctx.require_write()
        if scope not in SCOPES:
            raise ValidationError("scope must be current or all")
        clean = clean_params(params)
        filters = self.jobs.query_filters(ctx, clean)          # validates the filters before anything is stored
        page = dict(page or {})
        if scope == "current":
            order = str(page.get("order") or clean.get("order") or "-first_seen_at")
            limit = max(1, min(int(page.get("limit") or 50), CURRENT_MAX))
            offset = max(0, int(page.get("offset") or 0))
            page = {"order": order, "limit": limit, "offset": offset}
            total = min(max(0, self.store.count(ctx, "job_postings", filters) - offset), limit)
        else:
            page = {}
            total = self.store.count(ctx, "job_postings", filters)
        key = self._dedupe_key(ctx, scope, clean, page)
        active = self.store.first(ctx, "exports", {"dedupe_key": key, "status": ["queued", "running"]})
        if active is not None:
            return {**active, "deduplicated": True}
        record = self.store.insert(ctx, "exports", {
            "entity_type": "job_postings", "format": "csv", "status": "queued", "scope": scope,
            "filters": {"params": clean, "page": page}, "total_rows": total, "dedupe_key": key})
        audit(self.store, ctx, "job_export.created", entity_type="exports", entity_id=record["id"],
              summary=f"{scope}: {total:,} jobs", changes={"filters": clean})
        if scope == "current" or total <= SYNC_ROWS:
            return self.run(ctx, record["id"])
        task = self.platform.tasks.submit(ctx, "job_export", {"export_id": record["id"]}, max_attempts=3,
                                          idempotency_key=f"job_export:{record['id']}", entity_type="exports",
                                          entity_id=record["id"])
        return self.store.update(ctx, "exports", record["id"], {"task_id": task["id"]})

    # --- the work ----------------------------------------------------------------------

    def _rows(self, ctx: Ctx, record: Mapping[str, Any]) -> Iterator[Dict[str, Any]]:
        spec = record.get("filters") or {}
        filters = self.jobs.query_filters(ctx, spec.get("params") or {})
        if record.get("scope") == "current":
            page = spec.get("page") or {}
            yield from self.store.list(ctx, "job_postings", filters, order=page.get("order") or "-first_seen_at",
                                       limit=int(page.get("limit") or 50), offset=int(page.get("offset") or 0)).rows
            return
        last_id = ""
        while True:
            chunk_filters = dict(filters)
            if last_id:
                chunk_filters = {"all_of": [filters, {"id__gt": last_id}]} if filters else {"id__gt": last_id}
            rows = self.store.rows(ctx, "job_postings", chunk_filters, order="id", limit=CHUNK)
            if not rows:
                return
            last_id = rows[-1]["id"]
            yield from rows

    def run(self, ctx: Ctx, export_id: str, *, progress: Optional[Callable[[int, int], None]] = None,
            stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        """Write (or rewrite, on a retry) the export file and mark it completed."""
        record = self.store.get(ctx, "exports", export_id)
        if record["status"] == "completed":
            return record
        record = self.store.update(ctx, "exports", export_id, {"status": "running", "progress_rows": 0,
                                                                "error": None})
        total = int(record.get("total_rows") or 0)
        filename = f"jobs-{record['scope']}-{utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
        key = f"exports/{ctx.workspace_id}/{export_id}.csv"
        count = 0
        try:
            with tempfile.TemporaryDirectory() as scratch:
                path = Path(scratch) / filename
                with path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
                    writer.writerow([header for header, _ in EXPORT_COLUMNS])
                    for job in self._rows(ctx, record):
                        writer.writerow(csv_row(job))
                        count += 1
                        if count % CHUNK == 0:
                            self.store.update(ctx, "exports", export_id, {"progress_rows": count})
                            if progress is not None:
                                progress(count, max(total, count))
                            if stop is not None and stop():
                                raise InterruptedError("export cancelled")
                stored = self.platform.storage.put_file(key, path, content_type="text/csv; charset=utf-8")
        except Exception as error:
            self.store.update(ctx, "exports", export_id, {"status": "failed", "error": str(error)[:2000],
                                                          "progress_rows": count})
            raise
        done = self.store.update(ctx, "exports", export_id, {
            "status": "completed", "row_count": count, "progress_rows": count, "total_rows": count,
            "storage_key": key, "filename": filename, "sha256": stored.sha256, "size_bytes": stored.size_bytes,
            "finished_at": utcnow()})
        audit(self.store, ctx, "job_export.completed", entity_type="exports", entity_id=export_id,
              summary=f"{count:,} jobs ({record['scope']})")
        return done

    # --- reading back ----------------------------------------------------------------

    def get(self, ctx: Ctx, export_id: str) -> Dict[str, Any]:
        record = self.store.get(ctx, "exports", export_id)
        if record.get("entity_type") != "job_postings":
            raise NotFoundError("job export not found")
        if not (ctx.system or ctx.can_admin or record.get("created_by") == ctx.user_id):
            raise ForbiddenError("this export belongs to another user")
        return {**record, "available": bool(record.get("storage_key")) and record["status"] == "completed"
                and self.platform.storage.exists(record["storage_key"])}

    def history(self, ctx: Ctx, *, limit: int = 20) -> List[Dict[str, Any]]:
        filters: Dict[str, Any] = {"entity_type": "job_postings"}
        if not (ctx.system or ctx.can_admin):
            filters["created_by"] = ctx.user_id
        return self.store.list(ctx, "exports", filters, order="-created_at", limit=max(1, min(limit, 100))).rows


def run_job_export_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError

    export_id = str(task["params"].get("export_id") or "")
    if platform.store.find(ctx, "exports", export_id) is None:
        raise PermanentTaskError("export not found")
    service: JobExportService = platform.service("job_exports")

    def progress(done: int, total: int) -> None:
        reporter.progress(f"Exporting jobs: {done:,} of {total:,}", export_id=export_id, done=done, total=total)

    try:
        record = service.run(ctx, export_id, progress=progress, stop=reporter.is_cancelled)
    except ValidationError as error:
        raise PermanentTaskError(str(error)) from error
    return {"export_id": export_id, "rows": record["row_count"]}
