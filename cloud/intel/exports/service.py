"""Exports: any workspace entity (or a list's members) as CSV, XLSX or JSON.

Every export carries its provenance: each row gets the source kinds and names
that contributed to the record (from ``source_records``), first/last seen where
the entity has them, the workspace id and the export time. Cells that a
spreadsheet would read as a formula (``= + - @``) are neutralised, exactly as
CareerCloud's crawl results are (:func:`cloud.worker.results.neutralise_cell`).

Small exports are written synchronously; large ones (or ``async_=True``) run as
an ``export`` task. Files go to the platform's object storage and are streamed
back by export id only.
"""

from __future__ import annotations

import csv
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError, utcnow
from cloud.intel.store.spec import COMMON_COLUMNS, get_spec

__all__ = ["ExportService", "EXPORTABLE", "neutralise_cell", "run_export_task"]

EXPORTABLE = ("companies", "contacts", "job_postings", "opportunities", "hiring_signals", "research_results",
              "scrape_results", "company_technologies", "discovery_candidates", "list")
SYNC_LIMIT = 5_000
MAX_ROWS = 250_000
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_PROVENANCED = ("companies", "contacts")


def neutralise_cell(value: Any) -> Any:
    """Stop a string being read as a spreadsheet formula (same rule as crawl results)."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _flat(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return "; ".join(str(_flat(v)) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_value(v) for v in value]
    return value


class ExportService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    @property
    def store(self):
        return self.platform.store

    # --- gathering ------------------------------------------------------------------

    def _rows_for(self, ctx: Ctx, entity_type: str, filters: Mapping[str, Any]) -> List[Dict[str, Any]]:
        if entity_type == "list":
            list_id = filters.get("list_id")
            if not list_id:
                raise ValidationError("a list export needs filters.list_id")
            target = self.store.get(ctx, "lists", list_id)
            rows = []
            for member in self.store.all(ctx, "list_members", {"list_id": list_id}, cap=MAX_ROWS):
                record = self.store.find(ctx, target["entity_type"], member["entity_id"])
                if record is not None:
                    rows.append({**record, "_list": target["name"], "_added_reason": member["added_reason"]})
            return rows
        if entity_type not in EXPORTABLE:
            raise ValidationError(f"cannot export {entity_type}; choose one of {', '.join(EXPORTABLE)}")
        return self.store.all(ctx, entity_type, dict(filters), cap=MAX_ROWS)

    def _provenance(self, ctx: Ctx, entity_type: str, ids: Sequence[str]) -> Dict[str, Dict[str, List[str]]]:
        out: Dict[str, Dict[str, List[str]]] = {}
        for start in range(0, len(ids), 200):
            chunk = list(ids[start:start + 200])
            for record in self.store.all(ctx, "source_records", {"entity_type": entity_type, "entity_id__in": chunk}):
                entry = out.setdefault(record["entity_id"], {"kinds": [], "names": []})
                if record["source_kind"] not in entry["kinds"]:
                    entry["kinds"].append(record["source_kind"])
                if record["source_name"] not in entry["names"]:
                    entry["names"].append(record["source_name"])
        return out

    def _decorate(self, ctx: Ctx, entity_type: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        exported_at = utcnow().isoformat()
        base_type = entity_type
        if entity_type == "list" and rows:
            base_type = "companies" if rows[0]["id"].startswith("co_") else (
                "contacts" if rows[0]["id"].startswith("ct_") else "")
        prov = self._provenance(ctx, base_type, [r["id"] for r in rows]) if base_type in _PROVENANCED else {}
        out = []
        for row in rows:
            decorated = dict(row)
            if base_type in _PROVENANCED:
                entry = prov.get(row["id"], {"kinds": [], "names": []})
                decorated["_source_kinds"] = entry["kinds"]
                decorated["_source_names"] = entry["names"]
            decorated["_workspace_id"] = ctx.workspace_id
            decorated["_exported_at"] = exported_at
            out.append(decorated)
        return out

    @staticmethod
    def _columns(entity_type: str, rows: List[Dict[str, Any]]) -> List[str]:
        if entity_type in EXPORTABLE and entity_type != "list":
            spec = get_spec(entity_type)
            columns = ["id"] + list(spec.columns) + [c for c in COMMON_COLUMNS if c not in ("id",)]
        else:
            columns = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        return columns

    # --- writing ------------------------------------------------------------------------

    def _write(self, path: Path, fmt: str, columns: List[str], rows: Iterable[Dict[str, Any]]) -> int:
        count = 0
        if fmt == "csv":
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                for row in rows:
                    writer.writerow([neutralise_cell(_flat(row.get(c))) for c in columns])
                    count += 1
        elif fmt == "xlsx":
            from openpyxl import Workbook

            workbook = Workbook(write_only=True)
            sheet = workbook.create_sheet("export")
            sheet.append(columns)
            for row in rows:
                sheet.append([neutralise_cell(_flat(row.get(c))) for c in columns])
                count += 1
            workbook.save(path)
        elif fmt == "json":
            items = [{c: _json_value(row.get(c)) for c in columns} for row in rows]
            count = len(items)
            path.write_text(json.dumps({"columns": columns, "rows": items}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        else:
            raise ValidationError("format must be csv, xlsx or json")
        return count

    def export_rows(self, ctx: Ctx, entity_type: str, rows: List[Dict[str, Any]], fmt: str, filename_stem: str,
                    *, filters: Optional[Mapping[str, Any]] = None, export_row: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
        """Write ``rows`` to storage and record the export (see CONTRACTS.md)."""
        if fmt not in _CONTENT_TYPES:
            raise ValidationError("format must be csv, xlsx or json")
        decorated = self._decorate(ctx, entity_type, rows)
        columns = self._columns(entity_type, decorated)
        record = export_row or self.store.insert(ctx, "exports", {
            "entity_type": entity_type[:40], "format": fmt, "filters": dict(filters or {}), "status": "queued"})
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in filename_stem.lower())[:60] or "export"
        filename = f"{stem}.{fmt}"
        key = f"exports/{ctx.workspace_id}/{record['id']}.{fmt}"
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / filename
            count = self._write(path, fmt, columns, decorated)
            stored = self.platform.storage.put_file(key, path, content_type=_CONTENT_TYPES[fmt])
        updated = self.store.update(ctx, "exports", record["id"], {
            "status": "completed", "row_count": count, "storage_key": key, "filename": filename,
            "sha256": stored.sha256, "size_bytes": stored.size_bytes})
        audit(self.store, ctx, "exports.create", entity_type="exports", entity_id=record["id"],
              summary=f"{count} {entity_type} rows as {fmt}")
        return updated

    def export_entity(self, ctx: Ctx, entity_type: str, filters: Optional[Mapping[str, Any]], fmt: str, *,
                      async_: bool = False) -> Dict[str, Any]:
        filters = dict(filters or {})
        if fmt not in _CONTENT_TYPES:
            raise ValidationError("format must be csv, xlsx or json")
        if entity_type not in EXPORTABLE:
            raise ValidationError(f"cannot export {entity_type}; choose one of {', '.join(EXPORTABLE)}")
        if entity_type != "list":
            self.store.list(ctx, entity_type, filters, limit=1)  # validate filters before queueing anything
        count = None if entity_type == "list" else self.store.count(ctx, entity_type, filters)
        if async_ or (count or 0) > SYNC_LIMIT:
            record = self.store.insert(ctx, "exports", {"entity_type": entity_type, "format": fmt,
                                                        "filters": filters, "status": "queued"})
            task = self.platform.tasks.submit(ctx, "export", {"export_id": record["id"]},
                                              entity_type="exports", entity_id=record["id"])
            return {**record, "task_id": task["id"]}
        rows = self._rows_for(ctx, entity_type, filters)
        return self.export_rows(ctx, entity_type, rows, fmt, entity_type, filters=filters)

    def content_type(self, fmt: str) -> str:
        return _CONTENT_TYPES[fmt]


def run_export_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError

    service: ExportService = platform.service("exports")
    record = platform.store.find(ctx, "exports", task["params"].get("export_id") or "")
    if record is None:
        raise PermanentTaskError("export not found")
    reporter.progress(f"Gathering {record['entity_type']}")
    try:
        rows = service._rows_for(ctx, record["entity_type"], record["filters"])
        done = service.export_rows(ctx, record["entity_type"], rows, record["format"], record["entity_type"],
                                   filters=record["filters"], export_row=record)
    except ValidationError as error:
        platform.store.update(ctx, "exports", record["id"], {"status": "failed", "error": str(error)[:2000]})
        raise PermanentTaskError(str(error)) from error
    return {"export_id": done["id"], "rows": done["row_count"]}
