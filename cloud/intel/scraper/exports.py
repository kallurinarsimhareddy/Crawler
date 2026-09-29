"""CSV, XLSX and JSON output, and the Companies / Jobs / All-fields views.

Every row carries ``source_url`` (plus ``source_urls`` when duplicates were
merged), ``extraction_method``, ``confidence`` and ``extracted_at``. Cells that
a spreadsheet would run as a formula (``=``, ``+``, ``-``, ``@``) are
neutralised with a leading apostrophe.
"""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from cloud.intel.core.normalize import company_name_key, domain_of

__all__ = ["META_COLUMNS", "columns_for", "views", "write_outputs"]

META_COLUMNS = ["source_url", "extraction_method", "confidence", "extracted_at"]
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def columns_for(schema: Mapping[str, Any], view: str = "all") -> List[str]:
    fields = [f for f in schema["fields"]]
    if view == "jobs":
        names = [f["name"] for f in fields if f.get("level") == "job"]
        names = [n for n in ("company_name", "website") if any(f["name"] == n for f in fields)] + names
    elif view == "companies":
        names = [f["name"] for f in fields if f.get("level") != "job"]
    else:
        names = [f["name"] for f in fields]
    return names + META_COLUMNS


def views(records: Sequence[Mapping[str, Any]], schema: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """``{"all": [...], "companies": [...], "jobs": [...]}`` (``jobs`` is empty for company requests)."""
    all_rows = [dict(r) for r in records]
    if schema.get("entity") != "job":
        return {"all": all_rows, "companies": all_rows, "jobs": []}
    company_cols = [c for c in columns_for(schema, "companies") if c not in META_COLUMNS]
    companies: Dict[str, Dict[str, Any]] = {}
    for row in records:
        key = (row.get("domain") or (domain_of(row["website"]) if row.get("website") else None)
               or company_name_key(row.get("company_name")) or row.get("source_url") or "")
        if key not in companies:
            companies[key] = {**{c: row.get(c) for c in company_cols}, "source_url": row.get("source_url"),
                              "extraction_method": row.get("extraction_method"), "confidence": row.get("confidence"),
                              "extracted_at": row.get("extracted_at"), "job_count": 0}
        companies[key]["job_count"] += 1
    return {"all": all_rows, "companies": list(companies.values()), "jobs": all_rows}


def _cell(value: Any) -> Any:
    from cloud.worker.results import neutralise_cell

    if isinstance(value, list):
        value = " | ".join(str(v) for v in value)
    return neutralise_cell(value)


def _xlsx_cell(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        value = json.dumps(value, default=str)
    return _cell(value if isinstance(value, list) else str(value))


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(row.get(c)) for c in columns])


def write_outputs(storage: Any, base_key: str, run_id: str, schema: Mapping[str, Any],
                  records: Sequence[Mapping[str, Any]], pages: Sequence[Mapping[str, Any]],
                  summary: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Write every output file to ``storage`` under ``base_key``; returns ``{name: file info}``."""
    from openpyxl import Workbook

    tables = views(records, schema)
    sheets = [("All fields", "all", columns_for(schema, "all"))]
    if schema.get("entity") == "job":
        sheets += [("Jobs", "jobs", columns_for(schema, "jobs")),
                   ("Companies", "companies", columns_for(schema, "companies") + ["job_count"])]
    page_columns = ["row", "batch", "source", "url", "final_url", "outcome", "reason", "records", "pages_fetched",
                    "ai_used", "extraction_method"]
    files: Dict[str, Dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        outputs: List[tuple] = []
        for _title, view, columns in sheets:
            name = "results" if view == "all" else view
            path = root / f"{name}.csv"
            _write_csv(path, columns + (["source_urls"] if view != "companies" else []), tables[view])
            outputs.append(("csv" if view == "all" else f"{view}.csv", path, "text/csv", f"scrape-{run_id}-{name}.csv"))
        book = Workbook()
        book.remove(book.active)
        for title, view, columns in sheets:
            sheet = book.create_sheet(title)
            sheet.append(columns)
            for row in tables[view]:
                sheet.append([_xlsx_cell(row.get(c)) for c in columns])
        pages_sheet = book.create_sheet("Pages")
        pages_sheet.append(page_columns)
        for page in pages:
            pages_sheet.append([_xlsx_cell(page.get(c)) for c in page_columns])
        xlsx_path = root / "results.xlsx"
        book.save(xlsx_path)
        outputs.append(("xlsx", xlsx_path, _XLSX, f"scrape-{run_id}.xlsx"))
        json_path = root / "results.json"
        json_path.write_text(json.dumps({"run_id": run_id, "instruction": schema.get("instruction"), "schema": schema,
                                         "summary": dict(summary), "columns": columns_for(schema, "all"),
                                         "records": list(records), "companies": tables["companies"],
                                         "jobs": tables["jobs"], "pages": list(pages)},
                                        default=str, indent=1), encoding="utf-8")
        outputs.append(("json", json_path, "application/json", f"scrape-{run_id}.json"))
        for name, path, content_type, filename in outputs:
            key = f"{base_key}/{path.name}"
            stored = storage.put_file(key, path, content_type=content_type)
            files[name] = {"storage_key": key, "content_type": content_type, "filename": filename,
                           "size_bytes": getattr(stored, "size_bytes", None), "sha256": getattr(stored, "sha256", None)}
    return files
