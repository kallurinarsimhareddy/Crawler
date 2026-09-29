"""Internal data: many CSV/XLSX files from inside the company, compared, mapped
explicitly, merged with provenance, and reviewed where they disagree with the CRM.

Built on :class:`~cloud.intel.imports.service.ImportService` (upload, parse,
explicit mapping, the resumable ``import_merge`` task) and adds what a 12–30 file
batch needs:

* **Schema comparison** (:meth:`InternalDataService.compare_schema`): the union of
  every file's columns, which files have each one, per-file present / missing /
  extra columns, a type hint per column from sampled values, and a schema
  signature per file so identical layouts group together.
* **Mapping review** (:meth:`InternalDataService.mapping_review`): each column is
  ``confident``, ``ambiguous`` or ``unmapped``. Ambiguous columns (a bare "Name",
  a low-confidence guess, two columns in one file pointing at one field, values
  that contradict the suggested field) **must** be decided explicitly —
  :meth:`apply_mapping` refuses a mapping that leaves one undecided. Nothing is
  ever mapped because it looked likely.
* **Per-file mapping**: a column that means different things in different files
  can be mapped per file (``import_files.mapping``); the merge uses it.
* **Conflict review**: when a row matches an existing company or contact whose
  stored value differs, the merge keeps the stored value, records the
  disagreement in ``import_rows.conflicts`` and marks the row ``conflict``. A
  person keeps the existing value, takes the new one, or types another
  (:meth:`resolve_conflicts`); every decision is audited with provenance.
* **Import history** (:meth:`history`).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cloud.intel.core.audit import audit, provenance
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow
from cloud.intel.imports.parse import ParseError, iter_rows
from cloud.intel.imports.service import _LIST_FIELDS, _SYNONYMS, MAX_FILES, _key, _valid_targets

__all__ = ["InternalDataService", "merge_conflicts", "run_internal_data_task", "type_hint", "value_conflicts",
           "MIN_FILES_HINT"]

#: The batch sizes this workflow is designed around (a batch may still hold 1).
MIN_FILES_HINT = 12
SAMPLE_ROWS = 50
CONFIDENT = 0.85
#: Headers that never map without a person saying what they mean.
_AMBIGUOUS_HEADERS = {"name", "linkedin", "linkedinurl", "url", "link", "phone", "location", "type", "status",
                      "owner", "source", "notes", "id"}
#: Company/contact fields compared for conflicts (lists merge by union, names become aliases).
_NO_CONFLICT = {"name", "full_name", "aliases", "tags", "technologies", "sic_codes", "naics_codes", "custom_fields",
                "first_seen_at", "last_seen_at", "source_count", "company_id", "source", "source_date",
                # derived from other fields, not facts of their own
                "normalized_name", "function", "seniority", "first_name", "last_name"}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
_URL = re.compile(r"^(https?://|www\.)", re.I)
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)
_PHONE = re.compile(r"^\+?[\d\s().-]{7,20}$")
_NUMBER = re.compile(r"^\$?-?[\d,]+(\.\d+)?\s*[kmb]?$", re.I)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$|^\d{1,2}/\d{1,2}/\d{2,4}$")
_BOOL = {"true", "false", "yes", "no", "y", "n"}

#: What each target field expects, for spotting a contradicting suggestion.
_EXPECTS = {"email": {"email"}, "website": {"url", "domain"}, "domain": {"domain", "url"},
            "careers_url": {"url"}, "linkedin_url": {"url"}, "employee_count": {"number"},
            "revenue_usd": {"number"}, "phone": {"phone", "number"}}


def type_hint(values: Iterable[str]) -> str:
    """The dominant kind of a column's sampled values: email, url, domain, phone,
    number, date, boolean, text, or empty. Needs 80% agreement, else ``text``."""
    kinds: Counter = Counter()
    total = 0
    for raw in values:
        value = (raw or "").strip()
        if not value:
            continue
        total += 1
        if _EMAIL.match(value):
            kinds["email"] += 1
        elif _URL.match(value):
            kinds["url"] += 1
        elif value.lower() in _BOOL:
            kinds["boolean"] += 1
        elif _DATE.match(value):
            kinds["date"] += 1
        elif _NUMBER.match(value):
            kinds["number"] += 1
        elif _PHONE.match(value) and sum(c.isdigit() for c in value) >= 7:
            kinds["phone"] += 1
        elif _DOMAIN.match(value):
            kinds["domain"] += 1
        else:
            kinds["text"] += 1
    if total == 0:
        return "empty"
    kind, count = kinds.most_common(1)[0]
    return kind if count / total >= 0.8 else "text"


def _signature(columns: Sequence[str]) -> str:
    return hashlib.sha256("|".join(sorted(_key(c) for c in columns)).encode()).hexdigest()


def value_conflicts(existing: Mapping[str, Any], incoming: Mapping[str, Any], prefix: str) -> List[Dict[str, Any]]:
    """Fields where the stored value and the incoming value are both present and differ."""
    out = []
    for field, value in incoming.items():
        if field in _NO_CONFLICT or value in (None, "", [], {}):
            continue
        current = existing.get(field)
        if current in (None, "", [], {}):
            continue
        if isinstance(current, str) and isinstance(value, str):
            same = current.strip().lower() == value.strip().lower()
        else:
            same = current == value
        if not same:
            out.append({"field": f"{prefix}.{field}", "existing": current, "incoming": value, "resolution": None})
    return out


def merge_conflicts(crm: Any, ctx: Ctx, entity: str, existing: Mapping[str, Any],
                    values: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Conflicts between a matched record and one row's values, normalised the way the CRM
    stores them. A row the CRM cannot normalise yields none (the merge already handled it)."""
    try:
        if entity == "company":
            clean = crm._normalize_company(ctx, values)
        else:
            clean = crm._normalize_contact(ctx, {k: v for k, v in values.items() if k != "company_id"})
    except ValidationError:
        return []
    return value_conflicts(existing, clean, entity)


