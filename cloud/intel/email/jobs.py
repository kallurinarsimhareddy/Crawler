"""Email validation jobs: validate a whole file, list, contact set or scrape.

A job holds one ``email_validation_items`` row per input row, keeping the
original row beside its result, so the file can be exported back with the
validation columns appended. Validation itself is **not** re-implemented here:
every address goes through :class:`~cloud.intel.email.service.EmailValidationService`
(local syntax / MX / disposable / role / free checks, the 30-day cache, and the
paid provider only with ``allow_paid`` and a connected EmailListVerify key).

    job = jobs.create_upload(ctx, "leads.xlsx", data)      # columns, preview, detected email column
    jobs.set_email_column(ctx, job["id"], "Work Email")
    jobs.start(ctx, job["id"], settings={"allow_paid": False})
    # worker: run_validation_job_task -> items become VALID / INVALID / RISKY / ...

Nothing here adds anything to the CRM on its own. :meth:`add_to_list`,
:meth:`create_campaign` and :meth:`enroll` are explicit user actions; creating
contacts for rows that are not in the CRM yet needs ``create_missing_contacts``.
Local checks never answer VALID (there is no SMTP probing): a well-formed
address on a domain that receives mail is UNKNOWN until a paid mailbox check.

Resolving those UNKNOWNs is a separate, confirmed step on a completed job:
:meth:`unknowns_estimate` counts exactly the addresses EmailListVerify would be asked
about (built-in UNKNOWNs it has not answered, minus fresh EmailListVerify answers in
the cache) and :meth:`verify_unknowns` sends only those, after the caller confirms
that exact credit figure. Strict validation (on by default) keeps VALID meaning
"verified by EmailListVerify".
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ValidationError, utcnow
from cloud.intel.core.normalize import normalize_email
from cloud.intel.email import engine
from cloud.intel.email.providers import elv_enabled

__all__ = ["EmailValidationJobService", "detect_email_columns", "extract_email", "reason_for", "result_source",
           "run_validation_job_task", "STATUSES", "MAX_ROWS", "MAX_BYTES", "SOURCES"]

#: Result provenance labels (see :func:`result_source`).
SOURCES = ("Built-in", "EmailListVerify", "Built-in + EmailListVerify")

log = logging.getLogger(__name__)

STATUSES = ("VALID", "INVALID", "RISKY", "UNKNOWN", "DISPOSABLE", "ROLE", "FREE_PROVIDER")
#: Rows per job. Uploads are streamed into the job and processed in batches, so a
#: large file never has to be split by hand.
MAX_ROWS = 1_000_000
MAX_BYTES = 100 * 1024 * 1024
#: Rows validated per worker step; progress is written at most every PROGRESS_SECONDS.
BATCH = 200
PROGRESS_SECONDS = 3.0
PREVIEW_ROWS = 8
#: Evidence cache lifetimes (days), per check type.
EVIDENCE_TTL_DAYS = {"dns": 1, "smtp": 7, "public": 14, "catch_all": 30, "mailbox": 30}
#: Per-job caps on the slower, opt-in layers (distinct domains / mail hosts).
PUBLIC_MAX_SITES = 300
SMTP_MAX_HOSTS = 300
LOCAL_CHECKS = (
    {"key": "syntax", "label": "Syntax", "detail": "RFC-style address format and placeholder addresses"},
    {"key": "mx", "label": "MX / domain", "detail": "The domain exists and accepts mail (MX, else A record)"},
    {"key": "disposable", "label": "Disposable", "detail": "Throw-away mailbox domains"},
    {"key": "role", "label": "Role account", "detail": "info@, sales@, hr@ and similar shared inboxes"},
    {"key": "free_provider", "label": "Free provider", "detail": "Gmail, Outlook.com, Yahoo and other free mail"},
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_HEADER_HINTS = ("email", "e-mail", "e_mail", "mail", "email address", "work email", "business email")
_NAME_FIELDS = ("full_name", "full name", "name", "contact name", "contact")


def extract_email(value: Any) -> str:
    """The address in a cell: plain, ``mailto:``, or ``Name <a@b.com>``. A cell with
    text but no address comes back as-is (it validates as INVALID); empty is ``""``."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("mailto:"):
        text = text[7:].split("?", 1)[0]
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else text


def _key(email: str) -> str:
    return normalize_email(email) or email.strip().lower()


