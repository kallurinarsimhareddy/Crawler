"""The internal data engine: import 1–30 CSV/XLSX/JSON files as one batch, check
them, map their columns explicitly, and merge them into the CRM with provenance.

Lifecycle of a batch::

    create_batch ─► add_file ×N ─► validate ─► (suggest_mapping) ─► set_mapping ─► merge (task)
                      uploaded      validated|rejected           mapped            merging ─► merged|failed

Rules this module will not bend:

* **Nothing is mapped implicitly.** :meth:`ImportService.suggest_mapping`
  returns suggestions with a confidence; only :meth:`ImportService.set_mapping`,
  called with an explicit ``{source column: target field}``, decides what a
  column means. A column mapped to nothing is kept in each row's ``original``
  but never merged.
* **Incompatible files are rejected by name, with the reason** (missing required
  column, empty, duplicate headers, unreadable) and excluded from the merge; the
  compatible files still import.
* **Every merged value keeps its origin**: batch, file, row number, the exact
  original cells and the normalised values (``import_rows`` and
  ``source_records``).
* **Likely duplicates are not merged on a guess.** A PROBABLE/AMBIGUOUS company
  match leaves the row ``pending`` for review (:meth:`ImportService.resolve_row`).
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, new_id
from cloud.intel.core.normalize import blank, normalize_email
from cloud.intel.imports.parse import DEFAULT_MAX_ROWS, ParseError, detect_format, iter_rows, parse_file
from cloud.intel.store.spec import get_spec

__all__ = ["ImportService", "COMPANY_FIELDS", "CONTACT_FIELDS", "MAX_FILES", "run_merge_task"]

log = logging.getLogger(__name__)

MAX_FILES = 30
_REQ = "missing required field — "
MAX_FILE_BYTES = 50 * 1024 * 1024
CHUNK = 200

COMPANY_FIELDS = ("name", "legal_name", "domain", "website", "careers_url", "linkedin_url", "industry",
                  "sub_industry", "country", "state", "city", "employee_range", "employee_count", "revenue_range",
                  "revenue_usd", "description", "sic_codes", "naics_codes", "technologies", "tags", "aliases", "ats")
CONTACT_FIELDS = ("full_name", "first_name", "last_name", "title", "department", "seniority", "email", "phone",
                  "linkedin_url", "location", "tags")
_LIST_FIELDS = {"sic_codes", "naics_codes", "technologies", "tags", "aliases"}

# normalised header -> (target, confidence). Headers are compared lower-case, alphanumerics only.
_SYNONYMS: Dict[str, Tuple[str, float]] = {}


def _syn(target: str, confidence: float, *names: str) -> None:
    for name in names:
        _SYNONYMS[name] = (target, confidence)


_syn("company.name", 0.95, "company", "companyname", "accountname", "account", "organization", "organisation",
     "organizationname", "organisationname", "employer", "businessname", "firm")
_syn("company.legal_name", 0.9, "legalname", "companylegalname", "registeredname")
_syn("company.website", 0.95, "website", "companywebsite", "url", "websiteurl", "companyurl", "homepage", "web")
_syn("company.domain", 0.9, "domain", "companydomain", "websitedomain")
_syn("company.careers_url", 0.85, "careersurl", "careerpageurl", "careerpage", "careerurl", "careersjobsurl",
     "jobsurl", "itlink")
_syn("company.industry", 0.9, "industry", "primaryindustry", "sector", "vertical")
_syn("company.sub_industry", 0.85, "subindustry", "secondaryindustry")
_syn("company.city", 0.85, "city", "companycity", "hqcity")
_syn("company.state", 0.85, "state", "companystate", "hqstate", "province", "region", "stateprovince")
_syn("company.country", 0.85, "country", "companycountry", "hqcountry")
_syn("company.employee_range", 0.7, "employees", "employeerange", "employeesize", "companysize", "size",
     "headcountrange")
_syn("company.employee_count", 0.75, "employeecount", "numberofemployees", "headcount", "noofemployees")
_syn("company.revenue_range", 0.7, "revenue", "revenuerange", "annualrevenue", "revenueband")
_syn("company.revenue_usd", 0.7, "revenueusd", "annualrevenueusd")
_syn("company.description", 0.8, "description", "companydescription", "about")
_syn("company.technologies", 0.6, "technologies", "techstack", "technology", "erp", "erpsystem", "erps")
_syn("company.sic_codes", 0.85, "sic", "siccode", "siccodes")
_syn("company.naics_codes", 0.85, "naics", "naicscode", "naicscodes")
_syn("company.linkedin_url", 0.7, "companylinkedin", "companylinkedinurl", "linkedincompanyurl")
_syn("company.ats", 0.7, "ats", "applicanttrackingsystem", "platform")
_syn("contact.first_name", 0.95, "firstname", "first", "givenname")
_syn("contact.last_name", 0.95, "lastname", "last", "surname", "familyname")
_syn("contact.full_name", 0.85, "fullname", "contactname", "personname", "contact")
_syn("contact.title", 0.9, "title", "jobtitle", "position", "designation", "role")
_syn("contact.department", 0.85, "department", "dept", "jobfunction", "function")
_syn("contact.seniority", 0.8, "seniority", "managementlevel", "level", "senioritylevel")
_syn("contact.email", 0.95, "email", "emailaddress", "workemail", "businessemail", "contactemail", "mail")
_syn("contact.phone", 0.85, "phone", "directphone", "phonenumber", "mobile", "mobilephone", "workphone",
     "directdial")
_syn("contact.linkedin_url", 0.8, "personlinkedin", "linkedinprofile", "contactlinkedin", "linkedinprofileurl")
_syn("contact.location", 0.7, "location", "personlocation", "contactlocation")


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


REQUIRED = {
    "companies": ("company.name",),
    "contacts": ("contact.name", "contact.email|company.name"),
    "companies_and_contacts": ("company.name", "contact.name"),
}


def _valid_targets(ctx_defs: Mapping[str, List[str]], target: str) -> List[str]:
    fields = ["company." + f for f in COMPANY_FIELDS] + ["company.custom:" + k for k in ctx_defs.get("companies", [])]
    if target != "companies":
        fields += ["contact." + f for f in CONTACT_FIELDS] + ["contact.custom:" + k
                                                              for k in ctx_defs.get("contacts", [])]
    return fields


def _satisfied(requirement: str, targets: set) -> bool:
    for option in requirement.split("|"):
        if option == "contact.name":
            if "contact.full_name" in targets or {"contact.first_name", "contact.last_name"} <= targets \
                    or "contact.first_name" in targets:
                return True
        elif option in targets:
            return True
    return False


def _describe(requirement: str) -> str:
    return " or ".join("a contact name" if o == "contact.name" else o.split(".", 1)[1].replace("_", " ")
                       for o in requirement.split("|"))


_NUMBER = re.compile(r"^\$?\s*([0-9][0-9,]*\.?[0-9]*)\s*([kmb]|thousand|million|billion)?\s*$", re.I)


def _number(value: str) -> Optional[float]:
    match = _NUMBER.match(value.replace("USD", "").strip())
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    scale = (match.group(2) or "").lower()[:1]
    return number * {"k": 1e3, "t": 1e3, "m": 1e6, "b": 1e9}.get(scale, 1)


def _split_list(value: str) -> List[str]:
    return [part.strip() for part in re.split(r"[;|,\n]", value) if part.strip()]


class ImportService:
    def __init__(self, platform: Any, *, max_rows_per_file: int = DEFAULT_MAX_ROWS) -> None:
        self.platform = platform
        self.max_rows_per_file = max_rows_per_file

    @property
    def store(self):
        return self.platform.store

    # --- batches and files ------------------------------------------------------

    def create_batch(self, ctx: Ctx, name: str, target: str = "companies") -> Dict[str, Any]:
        if target not in REQUIRED:
            raise ValidationError(f"target must be one of {', '.join(REQUIRED)}")
        batch = self.store.insert(ctx, "import_batches", {"name": name, "target": target, "status": "uploaded"})
        audit(self.store, ctx, "imports.create", entity_type="import_batches", entity_id=batch["id"], summary=name)
        return batch

    def _storage_key(self, ctx: Ctx, batch_id: str, file_id: str, fmt: str) -> str:
        return f"imports/{ctx.workspace_id}/{batch_id}/{file_id}.{fmt}"

    def add_file(self, ctx: Ctx, batch_id: str, filename: str, data: bytes, *, sheet: Optional[str] = None
                 ) -> Dict[str, Any]:
        ctx.require_write()
        batch = self.store.get(ctx, "import_batches", batch_id)
        if batch["status"] in ("merging", "merged"):
            raise ConflictError(f"batch is already {batch['status']}")
        if batch["file_count"] >= MAX_FILES:
            raise ValidationError(f"a batch holds at most {MAX_FILES} files")
        if len(data) > MAX_FILE_BYTES:
            raise ValidationError(f"{filename} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        safe_name = re.sub(r"[^\w .()-]", "_", Path(filename).name)[:255] or "upload"
        try:
            fmt = detect_format(safe_name)
        except ParseError as error:
            raise ValidationError(str(error)) from None
        digest = hashlib.sha256(data).hexdigest()
        if self.store.first(ctx, "import_files", {"batch_id": batch_id, "sha256": digest}) is not None:
            raise ConflictError(f"{safe_name} is identical to a file already in this batch")
        try:
            parsed = parse_file(safe_name, data, sheet=sheet, max_rows=self.max_rows_per_file)
        except ParseError as error:
            parsed = None
            problems = [str(error)]
        file_id = new_id("if")
        key = self._storage_key(ctx, batch_id, file_id, fmt)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "upload"
            path.write_bytes(data)
            self.platform.storage.put_file(key, path, content_type="application/octet-stream")
        row = self.store.insert(ctx, "import_files", {
            "batch_id": batch_id, "filename": safe_name, "format": fmt, "sheet": parsed.sheet if parsed else sheet,
            "sha256": digest, "size_bytes": len(data), "row_count": parsed.row_count if parsed else 0,
            "columns": parsed.columns if parsed else [], "status": "uploaded",
            "problems": parsed.problems if parsed else problems, "storage_key": key})
        self.store.update(ctx, "import_batches", batch_id, {
            "file_count": batch["file_count"] + 1, "row_count": batch["row_count"] + row["row_count"],
            "status": "uploaded"})
        audit(self.store, ctx, "imports.add_file", entity_type="import_files", entity_id=row["id"],
              summary=f"{safe_name}: {row['row_count']} rows, {len(row['columns'])} columns")
        return row

    def files(self, ctx: Ctx, batch_id: str) -> List[Dict[str, Any]]:
        rows = self.store.all(ctx, "import_files", {"batch_id": batch_id}, order="created_at")
        # Files added in the same request can share a created_at to the millisecond: a stable
        # tie-break keeps the merge order (and so which value wins) deterministic.
        return sorted(rows, key=lambda r: (r["created_at"], str(r.get("filename") or ""), r["id"]))

    def _custom_defs(self, ctx: Ctx) -> Dict[str, List[str]]:
        defs: Dict[str, List[str]] = {}
        for row in self.store.all(ctx, "custom_field_defs"):
            defs.setdefault(row["entity_type"], []).append(row["key"])
        return defs

    # --- mapping ---------------------------------------------------------------------

    def suggest_mapping(self, ctx: Ctx, batch_id: str) -> Dict[str, Any]:
        """Suggestions only. Nothing is stored or applied."""
        batch = self.store.get(ctx, "import_batches", batch_id)
        valid = set(_valid_targets(self._custom_defs(ctx), batch["target"]))
        columns: List[str] = []
        for f in self.files(ctx, batch_id):
            for column in f["columns"]:
                if column not in columns:
                    columns.append(column)
        suggestions = []
        for column in columns:
            key = _key(column)
            target, confidence, why = None, 0.0, "no known synonym"
            if key in _SYNONYMS:
                target, confidence = _SYNONYMS[key]
                why = "known column name"
            else:
                for synonym, (candidate, conf) in sorted(_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
                    if len(synonym) >= 5 and synonym in key:
                        target, confidence, why = candidate, round(conf * 0.6, 2), f"contains '{synonym}'"
                        break
            if key == "name":
                target = "company.name" if batch["target"] == "companies" else "contact.full_name"
                confidence, why = 0.5, "'Name' is ambiguous; confirm whether it is a company or a person"
            if key in ("linkedin", "linkedinurl"):
                target = "company.linkedin_url" if batch["target"] == "companies" else "contact.linkedin_url"
                confidence, why = 0.5, "'LinkedIn' is ambiguous; confirm company page or personal profile"
            if target is not None and target not in valid:
                target, confidence, why = None, 0.0, f"{target} is not importable for a {batch['target']} batch"
            suggestions.append({"column": column, "target": target, "confidence": confidence, "reason": why})
        return {"batch_id": batch_id, "target": batch["target"], "suggestions": suggestions,
                "valid_targets": sorted(valid), "applied": False,
                "note": "Suggestions are not applied. Send an explicit mapping to PUT /mapping."}

    def set_mapping(self, ctx: Ctx, batch_id: str, mapping: Mapping[str, Optional[str]]) -> Dict[str, Any]:
        ctx.require_write()
        batch = self.store.get(ctx, "import_batches", batch_id)
        if batch["status"] in ("merging", "merged"):
            raise ConflictError(f"batch is already {batch['status']}")
        files = self.files(ctx, batch_id)
        known_columns = {c for f in files for c in f["columns"]}
        valid = set(_valid_targets(self._custom_defs(ctx), batch["target"]))
        clean: Dict[str, Optional[str]] = {}
        for column, target in mapping.items():
            if column not in known_columns:
                raise ValidationError(f"no uploaded file has a column named {column!r}")
            if target in (None, ""):
                clean[column] = None
                continue
            if target not in valid:
                raise ValidationError(f"{column!r} → {target!r}: unknown target field")
            clean[column] = target
        # Different files may name one field differently ("Company Name" in one, "Account
        # Name" in another), so several columns may map to one target. Two such columns
        # in the SAME file would make one value silently replace the other: refused.
        for f in files:
            seen: Dict[str, str] = {}
            for column in f["columns"]:
                target = clean.get(column)
                if not target or target.split(".", 1)[1] in _LIST_FIELDS:
                    continue
                if target in seen:
                    raise ValidationError(f"{f['filename']}: both {seen[target]!r} and {column!r} are mapped to "
                                          f"{target}; map only one, so no value silently replaces another")
                seen[target] = column
        targets = {t for t in clean.values() if t}
        missing = [_describe(r) for r in REQUIRED[batch["target"]] if not _satisfied(r, targets)]
        if missing:
            raise ValidationError("the mapping does not cover required field(s): " + ", ".join(missing))
        self.store.update(ctx, "import_batches", batch_id, {"mapping": clean, "status": "mapped"})
        audit(self.store, ctx, "imports.mapping", entity_type="import_batches", entity_id=batch_id, changes=clean)
        return self.validate(ctx, batch_id)

    # --- validation ------------------------------------------------------------------

    def validate(self, ctx: Ctx, batch_id: str) -> Dict[str, Any]:
        batch = self.store.get(ctx, "import_batches", batch_id)
        files = self.files(ctx, batch_id)
        if not files:
            raise ValidationError("upload at least one file first")
        mapping: Dict[str, Optional[str]] = batch["mapping"] or {}
        header_sets = Counter(frozenset(c.lower() for c in f["columns"]) for f in files if f["columns"])
        reference = set(header_sets.most_common(1)[0][0]) if header_sets else set()
        all_columns = sorted({c for f in files for c in f["columns"]}, key=str.lower)
        report_files = []
        compatible = 0
        for f in files:
            # Structural problems come from parsing and persist; required-field problems
            # depend on the mapping, so each validation recomputes them.
            problems = [p for p in (f["problems"] or []) if isinstance(p, str) and not p.startswith(_REQ)]
            warnings: List[str] = []
            if mapping:
                targets = {mapping[c] for c in f["columns"] if mapping.get(c)}
                how = "mapped"
            else:
                targets = {_SYNONYMS[_key(c)][0] for c in f["columns"] if _key(c) in _SYNONYMS}
                if any(_key(c) == "name" for c in f["columns"]):
                    targets.add("company.name" if batch["target"] == "companies" else "contact.full_name")
                how = "recognised"
            for requirement in REQUIRED[batch["target"]]:
                if not _satisfied(requirement, targets):
                    problems.append(f"{_REQ}no {how} column for required field: {_describe(requirement)}")
            lowered = {c.lower() for c in f["columns"]}
            missing_vs = sorted(reference - lowered)
            extra_vs = sorted(lowered - reference)
            if missing_vs:
                warnings.append("lacks columns the other files have: " + ", ".join(missing_vs[:15]))
            if extra_vs:
                warnings.append("has columns the other files do not: " + ", ".join(extra_vs[:15]))
            if mapping:
                unmapped = [c for c in f["columns"] if not mapping.get(c)]
                if unmapped:
                    warnings.append("kept as original only (not mapped): " + ", ".join(unmapped[:15]))
            fatal = [p for p in problems if not p.startswith("more than")]
            status = "incompatible" if fatal else "compatible"
            compatible += status == "compatible"
            self.store.update(ctx, "import_files", f["id"], {"status": status, "problems": problems})
            report_files.append({"file_id": f["id"], "filename": f["filename"], "format": f["format"],
                                 "rows": f["row_count"], "columns": f["columns"], "status": status,
                                 "problems": problems, "warnings": warnings})
        report = {"batch_id": batch_id, "target": batch["target"], "files": report_files,
                  "compatible": compatible, "incompatible": len(files) - compatible,
                  "all_columns": all_columns, "common_columns": sorted(set.intersection(
                      *[{c.lower() for c in f["columns"]} for f in files if f["columns"]]) if header_sets else []),
                  "mapped": bool(mapping)}
        status = "rejected" if compatible == 0 else ("mapped" if mapping else "validated")
        self.store.update(ctx, "import_batches", batch_id, {"validation": report, "status": status})
        audit(self.store, ctx, "imports.validate", entity_type="import_batches", entity_id=batch_id,
              summary=f"{compatible} compatible, {len(files) - compatible} incompatible")
        return report

    # --- merge -------------------------------------------------------------------------

    def merge(self, ctx: Ctx, batch_id: str, *, conflict_review: bool = False) -> Dict[str, Any]:
        """Start the merge task. With ``conflict_review`` a row whose values disagree with the
        matched CRM record is left ``conflict`` for a person (see ``imports.internal``)."""
        ctx.require_write()
        batch = self.store.get(ctx, "import_batches", batch_id)
        if batch["status"] != "mapped" or not batch["mapping"]:
            raise ConflictError("set an explicit column mapping (PUT /mapping) before merging")
        report = self.validate(ctx, batch_id)
        if report["compatible"] == 0:
            raise ConflictError("no file in this batch is compatible; nothing to merge")
        params: Dict[str, Any] = {"batch_id": batch_id}
        if conflict_review:
            params["conflict_review"] = True
        task = self.platform.tasks.submit(ctx, "import_merge", params,
                                          idempotency_key=f"import_merge:{batch_id}:{batch['version']}",
                                          entity_type="import_batches", entity_id=batch_id)
        self.store.update(ctx, "import_batches", batch_id, {"status": "merging"})
        return task

    def normalize_row(self, target: str, mapping: Mapping[str, Optional[str]], original: Mapping[str, str]
                      ) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
        """``(company_values, contact_values, problems)`` for one row, by the explicit mapping only."""
        company: Dict[str, Any] = {}
        contact: Dict[str, Any] = {}
        problems: List[str] = []
        for column, targetfield in mapping.items():
            if not targetfield or column not in original:
                continue
            raw = original[column]
            if raw is None or blank(raw):
                continue
            entity, field_name = targetfield.split(".", 1)
            bucket = company if entity == "company" else contact
            if field_name.startswith("custom:"):
                bucket.setdefault("custom_fields", {})[field_name[7:]] = raw
                continue
            if field_name in _LIST_FIELDS:
                bucket[field_name] = list(dict.fromkeys(bucket.get(field_name, []) + _split_list(raw)))
            elif field_name == "employee_count":
                number = _number(raw)
                if number is None:
                    problems.append(f"{column}: {raw!r} is not a number; kept only in the original")
                else:
                    bucket[field_name] = int(number)
            elif field_name == "revenue_usd":
                number = _number(raw)
                if number is None:
                    problems.append(f"{column}: {raw!r} is not an amount; kept only in the original")
                else:
                    bucket[field_name] = number
            elif field_name == "email":
                email = normalize_email(raw)
                if email is None:
                    problems.append(f"{column}: {raw!r} is not an email address; kept only in the original")
                else:
                    bucket[field_name] = email
            else:
                limit = (get_spec("companies" if entity == "company" else "contacts").columns[field_name].max_len
                         or 2000)
                bucket[field_name] = raw[:limit]
        return company, contact, problems

    def rows(self, ctx: Ctx, batch_id: str, *, status: Optional[str] = None, limit: int = 50, offset: int = 0):
        filters: Dict[str, Any] = {"batch_id": batch_id}
        if status:
            filters["status"] = status
        return self.store.list(ctx, "import_rows", filters, order="created_at", limit=limit, offset=offset)

    def resolve_row(self, ctx: Ctx, batch_id: str, row_id: str, action: str,
                    company_id: Optional[str] = None) -> Dict[str, Any]:
        """Decide a row left pending for review: ``create`` a new company or ``merge_into`` a chosen one."""
        row = self.store.get(ctx, "import_rows", row_id)
        if row["batch_id"] != batch_id:
            raise NotFoundError("row not in this batch")
        if row["status"] != "pending":
            raise ConflictError(f"row is already {row['status']}")
        crm = self.platform.service("crm")
        batch = self.store.get(ctx, "import_batches", batch_id)
        company_values = dict((row["normalized"] or {}).get("company") or {})
        file_row = self.store.get(ctx, "import_files", row["file_id"])
        prov = dict(source_kind="import", source_name=file_row["filename"], original=row["original"],
                    import_batch_id=batch_id, import_file_id=row["file_id"], row_number=row["row_number"])
        if action == "create":
            result = crm.upsert_company(ctx, company_values, create_if_ambiguous=True, **prov)
            company = result["company"]
        elif action == "merge_into":
            if not company_id:
                raise ValidationError("merge_into needs a company_id")
            existing = self.store.get(ctx, "companies", company_id)
            changes, conflicts = crm._merge_values(existing, crm._normalize_company(ctx, company_values))
            changes["source_count"] = existing["source_count"] + 1
            company = self.store.update(ctx, "companies", company_id, changes)
            from cloud.intel.core.audit import provenance

            provenance(self.store, ctx, "companies", company_id, normalized={"conflicts": conflicts},
                       match_rule="manual review: merge_into", **prov)
        else:
            raise ValidationError("action must be create or merge_into")
        contact_values = dict((row["normalized"] or {}).get("contact") or {})
        contact_id = None
        if contact_values and batch["target"] != "companies":
            contact_values["company_id"] = company["id"]
            contact_id = crm.upsert_contact(ctx, contact_values, **prov)["contact"]["id"]
        updated = self.store.update(ctx, "import_rows", row_id, {"status": "merged", "company_id": company["id"],
                                                                 "contact_id": contact_id})
        audit(self.store, ctx, "imports.resolve_row", entity_type="import_rows", entity_id=row_id,
              changes={"action": action, "company_id": company["id"]})
        return updated


def _load(platform: Any, key: str) -> bytes:
    with platform.storage.open(key) as handle:
        return handle.read()


def run_merge_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """Worker handler for ``import_merge`` tasks. Resumable from a checkpoint."""
    from cloud.intel.tasks.worker import PermanentTaskError, TaskCancelled, TaskPaused

    store = platform.store
    service: ImportService = platform.service("imports")
    crm = platform.service("crm")
    batch_id = task["params"].get("batch_id")
    batch = store.find(ctx, "import_batches", batch_id or "")
    if batch is None:
        raise PermanentTaskError("import batch not found")
    mapping = batch["mapping"] or {}
    if not mapping:
        raise PermanentTaskError("the batch has no explicit mapping")
    checkpoint = reporter.checkpoint
    conflict_review = bool(task["params"].get("conflict_review"))
    stats = {"rows": 0, "created": 0, "merged": 0, "duplicates": 0, "needs_review": 0, "rejected": 0,
             "contacts_created": 0, "contacts_matched": 0, **(batch["stats"] or {}), **checkpoint.get("stats", {})}
    if conflict_review:
        stats.setdefault("conflicts", 0)
    files = [f for f in service.files(ctx, batch_id) if f["status"] == "compatible"]
    start_file = int(checkpoint.get("file_index", 0))
    start_row = int(checkpoint.get("row", 0))
    target = batch["target"]

    for file_index, f in enumerate(files):
        if file_index < start_file:
            continue
        data = _load(platform, f["storage_key"])
        prov = dict(source_kind="import", source_name=f["filename"], import_batch_id=batch_id,
                    import_file_id=f["id"])
        # A per-file mapping (internal data) overrides the batch mapping for this file's columns.
        file_mapping = {**mapping, **(f.get("mapping") or {})}
        for row_number, original in iter_rows(f["format"], data, sheet=f["sheet"],
                                              max_rows=service.max_rows_per_file):
            if file_index == start_file and row_number <= start_row:
                continue
            if row_number % CHUNK == 0:
                store.update(ctx, "import_batches", batch_id, {"stats": stats})
                reporter.progress(f"{f['filename']}: row {row_number:,} of {f['row_count']:,}",
                                  file=f["filename"], row=row_number, stats=stats)
                if reporter.is_cancelled():
                    raise TaskCancelled()
                if reporter.should_pause():
                    raise TaskPaused({"file_index": file_index, "row": row_number - 1, "stats": stats})
            if store.first(ctx, "import_rows", {"file_id": f["id"], "row_number": row_number}) is not None:
                continue  # already processed before a restart
            stats["rows"] += 1
            company_values, contact_values, problems = service.normalize_row(target, file_mapping, original)
            conflicts: List[Dict[str, Any]] = []
            record = {"batch_id": batch_id, "file_id": f["id"], "row_number": row_number, "original": dict(original),
                      "normalized": {"company": company_values, "contact": contact_values}, "problems": problems}
            company_id = contact_id = None
            status = "duplicate"
            try:
                needs_company = target != "contacts" or bool(company_values.get("name"))
                if needs_company:
                    if not company_values.get("name"):
                        raise ValidationError("the company name is empty")
                    result = crm.upsert_company(ctx, company_values, original=original, row_number=row_number, **prov)
                    if result["needs_review"]:
                        status = "pending"
                        stats["needs_review"] += 1
                        problems.append("possible duplicate of " + ", ".join(result["match"]["candidates"][:5])
                                        + ": " + "; ".join(result["match"]["reasons"])[:300])
                    else:
                        company_id = result["company"]["id"]
                        if result["created"]:
                            stats["created"] += 1
                            status = "merged"
                        else:
                            stats["duplicates"] += 1
                            if conflict_review:
                                from cloud.intel.imports.internal import merge_conflicts

                                conflicts += merge_conflicts(crm, ctx, "company", result["company"], company_values)
                if target != "companies" and status != "pending":
                    if not (contact_values.get("full_name") or contact_values.get("first_name")):
                        raise ValidationError("the contact name is empty")
                    if company_id:
                        contact_values["company_id"] = company_id
                    result = crm.upsert_contact(ctx, contact_values, original=original, row_number=row_number, **prov)
                    contact_id = result["contact"]["id"]
                    if result["created"]:
                        stats["contacts_created"] += 1
                        status = "merged"
                    else:
                        stats["contacts_matched"] += 1
                        if conflict_review:
                            from cloud.intel.imports.internal import merge_conflicts

                            conflicts += merge_conflicts(crm, ctx, "contact", result["contact"], contact_values)
                if status == "merged" and not company_id and not contact_id:
                    status = "duplicate"
            except ValidationError as error:
                status = "rejected"
                stats["rejected"] += 1
                problems.append(str(error))
            if status == "merged":
                stats["merged"] += 1
            if conflicts and status in ("merged", "duplicate"):
                status = "conflict"
                stats["conflicts"] += 1
            record.update(status=status, company_id=company_id, contact_id=contact_id, problems=problems)
            if conflicts:
                record["conflicts"] = conflicts
            try:
                store.insert(ctx, "import_rows", record)
            except ConflictError:
                pass
        store.update(ctx, "import_files", f["id"], {"status": "merged"})
        start_row = 0

    final: Dict[str, Any] = {"status": "merged", "stats": stats}
    if conflict_review:
        final["conflict_count"] = store.count(ctx, "import_rows", {"batch_id": batch_id, "status": "conflict"})
    store.update(ctx, "import_batches", batch_id, final)
    audit(store, ctx, "imports.merged", entity_type="import_batches", entity_id=batch_id, changes=stats)
    try:  # workflows on "import_completed"; best-effort, never fails the merge
        platform.service("automation").emit(ctx, "import_completed", f"import:{batch_id}",
                                            {"batch_id": batch_id, "stats": dict(stats)})
    except Exception:  # noqa: BLE001
        log.debug("import_completed emit skipped", exc_info=True)
    return {"batch_id": batch_id, **stats}
