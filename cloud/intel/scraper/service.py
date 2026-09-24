"""The AI scraper: URLs + an instruction in, validated records and files out.

    run = platform.service("scraper").start(ctx, urls=[...], instruction="Get company name, careers URL and ATS.")

Flow per run::

    instruction ─► schema (rules first, AI only for unknown phrases, if allowed)
    URLs (list, or a column of an uploaded CSV/XLSX) ─► static SSRF check ─► scrape_runs row ─► "scraper" task
    task, per URL (pause/cancel checked between URLs):
        SafeFetcher (robots, SSRF on every redirect) ─► deterministic extraction
        ─► official ATS API for job boards ─► optional browser (off by default)
        ─► AI for still-missing fields (if allowed) ─► scrape_results row
    then: validate ─► filters (e.g. posted within N days) ─► dedupe ─► CSV / XLSX / JSON

The platform never scrapes behind a login, solves a CAPTCHA or rotates proxies;
blocked pages are reported as ``blocked``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.core.http import SafeFetcher, UnsafeTargetError, check_url
from cloud.intel.scraper.extract import browser_renderer, scrape_url
from cloud.intel.scraper.schema import instruction_to_schema
from cloud.intel.scraper.validate import apply_filters, dedupe_records, validate_record

__all__ = ["MAX_URLS", "ScraperService", "read_urls_from_file", "run_scrape_task"]

log = logging.getLogger(__name__)

MAX_URLS = 5000


def read_urls_from_file(data: bytes, filename: str, column: Optional[str] = None) -> List[str]:
    """URLs from one column of a CSV or XLSX upload (the first URL-looking column if none is named)."""
    name = (filename or "").lower()
    rows: List[Dict[str, Any]] = []
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        values = list(sheet.iter_rows(values_only=True))
        if not values:
            return []
        header = [str(h or "").strip() for h in values[0]]
        rows = [dict(zip(header, row)) for row in values[1:]]
    elif name.endswith((".csv", ".txt")):
        text = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
    else:
        raise ValidationError("upload a .csv or .xlsx file")
    if not rows:
        return []
    columns = list(rows[0].keys())
    if column:
        if column not in columns:
            raise ValidationError(f"column {column!r} is not in the file (columns: {', '.join(columns)})")
    else:
        candidates = [c for c in columns if any(w in c.lower() for w in ("url", "website", "link", "site", "domain"))]
        if not candidates:
            raise ValidationError("name the column that holds the URLs")
        column = candidates[0]
    urls = []
    for row in rows:
        value = str(row.get(column) or "").strip()
        if value:
            urls.append(value if "://" in value else "https://" + value)
    return urls


class ScraperService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    def _ai(self, ctx: Ctx):
        return self.platform.service("ai").for_ctx(ctx, "extraction")

    def instruction_to_schema(self, ctx: Ctx, instruction: str) -> Dict[str, Any]:
        if not (instruction or "").strip():
            raise ValidationError("describe what to collect")
        ai = self._ai(ctx)
        return instruction_to_schema(instruction, ai=ai if ai.external else None)

    def start(self, ctx: Ctx, urls: Optional[Sequence[str]] = None, instruction: str = "", *,
              file_bytes: Optional[bytes] = None, filename: Optional[str] = None, column: Optional[str] = None,
              schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ctx.require_write()
        candidates = list(urls or [])
        if file_bytes is not None:
            candidates.extend(read_urls_from_file(file_bytes, filename or "", column))
        accepted, rejected, seen = [], [], set()
        for url in candidates:
            url = str(url).strip()
            if url and "://" not in url:
                url = "https://" + url
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                check_url(url, resolve=False)  # DNS is re-checked on every hop at fetch time
                accepted.append(url)
            except UnsafeTargetError as error:
                rejected.append({"url": url[:300], "reason": str(error)})
        if not accepted:
            raise ValidationError("no fetchable public http(s) URLs were given")
        if len(accepted) > MAX_URLS:
            raise ValidationError(f"at most {MAX_URLS} URLs per run (got {len(accepted)})")
        schema = schema or self.instruction_to_schema(ctx, instruction)
        run = self.platform.store.insert(ctx, "scrape_runs", {
            "instruction": instruction or schema.get("instruction") or "(schema supplied)",
            "schema": schema, "urls": [], "status": "queued",
            "stats": {"urls": accepted, "url_count": len(accepted), "rejected": rejected}})
        task = self.platform.tasks.submit(ctx, "scraper", {"run_id": run["id"]}, entity_type="scrape_runs",
                                          entity_id=run["id"])
        run = self.platform.store.update(ctx, "scrape_runs", run["id"], {"task_id": task["id"]})
        audit(self.platform.store, ctx, "scraper.start", entity_type="scrape_runs", entity_id=run["id"],
              summary=f"{len(accepted)} URLs: {instruction[:200]}")
        return run


# --- the task ---------------------------------------------------------------------------


def _fetcher(platform: Any) -> Any:
    factory = platform.config.extra.get("fetcher_factory")
    return factory() if factory else SafeFetcher()


def _write_outputs(platform: Any, ctx: Ctx, run_id: str, fields: List[str], records: List[Dict[str, Any]]
                   ) -> Dict[str, Dict[str, Any]]:
    from cloud.worker.results import neutralise_cell

    columns = fields + ["source_url"]
    files: Dict[str, Dict[str, Any]] = {}
    base = f"platform/{ctx.workspace_id}/scrape/{run_id}"
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        csv_path = root / "results.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow({k: neutralise_cell(record.get(k)) for k in columns})
        json_path = root / "results.json"
        json_path.write_text(json.dumps({"run_id": run_id, "columns": columns, "records": records},
                                        default=str, indent=1), encoding="utf-8")
        from openpyxl import Workbook

        xlsx_path = root / "results.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.title = "Results"
        sheet.append(columns)
        for record in records:
            sheet.append([neutralise_cell(None if record.get(k) is None else str(record.get(k))) for k in columns])
        book.save(xlsx_path)
        for fmt, path, content_type in (
                ("csv", csv_path, "text/csv"), ("json", json_path, "application/json"),
                ("xlsx", xlsx_path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")):
            stored = platform.storage.put_file(f"{base}/results.{fmt}", path, content_type=content_type)
            files[fmt] = {"storage_key": f"{base}/results.{fmt}", "content_type": content_type,
                          "size_bytes": getattr(stored, "size_bytes", None), "sha256": getattr(stored, "sha256", None),
                          "filename": f"scrape-{run_id}.{fmt}"}
    return files


def run_scrape_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError, TaskCancelled, TaskPaused

    store = platform.store
    run = store.find(ctx, "scrape_runs", task["params"].get("run_id", ""))
    if run is None:
        raise PermanentTaskError("scrape run not found")
    schema = run["schema"]
    urls: List[str] = list(run["stats"].get("urls") or [])
    fields = [f["name"] for f in schema["fields"]]
    store.update(ctx, "scrape_runs", run["id"], {"status": "running"})
    ai = platform.service("ai").for_ctx(ctx, "extraction")
    ai = ai if ai.external else None
    fetcher = _fetcher(platform)
    renderer = browser_renderer(bool(platform.config.extra.get("scraper_browser_enabled")))
    start = int(reporter.checkpoint.get("next", 0))
    counts = dict(run["stats"].get("counts") or {"ok": 0, "blocked": 0, "error": 0, "empty": 0})
    for index in range(start, len(urls)):
        if reporter.is_cancelled():
            store.update(ctx, "scrape_runs", run["id"], {"status": "cancelled"})
            raise TaskCancelled()
        if reporter.should_pause():
            store.update(ctx, "scrape_runs", run["id"], {"stats": {**run["stats"], "counts": counts}})
            raise TaskPaused({"next": index})
        url = urls[index]
        try:
            page = scrape_url(url, schema, fetcher=fetcher, ai=ai, renderer=renderer)
        except Exception as error:  # noqa: BLE001 - one bad page must not sink the run
            log.exception("scraping %s failed", url)
            from cloud.intel.scraper.extract import PageExtraction

            page = PageExtraction(url, url, "error", problems=[f"{type(error).__name__}: {error}"])
        counts[page.status] = counts.get(page.status, 0) + 1
        store.insert(ctx, "scrape_results", {
            "run_id": run["id"], "url": url[:2048], "final_url": (page.final_url or url)[:2048],
            "status": page.status, "method": page.method[:60], "data": {"records": page.records},
            "field_sources": page.field_sources, "problems": page.problems[:50],
            "fetched_at": page.fetched_at or datetime.now(timezone.utc)})
        reporter.progress(f"Scraped {index + 1} of {len(urls)}", done=index + 1, total=len(urls))

    # Assemble every result row (including ones from before a pause).
    records: List[Dict[str, Any]] = []
    problems = 0
    for row in store.all(ctx, "scrape_results", {"run_id": run["id"]}, order="created_at"):
        for record in row["data"].get("records", []):
            clean, issues = validate_record(record, schema["fields"])
            problems += len(issues)
            clean["source_url"] = row["final_url"] or row["url"]
            records.append(clean)
    records, filtered_out = apply_filters(records, schema.get("filters", []))
    records, duplicates = dedupe_records(records)
    files = _write_outputs(platform, ctx, run["id"], fields, records)
    stats = {**run["stats"], "counts": counts, "records": len(records), "duplicates_removed": duplicates,
             "filtered_out": filtered_out, "validation_problems": problems, "files": files}
    store.update(ctx, "scrape_runs", run["id"], {"status": "completed", "stats": stats})
    return {"run_id": run["id"], "records": len(records), "counts": counts, "duplicates_removed": duplicates,
            "filtered_out": filtered_out}
