"""The AI scraper: URLs + a plain-language instruction in, validated records and files out.

    service = platform.service("scraper")
    service.preview(ctx, instruction)                     # the extraction schema, shown before a run
    run = service.start(ctx, "https://a.com\\nhttps://b.com", "Get company name, website and job titles")
    run = service.start(ctx, None, instruction, file_bytes=..., filename="list.xlsx")
    service.cancel(ctx, run["id"]);  service.retry(ctx, run["id"])
    service.records(ctx, run["id"], view="jobs")          # after completion: all | companies | jobs

Module map (``cloud/intel/scraper``)::

    models.py      data types and page outcomes
    schemas.py     the field vocabulary and the JSON schemas sent to AI
    planner.py     instruction -> extraction schema (rules; AI names unknown fields)
    fetcher.py     SafeFetcher + outcome classification (CAPTCHA, WAF, login…); optional browser
    extractor.py   JSON-LD, official ATS APIs, links/headings, regex; AI fallback with evidence checks
    normalizer.py  canonical URLs, ISO dates, derived domain / remote mode
    validator.py   input URL checks, type checks, required fields, filters
    dedupe.py      job URL / company / field-fingerprint keys; source URLs kept
    exports.py     CSV / XLSX / JSON and the Companies / Jobs / All-fields views
    runner.py      the background task: progress, cancel, pause, retry, restart recovery
    service.py     this file: inputs, runs, files

The scraper never logs in, solves a CAPTCHA, gets past a WAF or rotates
proxies; such pages are reported with their outcome. AI goes only through the
provider layer (``platform.service("ai")``), so free-only mode, the $0 budget
and quota exhaustion (rules fallback) apply unchanged.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.core.http import UnsafeTargetError, check_url
from cloud.intel.scraper.exports import columns_for, views
from cloud.intel.scraper.models import TRANSIENT, ScrapeInput
from cloud.intel.scraper.planner import instruction_to_schema
from cloud.intel.scraper.runner import run_scrape_task
from cloud.intel.scraper.schemas import FIELD_TYPES
from cloud.intel.scraper.validator import check_input_url

__all__ = ["MAX_URLS", "ScraperService", "parse_inputs", "read_file_rows", "run_scrape_task"]

MAX_URLS = 5000
_URL_HEADER = re.compile(r"url|website|link|site|domain|homepage|careers", re.I)
_LOOKS_LIKE_URL = re.compile(r"^(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:[/:?#]|$)", re.I)


def read_file_rows(data: bytes, filename: str, column: Optional[str] = None) -> List[Tuple[int, str]]:
    """``(spreadsheet row, cell)`` pairs from the URL column of a CSV, TXT or XLSX upload.

    The column is the one named, else the first whose header mentions url/website/
    link/domain, else the column whose cells most often look like URLs. A file
    without a header row (a bare list of URLs) is read from row 1.
    """
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        try:
            sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
            table = [["" if v is None else str(v).strip() for v in row] for row in sheet.iter_rows(values_only=True)]
        except Exception as error:  # noqa: BLE001 - a corrupt upload is the user's input error
            raise ValidationError(f"could not read the XLSX file: {type(error).__name__}") from None
    elif name.endswith((".csv", ".txt", ".tsv")):
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.excel_tab if name.endswith(".tsv") else csv.excel
        table = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]
    else:
        raise ValidationError("upload a .csv, .txt or .xlsx file")
    if not table:
        return []
    width = max((len(r) for r in table), default=0)
    if width == 0:
        return []
    table = [r + [""] * (width - len(r)) for r in table]
    header = table[0]
    has_header = not any(_LOOKS_LIKE_URL.match(v or "") for v in header)
    index: Optional[int] = None
    if column:
        if not has_header or column not in header:
            raise ValidationError(f"column {column!r} is not in the file (columns: {', '.join(h for h in header if h)})")
        index = header.index(column)
    elif has_header:
        index = next((i for i, h in enumerate(header) if _URL_HEADER.search(h or "")), None)
    if index is None:
        body = table[1:] if has_header else table
        scores = [sum(1 for r in body if _LOOKS_LIKE_URL.match(r[i] or "")) for i in range(width)]
        if max(scores) == 0:
            raise ValidationError("no URL column found; name the column that holds the URLs")
        index = scores.index(max(scores))
    start = 2 if has_header else 1
    return [(start + offset, row[index]) for offset, row in enumerate(table[start - 1:])]


def parse_inputs(urls: Union[None, str, Sequence[Any]] = None, *, file_bytes: Optional[bytes] = None,
                 filename: Optional[str] = None, column: Optional[str] = None
                 ) -> Tuple[List[ScrapeInput], List[Dict[str, Any]], Dict[str, int]]:
    """Validate pasted URLs and/or an uploaded file.

    Returns ``(accepted, rejected, report)``. Every accepted input keeps its row
    number and import batch; rejected ones carry a reason (invalid syntax,
    unsupported protocol, private address, duplicate). Empty rows are counted.
    """
    candidates: List[Tuple[int, str, str, str]] = []   # (row, raw, batch, source)
    if urls:
        batch = "b_" + uuid.uuid4().hex[:12]
        if isinstance(urls, str):
            lines = urls.splitlines()
            if len(lines) == 1 and re.search(r"[,\s]", lines[0].strip()):   # "a.com, b.com" on one line
                lines = re.split(r"[,\s]+", lines[0].strip())
            source = "paste"
        else:
            lines, source = ["" if u is None else str(u) for u in urls], "list"
        candidates += [(i + 1, line, batch, source) for i, line in enumerate(lines)]
    if file_bytes is not None:
        batch = "b_" + uuid.uuid4().hex[:12]
        candidates += [(row, cell, batch, (filename or "upload")[:120])
                       for row, cell in read_file_rows(file_bytes, filename or "", column)]
    accepted: List[ScrapeInput] = []
    rejected: List[Dict[str, Any]] = []
    report = {"received": 0, "accepted": 0, "empty": 0, "invalid": 0, "unsafe": 0, "duplicates": 0}
    seen = set()
    for row, raw, batch, source in candidates:
        if not str(raw or "").strip():
            report["empty"] += 1
            continue
        report["received"] += 1
        url, reason = check_input_url(raw)
        if url is None:
            report["invalid"] += 1
            rejected.append({"row": row, "batch": batch, "source": source, "url": str(raw)[:300], "reason": reason})
            continue
        try:
            check_url(url, resolve=False)   # DNS is re-checked on every hop at fetch time
        except UnsafeTargetError as error:
            report["unsafe"] += 1
            rejected.append({"row": row, "batch": batch, "source": source, "url": url[:300], "reason": str(error)})
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            report["duplicates"] += 1
            rejected.append({"row": row, "batch": batch, "source": source, "url": url[:300], "reason": "duplicate"})
            continue
        seen.add(key)
        accepted.append(ScrapeInput(url[:2048], row, batch, source))
    report["accepted"] = len(accepted)
    return accepted, rejected, report


def _clean_schema(schema: Mapping[str, Any]) -> Dict[str, Any]:
    """A schema the client supplied or edited: keep only well-formed fields."""
    fields = []
    seen = set()
    for item in schema.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        name = re.sub(r"[^a-z0-9]+", "_", str(item.get("name", "")).lower()).strip("_")[:60]
        if not name or name in seen:
            continue
        seen.add(name)
        fields.append({"name": name, "type": item.get("type") if item.get("type") in FIELD_TYPES else "string",
                       "level": "job" if item.get("level") == "job" else "company",
                       "description": str(item.get("description") or name)[:300],
                       "required": bool(item.get("required")), "source": str(item.get("source") or "user")[:20]})
    if not fields:
        raise ValidationError("the schema has no fields")
    entity = "job" if schema.get("entity") == "job" or any(f["level"] == "job" for f in fields) else "company"
    names = {f["name"] for f in fields}
    filters = [dict(f) for f in schema.get("filters") or [] if isinstance(f, Mapping) and f.get("op") == "within_days"
               and isinstance(f.get("value"), int) and f.get("field") in names]
    return {"entity": entity, "fields": fields, "filters": filters, "custom": list(schema.get("custom") or [])[:20],
            "parser": str(schema.get("parser") or "user")[:40],
            "instruction": str(schema.get("instruction") or "")[:4000]}


class ScraperService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    def _ai(self, ctx: Ctx):
        return self.platform.service("ai").for_ctx(ctx, "extraction")

    # --- schema ------------------------------------------------------------------------------

    def instruction_to_schema(self, ctx: Ctx, instruction: str) -> Dict[str, Any]:
        """The extraction schema for ``instruction`` (AI is asked only about unrecognised phrases)."""
        if not (instruction or "").strip():
            raise ValidationError("describe what to extract")
        ai = self._ai(ctx)
        return instruction_to_schema(instruction, ai=ai if ai.external else None)

    preview = instruction_to_schema

    # --- runs --------------------------------------------------------------------------------

    def start(self, ctx: Ctx, urls: Union[None, str, Sequence[Any]] = None, instruction: str = "", *,
              file_bytes: Optional[bytes] = None, filename: Optional[str] = None, column: Optional[str] = None,
              schema: Optional[Mapping[str, Any]] = None, use_ai: bool = True,
              max_ai_calls: Optional[int] = None) -> Dict[str, Any]:
        ctx.require_write()
        accepted, rejected, report = parse_inputs(urls, file_bytes=file_bytes, filename=filename, column=column)
        if not accepted:
            reasons = sorted({str(r["reason"]) for r in rejected})[:3]
            raise ValidationError("no fetchable public http(s) URLs were given"
                                  + (f" ({'; '.join(reasons)})" if reasons else ""))
        if len(accepted) > MAX_URLS:
            raise ValidationError(f"at most {MAX_URLS} URLs per run (got {len(accepted)})")
        schema = _clean_schema(schema) if schema else self.instruction_to_schema(ctx, instruction)
        options: Dict[str, Any] = {"use_ai": bool(use_ai)}
        if max_ai_calls is not None:
            options["max_ai_calls"] = max(0, min(int(max_ai_calls), 500))
        run = self.platform.store.insert(ctx, "scrape_runs", {
            "instruction": (instruction or schema.get("instruction") or "(schema supplied)")[:4000],
            "schema": schema, "urls": [], "status": "queued",
            "stats": {"inputs": [i.as_dict() for i in accepted], "url_count": len(accepted),
                      "rejected": rejected[:500], "input_report": report, "options": options,
                      "progress": {"total": len(accepted), "processed": 0, "completed": 0, "failed": 0, "pages": 0,
                                   "records": 0, "stage": "Queued", "current_url": None}}})
        task = self.platform.tasks.submit(ctx, "scraper", {"run_id": run["id"]}, entity_type="scrape_runs",
                                          entity_id=run["id"])
        run = self.platform.store.update(ctx, "scrape_runs", run["id"], {"task_id": task["id"]})
        audit(self.platform.store, ctx, "scraper.start", entity_type="scrape_runs", entity_id=run["id"],
              summary=f"{len(accepted)} URLs: {(instruction or '')[:200]}")
        return run

    def get(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        return self.platform.store.get(ctx, "scrape_runs", run_id)

    def cancel(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        """Stop a run. A queued run stops now; a running one at the next URL, keeping
        (and exporting) what it has scraped so far."""
        ctx.require_write()
        run = self.get(ctx, run_id)
        if run["status"] not in ("queued", "running"):
            raise ValidationError(f"a {run['status']} run cannot be cancelled")
        task = None
        if run.get("task_id"):
            try:
                task = self.platform.tasks.cancel(ctx, run["task_id"])
            except Exception:  # noqa: BLE001 - the task already finished: just mark the run
                task = None
        audit(self.platform.store, ctx, "scraper.cancel", entity_type="scrape_runs", entity_id=run_id)
        if task is not None and task.get("status") == "running":
            return run
        return self.platform.store.update(ctx, "scrape_runs", run_id, {
            "status": "cancelled",
            "stats": {**run["stats"], "progress": {**(run["stats"].get("progress") or {}), "stage": "Cancelled"}}})

    def retry(self, ctx: Ctx, run_id: str) -> Dict[str, Any]:
        """Run again: URLs without a result, plus those that failed transiently (timeout,
        network error, rate limit). Pages that refused us (CAPTCHA, WAF, login) are not retried."""
        ctx.require_write()
        run = self.get(ctx, run_id)
        if run["status"] in ("queued", "running"):
            raise ValidationError("the run is still in progress")
        results = self.platform.store.all(ctx, "scrape_results", {"run_id": run_id})
        final = {int(r["data"].get("index", -1)) for r in results if r["data"].get("outcome") not in TRANSIENT}
        todo = len(run["stats"].get("inputs") or []) - len(final)
        if todo <= 0:
            raise ValidationError("nothing to retry: every URL has a final result")
        task = self.platform.tasks.submit(ctx, "scraper", {"run_id": run_id, "retry": True},
                                          entity_type="scrape_runs", entity_id=run_id)
        run = self.platform.store.update(ctx, "scrape_runs", run_id, {
            "status": "queued", "task_id": task["id"], "error": None,
            "stats": {**run["stats"], "progress": {**(run["stats"].get("progress") or {}), "stage": "Queued"}}})
        audit(self.platform.store, ctx, "scraper.retry", entity_type="scrape_runs", entity_id=run_id,
              summary=f"{todo} URLs")
        return run

    # --- results -----------------------------------------------------------------------------

    def records(self, ctx: Ctx, run_id: str, view: str = "all", *, limit: int = 500, offset: int = 0
                ) -> Dict[str, Any]:
        """Final (normalised, de-duplicated) rows of a finished run, by view."""
        if view not in ("all", "companies", "jobs"):
            raise ValidationError("view must be all, companies or jobs")
        run = self.get(ctx, run_id)
        info = (run["stats"].get("files") or {}).get("json")
        if not info:
            return {"view": view, "columns": [], "items": [], "total": 0, "final": False}
        with self.platform.storage.open(info["storage_key"]) as handle:
            payload = json.loads(handle.read())
        rows = payload.get(view if view != "all" else "records")
        if rows is None:
            rows = views(payload.get("records") or [], run["schema"])[view]
        columns = columns_for(run["schema"], view)
        if view == "companies" and run["schema"].get("entity") == "job":
            columns = columns + ["job_count"]
        limit = max(1, min(int(limit), 5000))
        return {"view": view, "columns": columns, "items": rows[offset: offset + limit], "total": len(rows),
                "final": True, "summary": payload.get("summary") or {}}
