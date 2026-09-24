"""The CRM: companies, contacts, opportunities, pipelines, tasks, notes,
activities, lists, segments, custom fields and company relationships.

**Merging never overwrites silently.** When an incoming record matches an
existing company (EXACT or STRONG, see :mod:`cloud.intel.imports.dedupe`), only
*empty* fields are filled. A value that differs from what is stored is kept in
the record's provenance under ``normalized.conflicts`` for a person to review,
and list fields (aliases, tags, technologies, codes) are unioned. A PROBABLE or
AMBIGUOUS match is never merged automatically: the caller gets
``needs_review=True`` and nothing is written unless it asked to create anyway.

Every mutation writes an ``audit_log`` row; every value that came from
somewhere writes a ``source_records`` row.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cloud.intel.core.audit import audit, provenance, record_activity
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow
from cloud.intel.core.normalize import (
    FREE_EMAIL_DOMAINS,
    blank,
    domain_of,
    normalize_country,
    normalize_email,
    normalize_name,
    normalize_website,
    split_full_name,
)
from cloud.intel.imports.dedupe import MERGEABLE
from cloud.intel.store.spec import get_spec

__all__ = ["CrmService", "DEFAULT_STAGES"]

log = logging.getLogger(__name__)

DEFAULT_PIPELINE = "Sales"
DEFAULT_STAGES = (
    ("New", 5, False, False), ("Researching", 10, False, False), ("Qualified", 20, False, False),
    ("Contacted", 30, False, False), ("Engaged", 45, False, False), ("Opportunity", 60, False, False),
    ("Proposal", 75, False, False), ("Won", 100, True, False), ("Lost", 0, False, True),
)

#: Company list fields that accumulate rather than conflict.
_UNION_FIELDS = ("aliases", "tags", "technologies", "sic_codes", "naics_codes", "hiring_signals")
#: Fields a merge never copies from an incoming record.
_NEVER_MERGE = frozenset({"status", "merged_into_id", "lifecycle", "owner_id", "account_score", "hiring_score",
                          "opportunity_score", "score_breakdown", "source_count", "first_seen_at", "last_seen_at",
                          "hiring_count", "hiring_velocity", "confidence", "normalized_name"})
#: Entities whose company_id moves to the survivor when companies merge.
_COMPANY_CHILDREN = ("contacts", "job_postings", "opportunities", "crm_tasks", "notes", "hiring_signals",
                     "company_technologies", "activities", "change_events", "discovery_candidates",
                     "research_results", "company_snapshots")


def _emit(platform: Any, ctx: Ctx, trigger: str, event_key: str, payload: Dict[str, Any]) -> None:
    try:
        platform.service("automation").emit(ctx, trigger, event_key, payload)
    except Exception:  # noqa: BLE001 - automation is best-effort by contract
        log.debug("automation emit %s skipped", trigger, exc_info=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class CrmService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform

    @property
    def store(self):
        return self.platform.store

    # --- pipelines -------------------------------------------------------------

    def ensure_defaults(self, ctx: Ctx) -> Dict[str, Any]:
        """The default "Sales" pipeline and its stages. Safe to call repeatedly."""
        pipeline = self.store.first(ctx, "pipelines", {"name": DEFAULT_PIPELINE})
        if pipeline is None:
            has_default = self.store.count(ctx, "pipelines", {"is_default": True}) > 0
            try:
                pipeline = self.store.insert(ctx, "pipelines", {
                    "name": DEFAULT_PIPELINE, "is_default": not has_default,
                    "description": "Default pipeline: New → Won/Lost"})
            except ConflictError:
                pipeline = self.store.first(ctx, "pipelines", {"name": DEFAULT_PIPELINE})
        existing = {s["name"] for s in self.store.all(ctx, "pipeline_stages", {"pipeline_id": pipeline["id"]})}
        for position, (name, probability, won, lost) in enumerate(DEFAULT_STAGES):
            if name in existing:
                continue
            try:
                self.store.insert(ctx, "pipeline_stages", {
                    "pipeline_id": pipeline["id"], "name": name, "position": position,
                    "probability": probability, "is_won": won, "is_lost": lost})
            except ConflictError:
                pass
        return pipeline

    def default_pipeline(self, ctx: Ctx) -> Dict[str, Any]:
        pipeline = self.store.first(ctx, "pipelines", {"is_default": True}) or self.store.first(
            ctx, "pipelines", {"name": DEFAULT_PIPELINE})
        return pipeline or self.ensure_defaults(ctx)

    def create_pipeline(self, ctx: Ctx, name: str, stages: Sequence[str], *, description: Optional[str] = None
                        ) -> Dict[str, Any]:
        """A custom pipeline for this workspace. The last two stages named Won/Lost are flagged."""
        if not stages:
            raise ValidationError("a pipeline needs at least one stage")
        pipeline = self.store.insert(ctx, "pipelines", {"name": name, "description": description})
        for position, stage in enumerate(stages):
            self.store.insert(ctx, "pipeline_stages", {
                "pipeline_id": pipeline["id"], "name": stage, "position": position,
                "is_won": stage.strip().lower() == "won", "is_lost": stage.strip().lower() == "lost"})
        audit(self.store, ctx, "pipelines.create", entity_type="pipelines", entity_id=pipeline["id"], summary=name)
        return pipeline

    def stages(self, ctx: Ctx, pipeline_id: str) -> List[Dict[str, Any]]:
        return self.store.all(ctx, "pipeline_stages", {"pipeline_id": pipeline_id}, order="position")

    # --- custom fields -----------------------------------------------------------

    def validate_custom_fields(self, ctx: Ctx, entity_type: str, values: Mapping[str, Any]) -> Dict[str, Any]:
        if not values:
            return {}
        defs = {d["key"]: d for d in self.store.all(ctx, "custom_field_defs", {"entity_type": entity_type})}
        out: Dict[str, Any] = {}
        for key, value in values.items():
            spec = defs.get(key)
            if spec is None:
                raise ValidationError(f"custom field {key!r} is not defined for {entity_type}")
            if value is None or value == "":
                out[key] = None
                continue
            kind = spec["field_type"]
            try:
                if kind == "number":
                    value = float(value)
                elif kind == "bool":
                    if isinstance(value, str):
                        if value.strip().lower() not in ("true", "false", "yes", "no", "1", "0"):
                            raise ValueError
                        value = value.strip().lower() in ("true", "yes", "1")
                    value = bool(value)
                elif kind == "date":
                    value = datetime.fromisoformat(str(value)[:10]).date().isoformat()
                elif kind == "select":
                    if str(value) not in (spec["options"] or []):
                        raise ValidationError(f"custom field {key!r} must be one of {', '.join(spec['options'])}")
                    value = str(value)
                else:
                    value = str(value)[:2000]
            except ValidationError:
                raise
            except (TypeError, ValueError):
                raise ValidationError(f"custom field {key!r} must be a {kind}") from None
            out[key] = value
        return out

    # --- companies ---------------------------------------------------------------

    def _normalize_company(self, ctx: Ctx, values: Mapping[str, Any]) -> Dict[str, Any]:
        spec = get_spec("companies")
        clean: Dict[str, Any] = {}
        for key, value in values.items():
            if key not in spec.columns:
                raise ValidationError(f"companies has no field {key!r}")
            if isinstance(value, str) and blank(value):
                continue
            if value is None:
                continue
            clean[key] = value
        if not clean.get("name"):
            raise ValidationError("a company needs a name")
        clean["name"] = str(clean["name"]).strip()
        if clean.get("website"):
            website = normalize_website(clean["website"])
            if website:
                clean["website"] = website
            else:
                clean.pop("website")
        domain = domain_of(clean.get("domain") or clean.get("website") or "")
        if domain:
            clean["domain"] = domain
        else:
            clean.pop("domain", None)
        clean["normalized_name"] = normalize_name(clean["name"])
        if clean.get("country"):
            clean["country"] = normalize_country(clean["country"])
        if clean.get("custom_fields"):
            clean["custom_fields"] = self.validate_custom_fields(ctx, "companies", clean["custom_fields"])
        return clean

    def upsert_company(self, ctx: Ctx, values: Mapping[str, Any], *, source_kind: str, source_name: str,
                       source_ref: Optional[str] = None, original: Optional[Mapping[str, Any]] = None,
                       confidence: Optional[float] = None, auto_merge: bool = True,
                       create_if_ambiguous: bool = False, import_batch_id: Optional[str] = None,
                       import_file_id: Optional[str] = None, row_number: Optional[int] = None) -> Dict[str, Any]:
        ctx.require_write()
        clean = self._normalize_company(ctx, values)
        match = self.platform.service("dedupe").resolve(ctx, clean)
        now = utcnow()
        prov = dict(source_kind=source_kind, source_name=source_name, source_ref=source_ref,
                    import_batch_id=import_batch_id, import_file_id=import_file_id, row_number=row_number,
                    confidence=confidence)
        original = dict(original if original is not None else values)

        if match["outcome"] in MERGEABLE and match["company_id"] and auto_merge:
            existing = self.store.get(ctx, "companies", match["company_id"])
            changes, conflicts = self._merge_values(existing, clean)
            changes.update({"source_count": existing["source_count"] + 1, "last_seen_at": now})
            if existing["first_seen_at"] is None:
                changes["first_seen_at"] = now
            company = self.store.update(ctx, "companies", existing["id"], changes)
            provenance(self.store, ctx, "companies", company["id"], original=original,
                       normalized={**{k: _jsonable(v) for k, v in clean.items()}, "conflicts": conflicts},
                       match_rule=f"{match['outcome'].lower()}: " + "; ".join(match["reasons"])[:80], **prov)
            audit(self.store, ctx, "companies.merge_record", entity_type="companies", entity_id=company["id"],
                  summary=f"{source_kind}:{source_name} matched ({match['outcome']})",
                  changes={"filled": sorted(k for k in changes if k not in ("source_count", "last_seen_at",
                                                                             "first_seen_at")),
                           "conflicts": conflicts})
            return {"company": company, "created": False, "match": match, "needs_review": False}

        if match["outcome"] not in (None, "NONE") and match["candidates"] and not create_if_ambiguous:
            return {"company": None, "created": False, "match": match, "needs_review": True}

        clean.setdefault("first_seen_at", now)
        clean["last_seen_at"] = now
        clean["source_count"] = 1
        if confidence is not None:
            clean.setdefault("confidence", confidence)
        company = self.store.insert(ctx, "companies", clean)
        provenance(self.store, ctx, "companies", company["id"], original=original,
                   normalized={k: _jsonable(v) for k, v in clean.items()}, match_rule="created", **prov)
        audit(self.store, ctx, "companies.create", entity_type="companies", entity_id=company["id"],
              summary=f"{company['name']} from {source_kind}:{source_name}")
        _emit(self.platform, ctx, "new_company", f"company:{company['id']}",
              {"company_id": company["id"], "name": company["name"], "source": source_kind})
        return {"company": company, "created": True, "match": match, "needs_review": False}

    @staticmethod
    def _merge_values(existing: Mapping[str, Any], incoming: Mapping[str, Any]):
        changes: Dict[str, Any] = {}
        conflicts: Dict[str, Any] = {}
        for key, value in incoming.items():
            if key in _NEVER_MERGE or value is None:
                continue
            current = existing.get(key)
            if key in _UNION_FIELDS:
                merged = list(current or [])
                for item in value if isinstance(value, list) else [value]:
                    if item not in merged:
                        merged.append(item)
                if merged != list(current or []):
                    changes[key] = merged
            elif key == "name":
                if value != current:
                    aliases = list(changes.get("aliases", existing.get("aliases") or []))
                    if value not in aliases:
                        changes["aliases"] = aliases + [value]
            elif key in ("custom_fields", "score_breakdown"):
                merged = dict(current or {})
                for k, v in value.items():
                    if merged.get(k) in (None, ""):
                        merged[k] = v
                    elif merged[k] != v:
                        conflicts[f"{key}.{k}"] = {"kept": merged[k], "incoming": v}
                if merged != (current or {}):
                    changes[key] = merged
            elif current is None or current == "":
                changes[key] = value
            elif current != value:
                conflicts[key] = {"kept": _jsonable(current), "incoming": _jsonable(value)}
        return changes, conflicts

    def create_company(self, ctx: Ctx, values: Mapping[str, Any], *, force: bool = False) -> Dict[str, Any]:
        """Manual creation from the UI/API. A likely duplicate is a 409 unless ``force``."""
        result = self.upsert_company(ctx, values, source_kind="manual", source_name="user",
                                     create_if_ambiguous=force)
        if result["needs_review"]:
            raise ConflictError("this may duplicate an existing company ("
                                + ", ".join(result["match"]["candidates"][:5])
                                + "); review it or create anyway with force=true")
        return result

    def update_company(self, ctx: Ctx, company_id: str, changes: Mapping[str, Any],
                       expected_version: Optional[int] = None) -> Dict[str, Any]:
        changes = dict(changes)
        if "website" in changes and changes["website"]:
            changes["website"] = normalize_website(changes["website"]) or changes["website"]
            changes.setdefault("domain", domain_of(changes["website"]))
        if "domain" in changes and changes["domain"]:
            changes["domain"] = domain_of(changes["domain"])
        if "name" in changes and changes["name"]:
            changes["normalized_name"] = normalize_name(changes["name"])
        if changes.get("custom_fields"):
            changes["custom_fields"] = self.validate_custom_fields(ctx, "companies", changes["custom_fields"])
        company = self.store.update(ctx, "companies", company_id, changes, expected_version=expected_version)
        provenance(self.store, ctx, "companies", company_id, source_kind="manual", source_name="user",
                   original=changes, normalized=changes, match_rule="edit")
        audit(self.store, ctx, "companies.update", entity_type="companies", entity_id=company_id,
              changes=changes)
        return company

    def merge_companies(self, ctx: Ctx, keep_id: str, merge_ids: Sequence[str]) -> Dict[str, Any]:
        """Fold duplicates into ``keep_id``. Children move; losers are marked merged, never deleted."""
        ctx.require_write()
        keep = self.store.get(ctx, "companies", keep_id)
        system = ctx.as_system()
        moved: Dict[str, int] = {}
        for merge_id in merge_ids:
            if merge_id == keep_id:
                continue
            loser = self.store.get(ctx, "companies", merge_id)
            if loser["status"] == "merged":
                raise ConflictError(f"{merge_id} is already merged into {loser['merged_into_id']}")
            for entity in _COMPANY_CHILDREN:
                for row in self.store.all(ctx, entity, {"company_id": merge_id}):
                    try:
                        self.store.update(system, entity, row["id"], {"company_id": keep_id})
                        moved[entity] = moved.get(entity, 0) + 1
                    except ConflictError:
                        pass  # the survivor already has the same unique row (e.g. a technology)
            for row in self.store.all(ctx, "source_records", {"entity_type": "companies", "entity_id": merge_id}):
                self.store.update(system, "source_records", row["id"],
                                  {"entity_id": keep_id, "match_rule": f"merged_from:{merge_id}"})
            loser_values = {k: v for k, v in loser.items() if k in get_spec("companies").columns}
            loser_domain = loser_values.pop("domain", None)
            changes, conflicts = self._merge_values(keep, loser_values)
            self.store.update(ctx, "companies", merge_id, {"status": "merged", "merged_into_id": keep_id,
                                                           "domain": None})
            if loser_domain and not keep.get("domain"):
                changes["domain"] = loser_domain
            elif loser_domain and loser_domain != keep.get("domain"):
                conflicts["domain"] = {"kept": keep.get("domain"), "incoming": loser_domain}
            changes["source_count"] = keep["source_count"] + loser["source_count"]
            keep = self.store.update(ctx, "companies", keep_id, changes)
            audit(self.store, ctx, "companies.merge", entity_type="companies", entity_id=keep_id,
                  summary=f"merged {merge_id} into {keep_id}", changes={"conflicts": conflicts})
            record_activity(self.store, ctx, "company_merged", f"Merged {loser['name']} into this company",
                            company_id=keep_id, data={"merged_id": merge_id, "conflicts": conflicts})
        return {"company": keep, "moved": moved}

    # --- relationships -------------------------------------------------------------

    def add_relationship(self, ctx: Ctx, company_id: str, related_company_id: str, relationship: str, *,
                         source: str = "manual", confidence: Optional[float] = None,
                         evidence: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """``(A, B, "subsidiary")``: A is a subsidiary of B. ``(A, B, "parent")``: A is B's parent."""
        if company_id == related_company_id:
            raise ValidationError("a company cannot be related to itself")
        self.store.get(ctx, "companies", company_id)
        self.store.get(ctx, "companies", related_company_id)
        row = self.store.insert(ctx, "company_relationships", {
            "company_id": company_id, "related_company_id": related_company_id, "relationship": relationship,
            "source": source, "confidence": confidence, "evidence": dict(evidence or {})})
        if relationship == "subsidiary":
            self.store.update(ctx, "companies", company_id, {"parent_company_id": related_company_id})
        elif relationship == "parent":
            self.store.update(ctx, "companies", related_company_id, {"parent_company_id": company_id})
        audit(self.store, ctx, "company_relationships.create", entity_type="company_relationships",
              entity_id=row["id"], summary=f"{company_id} {relationship} {related_company_id}")
        return row

    def relationships(self, ctx: Ctx, company_id: str) -> List[Dict[str, Any]]:
        out = self.store.all(ctx, "company_relationships", {"company_id": company_id})
        out += self.store.all(ctx, "company_relationships", {"related_company_id": company_id})
        return out

    # --- contacts --------------------------------------------------------------------

    def _normalize_contact(self, ctx: Ctx, values: Mapping[str, Any]) -> Dict[str, Any]:
        spec = get_spec("contacts")
        clean = {k: v for k, v in values.items() if v is not None and not (isinstance(v, str) and blank(v))}
        for key in clean:
            if key not in spec.columns:
                raise ValidationError(f"contacts has no field {key!r}")
        if not clean.get("full_name"):
            name = " ".join(p for p in (clean.get("first_name"), clean.get("last_name")) if p)
            if not name:
                raise ValidationError("a contact needs a name")
            clean["full_name"] = name
        clean["full_name"] = " ".join(str(clean["full_name"]).split())
        if not clean.get("first_name") and not clean.get("last_name"):
            first, last = split_full_name(clean["full_name"])
            clean["first_name"], clean["last_name"] = first or None, last or None
        if "email" in clean:
            email = normalize_email(clean["email"])
            if email is None:
                raise ValidationError(f"{clean['email']!r} is not an email address")
            clean["email"] = email
        if clean.get("title") and (not clean.get("function") or not clean.get("seniority")):
            self._classify_title(clean)
        if clean.get("custom_fields"):
            clean["custom_fields"] = self.validate_custom_fields(ctx, "contacts", clean["custom_fields"])
        return clean

    @staticmethod
    def _classify_title(clean: Dict[str, Any]) -> None:
        try:
            from cloud.intel.vendor import seamless_targeting, zc_extract

            if not clean.get("function"):
                clean["function"] = seamless_targeting.classify(clean["title"])
            role = zc_extract.role_of(clean["title"])
            if role:
                _bucket, department, seniority = role
                if department and not clean.get("department"):
                    clean["department"] = department[:100]
                if seniority and not clean.get("seniority"):
                    clean["seniority"] = seniority[:60]
        except Exception:  # noqa: BLE001 - classification is a hint, never a blocker
            log.debug("title classification failed", exc_info=True)

    def _company_for_email(self, ctx: Ctx, email: Optional[str]) -> Optional[str]:
        if not email:
            return None
        domain = domain_of(email)
        if not domain or domain in FREE_EMAIL_DOMAINS:
            return None
        rows = self.store.list(ctx, "companies", {"domain": domain, "status__ne": "merged"}, limit=2).rows
        return rows[0]["id"] if len(rows) == 1 else None

    def upsert_contact(self, ctx: Ctx, values: Mapping[str, Any], *, source_kind: str, source_name: str,
                       source_ref: Optional[str] = None, original: Optional[Mapping[str, Any]] = None,
                       confidence: Optional[float] = None, import_batch_id: Optional[str] = None,
                       import_file_id: Optional[str] = None, row_number: Optional[int] = None) -> Dict[str, Any]:
        ctx.require_write()
        clean = self._normalize_contact(ctx, values)
        if clean.get("company_id"):
            self.store.get(ctx, "companies", clean["company_id"])
        else:
            company_id = self._company_for_email(ctx, clean.get("email"))
            if company_id:
                clean["company_id"] = company_id
        existing = None
        if clean.get("email"):
            existing = self.store.first(ctx, "contacts", {"email": clean["email"]})
        if existing is None and clean.get("company_id"):
            existing = self.store.first(ctx, "contacts", {"full_name": clean["full_name"],
                                                          "company_id": clean["company_id"]})
        prov = dict(source_kind=source_kind, source_name=source_name, source_ref=source_ref,
                    import_batch_id=import_batch_id, import_file_id=import_file_id, row_number=row_number,
                    confidence=confidence, original=dict(original if original is not None else values))
        if existing is not None:
            changes, conflicts = self._merge_values(existing, {k: v for k, v in clean.items()
                                                              if k not in ("full_name",)})
            contact = self.store.update(ctx, "contacts", existing["id"], changes) if changes else existing
            provenance(self.store, ctx, "contacts", contact["id"], match_rule="matched",
                       normalized={**{k: _jsonable(v) for k, v in clean.items()}, "conflicts": conflicts}, **prov)
            audit(self.store, ctx, "contacts.merge_record", entity_type="contacts", entity_id=contact["id"],
                  changes={"filled": sorted(changes), "conflicts": conflicts})
            return {"contact": contact, "created": False}
        clean.setdefault("source", source_kind[:60])
        clean.setdefault("source_date", utcnow())
        if confidence is not None:
            clean.setdefault("confidence", confidence)
        contact = self.store.insert(ctx, "contacts", clean)
        provenance(self.store, ctx, "contacts", contact["id"], match_rule="created",
                   normalized={k: _jsonable(v) for k, v in clean.items()}, **prov)
        audit(self.store, ctx, "contacts.create", entity_type="contacts", entity_id=contact["id"],
              summary=contact["full_name"])
        if contact.get("company_id"):
            record_activity(self.store, ctx, "contact_added", f"New contact: {contact['full_name']}",
                            company_id=contact["company_id"], contact_id=contact["id"])
        _emit(self.platform, ctx, "new_contact", f"contact:{contact['id']}",
              {"contact_id": contact["id"], "company_id": contact.get("company_id")})
        return {"contact": contact, "created": True}

    def update_contact(self, ctx: Ctx, contact_id: str, changes: Mapping[str, Any],
                       expected_version: Optional[int] = None) -> Dict[str, Any]:
        changes = dict(changes)
        if changes.get("email"):
            email = normalize_email(changes["email"])
            if email is None:
                raise ValidationError(f"{changes['email']!r} is not an email address")
            changes["email"] = email
            # A changed address has not been validated yet, whatever the old one's status was.
            changes.setdefault("email_status", "UNVERIFIED")
        if changes.get("custom_fields"):
            changes["custom_fields"] = self.validate_custom_fields(ctx, "contacts", changes["custom_fields"])
        contact = self.store.update(ctx, "contacts", contact_id, changes, expected_version=expected_version)
        audit(self.store, ctx, "contacts.update", entity_type="contacts", entity_id=contact_id, changes=changes)
        return contact

    # --- opportunities -------------------------------------------------------------

    def create_opportunity(self, ctx: Ctx, company_id: str, title: str, *, signal_ids: Iterable[str] = (),
                           signal_types: Iterable[str] = (), score: Optional[float] = None,
                           score_breakdown: Optional[Mapping[str, Any]] = None, reason: Optional[str] = None,
                           campaign_id: Optional[str] = None, contact_id: Optional[str] = None,
                           evidence: Iterable[Any] = (), source: str = "manual",
                           pipeline_id: Optional[str] = None, stage_id: Optional[str] = None,
                           owner_id: Optional[str] = None, amount: Optional[float] = None) -> Dict[str, Any]:
        ctx.require_write()
        company = self.store.get(ctx, "companies", company_id)
        pipeline = self.store.get(ctx, "pipelines", pipeline_id) if pipeline_id else self.default_pipeline(ctx)
        stages = self.stages(ctx, pipeline["id"])
        if not stages:
            raise ValidationError(f"pipeline {pipeline['name']} has no stages")
        if stage_id:
            stage = next((s for s in stages if s["id"] == stage_id), None)
            if stage is None:
                raise ValidationError("that stage does not belong to the pipeline")
        else:
            stage = stages[0]
        opportunity = self.store.insert(ctx, "opportunities", {
            "company_id": company_id, "contact_id": contact_id, "title": title, "pipeline_id": pipeline["id"],
            "stage_id": stage["id"], "status": "won" if stage["is_won"] else "lost" if stage["is_lost"] else "open",
            "score": score, "score_breakdown": dict(score_breakdown or {}), "signal_ids": list(signal_ids),
            "signal_types": list(signal_types), "reason": reason, "campaign_id": campaign_id,
            "evidence": list(evidence), "source": source, "owner_id": owner_id or ctx.user_id, "amount": amount})
        record_activity(self.store, ctx, "opportunity_created", f"Opportunity created: {title}",
                        company_id=company_id, opportunity_id=opportunity["id"], campaign_id=campaign_id,
                        data={"stage": stage["name"], "reason": reason, "source": source})
        audit(self.store, ctx, "opportunities.create", entity_type="opportunities", entity_id=opportunity["id"],
              summary=f"{title} for {company['name']}")
        return opportunity

    def move_stage(self, ctx: Ctx, opportunity_id: str, stage_id: str) -> Dict[str, Any]:
        opportunity = self.store.get(ctx, "opportunities", opportunity_id)
        stage = self.store.get(ctx, "pipeline_stages", stage_id)
        if stage["pipeline_id"] != opportunity["pipeline_id"]:
            raise ValidationError("that stage belongs to a different pipeline")
        previous = self.store.get(ctx, "pipeline_stages", opportunity["stage_id"])
        status = "won" if stage["is_won"] else "lost" if stage["is_lost"] else "open"
        updated = self.store.update(ctx, "opportunities", opportunity_id, {"stage_id": stage_id, "status": status},
                                    expected_version=opportunity["version"])
        record_activity(self.store, ctx, "stage_changed", f"{previous['name']} → {stage['name']}",
                        company_id=opportunity["company_id"], opportunity_id=opportunity_id,
                        data={"from": previous["id"], "to": stage_id, "status": status})
        audit(self.store, ctx, "opportunities.stage", entity_type="opportunities", entity_id=opportunity_id,
              changes={"from": previous["name"], "to": stage["name"]})
        return updated

    # --- tasks, notes, activities ----------------------------------------------------

    def create_task(self, ctx: Ctx, values: Mapping[str, Any]) -> Dict[str, Any]:
        task = self.store.insert(ctx, "crm_tasks", {"assignee_id": ctx.user_id, **values})
        audit(self.store, ctx, "crm_tasks.create", entity_type="crm_tasks", entity_id=task["id"],
              summary=task["title"])
        if task.get("company_id"):
            record_activity(self.store, ctx, "task_created", f"Task: {task['title']}", company_id=task["company_id"],
                            contact_id=task.get("contact_id"), opportunity_id=task.get("opportunity_id"))
        return task

    def add_note(self, ctx: Ctx, body: str, **links: Any) -> Dict[str, Any]:
        note = self.store.insert(ctx, "notes", {"body": body, **links})
        audit(self.store, ctx, "notes.create", entity_type="notes", entity_id=note["id"])
        return note

    def log_activity(self, ctx: Ctx, kind: str, summary: str, **links: Any) -> Dict[str, Any]:
        return record_activity(self.store, ctx, kind, summary, **links)

    def company_timeline(self, ctx: Ctx, company_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        self.store.get(ctx, "companies", company_id)
        items: List[Dict[str, Any]] = []
        sources = (
            ("activities", "occurred_at", lambda r: r["summary"], lambda r: r["kind"]),
            ("notes", "created_at", lambda r: r["body"][:300], lambda r: "note"),
            ("crm_tasks", "created_at", lambda r: r["title"], lambda r: f"task:{r['status']}"),
            ("hiring_signals", "detected_at", lambda r: r.get("summary") or r["signal_type"],
             lambda r: f"signal:{r['signal_type']}"),
            ("change_events", "detected_at", lambda r: r.get("summary") or r["change_type"],
             lambda r: f"change:{r['change_type']}"),
        )
        for entity, when, text, kind in sources:
            for row in self.store.list(ctx, entity, {"company_id": company_id}, order=f"-{when}", limit=limit).rows:
                items.append({"type": entity, "id": row["id"], "at": row[when], "kind": kind(row),
                              "summary": text(row)})
        items.sort(key=lambda item: item["at"], reverse=True)
        return items[:limit]

    # --- lists and segments ------------------------------------------------------------

    _LIST_ENTITIES = {"companies", "contacts", "job_postings", "opportunities"}

    def create_list(self, ctx: Ctx, name: str, entity_type: str, *, description: Optional[str] = None,
                    source: Optional[str] = None) -> Dict[str, Any]:
        row = self.store.insert(ctx, "lists", {"name": name, "entity_type": entity_type, "description": description,
                                               "source": source})
        audit(self.store, ctx, "lists.create", entity_type="lists", entity_id=row["id"], summary=name)
        return row

    def add_to_list(self, ctx: Ctx, list_id: str, entity_type: str, ids: Sequence[str],
                    reason: Optional[str] = None) -> int:
        target = self.store.get(ctx, "lists", list_id)
        if entity_type != target["entity_type"]:
            raise ValidationError(f"list {target['name']} holds {target['entity_type']}, not {entity_type}")
        added = 0
        for entity_id in dict.fromkeys(ids):
            if self.store.find(ctx, entity_type, entity_id) is None:
                raise NotFoundError(f"{entity_type} {entity_id} not found")
            try:
                self.store.insert(ctx, "list_members", {"list_id": list_id, "entity_type": entity_type,
                                                        "entity_id": entity_id, "added_reason": reason})
                added += 1
            except ConflictError:
                pass
        self._recount(ctx, list_id)
        audit(self.store, ctx, "lists.add", entity_type="lists", entity_id=list_id, changes={"added": added})
        return added

    def remove_from_list(self, ctx: Ctx, list_id: str, ids: Sequence[str]) -> int:
        removed = 0
        for entity_id in ids:
            row = self.store.first(ctx, "list_members", {"list_id": list_id, "entity_id": entity_id})
            if row is not None:
                self.store.delete(ctx, "list_members", row["id"])
                removed += 1
        self._recount(ctx, list_id)
        audit(self.store, ctx, "lists.remove", entity_type="lists", entity_id=list_id, changes={"removed": removed})
        return removed

    def _recount(self, ctx: Ctx, list_id: str) -> None:
        self.store.update(ctx, "lists", list_id,
                          {"member_count": self.store.count(ctx, "list_members", {"list_id": list_id})})

    def list_members(self, ctx: Ctx, list_id: str, *, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        target = self.store.get(ctx, "lists", list_id)
        page = self.store.list(ctx, "list_members", {"list_id": list_id}, limit=limit, offset=offset)
        items = []
        for member in page.rows:
            record = self.store.find(ctx, target["entity_type"], member["entity_id"])
            items.append({**member, "record": record})
        return {"items": items, "total": page.total, "limit": page.limit, "offset": page.offset}

    def evaluate_segment(self, ctx: Ctx, segment_id: str, *, limit: int = 50, offset: int = 0,
                         order: Optional[str] = None):
        segment = self.store.get(ctx, "segments", segment_id)
        return self.store.list(ctx, segment["entity_type"], segment["filters"], order=order, limit=limit,
                               offset=offset)

    def create_segment(self, ctx: Ctx, name: str, entity_type: str, filters: Mapping[str, Any],
                       description: Optional[str] = None) -> Dict[str, Any]:
        # Validate the filters now, so a broken segment is refused rather than stored.
        self.store.list(ctx, entity_type, filters, limit=1)
        row = self.store.insert(ctx, "segments", {"name": name, "entity_type": entity_type,
                                                  "filters": dict(filters), "description": description})
        audit(self.store, ctx, "segments.create", entity_type="segments", entity_id=row["id"], summary=name)
        return row