class InternalDataService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    @property
    def store(self):
        return self.platform.store

    @property
    def imports(self):
        return self.platform.service("imports")

    # --- batches and files ------------------------------------------------------------

    def create_batch(self, ctx: Ctx, name: str, target: str = "companies_and_contacts") -> Dict[str, Any]:
        batch = self.imports.create_batch(ctx, name, target)
        audit(self.store, ctx, "internal_data.create", entity_type="import_batches", entity_id=batch["id"],
              summary=name)
        return batch

    def add_files(self, ctx: Ctx, batch_id: str, files: Sequence[Tuple[str, bytes]]) -> Dict[str, Any]:
        """Add several files; one unreadable or duplicate file never stops the others."""
        ctx.require_write()
        batch = self.store.get(ctx, "import_batches", batch_id)
        if batch["file_count"] + len(files) > MAX_FILES:
            raise ValidationError(f"a batch holds at most {MAX_FILES} files ({batch['file_count']} already added)")
        added, failed = [], []
        for filename, data in files:
            try:
                added.append(self.imports.add_file(ctx, batch_id, filename, data))
            except (ValidationError, ConflictError) as error:
                failed.append({"filename": filename, "error": str(error)})
        return {"batch_id": batch_id, "added": added, "failed": failed, "file_count": batch["file_count"] + len(added)}

    def _sample(self, file_row: Mapping[str, Any]) -> Dict[str, List[str]]:
        samples: Dict[str, List[str]] = {c: [] for c in file_row["columns"]}
        if not file_row.get("storage_key") or not file_row["columns"]:
            return samples
        try:
            with self.platform.storage.open(file_row["storage_key"]) as handle:
                data = handle.read()
            for _number, row in iter_rows(file_row["format"], data, sheet=file_row.get("sheet"),
                                          max_rows=SAMPLE_ROWS):
                for column, value in row.items():
                    if column in samples and value:
                        samples[column].append(value)
        except (ParseError, OSError, KeyError):
            pass
        return samples

    # --- schema comparison --------------------------------------------------------------

    def compare_schema(self, ctx: Ctx, batch_id: str) -> Dict[str, Any]:
        """Union schema, per-file present/missing/extra columns, type hints, layout groups."""
        batch = self.store.get(ctx, "import_batches", batch_id)
        files = self.imports.files(ctx, batch_id)
        if not files:
            raise ValidationError("upload at least one file first")
        union: Dict[str, Dict[str, Any]] = {}
        per_file = []
        signatures: Dict[str, List[str]] = {}
        for f in files:
            samples = self._sample(f)
            signature = _signature(f["columns"])
            signatures.setdefault(signature, []).append(f["filename"])
            if f.get("schema_signature") != signature:
                self.store.update(ctx, "import_files", f["id"], {"schema_signature": signature})
            hints = {}
            for column in f["columns"]:
                key = _key(column)
                hint = type_hint(samples.get(column, []))
                hints[column] = hint
                entry = union.setdefault(key, {"column": column, "variants": [], "files": [], "type_hints": [],
                                               "samples": []})
                if column not in entry["variants"]:
                    entry["variants"].append(column)
                entry["files"].append(f["filename"])
                if hint != "empty":
                    entry["type_hints"].append(hint)
                entry["samples"].extend(v[:80] for v in samples.get(column, [])[:3] if len(entry["samples"]) < 5)
            per_file.append({"file_id": f["id"], "filename": f["filename"], "format": f["format"],
                             "rows": f["row_count"], "status": f["status"], "signature": signature,
                             "present": list(f["columns"]), "hints": hints})
        common = {k for k, v in union.items() if len(set(v["files"])) == len(files)}
        for item in per_file:
            keys = {_key(c) for c in item["present"]}
            item["missing"] = [union[k]["column"] for k in union if k not in keys]
            item["extra"] = [c for c in item["present"] if _key(c) not in common]
        columns = []
        for key, entry in union.items():
            hints = Counter(entry["type_hints"])
            hint = hints.most_common(1)[0][0] if hints else "empty"
            columns.append({"key": key, "column": entry["column"], "variants": entry["variants"],
                            "files_present": len(set(entry["files"])), "files_total": len(files),
                            "in_all_files": key in common, "type_hint": hint,
                            "type_conflict": len(hints) > 1, "samples": entry["samples"]})
        report = {"batch_id": batch_id, "file_count": len(files), "column_count": len(columns),
                  "common_columns": sorted(union[k]["column"] for k in common), "columns": columns,
                  "files": per_file,
                  "layouts": [{"signature": sig, "files": names} for sig, names in signatures.items()],
                  "note": (f"{len(files)} file(s); batches of {MIN_FILES_HINT}–{MAX_FILES} files are supported"),
                  "compared_at": utcnow().isoformat()}
        self.store.update(ctx, "import_batches", batch_id, {"schema_report": report})
        return report

    # --- mapping review -------------------------------------------------------------------

    def mapping_review(self, ctx: Ctx, batch_id: str) -> Dict[str, Any]:
        """Every column as ``confident``, ``ambiguous`` (a person must decide) or ``unmapped``."""
        batch = self.store.get(ctx, "import_batches", batch_id)
        suggestions = self.imports.suggest_mapping(ctx, batch_id)
        report = batch.get("schema_report") or {}
        if not report.get("columns"):
            report = self.compare_schema(ctx, batch_id)
        hints = {c["key"]: c for c in report.get("columns") or []}
        valid = set(suggestions["valid_targets"])
        files = self.imports.files(ctx, batch_id)
        # Two columns of one file suggesting the same single-valued field.
        clashes: Dict[str, List[str]] = {}
        by_column = {s["column"]: s for s in suggestions["suggestions"]}
        for f in files:
            seen: Dict[str, str] = {}
            for column in f["columns"]:
                target = (by_column.get(column) or {}).get("target")
                if not target or target.split(".", 1)[1] in _LIST_FIELDS:
                    continue
                if target in seen and seen[target] != column:
                    clashes.setdefault(column, []).append(f"{f['filename']}: also {seen[target]!r} → {target}")
                    clashes.setdefault(seen[target], []).append(f"{f['filename']}: also {column!r} → {target}")
                seen.setdefault(target, column)
        out = []
        for s in suggestions["suggestions"]:
            column, target, confidence = s["column"], s["target"], s["confidence"]
            key = _key(column)
            info = hints.get(key) or {}
            reasons: List[str] = []
            if target is None:
                status = "unmapped"
                reasons.append(s["reason"])
            else:
                status = "confident"
                if confidence < CONFIDENT:
                    status = "ambiguous"
                    reasons.append(s["reason"] if s["reason"] != "known column name"
                                   else f"confidence {confidence:.2f} is below {CONFIDENT}")
                if key in _AMBIGUOUS_HEADERS:
                    status = "ambiguous"
                    reasons.append(f"{column!r} can mean more than one field")
                field_name = target.split(".", 1)[1]
                expects = _EXPECTS.get(field_name)
                hint = info.get("type_hint")
                if expects and hint not in (None, "empty", "text") and hint not in expects:
                    status = "ambiguous"
                    reasons.append(f"values look like {hint}, not {field_name.replace('_', ' ')}")
                if column in clashes:
                    status = "ambiguous"
                    reasons.extend(clashes[column])
            alternatives = sorted({t for syn, (t, _c) in _SYNONYMS.items() if len(syn) >= 4 and syn in key
                                   and t in valid and t != target})[:5]
            if key == "name":
                alternatives = [t for t in ("company.name", "contact.full_name") if t in valid and t != target]
            out.append({"column": column, "suggestion": target, "confidence": confidence, "status": status,
                        "reasons": reasons, "alternatives": alternatives, "type_hint": info.get("type_hint"),
                        "files_present": info.get("files_present"), "samples": info.get("samples") or []})
        return {"batch_id": batch_id, "target": batch["target"], "columns": out,
                "requires_decision": [c["column"] for c in out if c["status"] == "ambiguous"],
                "valid_targets": sorted(valid), "current_mapping": batch.get("mapping") or {},
                "note": "Suggestions are never applied. Every ambiguous column needs an explicit choice "
                        "(a field, or none)."}

    def apply_mapping(self, ctx: Ctx, batch_id: str, mapping: Mapping[str, Optional[str]], *,
                      file_mappings: Optional[Mapping[str, Mapping[str, Optional[str]]]] = None) -> Dict[str, Any]:
        """Store an explicit mapping. Refused while any ambiguous column is undecided."""
        ctx.require_write()
        review = self.mapping_review(ctx, batch_id)
        file_mappings = dict(file_mappings or {})
        files = {f["id"]: f for f in self.imports.files(ctx, batch_id)}
        decided = set(mapping)
        for per_file in file_mappings.values():
            decided |= set(per_file)
        undecided = [c for c in review["requires_decision"] if c not in decided]
        if undecided:
            raise ValidationError("these columns are ambiguous and need an explicit decision (a field, or none): "
                                  + ", ".join(undecided))
        valid = set(_valid_targets(self.imports._custom_defs(ctx), review["target"]))
        combined: Dict[str, Optional[str]] = dict(mapping)
        for file_id, per_file in file_mappings.items():
            f = files.get(file_id)
            if f is None:
                raise NotFoundError(f"file {file_id} is not in this batch")
            clean: Dict[str, Optional[str]] = {}
            for column, target in per_file.items():
                if column not in f["columns"]:
                    raise ValidationError(f"{f['filename']} has no column named {column!r}")
                if target not in (None, "") and target not in valid:
                    raise ValidationError(f"{f['filename']}: {column!r} → {target!r}: unknown target field")
                clean[column] = target or None
                combined.setdefault(column, target or None)
            self.store.update(ctx, "import_files", file_id, {"mapping": clean})
        for file_id, f in files.items():
            if file_id not in file_mappings and f.get("mapping"):
                self.store.update(ctx, "import_files", file_id, {"mapping": {}})
        report = self.imports.set_mapping(ctx, batch_id, combined)
        audit(self.store, ctx, "internal_data.mapping", entity_type="import_batches", entity_id=batch_id,
              changes={"mapping": dict(mapping), "file_mappings": {k: dict(v) for k, v in file_mappings.items()}})
        return report

    # --- merge and conflicts ----------------------------------------------------------------

    def merge(self, ctx: Ctx, batch_id: str) -> Dict[str, Any]:
        """Start the merge with conflict review on: differing values wait for a person."""
        return self.imports.merge(ctx, batch_id, conflict_review=True)

    def conflicts(self, ctx: Ctx, batch_id: str, *, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        self.store.get(ctx, "import_batches", batch_id)
        page = self.store.list(ctx, "import_rows", {"batch_id": batch_id, "status": "conflict"},
                               order="created_at", limit=limit, offset=offset)
        return {"items": page.rows, "total": page.total}

    def resolve_conflicts(self, ctx: Ctx, batch_id: str, row_id: str,
                          decisions: Mapping[str, Any]) -> Dict[str, Any]:
        """``decisions``: ``{"company.industry": "keep" | "take_new" | {"value": ...}}``."""
        ctx.require_write()
        row = self.store.get(ctx, "import_rows", row_id)
        if row["batch_id"] != batch_id:
            raise NotFoundError("row not in this batch")
        if row["status"] != "conflict":
            raise ConflictError(f"row is {row['status']}, not awaiting conflict review")
        file_row = self.store.get(ctx, "import_files", row["file_id"])
        conflicts = [dict(c) for c in row.get("conflicts") or []]
        known = {c["field"] for c in conflicts}
        unknown = set(decisions) - known
        if unknown:
            raise ValidationError(f"no conflict on: {', '.join(sorted(unknown))}")
        changes: Dict[str, Dict[str, Any]] = {"company": {}, "contact": {}}
        for conflict in conflicts:
            decision = decisions.get(conflict["field"])
            if decision is None or conflict.get("resolution"):
                continue
            entity, field = conflict["field"].split(".", 1)
            if decision == "keep":
                conflict["resolution"] = "kept existing"
            elif decision == "take_new":
                changes[entity][field] = conflict["incoming"]
                conflict["resolution"] = "took new value"
            elif isinstance(decision, Mapping) and "value" in decision:
                changes[entity][field] = decision["value"]
                conflict["resolution"] = "manual value"
            else:
                raise ValidationError(f"{conflict['field']}: decision must be keep, take_new or {{'value': …}}")
            conflict["resolved_by"] = ctx.user_id
            conflict["resolved_at"] = utcnow().isoformat()
        prov = dict(source_kind="import", source_name=file_row["filename"], original=row["original"],
                    import_batch_id=batch_id, import_file_id=row["file_id"], row_number=row["row_number"],
                    match_rule="conflict review")
        for entity, table, entity_id in (("company", "companies", row.get("company_id")),
                                         ("contact", "contacts", row.get("contact_id"))):
            if changes[entity]:
                if not entity_id:
                    raise ConflictError(f"the row has no {entity} to update")
                self.store.update(ctx, table, entity_id, changes[entity])
                provenance(self.store, ctx, table, entity_id, normalized=changes[entity], **prov)
        open_left = [c for c in conflicts if not c.get("resolution")]
        values: Dict[str, Any] = {"conflicts": conflicts}
        if not open_left:
            values["status"] = "merged"
        updated = self.store.update(ctx, "import_rows", row_id, values)
        self._recount(ctx, batch_id)
        audit(self.store, ctx, "internal_data.resolve_conflicts", entity_type="import_rows", entity_id=row_id,
              changes={"decisions": {k: (v if isinstance(v, str) else "manual") for k, v in decisions.items()},
                       "open": len(open_left)})
        return updated

    def _recount(self, ctx: Ctx, batch_id: str) -> int:
        count = self.store.count(ctx, "import_rows", {"batch_id": batch_id, "status": "conflict"})
        self.store.update(ctx, "import_batches", batch_id, {"conflict_count": count})
        return count

    # --- overview and history ----------------------------------------------------------------

    def overview(self, ctx: Ctx, batch_id: str) -> Dict[str, Any]:
        batch = self.store.get(ctx, "import_batches", batch_id)
        files = self.imports.files(ctx, batch_id)
        statuses: Dict[str, int] = {}
        for status in ("pending", "merged", "duplicate", "rejected", "conflict", "needs_review"):
            count = self.store.count(ctx, "import_rows", {"batch_id": batch_id, "status": status})
            if count:
                statuses[status] = count
        return {"batch": batch, "files": files, "row_status": statuses}

    def history(self, ctx: Ctx, *, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        page = self.store.list(ctx, "import_batches", {}, order="-created_at", limit=limit, offset=offset)
        items = [{"id": b["id"], "name": b["name"], "target": b["target"], "status": b["status"],
                  "file_count": b["file_count"], "row_count": b["row_count"], "stats": b["stats"],
                  "conflict_count": b.get("conflict_count", 0), "created_at": b["created_at"],
                  "created_by": b.get("created_by")} for b in page.rows]
        return {"items": items, "total": page.total}


def run_internal_data_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Worker handler for ``internal_data``: schema comparison of a large batch."""
    from cloud.intel.tasks.worker import PermanentTaskError

    params = task["params"]
    batch_id = params.get("batch_id")
    if not batch_id:
        raise PermanentTaskError("batch_id is required")
    action = params.get("action") or "schema"
    service: InternalDataService = platform.service("internal_data")
    if action == "schema":
        report = service.compare_schema(ctx, batch_id)
        reporter.progress(f"compared {report['file_count']} file(s)", done=1, total=1)
        return {"batch_id": batch_id, "files": report["file_count"], "columns": report["column_count"]}
    if action == "review":
        review = service.mapping_review(ctx, batch_id)
        return {"batch_id": batch_id, "requires_decision": review["requires_decision"]}
    raise PermanentTaskError(f"unknown internal_data action {action!r}")
