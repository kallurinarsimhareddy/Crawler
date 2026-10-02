"""Historical job import: CSV / XLSX -> the master ``job_postings`` table (the baseline).

Upload -> inspect headers -> suggested mapping (the person can correct it) ->
validate (preview + problems) -> import in the background (resumable, 500-row
batches). Rows are normalised to the 14 mandatory fields, deduplicated by Job URL
(within the file and against the database), and keep their own Source and Scraped
Date. Imported jobs are UNKNOWN until a monitor observes them; an import never
overwrites a job a monitor already observed, it only fills blank fields.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError, utcnow
from cloud.intel.job_monitor.schema import FIELD_COLUMNS, JOB_FIELDS, field_values, normalize_job

__all__ = ["JobImportService", "run_job_import_task", "suggest_mapping", "import_report_csv", "IMPORT_FIELDS",
           "MAX_IMPORT_BYTES", "MAX_IMPORT_ROWS"]

log = logging.getLogger(__name__)

MAX_IMPORT_BYTES = 250 * 1024 * 1024
MAX_IMPORT_ROWS = 2_000_000
CHUNK = 500
#: Validation reads at most this many rows synchronously (the import itself reads all).
VALIDATE_ROWS = 50_000

#: The 14 mandatory fields plus the optional context an upload may carry.
IMPORT_FIELDS: Tuple[str, ...] = (*JOB_FIELDS, "Source Board", "Search Term")
LABEL_COLUMNS: Dict[str, str] = {**FIELD_COLUMNS, "Source Board": "source_board", "Search Term": "search_term"}
#: Rejected rows kept for the downloadable import report (the counts cover every row).
REPORT_ROWS = 1000
#: How often (rows) a stream-scan of an upload reports nothing — the scan is bounded memory.
_CSV_FIELD_LIMIT = 10_000_000

#: field label -> header spellings recognised automatically (compared without case/punctuation).
SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "Job URL": ("job url", "url", "job link", "link", "job posting url", "posting url", "apply url", "job page"),
    "Job Title": ("job title", "title", "position", "position title", "role", "job name", "job"),
    "Company Name": ("company name", "company", "employer", "organization", "organisation", "hiring company"),
    "Location": ("location", "job location", "city", "place", "work location"),
    "Experience Level": ("experience level", "experience", "seniority", "level", "career level"),
    "Salary Budget": ("salary budget", "salary", "compensation", "pay", "salary range", "budget"),
    **{f"Keyword {i}": (f"keyword {i}", f"keywords {i}", f"skill {i}", f"tag {i}", f"kw{i}") for i in range(1, 6)},
    "Remote": ("remote", "remote type", "work type", "workplace", "workplace type", "remote onsite", "work mode"),
    "Source": ("source", "job source", "site", "job board", "board"),
    "Scraped Date": ("scraped date", "scrape date", "date scraped", "scraped at", "scraped on", "date"),
    "Source Board": ("source board", "source_board", "board name", "sub source", "channel"),
    "Search Term": ("search term", "search_term", "search", "query", "search query", "keyword searched"),
}


def _norm_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").replace("﻿", "").lower())


def suggest_mapping(headers: List[str]) -> Dict[str, Optional[str]]:
    """field label -> file header (or None). Exact label first, then synonyms; a header is used once."""
    by_norm = {}
    for header in headers:
        by_norm.setdefault(_norm_header(header), header)
    used: set = set()
    mapping: Dict[str, Optional[str]] = {}
    for label in IMPORT_FIELDS:
        choice = None
        for candidate in (label, *SYNONYMS.get(label, ())):
            header = by_norm.get(_norm_header(candidate))
            if header and header not in used:
                choice = header
                break
        if choice:
            used.add(choice)
        mapping[label] = choice
    return mapping


def _check_mapping(mapping: Mapping[str, Any], headers: List[str]) -> Dict[str, Optional[str]]:
    clean: Dict[str, Optional[str]] = {}
    for label in IMPORT_FIELDS:
        header = mapping.get(label)
        if header in (None, ""):
            clean[label] = None
            continue
        if header not in headers:
            raise ValidationError(f"{label}: the file has no column {header!r}")
        clean[label] = header
    unknown = [k for k in mapping if k not in IMPORT_FIELDS]
    if unknown:
        raise ValidationError(f"unknown job field(s): {', '.join(map(str, unknown[:5]))}")
    if not clean["Job URL"] or not clean["Job Title"]:
        raise ValidationError("map at least Job URL and Job Title")
    return clean


def _raw(mapping: Mapping[str, Optional[str]], record: Mapping[str, Any]) -> Dict[str, Any]:
    return {LABEL_COLUMNS[label]: (record.get(header) if header else None) for label, header in mapping.items()
            if label in LABEL_COLUMNS}


def _scan_csv(path: Any, filename: str) -> Tuple[List[str], int]:
    """Headers and data-row count of a CSV on disk, streamed (bounded memory, any size).
    Raises UnicodeDecodeError for a non-UTF-8 file and ValidationError for a broken one."""
    import csv

    delimiter = "\t" if str(filename).lower().endswith(".tsv") else ","
    csv.field_size_limit(_CSV_FIELD_LIMIT)
    header: Optional[List[str]] = None
    rows = 0
    with open(path, "r", encoding="utf-8-sig", errors="strict", newline="") as handle:
        try:
            # strict: an unterminated quoted field is an error, not a silently merged row
            for raw in csv.reader(handle, delimiter=delimiter, strict=True):
                if not any(str(c).strip() for c in raw):
                    continue
                if header is None:
                    header = [str(c).replace("\ufeff", "").strip() for c in raw]
                    continue
                rows += 1
                if rows > MAX_IMPORT_ROWS:
                    raise ValidationError(f"the file has more than {MAX_IMPORT_ROWS:,} rows")
        except csv.Error as error:
            raise ValidationError(f"the CSV could not be read near data row {rows + 1:,}: {error}") from error
    if not header or not any(header):
        raise ValidationError("the file is empty: no header row")
    return header, rows


def import_report_csv(row: Mapping[str, Any]) -> str:
    """The downloadable import report: the summary, then the rejected rows kept with their reason."""
    import csv
    import io

    stats = row.get("stats") or {}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["SANA GTM job import report"])
    for label, value in (("File", row.get("filename")), ("Import id", row.get("id")), ("Status", row.get("status")),
                         ("Rows in file", row.get("row_count")), ("Rows read", stats.get("rows", 0)),
                         ("New jobs", stats.get("new", 0)), ("Updated jobs", stats.get("updated", 0)),
                         ("Unchanged", stats.get("unchanged", 0)), ("Duplicates", stats.get("duplicates", 0)),
                         ("Rejected", stats.get("rejected", 0)), ("Errors", stats.get("errors", 0)),
                         ("Started", row.get("created_at")), ("Finished", stats.get("finished_at"))):
        writer.writerow([label, "" if value is None else value])
    writer.writerow([])
    writer.writerow(["Problem", "Count"])
    for kind, count in sorted((stats.get("problems") or {}).items(), key=lambda kv: -kv[1]):
        writer.writerow([kind, count])
    writer.writerow([])
    writer.writerow(["Row", "Job URL", "Reason"])
    for reject in stats.get("rejects") or []:
        url = str(reject.get("url") or "")
        writer.writerow([reject.get("row"), ("'" + url) if url.startswith(("=", "+", "-", "@")) else url,
                         reject.get("reason")])
    return out.getvalue()


class JobImportService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    def _key(self, ctx: Ctx, import_id: str, fmt: str) -> str:
        return f"job-imports/{ctx.workspace_id}/{import_id}/upload.{fmt}"

    def _load(self, row: Mapping[str, Any]) -> bytes:
        with self.platform.storage.open(row["storage_key"]) as handle:
            return handle.read()

    def _rows(self, row: Mapping[str, Any], data: Optional[bytes] = None) -> Iterator[Tuple[int, Dict[str, str]]]:
        """``(row_number, {header: value})`` for every non-empty data row. CSV is streamed from
        storage (bounded memory, any size); XLSX goes through the shared parser."""
        from cloud.intel.imports.parse import iter_rows

        if row["format"] == "csv" and data is None:
            try:
                yield from self._stream_csv(row)
                return
            except UnicodeDecodeError:
                log.info("%s is not UTF-8; reading it with the legacy-encoding parser", row["filename"])
        yield from iter_rows(row["format"], data if data is not None else self._load(row), sheet=row.get("sheet"),
                             max_rows=MAX_IMPORT_ROWS)

    def _stream_csv(self, row: Mapping[str, Any]) -> Iterator[Tuple[int, Dict[str, str]]]:
        import csv
        import io

        delimiter = "\t" if str(row["filename"]).lower().endswith(".tsv") else ","
        csv.field_size_limit(10_000_000)
        with self.platform.storage.open(row["storage_key"]) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8-sig", errors="strict", newline="")
            header: Optional[List[str]] = None
            number = 0
            for raw in csv.reader(text, delimiter=delimiter):
                cells = [str(c).strip() for c in raw]
                if not any(cells):
                    continue
                if header is None:
                    header = [c.replace("\ufeff", "") for c in cells]
                    continue
                number += 1
                if number > MAX_IMPORT_ROWS:
                    return
                record: Dict[str, str] = {}
                for index, name in enumerate(header):
                    if name and name not in record:
                        record[name] = cells[index] if index < len(cells) else ""
                yield number, record

    def upload(self, ctx: Ctx, filename: str, data: bytes, *, sheet: Optional[str] = None) -> Dict[str, Any]:
        """Bytes in hand (small files, tests): written to a temp file and stored like a stream."""
        import tempfile
        from pathlib import Path

        ctx.require_write()
        if len(data) > MAX_IMPORT_BYTES:
            raise ValidationError(f"the file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "upload"
            path.write_bytes(data)
            return self.upload_file(ctx, filename, path, sheet=sheet)

    def upload_file(self, ctx: Ctx, filename: str, path: Any, *, sheet: Optional[str] = None) -> Dict[str, Any]:
        """A file already on disk (the API streams the request body there in chunks). A CSV is
        scanned and stored without ever being held in memory; an XLSX goes through the parser."""
        import os
        from pathlib import Path

        from cloud.intel.imports.parse import ParseError, detect_format, parse_file

        ctx.require_write()
        size = os.path.getsize(path)
        if size > MAX_IMPORT_BYTES:
            raise ValidationError(f"the file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
        if size == 0:
            raise ValidationError("the file is empty")
        problems: List[str] = []
        try:
            fmt = detect_format(filename)
            if fmt not in ("csv", "xlsx"):
                raise ValidationError("job imports accept CSV (UTF-8) or XLSX files")
            if fmt == "csv":
                try:
                    headers, row_count = _scan_csv(path, filename)
                    sheet_name = None
                except UnicodeDecodeError:
                    with open(path, "rb") as handle:
                        parsed = parse_file(filename, handle.read(), sheet=sheet, max_rows=MAX_IMPORT_ROWS)
                    headers, row_count, sheet_name = list(parsed.columns), parsed.row_count, parsed.sheet
                    problems = list(parsed.problems) + ["the file is not UTF-8; read with the legacy-encoding parser"]
            else:
                with open(path, "rb") as handle:
                    parsed = parse_file(filename, handle.read(), sheet=sheet, max_rows=MAX_IMPORT_ROWS)
                headers, row_count, sheet_name, problems = (list(parsed.columns), parsed.row_count, parsed.sheet,
                                                            list(parsed.problems))
        except ParseError as error:
            raise ValidationError(str(error)) from error
        headers = [str(h).replace("\ufeff", "").strip() for h in headers]
        if not headers or not any(headers):
            raise ValidationError("; ".join(problems) or "the file has no header row")
        if row_count == 0:
            raise ValidationError("the file has a header row but no data rows")
        row = self.store.insert(ctx, "job_imports", {
            "filename": filename[:300], "format": fmt, "storage_key": "pending", "size_bytes": size,
            "sheet": sheet_name, "headers": headers, "row_count": row_count,
            "mapping": suggest_mapping(headers), "status": "uploaded", "validation": {"problems": problems}})
        key = self._key(ctx, row["id"], fmt)
        self.platform.storage.put_file(key, Path(path), content_type="application/octet-stream")
        row = self.store.update(ctx, "job_imports", row["id"], {"storage_key": key})
        audit(self.store, ctx, "job_import.uploaded", entity_type="job_imports", entity_id=row["id"],
              summary=f"{filename}: {row_count:,} rows, {len(headers)} columns")
        return {**row, "fields": list(IMPORT_FIELDS)}

    def validate(self, ctx: Ctx, import_id: str, mapping: Mapping[str, Any], *,
                 default_source: Optional[str] = None) -> Dict[str, Any]:
        """Save the (corrected) mapping and check the file: preview rows, missing / invalid
        values, duplicates in the file, how many URLs are already stored. Reads at most
        :data:`VALIDATE_ROWS` rows; the import itself reads every row."""
        ctx.require_write()
        row = self.store.get(ctx, "job_imports", import_id)
        if row["status"] in ("importing", "completed"):
            raise ValidationError(f"this import is already {row['status']}")
        clean = _check_mapping(mapping, list(row["headers"]))
        report: Dict[str, Any] = {"rows_checked": 0, "valid": 0, "rejected": 0, "duplicates_in_file": 0,
                                  "problems": {}, "filled": {label: 0 for label in IMPORT_FIELDS}, "preview": [],
                                  "examples": [], "already_stored": 0, "truncated": False}
        seen: set = set()
        keys_sample: List[str] = []
        today = utcnow().date()
        for number, record in self._rows(row):
            if report["rows_checked"] >= VALIDATE_ROWS:
                report["truncated"] = True
                break
            report["rows_checked"] += 1
            values, problems = normalize_job(_raw(clean, record), source=default_source)
            for problem in problems:
                kind = problem.split(":")[0].split("(")[0].strip()
                report["problems"][kind] = report["problems"].get(kind, 0) + 1
                if len(report["examples"]) < 20:
                    report["examples"].append({"row": number, "problem": problem})
            if values is None:
                report["rejected"] += 1
                continue
            digest = hashlib.blake2b(values["url_key"].encode(), digest_size=10).digest()
            if digest in seen:
                report["duplicates_in_file"] += 1
                continue
            seen.add(digest)
            report["valid"] += 1
            labelled = {**field_values(values), "Source Board": values.get("source_board"),
                        "Search Term": values.get("search_term")}
            for label, value in labelled.items():
                if value not in (None, ""):
                    report["filled"][label] += 1
            if len(report["preview"]) < 20:
                report["preview"].append({"row": number, **labelled})
            if len(keys_sample) < 2000:
                keys_sample.append(values["url_key"])
        for start in range(0, len(keys_sample), 500):
            report["already_stored"] += self.store.count(ctx, "job_postings",
                                                         {"url_key": keys_sample[start:start + 500]})
        report["already_stored_checked"] = len(keys_sample)
        report["today"] = today.isoformat()
        return self.store.update(ctx, "job_imports", import_id, {
            "mapping": clean, "default_source": (default_source or None) and str(default_source)[:200],
            "status": "validated", "validation": report})

    def start(self, ctx: Ctx, import_id: str) -> Dict[str, Any]:
        ctx.require_write()
        row = self.store.get(ctx, "job_imports", import_id)
        if row["status"] != "validated":
            raise ValidationError("validate the mapping before importing")
        task = self.platform.tasks.submit(ctx, "job_import", {"import_id": import_id}, max_attempts=5,
                                          idempotency_key=f"job_import:{import_id}", entity_type="job_imports",
                                          entity_id=import_id)
        audit(self.store, ctx, "job_import.started", entity_type="job_imports", entity_id=import_id,
              summary=f"{row['filename']}: {row['row_count']:,} rows")
        return self.store.update(ctx, "job_imports", import_id, {"status": "importing", "task_id": task["id"]})


def run_job_import_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Worker handler: import every row in 500-row batches; resumes after the last stored batch."""
    from cloud.intel.tasks.worker import PermanentTaskError, TaskCancelled, TaskPaused

    store = platform.store
    imports: JobImportService = platform.service("job_imports")
    jobs = platform.service("job_monitors")
    row = store.find(ctx, "job_imports", str(task["params"].get("import_id") or ""))
    if row is None:
        raise PermanentTaskError("job import not found")
    if row["status"] in ("completed", "cancelled"):
        return {"import_id": row["id"], "status": row["status"]}
    mapping = row["mapping"] or {}
    checkpoint = dict(row.get("checkpoint") or {})
    done = int(checkpoint.get("row", 0))
    stats = {"rows": 0, "new": 0, "updated": 0, "unchanged": 0, "duplicates": 0, "filled": 0, "rejected": 0,
             "errors": 0, "linked": 0, "review": 0, "problems": {}, "rejects": [], **(row.get("stats") or {})}
    source_name = f"Import: {row['filename']}"[:200]
    started = utcnow()

    def flush(batch: List[Dict[str, Any]], last_row: int) -> None:
        nonlocal done
        if batch:
            result = jobs.upsert_batch(ctx, batch, observed_at=started, source_kind="import", source_name=source_name,
                                       import_id=row["id"])
            for key in ("new", "duplicates", "filled", "updated", "unchanged", "linked", "review"):
                stats[key] = stats.get(key, 0) + result.get(key, 0)
        done = last_row
        total = max(int(row["row_count"] or 0), done, 1)
        stats["percent"] = round(100.0 * done / total, 1)
        # a snapshot: the counters keep moving after this checkpoint (a resumed run starts from it)
        store.update(ctx, "job_imports", row["id"], {"checkpoint": {"row": done}, "stats": copy.deepcopy(stats)})
        reporter.progress(f"{row['filename']}: row {done:,} of {row['row_count']:,}", import_id=row["id"],
                          row=done, total=row["row_count"], percent=stats["percent"])

    try:
        batch: List[Dict[str, Any]] = []
        last = done
        for number, record in imports._rows(row):
            if number <= done:
                continue
            stats["rows"] += 1
            values, problems = normalize_job(_raw(mapping, record), source=row.get("default_source"))
            for problem in problems:
                kind = problem.split(":")[0].split("(")[0].strip()
                stats["problems"][kind] = stats["problems"].get(kind, 0) + 1
            if values is None:
                stats["rejected"] += 1
                if len(stats["rejects"]) < REPORT_ROWS:
                    url = _raw(mapping, record).get("job_url")
                    stats["rejects"].append({"row": number, "url": str(url or "")[:500],
                                             "reason": "; ".join(problems)[:300]})
            else:
                batch.append(values)
            last = number
            if len(batch) >= CHUNK:
                flush(batch, last)
                batch = []
                if reporter.is_cancelled():
                    store.update(ctx, "job_imports", row["id"], {"status": "cancelled"})
                    raise TaskCancelled()
                if reporter.should_pause():
                    raise TaskPaused({"import_id": row["id"]})
        flush(batch, last)
    except (TaskPaused, TaskCancelled):
        raise
    except Exception as error:  # noqa: BLE001
        if int(task.get("attempts") or 1) >= int(task.get("max_attempts") or 1):
            store.update(ctx, "job_imports", row["id"], {"status": "failed", "error": str(error)[:2000]})
        raise
    stats["percent"] = 100.0
    stats["finished_at"] = utcnow().isoformat()
    row = store.update(ctx, "job_imports", row["id"], {"status": "completed", "stats": stats})
    audit(store, ctx, "job_import.completed", entity_type="job_imports", entity_id=row["id"],
          summary=f"{row['filename']}: {stats['new']:,} new, {stats['updated']:,} updated, "
                  f"{stats['unchanged']:,} unchanged, {stats['duplicates']:,} duplicates, {stats['rejected']:,} rejected")
    try:
        platform.service("notifications").notify(
            ctx, title=f"Job import finished: {stats['new']:,} jobs added", kind="job_import", severity="success",
            body=f"{row['filename']}: {stats['rows']:,} rows, {stats['updated']:,} updated, "
                 f"{stats['unchanged']:,} unchanged, {stats['duplicates']:,} duplicates, {stats['rejected']:,} rejected.",
            link=f"/jobs?import={row['id']}", entity_type="job_imports",
            entity_id=row["id"])
    except Exception:  # noqa: BLE001
        log.exception("could not notify about job import %s", row["id"])
    return {"import_id": row["id"], **{k: v for k, v in stats.items() if k not in ("problems", "rejects")}}