def detect_email_columns(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Likely email columns, best first: ``{"column", "score", "hits", "sampled", "reason"}``.

    Content counts more than the header: a column is a candidate when at least a
    quarter of its sampled non-empty cells hold an address, or its header says email.
    """
    sample = list(rows[:200])
    out: List[Dict[str, Any]] = []
    for column in columns:
        values = [str(r.get(column) or "").strip() for r in sample]
        filled = [v for v in values if v]
        hits = sum(1 for v in filled if _EMAIL_RE.search(v))
        ratio = hits / len(filled) if filled else 0.0
        lowered = column.strip().lower()
        header = any(h == lowered or h in lowered for h in _HEADER_HINTS)
        if ratio < 0.25 and not header:
            continue
        score = round(ratio * 80 + (20 if header else 0), 1)
        reasons = []
        if header:
            reasons.append("header mentions email")
        if filled:
            reasons.append(f"{hits} of {len(filled)} sampled values are addresses")
        out.append({"column": column, "score": score, "hits": hits, "sampled": len(filled),
                    "reason": "; ".join(reasons) or "header"})
    out.sort(key=lambda c: (-c["score"], columns.index(c["column"])))
    return out


def reason_for(status: str, checks: Mapping[str, Any]) -> str:
    """One plain-language sentence explaining a result (used in exports and the UI)."""
    checks = checks or {}
    if checks.get("empty"):
        return "No email address in this row"
    if checks.get("paid_error"):
        base = {"UNKNOWN": "Mail server exists; mailbox not verified"}.get(status, "")
        return f"{base} (paid check failed: {checks['paid_error']})".strip()
    if status == "VALID":
        return "Mailbox verified by the external provider"
    if status == "INVALID":
        if checks.get("syntax") is False:
            return "Not a valid email address"
        if checks.get("placeholder"):
            return "Placeholder address"
        if checks.get("mx") is False:
            return "The domain does not accept email"
        code = checks.get("result_code")
        return f"Rejected by the external provider ({code})" if code else "Undeliverable"
    if status == "DISPOSABLE":
        return "Disposable (throw-away) mailbox domain"
    if status == "ROLE":
        return "Role / shared inbox (info@, sales@, hr@ …)"
    if status == "FREE_PROVIDER":
        return "Free mailbox provider (Gmail, Outlook.com …)"
    if status == "RISKY":
        return f"Accept-all or protected server ({checks.get('result_code', 'risky')})"
    if status == "UNKNOWN":
        if checks.get("dns") == "transient failure":
            return "DNS lookup failed temporarily; try again later"
        if checks.get("result_code"):
            return f"EmailListVerify could not confirm the mailbox ({checks['result_code']})"
        if checks.get("strict_demoted"):
            return "Not externally verified (strict validation)"
        return "Mail server exists; mailbox not verified"
    return ""


#: Evidence columns appended to exports (see engine.evidence_summary).
_EXPORT_SIGNALS = ("technical", "domain", "mx", "spf", "dmarc", "smtp", "catch_all", "role", "disposable",
                   "free_provider", "person_match", "public_evidence", "mailbox_verification")


def _contact_mode(columns: Mapping[str, str]) -> bool:
    """Contact mode: the input names the person (first/last or full name)."""
    return bool(columns.get("first_name") or columns.get("last_name") or columns.get("full_name"))


def result_source(provider: Optional[str], checks: Optional[Mapping[str, Any]]) -> str:
    """Where a result came from: ``Built-in`` (local checks only), ``Built-in + EmailListVerify``
    (the built-in checks ran and EmailListVerify gave the final answer) or ``EmailListVerify``
    (an external answer with no built-in record, e.g. an older cached result)."""
    checks = checks or {}
    if provider != "emaillistverify":
        return "Built-in"
    if "builtin_status" in checks or "syntax" in checks or "mx" in checks:
        return "Built-in + EmailListVerify"
    return "EmailListVerify"


def _name_from(row: Mapping[str, Any], email: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for field in _NAME_FIELDS:
        value = str(lowered.get(field) or "").strip()
        if value:
            return value[:200]
    first = str(lowered.get("first_name") or lowered.get("first name") or "").strip()
    last = str(lowered.get("last_name") or lowered.get("last name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()[:200]
    return (email.split("@", 1)[0] or email)[:200]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "campaign"


class _EvidenceRun:
    """Per-run evidence with two cache levels: in memory for the run, and the workspace's
    ``email_check_cache`` (hashed subjects, per-check expiry) across runs. The slower
    layers (SMTP preflight, public evidence) run only when the job asked for them, and
    at most once per distinct host or site."""

    def __init__(self, service: "EmailValidationJobService", ctx: Ctx, settings: Mapping[str, Any]) -> None:
        self.store = service.store
        self.ctx = ctx
        self.dns = service.dns_client or engine.DnsPythonClient()
        self.smtp = (engine.SmtpPreflight(connect=service.smtp_connect) if settings.get("smtp_preflight")
                     else None)
        self.finder = (engine.PublicEvidenceFinder(service.public_fetcher) if settings.get("public_evidence")
                       else None)
        self.memory: Dict[Tuple[str, str], Any] = {}

    def _cached(self, check_type: str, subject: str) -> Any:
        key = (check_type, subject)
        if key in self.memory:
            return self.memory[key]
        row = self.store.first(self.ctx, "email_check_cache",
                               {"subject_hash": engine.sha256(subject), "check_type": check_type})
        value = row["result"] if row is not None and row["expires_at"] > utcnow() else None
        if value is not None:
            self.memory[key] = value
        return value

    def remember(self, check_type: str, subject: str, kind: str, result: Any, *, source: str = "built-in",
                 provider: Optional[str] = None, ttl_type: Optional[str] = None) -> Any:
        result = engine.cache_json(result)
        self.memory[(check_type, subject)] = result
        now = utcnow()
        values = {"subject_hash": engine.sha256(subject), "subject_kind": kind, "check_type": check_type,
                  "result": result, "source": source, "provider": provider, "checked_at": now,
                  "expires_at": now + timedelta(days=EVIDENCE_TTL_DAYS[ttl_type or check_type])}
        try:
            existing = self.store.first(self.ctx, "email_check_cache",
                                        {"subject_hash": values["subject_hash"], "check_type": check_type})
            if existing is None:
                self.store.insert(self.ctx, "email_check_cache", values)
            else:
                self.store.update(self.ctx, "email_check_cache", existing["id"], values)
        except Exception:  # noqa: BLE001 - the cache is an optimisation; never fail a row over it
            log.debug("evidence cache write skipped (%s)", check_type)
        return result

    def domain(self, domain: str) -> Dict[str, Any]:
        cached = self._cached("dns", domain)
        if cached is not None:
            return cached
        info = engine.inspect_domain(domain, self.dns)
        if info.get("dns_error") in ("servfail", "timeout", "error"):
            self.memory[("dns", domain)] = info  # transient: reuse within this run only
            return info
        return self.remember("dns", domain, "domain", info)

    def smtp_check(self, host: str) -> Optional[Dict[str, Any]]:
        if self.smtp is None:
            return None
        cached = self._cached("smtp", host)
        if cached is not None:
            return cached
        if sum(1 for (kind, _) in self.memory if kind == "smtp") >= SMTP_MAX_HOSTS:
            return {"smtp": "unknown", "detail": "per-job SMTP host limit reached", "host": host}
        return self.remember("smtp", host, "host", self.smtp.check(host))

    def public_scan(self, site: str) -> Optional[Dict[str, Any]]:
        if self.finder is None or not site:
            return None
        cached = self._cached("public", site)
        if cached is not None:
            return cached
        if sum(1 for (kind, _) in self.memory if kind == "public") >= PUBLIC_MAX_SITES:
            return None
        scan = {**self.finder.scan(site), "checked_at": utcnow().isoformat()}
        return self.remember("public", site, "domain", scan)

    def catch_all(self, domain: str) -> Optional[bool]:
        cached = self._cached("catch_all", domain)
        return None if cached is None else cached.get("catch_all")

    def learn_catch_all(self, domain: str, value: Optional[bool]) -> None:
        if value is not None and self.catch_all(domain) is not True:  # "yes" is never downgraded by one "ok"
            self.remember("catch_all", domain, "domain", {"catch_all": value}, source="EmailListVerify",
                          provider="emaillistverify")


class EmailValidationJobService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store
        #: Injection points (tests): DNS client, SMTP connector, public-page fetcher.
        self.dns_client: Any = None
        self.smtp_connect: Any = None
        self.public_fetcher: Any = None

    # --- creation ---------------------------------------------------------------------

    def _insert_items(self, ctx: Ctx, job_id: str, rows: Iterable[Tuple[int, Dict[str, Any]]],
                      contact_ids: Optional[Dict[int, str]] = None) -> int:
        batch: List[Dict[str, Any]] = []
        count = 0
        for number, row in rows:
            batch.append({"job_id": job_id, "row_number": number, "row": row, "status": "PENDING",
                          "contact_id": (contact_ids or {}).get(number)})
            count += 1
            if len(batch) >= 500:
                self.store.insert_many(ctx, "email_validation_items", batch)
                batch = []
        if batch:
            self.store.insert_many(ctx, "email_validation_items", batch)
        return count

    def create_upload(self, ctx: Ctx, filename: str, data: bytes, *, name: Optional[str] = None) -> Dict[str, Any]:
        """Read a CSV or XLSX file into a job. The best email column is pre-selected
        only when detection is unambiguous; otherwise the user picks one."""
        from cloud.intel.imports.parse import ParseError, detect_format, iter_rows, parse_file

        ctx.require_write()
        filename = (filename or "upload").strip()[:255]
        try:
            fmt = detect_format(filename)
        except ParseError as error:
            raise ValidationError(str(error)) from None
        if fmt not in ("csv", "xlsx"):
            raise ValidationError("upload a CSV or XLSX file")
        if not data:
            raise ValidationError("the file is empty")
        if len(data) > MAX_BYTES:
            raise ValidationError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB; split it")
        try:
            parsed = parse_file(filename, data, max_rows=MAX_ROWS)  # streams: header, count, problems
            sample = []
            for pair in iter_rows(fmt, data, max_rows=200):
                sample.append(pair)
        except ParseError as error:
            raise ValidationError(str(error)) from None
        if not parsed.columns:
            raise ValidationError("; ".join(parsed.problems) or "the file has no header row")
        if not sample:
            raise ValidationError("the file has a header but no data rows")
        candidates = detect_email_columns(parsed.columns, [r for _, r in sample])
        chosen = None
        if candidates and (len(candidates) == 1 or candidates[0]["score"] - candidates[1]["score"] >= 15):
            chosen = candidates[0]["column"]
        contact_columns = engine.detect_contact_columns(parsed.columns)
        job = self.store.insert(ctx, "email_validation_jobs", {
            "name": (name or filename)[:200], "source_type": "upload", "filename": filename, "format": fmt,
            "size_bytes": len(data), "columns": parsed.columns, "email_column": chosen,
            "preview": [r for _, r in sample[:PREVIEW_ROWS]], "row_count": 0,
            "status": "ready" if chosen else "uploaded",
            "settings": {"candidates": candidates, "problems": parsed.problems,
                         "truncated": any("more than" in p for p in parsed.problems),
                         "contact_columns": contact_columns, "contact_mode": _contact_mode(contact_columns)},
            "counts": {"total": 0, "processed": 0}})
        # Streamed straight into the job in chunks of 500: the file is never held as a list of rows.
        total = self._insert_items(ctx, job["id"], iter_rows(fmt, data, max_rows=MAX_ROWS))
        job = self.store.update(ctx, "email_validation_jobs", job["id"], {
            "row_count": total, "counts": {"total": total, "processed": 0}})
        audit(self.store, ctx, "email_validation.upload", entity_type="email_validation_jobs", entity_id=job["id"],
              summary=f"{filename}: {total} row(s)", changes={"size_bytes": len(data), "email_column": chosen})
        return job

    def create_from_rows(self, ctx: Ctx, *, name: str, rows: Sequence[Mapping[str, Any]], email_field: str,
                         source_type: str = "scrape", source_id: Optional[str] = None, start: bool = False,
                         settings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        ctx.require_write()
        if source_type not in ("scrape", "manual", "upload", "list", "contacts"):
            raise ValidationError("unknown source type")
        records = [dict(r) for r in rows][:MAX_ROWS]
        if not records:
            raise ValidationError("there are no rows to validate")
        columns: List[str] = []
        for record in records:
            for column in record:
                if column not in columns:
                    columns.append(str(column))
        if email_field not in columns:
            raise ValidationError(f"the rows have no {email_field!r} field")
        contact_columns = engine.detect_contact_columns(c for c in columns if c != email_field)
        job = self.store.insert(ctx, "email_validation_jobs", {
            "name": name[:200], "source_type": source_type, "source_id": source_id, "columns": columns,
            "email_column": email_field, "preview": records[:PREVIEW_ROWS], "row_count": len(records),
            "status": "ready", "counts": {"total": len(records), "processed": 0},
            "settings": {"contact_columns": contact_columns, "contact_mode": _contact_mode(contact_columns)}})
        self._insert_items(ctx, job["id"], enumerate(records, start=1))
        audit(self.store, ctx, "email_validation.create", entity_type="email_validation_jobs", entity_id=job["id"],
              summary=f"{name}: {len(records)} row(s) from {source_type}")
        return self.start(ctx, job["id"], settings=settings) if start else job

    def create_from_contacts(self, ctx: Ctx, *, name: str, contact_ids: Optional[Sequence[str]] = None,
                             list_id: Optional[str] = None, start: bool = False,
                             settings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        ctx.require_write()
        ids: List[str] = list(dict.fromkeys(contact_ids or []))
        if list_id:
            target = self.store.get(ctx, "lists", list_id)
            if target["entity_type"] != "contacts":
                raise ValidationError(f"list {target['name']} holds {target['entity_type']}, not contacts")
            ids += [m["entity_id"] for m in self.store.all(ctx, "list_members", {"list_id": list_id}, cap=MAX_ROWS)]
            ids = list(dict.fromkeys(ids))
        if not ids:
            raise ValidationError("choose contacts or a contact list")
        records: List[Dict[str, Any]] = []
        links: Dict[int, str] = {}
        for contact_id in ids[:MAX_ROWS]:
            contact = self.store.find(ctx, "contacts", contact_id)
            if contact is None:
                continue
            records.append({"full_name": contact.get("full_name"), "email": contact.get("email") or "",
                            "title": contact.get("title") or "", "contact_id": contact["id"]})
            links[len(records)] = contact["id"]
        if not records:
            raise ValidationError("none of the chosen contacts exist")
        source = "list" if list_id else "contacts"
        job = self.store.insert(ctx, "email_validation_jobs", {
            "name": name[:200], "source_type": source, "source_id": list_id,
            "columns": ["full_name", "email", "title", "contact_id"], "email_column": "email",
            "preview": records[:PREVIEW_ROWS], "row_count": len(records), "status": "ready",
            "counts": {"total": len(records), "processed": 0}})
        self._insert_items(ctx, job["id"], enumerate(records, start=1), links)
        audit(self.store, ctx, "email_validation.create", entity_type="email_validation_jobs", entity_id=job["id"],
              summary=f"{name}: {len(records)} contact(s)")
        return self.start(ctx, job["id"], settings=settings) if start else job

    # --- lifecycle --------------------------------------------------------------------

    def get(self, ctx: Ctx, job_id: str) -> Dict[str, Any]:
        """The job, with its status reconciled against its background task."""
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        task = self.store.find(ctx, "platform_tasks", job["task_id"]) if job.get("task_id") else None
        if task is not None and job["status"] in ("queued", "running", "paused"):
            wanted = {"paused": "paused", "cancelled": "cancelled", "failed": "failed"}.get(task["status"])
            if wanted and wanted != job["status"] and ctx.can_write:
                job = self.store.update(ctx, "email_validation_jobs", job_id, {
                    "status": wanted, "error": task.get("error") if wanted == "failed" else job.get("error")})
            elif task["status"] == "queued" and job["status"] == "paused" and ctx.can_write:
                job = self.store.update(ctx, "email_validation_jobs", job_id, {"status": "queued"})
        job = dict(job)
        job["task"] = ({"id": task["id"], "status": task["status"], "progress": task.get("progress"),
                        "error": task.get("error")} if task else None)
        return job

    def set_email_column(self, ctx: Ctx, job_id: str, column: str) -> Dict[str, Any]:
        ctx.require_write()
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        if job["status"] not in ("uploaded", "ready"):
            raise ConflictError(f"the job is already {job['status']}")
        if column not in (job.get("columns") or []):
            raise ValidationError(f"the file has no column {column!r}")
        return self.store.update(ctx, "email_validation_jobs", job_id, {"email_column": column, "status": "ready"})

    def start(self, ctx: Ctx, job_id: str, *, settings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        ctx.require_write()
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        if job["status"] != "ready":
            if job["status"] == "uploaded":
                raise ValidationError("choose the email column first")
            raise ConflictError(f"the job is already {job['status']}")
        strict = (settings or {}).get("strict_validation", (job.get("settings") or {}).get("strict_validation", True))
        clean = {"allow_paid": bool((settings or {}).get("allow_paid")),
                 "max_age_days": max(0, min(365, int((settings or {}).get("max_age_days") or 30))),
                 "strict_validation": bool(strict),
                 # Opt-in evidence layers: slower, so never on by default.
                 "public_evidence": bool((settings or {}).get("public_evidence")),
                 "smtp_preflight": bool((settings or {}).get("smtp_preflight"))}
        if clean["allow_paid"] and not elv_enabled(self.platform.service("providers"), ctx):
            raise ValidationError("EmailListVerify is not configured and verified; run with built-in checks only")
        merged = {**(job.get("settings") or {}), **clean}
        task = self.platform.tasks.submit(ctx, "email_validation_job", {"job_id": job_id},
                                          entity_type="email_validation_jobs", entity_id=job_id)
        job = self.store.update(ctx, "email_validation_jobs", job_id, {"status": "queued", "task_id": task["id"],
                                                                        "settings": merged})
        audit(self.store, ctx, "email_validation.start", entity_type="email_validation_jobs", entity_id=job_id,
              changes={"allow_paid": clean["allow_paid"], "rows": job["row_count"]})
        return job

    def _task(self, ctx: Ctx, job: Mapping[str, Any]) -> str:
        if not job.get("task_id"):
            raise ConflictError("the job has not been started")
        return job["task_id"]

    def pause(self, ctx: Ctx, job_id: str) -> Dict[str, Any]:
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        if job["status"] not in ("queued", "running"):
            raise ConflictError(f"cannot pause a {job['status']} job")
        task = self.platform.tasks.pause(ctx, self._task(ctx, job))
        if task["status"] == "paused":
            self.store.update(ctx, "email_validation_jobs", job_id, {"status": "paused"})
        audit(self.store, ctx, "email_validation.pause", entity_type="email_validation_jobs", entity_id=job_id)
        return self.get(ctx, job_id)

    def resume(self, ctx: Ctx, job_id: str) -> Dict[str, Any]:
        job = self.get(ctx, job_id)
        if job["status"] != "paused":
            raise ConflictError(f"cannot resume a {job['status']} job")
        self.platform.tasks.resume(ctx, self._task(ctx, job))
        self.store.update(ctx, "email_validation_jobs", job_id, {"status": "queued"})
        audit(self.store, ctx, "email_validation.resume", entity_type="email_validation_jobs", entity_id=job_id)
        return self.get(ctx, job_id)

    def cancel(self, ctx: Ctx, job_id: str) -> Dict[str, Any]:
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        if job["status"] in ("completed", "cancelled", "failed"):
            raise ConflictError(f"the job is already {job['status']}")
        if job.get("task_id"):
            task = self.platform.tasks.get(ctx, job["task_id"])
            if task["status"] not in ("completed", "failed", "cancelled"):
                task = self.platform.tasks.cancel(ctx, job["task_id"])
            if task["status"] == "cancelled":
                self._finish(ctx, job_id, "cancelled")
        else:
            self._finish(ctx, job_id, "cancelled")
        audit(self.store, ctx, "email_validation.cancel", entity_type="email_validation_jobs", entity_id=job_id)
        return self.get(ctx, job_id)

    def delete(self, ctx: Ctx, job_id: str) -> None:
        ctx.require_write()
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        if job["status"] in ("queued", "running"):
            raise ConflictError("cancel the job before deleting it")
        while True:
            rows = self.store.list(ctx, "email_validation_items", {"job_id": job_id}, limit=500).rows
            if not rows:
                break
            for row in rows:
                self.store.delete(ctx, "email_validation_items", row["id"])
        self.store.delete(ctx, "email_validation_jobs", job_id)
        audit(self.store, ctx, "email_validation.delete", entity_type="email_validation_jobs", entity_id=job_id,
              summary=job["name"])

    # --- counts & results ---------------------------------------------------------------

    def counts(self, ctx: Ctx, job_id: str) -> Dict[str, int]:
        grouped = self.store.group_count(ctx, "email_validation_items", "status", {"job_id": job_id})
        out: Dict[str, int] = {status: int(grouped.get(status, 0)) for status in STATUSES}
        pending = int(grouped.get("PENDING", 0))
        out["total"] = sum(int(v) for v in grouped.values())
        out["pending"] = pending
        out["processed"] = out["total"] - pending
        # Provenance filters: rows EmailListVerify answered vs. built-in only (pending rows have no provider).
        by_provider = self.store.group_count(ctx, "email_validation_items", "provider", {"job_id": job_id})
        out["provider_emaillistverify"] = int(by_provider.get("emaillistverify", 0))
        out["provider_local"] = int(by_provider.get("local", 0))
        out["unknown_builtin"] = self.store.count(ctx, "email_validation_items",
                                                  {"job_id": job_id, "status": "UNKNOWN", "provider": "local"})
        # The three user-facing statuses (see engine.final_status).
        out["final_valid"] = self.store.count(ctx, "email_validation_items", {
            "job_id": job_id, "status": "VALID", "provider__in": list(engine.VERIFIERS)})
        out["final_invalid"] = out["INVALID"]
        out["final_not_verified"] = out["processed"] - out["final_valid"] - out["final_invalid"]
        return out

    def _finish(self, ctx: Ctx, job_id: str, status: str, error: Optional[str] = None) -> Dict[str, Any]:
        counts = self.counts(ctx, job_id)
        return self.store.update(ctx, "email_validation_jobs", job_id, {
            "status": status, "counts": counts, "processed": counts["processed"], "finished_at": utcnow(),
            "error": error})

    # --- resolving UNKNOWNs with EmailListVerify (explicit, confirmed, paid) ----------------

    _CANDIDATES = {"status__in": list(engine.PAID_CANDIDATE_STATUSES), "provider": "local"}

    def _catch_all_domains(self, ctx: Ctx, domains: Iterable[str]) -> set:
        """Domains a verifier already reported as catch-all: checking another address there
        cannot confirm a mailbox, so it is not sent (and not charged)."""
        wanted = {engine.sha256(d): d for d in domains if d}
        found = set()
        keys = list(wanted)
        for offset in range(0, len(keys), 500):
            for row in self.store.all(ctx, "email_check_cache", {"subject_hash__in": keys[offset:offset + 500],
                                                                 "check_type": "catch_all"}, cap=500):
                if (row.get("result") or {}).get("catch_all") is True and row["expires_at"] > utcnow():
                    found.add(wanted[row["subject_hash"]])
        return found

    def _unresolved_emails(self, ctx: Ctx, job_id: str) -> Tuple[List[str], int]:
        """Distinct NOT VERIFIED addresses only the built-in checks answered (unknown, role,
        free provider), in row order, and how many sit on a known catch-all domain (skipped)."""
        seen: Dict[str, str] = {}
        offset = 0
        while True:
            rows = self.store.list(ctx, "email_validation_items", {"job_id": job_id, **self._CANDIDATES},
                                   order="row_number", limit=500, offset=offset).rows
            if not rows:
                break
            for row in rows:
                if row.get("email"):
                    seen.setdefault(row["email"], row.get("domain") or row["email"].rpartition("@")[2])
            offset += len(rows)
        catch_all = self._catch_all_domains(ctx, set(seen.values()))
        emails = [e for e, d in seen.items() if d not in catch_all]
        return emails, len(seen) - len(emails)

    def unknowns_estimate(self, ctx: Ctx, job_id: str) -> Dict[str, Any]:
        """Exactly what :meth:`verify_unknowns` would spend: built-in UNKNOWNs that EmailListVerify
        has not answered, minus addresses with a fresh EmailListVerify answer in the cache (those
        are reused for free). Already-resolved rows (VALID, INVALID, RISKY, ...) are never sent."""
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        settings = job.get("settings") or {}
        max_age = int(settings.get("max_age_days") or 30)
        emails, skipped_catch_all = self._unresolved_emails(ctx, job_id)
        cutoff = utcnow() - timedelta(days=max_age)
        reusable = 0
        for offset in range(0, len(emails), 500):
            chunk = emails[offset:offset + 500]
            for row in self.store.all(ctx, "email_validations", {"email__in": chunk, "provider": "emaillistverify"},
                                      cap=len(chunk)):
                if row["validated_at"] >= cutoff:
                    reusable += 1
        registry = self.platform.service("providers")
        conn_settings = (registry.connection(ctx, "emaillistverify") or {}).get("settings") or {}
        cost = float(conn_settings.get("cost_per_check") or 1.0)
        balance = self.platform.service("credits").balance(ctx, "emaillistverify")
        to_check = len(emails) - reusable
        credits = round(to_check * cost, 4)
        active = elv_enabled(registry, ctx)
        blocker = None
        if job["status"] != "completed":
            blocker = "Finish the built-in run first; unknowns can be verified on a completed job."
        elif not active:
            blocker = "EmailListVerify is not configured and verified (Sources → providers)."
        elif not emails:
            blocker = "There are no unresolved unknowns to verify."
        elif credits > 0 and not balance.get("known"):
            blocker = "The EmailListVerify balance is unknown; refresh it (a free call) before spending."
        elif credits > 0 and balance.get("remaining", 0) < credits:
            blocker = f"Only {balance.get('remaining', 0):g} EmailListVerify credits remain; {credits:g} are needed."
        disposable = self.store.count(ctx, "email_validation_items",
                                      {"job_id": job_id, "status": "DISPOSABLE", "provider": "local"})
        return {"job_id": job_id, "unresolved": len(emails), "reused_from_cache": reusable, "to_check": to_check,
                "skipped_catch_all": skipped_catch_all, "skipped_disposable": disposable,
                "cost_per_check": cost, "credits": credits, "provider_active": active,
                "credits_known": bool(balance.get("known")), "credits_remaining": balance.get("remaining"),
                "can_verify": blocker is None, "blocker": blocker}

    def verify_unknowns(self, ctx: Ctx, job_id: str, *, confirm: bool, expected_credits: Any) -> Dict[str, Any]:
        """Send the job's unresolved UNKNOWNs (and only those) to EmailListVerify. The caller must
        confirm the exact credit figure from :meth:`unknowns_estimate`; if it changed, nothing runs."""
        ctx.require_write()
        estimate = self.unknowns_estimate(ctx, job_id)
        if estimate["blocker"]:
            raise ValidationError(estimate["blocker"])
        try:
            expected = float(expected_credits)
        except (TypeError, ValueError):
            expected = None
        if not confirm or expected is None or abs(expected - estimate["credits"]) > 1e-9:
            raise ConflictError(f"confirm spending exactly {estimate['credits']:g} EmailListVerify credits on "
                                f"{estimate['to_check']} address(es) (estimate changed or not confirmed)")
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        send = set(self._unresolved_emails(ctx, job_id)[0])
        offset = 0
        while True:  # candidates that are sent become PENDING; the rest (catch-all) keep their result
            rows = self.store.list(ctx, "email_validation_items", {"job_id": job_id, **self._CANDIDATES},
                                   order="row_number", limit=500, offset=offset).rows
            if not rows:
                break
            kept = 0
            for row in rows:
                if row.get("email") in send:
                    self.store.update(ctx, "email_validation_items", row["id"], {"status": "PENDING"})
                else:
                    kept += 1
            offset += kept  # reset rows left the filter; skip only the ones that stayed
        settings = {**(job.get("settings") or {}), "allow_paid": True, "recheck_unknowns": True,
                    "paid_statuses": list(engine.PAID_CANDIDATE_STATUSES)}
        task = self.platform.tasks.submit(ctx, "email_validation_job", {"job_id": job_id},
                                          entity_type="email_validation_jobs", entity_id=job_id)
        job = self.store.update(ctx, "email_validation_jobs", job_id, {
            "status": "queued", "task_id": task["id"], "settings": settings, "finished_at": None})
        audit(self.store, ctx, "email_validation.verify_unknowns", entity_type="email_validation_jobs",
              entity_id=job_id, changes={"unresolved": estimate["unresolved"], "to_check": estimate["to_check"],
                                         "credits": estimate["credits"]})
        return {**job, "estimate": estimate}

    def set_strict(self, ctx: Ctx, job_id: str, enabled: bool) -> Dict[str, Any]:
        """Strict validation: VALID only when EmailListVerify verified the mailbox."""
        ctx.require_write()
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        return self.store.update(ctx, "email_validation_jobs", job_id, {
            "settings": {**(job.get("settings") or {}), "strict_validation": bool(enabled)}})

    def items(self, ctx: Ctx, job_id: str, filters: Optional[Mapping[str, Any]] = None, *, limit: int = 50,
              offset: int = 0, order: Optional[str] = None):
        self.store.get(ctx, "email_validation_jobs", job_id)
        clean = {k: v for k, v in (filters or {}).items() if k != "job_id"}
        page = self.store.list(ctx, "email_validation_items", {**clean, "job_id": job_id}, order=order,
                               limit=min(max(1, limit), 500), offset=max(0, offset))
        for row in page.rows:
            if row.get("status") != "PENDING":
                row["final_status"] = engine.final_status(row.get("status"), row.get("provider"))
                row["summary"] = engine.evidence_summary(row.get("status"), row.get("provider"), row.get("checks"))
        return page

    def _iter_items(self, ctx: Ctx, job_id: str, statuses: Optional[Sequence[str]] = None, *,
                    provider: Optional[str] = None) -> Iterable[Dict[str, Any]]:
        filters: Dict[str, Any] = {"job_id": job_id}
        if statuses:
            filters["status__in"] = list(statuses)
        if provider:
            filters["provider"] = provider
        offset = 0
        while True:
            rows = self.store.list(ctx, "email_validation_items", filters, order="row_number", limit=500,
                                   offset=offset).rows
            if not rows:
                return
            yield from rows
            offset += len(rows)

    def export(self, ctx: Ctx, job_id: str, fmt: str = "csv", status: Optional[Sequence[str]] = None,
               provider: Optional[str] = None) -> Tuple[str, bytes, str]:
        """``(filename, content, media type)``: the original columns plus the result."""
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        if fmt not in ("csv", "xlsx"):
            raise ValidationError("format must be csv or xlsx")
        statuses = [s for s in (status or []) if s]
        for s in statuses:
            if s not in STATUSES + ("PENDING",):
                raise ValidationError(f"unknown status {s!r}")
        if provider not in (None, "", "local", "emaillistverify"):
            raise ValidationError(f"unknown provider {provider!r}")
        columns = list(job.get("columns") or [])
        extra = ["validation_email", "validation_status", "validation_detail", "validation_score",
                 "validation_reason", "validation_provider", "validation_source", "validated_at"] + [
                     f"signal_{key}" for key in _EXPORT_SIGNALS]
        header = columns + [c for c in extra if c not in columns]

        def records():
            for item in self._iter_items(ctx, job_id, statuses, provider=provider or None):
                row = item.get("row") or {}
                values = [row.get(c, "") for c in columns]
                pending = item["status"] == "PENDING"
                summary = {} if pending else engine.evidence_summary(item["status"], item.get("provider"),
                                                                      item.get("checks"))
                values += [item.get("email") or "",
                           "PENDING" if pending else engine.final_status(item["status"], item.get("provider")),
                           item["status"], "" if item.get("score") is None else item["score"],
                           reason_for(item["status"], item.get("checks") or {}), item.get("provider") or "",
                           "" if pending else summary["source"],
                           item["validated_at"].isoformat() if item.get("validated_at") else ""]
                values += [summary.get(key, "") for key in _EXPORT_SIGNALS]
                yield [str(v) if v is not None else "" for v in values]

        base = re.sub(r"[^A-Za-z0-9._-]+", "-", job["name"]).strip("-")[:60] or "validation"
        suffix = f"-{'-'.join(s.lower() for s in statuses)}" if statuses else ""
        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(header)
            for values in records():
                writer.writerow([("'" + v) if v[:1] in ("=", "+", "-", "@") else v for v in values])
            content = buffer.getvalue().encode("utf-8-sig")
            media = "text/csv"
        else:
            from openpyxl import Workbook

            workbook = Workbook(write_only=True)
            sheet = workbook.create_sheet("Validation")
            sheet.append(header)
            for values in records():
                sheet.append(values)
            out = io.BytesIO()
            workbook.save(out)
            content = out.getvalue()
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        audit(self.store, ctx, "email_validation.export", entity_type="email_validation_jobs", entity_id=job_id,
              changes={"format": fmt, "statuses": statuses})
        return f"{base}{suffix}.{fmt}", content, media

    # --- running (worker) -----------------------------------------------------------------

    def run(self, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
        from cloud.intel.tasks.worker import TaskPaused

        job_id = task["params"]["job_id"]
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        if job["status"] == "cancelled":
            return {"job_id": job_id, "status": "cancelled"}
        settings = job.get("settings") or {}
        column = job.get("email_column") or "email"
        email_service = self.platform.service("email")
        evidence = _EvidenceRun(self, ctx, settings)
        self.store.update(ctx, "email_validation_jobs", job_id, {
            "status": "running", "started_at": job.get("started_at") or utcnow()})
        last_progress = 0.0
        while True:
            if reporter.is_cancelled():
                self._finish(ctx, job_id, "cancelled")
                return {"job_id": job_id, "status": "cancelled", "counts": self.counts(ctx, job_id)}
            if reporter.should_pause():
                counts = self.counts(ctx, job_id)
                self.store.update(ctx, "email_validation_jobs", job_id, {"status": "paused", "counts": counts,
                                                                          "processed": counts["processed"]})
                raise TaskPaused({"processed": counts["processed"]})
            batch = self.store.list(ctx, "email_validation_items", {"job_id": job_id, "status": "PENDING"},
                                    order="row_number", limit=BATCH).rows
            if not batch:
                break
            self._process(ctx, email_service, batch, column, settings, task_id=task.get("id"), evidence=evidence)
            if time.monotonic() - last_progress >= PROGRESS_SECONDS:  # counting a huge job every batch is waste
                last_progress = time.monotonic()
                counts = self.counts(ctx, job_id)
                self.store.update(ctx, "email_validation_jobs", job_id, {"counts": counts,
                                                                          "processed": counts["processed"]})
                reporter.progress(f"validated {counts['processed']} of {counts['total']}",
                                  done=counts["processed"], total=counts["total"], counts=counts)
        final = self._finish(ctx, job_id, "completed")
        if settings.get("recheck_unknowns"):  # a confirmed recheck authorises one paid run, not future ones
            final = self.store.update(ctx, "email_validation_jobs", job_id, {
                "settings": {**settings, "allow_paid": False, "recheck_unknowns": False, "paid_statuses": None,
                             "last_recheck_at": utcnow().isoformat()}})
        audit(self.store, ctx, "email_validation.complete", entity_type="email_validation_jobs", entity_id=job_id,
              summary=f"{final['counts'].get('processed', 0)} row(s) validated", changes={"counts": final["counts"]})
        self._notify(ctx, final)
        try:  # workflows on "validation_job_completed"; best-effort
            self.platform.service("automation").emit(ctx, "validation_job_completed", f"evj:{job_id}",
                                                     {"job_id": job_id, "counts": final["counts"],
                                                      "list_id": final.get("source_id")
                                                      if final.get("source_type") == "list" else None})
        except Exception:  # noqa: BLE001
            log.debug("validation_job_completed emit skipped", exc_info=True)
        return {"job_id": job_id, "status": "completed", "counts": final["counts"]}

    def _process(self, ctx: Ctx, email_service: Any, batch: List[Dict[str, Any]], column: str,
                 settings: Mapping[str, Any], *, task_id: Optional[str], evidence: Optional[_EvidenceRun] = None
                 ) -> None:
        evidence = evidence or _EvidenceRun(self, ctx, settings)
        emails: Dict[str, str] = {}
        originals: Dict[str, str] = {}
        for item in batch:
            original = extract_email((item.get("row") or {}).get(column))
            originals[item["id"]] = original
            emails[item["id"]] = engine.to_ascii_email(original)[0] if original else ""  # IDN -> punycode
        wanted = [e for e in emails.values() if e]
        results = {}
        if wanted:
            paid_statuses = tuple(settings.get("paid_statuses") or ("UNKNOWN",))
            for result in email_service.validate(ctx, wanted, allow_paid=bool(settings.get("allow_paid")),
                                                 max_age_days=int(settings.get("max_age_days") or 30),
                                                 task_id=task_id, paid_statuses=paid_statuses):
                results[result["email"]] = result
        known_contacts = {}
        lookup = sorted({_key(e) for e in wanted if normalize_email(_key(e))})
        if lookup:  # one query per batch rather than one per row
            for contact in self.store.all(ctx, "contacts", {"email__in": lookup}, cap=len(lookup) * 2):
                known_contacts.setdefault(contact["email"], contact["id"])
        now = utcnow()
        for item in batch:
            raw = emails[item["id"]]
            if not raw:
                changes = {"status": "INVALID", "score": 0.0, "provider": "local", "validated_at": now,
                           "checks": {"syntax": False, "empty": True}, "email": None, "domain": None}
            else:
                key = _key(raw)
                result = results.get(key)
                if result is None:  # never expected; keep the row honest rather than guessing
                    changes = {"status": "UNKNOWN", "score": None, "provider": "local", "validated_at": now,
                               "checks": {"error": "no result returned"}, "email": key}
                else:
                    changes = {"status": result["status"], "score": result.get("score"),
                               "provider": result.get("provider"), "checks": result.get("checks") or {},
                               "cached": bool(result.get("cached")), "validated_at": result.get("validated_at") or now,
                               "email": result["email"][:320]}
                    if (settings.get("strict_validation", True) and changes["status"] == "VALID"
                            and changes["provider"] not in engine.VERIFIERS):
                        # Strict validation: VALID means "a mailbox verifier confirmed it", nothing less.
                        changes.update(status="UNKNOWN", score=None,
                                       checks={**changes["checks"], "strict_demoted": True})
                    self._add_evidence(changes, originals[item["id"]], key, item.get("row") or {}, settings,
                                       evidence, fresh=not result.get("cached"))
                domain = key.rpartition("@")[2] if "@" in key else None
                changes["domain"] = (domain or None) and domain[:253]
                if not item.get("contact_id") and key in known_contacts:
                    changes["contact_id"] = known_contacts[key]
            self.store.update(ctx, "email_validation_items", item["id"], changes)

    def _add_evidence(self, changes: Dict[str, Any], original: str, email: str, row: Mapping[str, Any],
                      settings: Mapping[str, Any], evidence: _EvidenceRun, *, fresh: bool) -> None:
        """Layers 1-8 for one address, stored as ``checks["evidence"]``. Only a domain that
        provably takes no mail (NXDOMAIN, null MX, nothing to deliver to) or a malformed address
        changes the result, to INVALID; everything else is evidence."""
        checks = dict(changes.get("checks") or {})
        fmt = engine.check_format(original)
        ev: Dict[str, Any] = {"format": {k: fmt[k] for k in ("ok", "idn", "issues", "placeholder", "did_you_mean")}}
        provider = changes.get("provider")
        if not fmt["ok"]:
            if changes["status"] != "INVALID" and provider not in engine.VERIFIERS:
                changes.update(status="INVALID", score=0.0)
                checks["syntax"] = False
        elif "@" in email:
            domain = email.rpartition("@")[2]
            dom = evidence.domain(domain)
            ev["domain"] = {k: dom.get(k) for k in ("exists", "dns_error", "mail", "null_mx", "a_fallback", "spf",
                                                     "spf_all", "dmarc", "dmarc_policy", "verdict", "invalid_reason")}
            ev["domain"]["mx_hosts"] = len(dom.get("mx") or [])
            if dom.get("verdict") == "invalid" and changes["status"] != "INVALID" and provider not in engine.VERIFIERS:
                changes.update(status="INVALID", score=0.0)
                checks["mx"] = False
                checks["domain_invalid"] = dom.get("invalid_reason")
            contact_columns = settings.get("contact_columns") or {}
            contact = {name: row.get(col) for name, col in contact_columns.items()}
            first, last = engine._names(contact)  # noqa: SLF001 - same module family
            ev["risk"] = engine.risk_signals(email, first_name=first, last_name=last)
            if dom.get("mx"):
                smtp = evidence.smtp_check(dom["mx"][0])
                if smtp is not None:
                    ev["smtp"] = smtp
            if evidence.finder is not None:
                site = engine._host_of(str(contact.get("website") or "")) or (  # noqa: SLF001
                    domain if not ev["risk"]["free_provider"] else "")
                scan = evidence.public_scan(site) if site else None
                ev["public"] = engine.public_evidence_for(email, scan, contact,
                                                          checked_at=(scan or {}).get("checked_at"))
            if settings.get("contact_mode"):
                ev["contact"] = engine.contact_signals(email, contact, public_evidence=ev.get("public"))
            learned = engine.catch_all_from(provider, checks)
            if learned is not None and fresh:
                evidence.learn_catch_all(domain, learned)
            ev["catch_all"] = learned if learned is not None else evidence.catch_all(domain)
            if provider in engine.VERIFIERS and fresh:
                evidence.remember("mailbox:" + str(provider), email, "email",
                                  {"status": changes["status"], "result_code": checks.get("result_code")},
                                  source=engine.VERIFIERS[provider]["label"], provider=provider, ttl_type="mailbox")
        checks["evidence"] = ev
        changes["checks"] = checks

    def _notify(self, ctx: Ctx, job: Mapping[str, Any]) -> None:
        try:
            counts = job.get("counts") or {}
            self.store.insert(ctx, "notifications", {
                "user_id": job.get("created_by"), "kind": "email_validation.completed", "severity": "success",
                "title": f"Email validation finished: {job['name']}"[:300],
                "body": f"{counts.get('processed', 0)} checked · {counts.get('VALID', 0)} valid · "
                        f"{counts.get('INVALID', 0)} invalid · {counts.get('UNKNOWN', 0)} unknown",
                "link": f"/email-validation/{job['id']}", "entity_type": "email_validation_jobs",
                "entity_id": job["id"]})
        except Exception:  # noqa: BLE001 - a notification must never fail the job
            log.debug("validation notification skipped", exc_info=True)

    # --- GTM actions (explicit user actions only) -------------------------------------------

    def _contacts_for(self, ctx: Ctx, job_id: str, statuses: Sequence[str], *, create_missing: bool,
                      job_name: str) -> Dict[str, Any]:
        contact_ids: List[str] = []
        missing = created = 0
        for item in self._iter_items(ctx, job_id, statuses):
            contact_id = item.get("contact_id")
            email = item.get("email")
            if contact_id and self.store.find(ctx, "contacts", contact_id) is None:
                contact_id = None
            if not contact_id and email and normalize_email(email):
                contact = self.store.first(ctx, "contacts", {"email": normalize_email(email)})
                contact_id = contact["id"] if contact else None
            if not contact_id and create_missing and email and normalize_email(email):
                result = self.platform.service("crm").upsert_contact(
                    ctx, {"full_name": _name_from(item.get("row") or {}, email), "email": email},
                    source_kind="email_validation", source_name=f"Email validation: {job_name}"[:200],
                    original=item.get("row") or {}, row_number=item["row_number"])
                contact_id = result["contact"]["id"]
                created += 1 if result.get("created") else 0
            if contact_id:
                if contact_id not in contact_ids:
                    contact_ids.append(contact_id)
                if item.get("contact_id") != contact_id:
                    self.store.update(ctx, "email_validation_items", item["id"], {"contact_id": contact_id})
            else:
                missing += 1
        return {"contact_ids": contact_ids, "not_in_crm": missing, "created_contacts": created}

    @staticmethod
    def _statuses(statuses: Optional[Sequence[str]]) -> List[str]:
        chosen = [s for s in (statuses or ["VALID"]) if s]
        for s in chosen:
            if s not in STATUSES:
                raise ValidationError(f"unknown status {s!r}")
        return chosen

    def add_to_list(self, ctx: Ctx, job_id: str, *, list_id: Optional[str] = None, list_name: Optional[str] = None,
                    statuses: Optional[Sequence[str]] = ("VALID",), create_missing_contacts: bool = False
                    ) -> Dict[str, Any]:
        """Put the contacts behind the chosen results into a contact list. Rows that are
        not in the CRM are skipped (and counted) unless ``create_missing_contacts``."""
        ctx.require_write()
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        chosen = self._statuses(statuses)
        crm = self.platform.service("crm")
        if list_id:
            target = self.store.get(ctx, "lists", list_id)
        elif list_name and list_name.strip():
            existing = self.store.first(ctx, "lists", {"name": list_name.strip()[:200]})
            target = existing or crm.create_list(ctx, list_name.strip()[:200], "contacts",
                                                 description=f"From email validation {job['name']}",
                                                 source="email_validation")
        else:
            raise ValidationError("choose a list or give a new list name")
        if target["entity_type"] != "contacts":
            raise ValidationError(f"list {target['name']} holds {target['entity_type']}, not contacts")
        found = self._contacts_for(ctx, job_id, chosen, create_missing=create_missing_contacts, job_name=job["name"])
        added = crm.add_to_list(ctx, target["id"], "contacts", found["contact_ids"],
                                reason=f"email validation {job['name']}: {', '.join(chosen)}"[:500]) \
            if found["contact_ids"] else 0
        audit(self.store, ctx, "email_validation.add_to_list", entity_type="email_validation_jobs", entity_id=job_id,
              changes={"list_id": target["id"], "added": added, "statuses": chosen, **{
                  k: found[k] for k in ("not_in_crm", "created_contacts")}})
        return {"list": self.store.get(ctx, "lists", target["id"]), "added": added,
                "matched": len(found["contact_ids"]), "not_in_crm": found["not_in_crm"],
                "created_contacts": found["created_contacts"]}

    def create_campaign(self, ctx: Ctx, job_id: str, *, name: str, statuses: Optional[Sequence[str]] = ("VALID",),
                        create_missing_contacts: bool = False, list_name: Optional[str] = None) -> Dict[str, Any]:
        """A **draft** campaign whose audience is a new list of the chosen results.
        Sending stays off; nothing is enrolled or sent."""
        ctx.require_write()
        if not (name or "").strip():
            raise ValidationError("the campaign needs a name")
        listed = self.add_to_list(ctx, job_id, list_name=list_name or f"{name.strip()} audience",
                                  statuses=statuses, create_missing_contacts=create_missing_contacts)
        key = _slug(name)
        suffix = 1
        while self.store.first(ctx, "campaigns", {"key": key}) is not None:
            suffix += 1
            key = f"{_slug(name)[:36]}-{suffix}"
        campaign = self.store.insert(ctx, "campaigns", {
            "key": key, "name": name.strip()[:200], "status": "draft", "sending_enabled": False,
            "description": f"Audience from email validation job {job_id}",
            "audience": {"list_ids": [listed["list"]["id"]], "source": "email_validation", "job_id": job_id},
            "list_id": listed["list"]["id"], "owner_id": ctx.user_id})
        audit(self.store, ctx, "campaigns.create", entity_type="campaigns", entity_id=campaign["id"],
              summary=campaign["name"], changes={"from_validation_job": job_id})
        return {"campaign": campaign, **{k: v for k, v in listed.items() if k != "list"}, "list": listed["list"]}

    def enroll(self, ctx: Ctx, job_id: str, *, sequence_id: str, statuses: Optional[Sequence[str]] = ("VALID",),
               campaign_id: Optional[str] = None) -> Dict[str, Any]:
        """Enroll the matching CRM contacts. Enrollments start ``pending_approval``;
        suppressed / unsubscribed / undeliverable contacts are skipped by the sequence."""
        ctx.require_write()
        job = self.store.get(ctx, "email_validation_jobs", job_id)
        found = self._contacts_for(ctx, job_id, self._statuses(statuses), create_missing=False, job_name=job["name"])
        results = self.platform.service("sequences").enroll(ctx, sequence_id, found["contact_ids"],
                                                            campaign_id=campaign_id) if found["contact_ids"] else []
        enrolled = sum(1 for r in results if r["status"] == "enrolled")
        audit(self.store, ctx, "email_validation.enroll", entity_type="email_validation_jobs", entity_id=job_id,
              changes={"sequence_id": sequence_id, "enrolled": enrolled})
        return {"enrolled": enrolled, "skipped": [r for r in results if r["status"] != "enrolled"][:200],
                "not_in_crm": found["not_in_crm"], "pending_approval": True}

    # --- provider status --------------------------------------------------------------------

    def provider_status(self, ctx: Ctx) -> Dict[str, Any]:
        registry = self.platform.service("providers")
        row = registry.connection(ctx, "emaillistverify")
        configured = registry.configured(ctx, "emaillistverify")
        verified = elv_enabled(registry, ctx)
        settings = (row or {}).get("settings") or {}
        ledger = self.platform.service("credits")
        usage = ledger.usage(ctx, "emaillistverify").get("operations", {}).get("emaillistverify:verify_email",
                                                                                {"calls": 0, "units": 0,
                                                                                 "failures": 0})
        return {
            "local": {"status": "active", "checks": list(LOCAL_CHECKS),
                      "note": "Built-in checks are free and always run. They never mark an address VALID: "
                              "without a mailbox-level check (no SMTP probing), a good address is UNKNOWN."},
            "emaillistverify": {
                "provider": "emaillistverify", "label": "EmailListVerify",
                "status": "active" if verified else ("configured_unverified" if configured else "not_configured"),
                "configured": configured, "verified": verified, "secret_hint": (row or {}).get("secret_hint") if configured else None,
                "last_checked_at": (row or {}).get("last_checked_at"), "last_error": (row or {}).get("last_error"),
                "cost_per_check": float(settings.get("cost_per_check") or 1.0),
                "credits": ledger.balance(ctx, "emaillistverify"), "usage": usage,
                "requirement": "an EmailListVerify API key with purchased credits (Sources → providers)"},
        }

    def test_provider(self, ctx: Ctx) -> Dict[str, Any]:
        """The free credit-balance call, only when a key is stored. No key: no network call."""
        ctx.require_write()
        registry = self.platform.service("providers")
        if registry.connection(ctx, "emaillistverify") is None or not registry.get_secrets(
                ctx, "emaillistverify").get("api_key"):
            return {"provider": "emaillistverify", "status": "not_configured",
                    "detail": "no EmailListVerify key is stored; built-in validation still works"}
        result = registry.verify(ctx, "emaillistverify")
        if result.get("status") == "ok" and isinstance(result.get("credits"), (int, float)):
            # The same free call reports the balance: record it so paid checks can reserve credits.
            self.platform.service("credits").sync(ctx, "emaillistverify", remaining=float(result["credits"]),
                                                  source="EmailListVerify /api/credits")
        return result


def run_validation_job_task(platform: Any, ctx: Ctx, task: Dict[str, Any], reporter: Any) -> Dict[str, Any]:
    return platform.service("email_jobs").run(ctx, task, reporter)
