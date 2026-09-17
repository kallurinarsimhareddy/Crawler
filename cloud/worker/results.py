"""Turn a finished run into downloadable result files.

For every completed job the worker keeps:

* ``summary.json`` — the job, per-company outcomes and every posting;
* ``jobs.csv`` — one row per posting;
* whatever the runner added (``jobs.xlsx`` from the crawler's own exporter,
  ``crawl.log``).

Files are written in the job's workspace, uploaded to object storage under
``results/<owner>/<job_id>/``, and recorded in ``job_results`` with their size
and SHA-256. Upload and record are keyed by (job, kind), so re-running an
attempt replaces results rather than duplicating them.

**Spreadsheet safety.** Postings are text scraped from arbitrary websites. A
title such as ``=HYPERLINK(...)`` would execute as a formula when a user opens
the CSV in Excel. Cells beginning with ``= + - @`` or a control character are
prefixed with an apostrophe (see :func:`neutralise_cell`).
"""

from __future__ import annotations

import csv
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

from cloud.shared.models import Job, ResultFile, ResultKind
from cloud.shared.storage import ObjectStorage
from cloud.worker.runner import Artifact, RunResult
from cloud.worker.workspace import JobWorkspace

__all__ = ["ResultWriter", "neutralise_cell"]

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")
_SAFE_SEGMENT = re.compile(r"[^a-z0-9_-]")

_FILENAMES = {
    ResultKind.SUMMARY_JSON: "summary.json",
    ResultKind.JOBS_CSV: "jobs.csv",
    ResultKind.JOBS_XLSX: "jobs.xlsx",
    ResultKind.CRAWL_LOG: "crawl.log",
}


def neutralise_cell(value: Any) -> Any:
    """Stop a scraped string being read as a spreadsheet formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _segment(value: Optional[str]) -> str:
    return _SAFE_SEGMENT.sub("-", (value or "system").lower())[:64] or "system"


class ResultWriter:
    def __init__(self, storage: ObjectStorage, *, clock=None) -> None:
        self._storage = storage
        self._clock = clock

    def _now(self) -> datetime:
        from datetime import timezone

        return self._clock() if self._clock else datetime.now(timezone.utc)

    def write(self, job: Job, result: RunResult, workspace: JobWorkspace) -> List[ResultFile]:
        workspace.output.mkdir(parents=True, exist_ok=True)
        artifacts: List[Artifact] = [
            self._write_summary(job, result, workspace.output / "summary.json"),
            self._write_csv(result.postings, result.posting_fields, workspace.output / "jobs.csv"),
            *result.artifacts,
        ]
        return [self._upload(job, artifact, workspace) for artifact in artifacts if artifact.path.is_file()]

    def _write_summary(self, job: Job, result: RunResult, path: Path) -> Artifact:
        payload = {
            "job": {
                "job_id": job.job_id,
                "type": job.type.value,
                "target": job.target_label(),
                "attempt": job.attempts,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": self._now().isoformat(),
            },
            "summary": result.summary,
            "companies": result.companies,
            "postings": result.postings,
        }
        path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        return Artifact(ResultKind.SUMMARY_JSON, path, "application/json", row_count=len(result.postings))

    @staticmethod
    def _write_csv(postings: Sequence[Mapping[str, Any]], fields: Sequence[str], path: Path) -> Artifact:
        columns: List[str] = list(fields)
        for posting in postings:
            for key in posting:
                if key not in columns:
                    columns.append(key)
        # utf-8-sig so Excel opens non-ASCII titles correctly.
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for posting in postings:
                writer.writerow({key: neutralise_cell(posting.get(key, "")) for key in columns})
        return Artifact(ResultKind.JOBS_CSV, path, "text/csv; charset=utf-8", row_count=len(postings))

    def _upload(self, job: Job, artifact: Artifact, workspace: JobWorkspace) -> ResultFile:
        source = artifact.path.resolve()
        if workspace.path.resolve() not in source.parents:
            raise ValueError(f"artifact {source} is outside the job workspace")
        filename = _FILENAMES[artifact.kind]
        key = f"results/{_segment(job.owner_id)}/{job.job_id}/{filename}"
        stored = self._storage.put_file(key, source, content_type=artifact.content_type)
        return ResultFile(
            result_id=f"res_{uuid.uuid4().hex}",
            job_id=job.job_id,
            owner_id=job.owner_id,
            kind=artifact.kind,
            filename=filename,
            content_type=artifact.content_type,
            storage_key=stored.key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            row_count=artifact.row_count,
            created_at=self._now(),
        )
