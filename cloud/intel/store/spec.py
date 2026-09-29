"""Every platform table, declared once.

The specs below are the single source of truth for the platform's data model.
They drive three things that must agree with each other:

* the generated SQL in ``cloud/db/migrations/0003_platform.sql``
  (``python -m cloud.intel.store.ddl --write``; a test fails if the committed
  file differs from what the specs generate);
* :class:`~cloud.intel.store.memory.MemoryStore`, used by offline tests;
* :class:`~cloud.intel.store.postgres.PostgresStore`, used everywhere else.

Every table is **workspace-scoped**: it has ``workspace_id`` and row-level
security that admits only members of that workspace. Column names that reach
SQL always come from these specs, never from a request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = ["ENTITIES", "Col", "EntitySpec", "entity", "COMMON_COLUMNS"]

# Column kinds and their PostgreSQL types.
PG_TYPES = {
    "text": "text",
    "int": "integer",
    "bigint": "bigint",
    "float": "double precision",
    "bool": "boolean",
    "json": "jsonb",
    "ts": "timestamptz",
    "date": "date",
    "uuid": "uuid",
    "tags": "text[]",
}


@dataclass(frozen=True)
class Col:
    kind: str = "text"
    required: bool = False
    default: Any = None
    max_len: Optional[int] = None
    choices: Optional[Tuple[str, ...]] = None
    #: Included in the free-text ``q`` search (ILIKE).
    search: bool = False
    #: Create a (workspace_id, column) index.
    index: bool = False
    #: Refuse changes after insert.
    immutable: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    #: Choices a later migration widened: ``(migration, original choices)``. The
    #: creating migration keeps the original list (applied files never change);
    #: the named migration replaces the check constraint with ``choices``.
    widened: Optional[Tuple[str, Tuple[str, ...]]] = None

    def __post_init__(self) -> None:
        if self.kind not in PG_TYPES:
            raise ValueError(f"unknown column kind {self.kind!r}")


@dataclass(frozen=True)
class EntitySpec:
    name: str
    table: str
    prefix: str
    columns: Mapping[str, Col]
    #: Unique column groups within a workspace. A group whose columns are all
    #: nullable is enforced only where every column is non-null.
    unique: Tuple[Tuple[str, ...], ...] = ()
    #: Users may insert and read but never update or delete (audit, ledgers).
    append_only: bool = False
    #: Users may only read; only the system scope (worker/service) writes.
    system_write: bool = False
    default_order: str = "created_at desc"
    description: str = ""
    #: Which generated migration creates this table. Applied migrations never
    #: change, so new entities go into a new migration file.
    migration: str = "0003"

    def column(self, name: str) -> Col:
        return self.columns[name]

    @property
    def searchable(self) -> Tuple[str, ...]:
        return tuple(n for n, c in self.columns.items() if c.search)

    @property
    def all_columns(self) -> Tuple[str, ...]:
        return tuple(COMMON_COLUMNS) + tuple(self.columns)


#: Present on every table, managed by the store (never by callers).
COMMON_COLUMNS: Dict[str, Col] = {
    "id": Col("text", required=True, immutable=True),
    "workspace_id": Col("uuid", required=True, immutable=True),
    "created_at": Col("ts", required=True, immutable=True),
    "updated_at": Col("ts", required=True),
    "created_by": Col("uuid", immutable=True),
    "version": Col("int", required=True, default=1),
}


def _t(max_len: int = 300, **kw: Any) -> Col:
    return Col("text", max_len=max_len, **kw)


def _choice(*choices: str, default: Optional[str] = None, required: bool = True, index: bool = True) -> Col:
    return Col("text", choices=tuple(choices), default=default if default is not None else choices[0],
               required=required, index=index)


def _j(default: Any = None) -> Col:
    return Col("json", required=True, default=({} if default is None else default))


def _tags() -> Col:
    return Col("tags", required=True, default=[])


def _score() -> Col:
    return Col("float", minimum=0, maximum=100)


def _conf() -> Col:
    return Col("float", minimum=0, maximum=1)


ENTITIES: Dict[str, EntitySpec] = {}


def entity(name: str, prefix: str, columns: Mapping[str, Col], **kw: Any) -> EntitySpec:
    spec = EntitySpec(name=name, table=kw.pop("table", name), prefix=prefix, columns=dict(columns), **kw)
    if name in ENTITIES:
        raise ValueError(f"duplicate entity {name}")
    for col in spec.columns:
        if col in COMMON_COLUMNS:
            raise ValueError(f"{name}.{col} shadows a common column")
    ENTITIES[name] = spec
    return spec


SOURCE_KINDS = ("manual", "import", "crawler", "discovery", "external_source", "zoominfo", "seamless",
                "scraper", "research", "public_web", "email_validation", "api", "workflow", "system")

# ---------------------------------------------------------------------------
# Track A: CRM
# ---------------------------------------------------------------------------

entity("companies", "co", {
    "name": _t(300, required=True, search=True, index=True),
    "normalized_name": _t(300, index=True),
    "legal_name": _t(300, search=True),
    "aliases": _tags(),
    "domain": _t(253, search=True, index=True),
    "website": _t(2048),
    "careers_url": _t(2048),
    "linkedin_url": _t(500),
    "industry": _t(200, index=True),
    "sub_industry": _t(200),
    "sic_codes": _tags(),
    "naics_codes": _tags(),
    "country": _t(100, index=True),
    "state": _t(100, index=True),
    "city": _t(120),
    "employee_range": _t(60),
    "employee_count": Col("int", minimum=0),
    "revenue_range": _t(60),
    "revenue_usd": Col("float", minimum=0),
    "technologies": _tags(),
    "ats": _t(100, index=True),
    "lifecycle": _choice("prospect", "account", "customer", "partner", "disqualified"),
    "status": _choice("active", "merged", "inactive", "archived"),
    "merged_into_id": _t(40),
    "parent_company_id": _t(40),
    "hiring_count": Col("int", required=True, default=0, minimum=0),
    "hiring_velocity": Col("float"),
    "hiring_signals": _tags(),
    "account_score": _score(),
    "hiring_score": _score(),
    "opportunity_score": _score(),
    "score_breakdown": _j(),
    "confidence": _conf(),
    "owner_id": Col("uuid", index=True),
    "tags": _tags(),
    "custom_fields": _j(),
    "first_seen_at": Col("ts"),
    "last_seen_at": Col("ts"),
    "last_crawled_at": Col("ts"),
    "source_count": Col("int", required=True, default=0, minimum=0),
    "description": _t(4000),
}, unique=(("domain",),), description="The company master: one row per real-world company.")

entity("company_relationships", "rel", {
    "company_id": _t(40, required=True, index=True),
    "related_company_id": _t(40, required=True, index=True),
    "relationship": _choice("parent", "subsidiary", "affiliate", "acquired", "partner", "competitor", "duplicate_of"),
    "source": _t(100),
    "confidence": _conf(),
    "evidence": _j(),
}, unique=(("company_id", "related_company_id", "relationship"),))

entity("contacts", "ct", {
    "company_id": _t(40, index=True),
    "full_name": _t(200, required=True, search=True),
    "first_name": _t(100),
    "last_name": _t(100),
    "title": _t(300, search=True),
    "department": _t(100, index=True),
    "seniority": _t(60, index=True),
    "function": _t(60, index=True),
    "email": _t(320, search=True, index=True),
    "email_status": _choice("UNVERIFIED", "VALID", "INVALID", "RISKY", "UNKNOWN", "DISPOSABLE", "ROLE",
                            "FREE_PROVIDER", default="UNVERIFIED"),
    "email_score": _score(),
    "email_validated_at": Col("ts"),
    "phone": _t(60),
    "linkedin_url": _t(500),
    "location": _t(200),
    "source": _t(60),
    "source_date": Col("ts"),
    "confidence": _conf(),
    "contact_score": _score(),
    "score_breakdown": _j(),
    "validation_status": _choice("unverified", "verified", "needs_verification", "rejected"),
    "status": _choice("active", "left_company", "do_not_contact", "merged", "archived"),
    "owner_id": Col("uuid", index=True),
    "tags": _tags(),
    "custom_fields": _j(),
    "unsubscribed": Col("bool", required=True, default=False),
}, unique=(("email",),))

entity("pipelines", "pl", {
    "name": _t(120, required=True),
    "is_default": Col("bool", required=True, default=False),
    "description": _t(1000),
}, unique=(("name",),))

entity("pipeline_stages", "st", {
    "pipeline_id": _t(40, required=True, index=True),
    "name": _t(120, required=True),
    "position": Col("int", required=True, default=0, minimum=0),
    "probability": Col("float", minimum=0, maximum=100),
    "is_won": Col("bool", required=True, default=False),
    "is_lost": Col("bool", required=True, default=False),
}, unique=(("pipeline_id", "name"),), default_order="position asc")

entity("opportunities", "op", {
    "company_id": _t(40, required=True, index=True),
    "contact_id": _t(40),
    "title": _t(300, required=True, search=True),
    "pipeline_id": _t(40, required=True, index=True),
    "stage_id": _t(40, required=True, index=True),
    "status": _choice("open", "won", "lost"),
    "score": _score(),
    "score_breakdown": _j(),
    "signal_ids": _tags(),
    "signal_types": _tags(),
    "reason": _t(2000),
    "amount": Col("float", minimum=0),
    "currency": _t(3),
    "campaign_id": _t(40, index=True),
    "owner_id": Col("uuid", index=True),
    "next_action": _t(500),
    "next_action_at": Col("ts"),
    "close_date": Col("date"),
    "source": _t(60),
    "evidence": _j([]),
    "tags": _tags(),
    "custom_fields": _j(),
})

entity("crm_tasks", "tk", {
    "title": _t(300, required=True, search=True),
    "description": _t(4000),
    "status": _choice("open", "in_progress", "done", "cancelled"),
    "priority": _choice("normal", "low", "high", "urgent"),
    "due_at": Col("ts", index=True),
    "assignee_id": Col("uuid", index=True),
    "company_id": _t(40, index=True),
    "contact_id": _t(40, index=True),
    "opportunity_id": _t(40, index=True),
    "source": _t(60),
})

entity("notes", "nt", {
    "body": _t(20000, required=True, search=True),
    "company_id": _t(40, index=True),
    "contact_id": _t(40, index=True),
    "opportunity_id": _t(40, index=True),
})

entity("activities", "ac", {
    "kind": _t(60, required=True, index=True),
    "summary": _t(1000, required=True, search=True),
    "occurred_at": Col("ts", required=True),
    "actor_id": Col("uuid"),
    "company_id": _t(40, index=True),
    "contact_id": _t(40, index=True),
    "opportunity_id": _t(40, index=True),
    "campaign_id": _t(40, index=True),
    "data": _j(),
}, append_only=True, default_order="occurred_at desc")

entity("tags", "tg", {
    "name": _t(80, required=True, search=True),
    "color": _t(20),
}, unique=(("name",),), default_order="name asc")

entity("lists", "ls", {
    "name": _t(200, required=True, search=True),
    "entity_type": _choice("companies", "contacts", "job_postings", "opportunities"),
    "description": _t(2000),
    "member_count": Col("int", required=True, default=0, minimum=0),
    "source": _t(60),
}, unique=(("name",),))

entity("list_members", "lm", {
    "list_id": _t(40, required=True, index=True),
    "entity_type": _t(40, required=True),
    "entity_id": _t(40, required=True, index=True),
    "added_reason": _t(500),
}, unique=(("list_id", "entity_id"),))

entity("segments", "sg", {
    "name": _t(200, required=True, search=True),
    "entity_type": _choice("companies", "contacts", "job_postings", "opportunities"),
    "filters": _j(),
    "description": _t(2000),
}, unique=(("name",),))

entity("custom_field_defs", "cf", {
    "entity_type": _choice("companies", "contacts", "opportunities"),
    "key": _t(60, required=True),
    "label": _t(120, required=True),
    "field_type": _choice("text", "number", "date", "bool", "select"),
    "options": _tags(),
}, unique=(("entity_type", "key"),))

# ---------------------------------------------------------------------------
# Provenance and the internal data engine (imports)
# ---------------------------------------------------------------------------

entity("source_records", "sr", {
    "entity_type": _t(40, required=True, index=True),
    "entity_id": _t(40, required=True, index=True),
    "source_kind": Col("text", choices=SOURCE_KINDS, required=True, index=True),
    "source_name": _t(200, required=True),
    "source_ref": _t(2048),
    "import_batch_id": _t(40, index=True),
    "import_file_id": _t(40),
    "row_number": Col("int", minimum=0),
    "original": _j(),
    "normalized": _j(),
    "observed_at": Col("ts", required=True),
    "confidence": _conf(),
    "match_rule": _t(100),
}, append_only=True)

entity("import_batches", "ib", {
    "name": _t(200, required=True),
    "target": _choice("companies", "contacts", "companies_and_contacts"),
    "status": _choice("uploaded", "validated", "rejected", "mapped", "merging", "merged", "failed"),
    "file_count": Col("int", required=True, default=0, minimum=0),
    "row_count": Col("int", required=True, default=0, minimum=0),
    "mapping": _j(),
    "validation": _j(),
    "stats": _j(),
    "error": _t(4000),
})

entity("import_files", "if", {
    "batch_id": _t(40, required=True, index=True),
    "filename": _t(255, required=True),
    "format": _choice("csv", "xlsx", "json"),
    "sheet": _t(120),
    "sha256": _t(64, required=True),
    "size_bytes": Col("bigint", required=True, default=0, minimum=0),
    "row_count": Col("int", required=True, default=0, minimum=0),
    "columns": _tags(),
    "status": _choice("uploaded", "compatible", "incompatible", "merged"),
    "problems": _j([]),
    "storage_key": _t(512),
}, unique=(("batch_id", "sha256"),))

entity("import_rows", "ir", {
    "batch_id": _t(40, required=True, index=True),
    "file_id": _t(40, required=True, index=True),
    "row_number": Col("int", required=True, minimum=0),
    "original": _j(),
    "normalized": _j(),
    "status": _choice("pending", "merged", "duplicate", "rejected"),
    "company_id": _t(40),
    "contact_id": _t(40),
    "problems": _j([]),
}, unique=(("file_id", "row_number"),), default_order="row_number asc")

# ---------------------------------------------------------------------------
# Track B: company discovery
# ---------------------------------------------------------------------------

entity("discovery_candidates", "dc", {
    "name": _t(300, required=True, search=True),
    "domain": _t(253, index=True),
    "website": _t(2048),
    "source_kind": Col("text", choices=SOURCE_KINDS, required=True),
    "source_name": _t(200, required=True),
    "status": _choice("NEW_COMPANY_DISCOVERY", "DUPLICATE", "NEEDS_REVIEW", "APPROVED", "REJECTED"),
    "matched_company_id": _t(40),
    "match_strength": _choice("none", "strong", "probable", "ambiguous", required=False),
    "website_verified": Col("bool"),
    "careers_url": _t(2048),
    "ats": _t(100),
    "industry": _t(200),
    "category": _t(200),
    "country": _t(100),
    "state": _t(100),
    "city": _t(120),
    "confidence": _conf(),
    "evidence": _j([]),
    "steps": _j(),
    "decided_by": Col("uuid"),
    "decided_at": Col("ts"),
    "company_id": _t(40),
})

# ---------------------------------------------------------------------------
# Track C/D: jobs and hiring intelligence
# ---------------------------------------------------------------------------

entity("job_postings", "jp", {
    "company_id": _t(40, index=True),
    "company_name": _t(300, required=True, search=True),
    "domain": _t(253, index=True),
    "title": _t(500, required=True, search=True),
    "normalized_title": _t(500),
    "job_url": _t(2048, required=True),
    "url_key": _t(2048, required=True),
    "external_id": _t(200),
    "description": _t(50000),
    "posted_at": Col("ts"),
    "first_seen_at": Col("ts", required=True, index=True),
    "last_seen_at": Col("ts", required=True),
    "closed_at": Col("ts"),
    "location": _t(500),
    "country": _t(100, index=True),
    "workplace_type": _choice("unknown", "remote", "hybrid", "onsite"),
    "employment_type": _t(60),
    "department": _t(100, index=True),
    "seniority": _t(60, index=True),
    "technologies": _tags(),
    "skills": _tags(),
    "years_experience_min": Col("int", minimum=0, maximum=60),
    "certifications": _tags(),
    "industry": _t(200),
    "ats": _t(100, index=True),
    "source_kind": Col("text", choices=SOURCE_KINDS, required=True),
    "source_name": _t(200, required=True, index=True),
    "status": _choice("open", "closed"),
    "is_relevant": Col("bool", required=True, default=False),
    "relevance_reasons": _tags(),
    "campaign_keys": _tags(),
}, unique=(("url_key",),), default_order="first_seen_at desc")

entity("hiring_signals", "hs", {
    "company_id": _t(40, required=True, index=True),
    "signal_type": _choice("NEW_ROLE", "MULTIPLE_RELEVANT_ROLES", "HIRING_SPIKE", "HIRING_VELOCITY",
                           "LONG_OPEN_ROLE", "HARD_TO_FILL", "SPECIALIZED_TECHNOLOGY", "PROJECT_IMPLEMENTATION",
                           "EXPANSION_HIRING", "BACKFILL_REPLACEMENT", "LEADERSHIP_HIRING"),
    "detected_at": Col("ts", required=True),
    "window_start": Col("ts"),
    "window_end": Col("ts"),
    "confidence": _conf(),
    "strength": _score(),
    "reason_codes": _tags(),
    "summary": _t(1000),
    "evidence": _j([]),
    "job_posting_ids": _tags(),
    "source": _t(100),
    "status": _choice("active", "expired", "dismissed"),
    "fingerprint": _t(200, required=True),
}, unique=(("fingerprint",),), default_order="detected_at desc")

# ---------------------------------------------------------------------------
# Track: technology intelligence
# ---------------------------------------------------------------------------

entity("company_technologies", "tc", {
    "company_id": _t(40, required=True, index=True),
    "technology": _t(200, required=True, search=True),
    "category": _t(100, index=True),
    "vendor": _t(200),
    "source": _t(100, required=True),
    "evidence_url": _t(2048),
    "evidence_text": _t(2000),
    "observed_at": Col("ts", required=True),
    "confidence": _conf(),
    "status": _choice("active", "removed"),
}, unique=(("company_id", "technology", "source"),))

# ---------------------------------------------------------------------------
# Providers, credentials and credits
# ---------------------------------------------------------------------------

entity("provider_connections", "pc", {
    "provider": _t(60, required=True, index=True),
    "kind": _choice("source", "enrichment", "technology", "email_validation", "ai", "sender", "scraper"),
    "label": _t(200),
    "status": _choice("not_configured", "configured", "verified", "error", "disabled"),
    "access_method": _choice("api", "browser_login", "public", "partner", "none"),
    "secret_ciphertext": _t(8000),
    "secret_hint": _t(40),
    "settings": _j(),
    "last_checked_at": Col("ts"),
    "last_error": _t(2000),
    "private": Col("bool", required=True, default=True),
}, unique=(("provider", "kind"),))

entity("credit_accounts", "ca", {
    "provider": _t(60, required=True),
    "total_credits": Col("float", required=True, default=0, minimum=0),
    "reserved_credits": Col("float", required=True, default=0, minimum=0),
    "consumed_credits": Col("float", required=True, default=0, minimum=0),
    "last_sync_at": Col("ts"),
    "sync_source": _t(100),
    "period": _t(60),
    "hard_limit": Col("float", minimum=0),
}, unique=(("provider",),), system_write=True)

entity("credit_ledger", "cl", {
    "provider": _t(60, required=True, index=True),
    "entry_type": _choice("grant", "sync", "reserve", "consume", "release", "adjust"),
    "amount": Col("float", required=True),
    "balance_after": Col("float"),
    "reason": _t(500, required=True),
    "action": _t(100),
    "task_id": _t(40, index=True),
    "reservation_id": _t(40, index=True),
    "idempotency_key": _t(200),
    "entity_type": _t(40),
    "entity_id": _t(40),
    "data": _j(),
}, unique=(("idempotency_key",),), append_only=True, system_write=True)

entity("usage_events", "ue", {
    "provider": _t(60, required=True, index=True),
    "operation": _t(100, required=True),
    "units": Col("int", required=True, default=1, minimum=0),
    "success": Col("bool", required=True, default=True),
    "latency_ms": Col("float"),
    "task_id": _t(40),
    "error": _t(1000),
}, append_only=True, system_write=True)

# ---------------------------------------------------------------------------
# Track G: email validation (the cache)
# ---------------------------------------------------------------------------

entity("email_validations", "ev", {
    "email": _t(320, required=True, index=True),
    "status": _choice("VALID", "INVALID", "RISKY", "UNKNOWN", "DISPOSABLE", "ROLE", "FREE_PROVIDER"),
    "score": _score(),
    "checks": _j(),
    "provider": _t(60, required=True),
    "validated_at": Col("ts", required=True),
    "expires_at": Col("ts", required=True),
    "raw": _j(),
}, unique=(("email",),))

# ---------------------------------------------------------------------------
# Track F: AI scraper
# ---------------------------------------------------------------------------

#: Fine-grained scraper run states (0006). ``running`` stays valid for older rows.
SCRAPE_RUN_STATES = ("queued", "planning", "fetching", "extracting", "paginating", "enriching", "validating",
                     "normalizing", "saving", "completed", "failed", "cancelled", "paused", "running")

entity("scrape_runs", "sc", {
    "instruction": _t(4000, required=True),
    "schema": _j(),
    "urls": _tags(),
    "status": Col("text", choices=SCRAPE_RUN_STATES, default="queued", required=True, index=True,
                  widened=("0006", ("queued", "running", "completed", "failed", "cancelled"))),
    "task_id": _t(40),
    "stats": _j(),
    "error": _t(4000),
})

entity("scrape_results", "sx", {
    "run_id": _t(40, required=True, index=True),
    "url": _t(2048, required=True),
    "final_url": _t(2048),
    "status": _choice("ok", "blocked", "error", "empty"),
    "method": _t(60),
    "data": _j(),
    "field_sources": _j(),
    "problems": _j([]),
    "fetched_at": Col("ts"),
}, default_order="created_at asc")

# AI scraper V2-V4 (0006): every page a run visited, saved templates, and CRM
# proposals built from results (applied only after review).

entity("scrape_pages", "spg", {
    "run_id": _t(40, required=True, index=True),
    "input_index": Col("int", required=True, default=0, minimum=0),
    "url_key": _t(64, required=True),
    "url": _t(2048, required=True),
    "final_url": _t(2048),
    "kind": _choice("input", "discovery", "careers", "listing", "detail", "api"),
    "depth": Col("int", required=True, default=0, minimum=0),
    "page_no": Col("int", minimum=0),
    "outcome": _t(30, required=True, index=True),
    "http_status": Col("int", minimum=0),
    "attempts": Col("int", required=True, default=1, minimum=0),
    "records": Col("int", required=True, default=0, minimum=0),
    "browser_used": Col("bool", required=True, default=False),
    "browser_reason": _t(200),
    "browser_duration_ms": Col("float", minimum=0),
    "browser_outcome": _t(30),
    "error": _t(1000),
    "fetched_at": Col("ts"),
}, unique=(("run_id", "url_key"),), default_order="created_at asc", migration="0006",
   description="Every page a scraper run visited, with its outcome (never fetched twice in one run).")

entity("scrape_templates", "stp", {
    "name": _t(200, required=True, search=True),
    "description": _t(1000),
    "category": _t(60),
    "instruction": _t(4000, required=True),
    "schema": _j(),
    "options": _j(),
}, unique=(("name",),), default_order="name asc", migration="0006",
   description="Saved scraper templates: instruction, edited schema and run options.")

entity("scrape_proposals", "spr", {
    "run_id": _t(40, required=True, index=True),
    "record_key": _t(200, required=True),
    "action": _choice("company", "contact", "job", "opportunity", "task"),
    "status": _choice("proposed", "approved", "rejected", "applied", "failed"),
    "match": _choice("new", "existing", "possible_duplicate", "conflict"),
    "match_company_id": _t(40),
    "match_reasons": _j([]),
    "payload": _j(),
    "applied_entity_type": _t(40),
    "applied_entity_id": _t(40),
    "error": _t(1000),
    "reviewed_at": Col("ts"),
}, unique=(("run_id", "record_key", "action"),), migration="0006",
   description="CRM changes proposed from scraper results. Applied only after a person approves them.")

# ---------------------------------------------------------------------------
# Track I: GTM campaigns, sequences, suppression
# ---------------------------------------------------------------------------

entity("campaigns", "cp", {
    "key": _t(60, required=True),
    "name": _t(200, required=True, search=True),
    "brand": _t(120),
    "description": _t(4000),
    "status": _choice("draft", "active", "paused", "archived"),
    "focus_keywords": _tags(),
    "technologies": _tags(),
    "departments": _tags(),
    "signal_types": _tags(),
    "target_titles": _tags(),
    "rules": _j(),
    "default_sequence_id": _t(40),
    "sending_enabled": Col("bool", required=True, default=False),
}, unique=(("key",),))

entity("email_templates", "et", {
    "name": _t(200, required=True, search=True),
    "subject": _t(500, required=True),
    "body": _t(20000, required=True),
    "variables": _tags(),
    "campaign_id": _t(40, index=True),
})

entity("sequences", "sq", {
    "name": _t(200, required=True, search=True),
    "campaign_id": _t(40, index=True),
    "status": _choice("draft", "active", "paused", "archived"),
    "stop_on_reply": Col("bool", required=True, default=True),
    "description": _t(2000),
})

entity("sequence_steps", "ss", {
    "sequence_id": _t(40, required=True, index=True),
    "position": Col("int", required=True, minimum=0),
    "channel": _choice("email", "task", "call", "linkedin_task"),
    "delay_days": Col("int", required=True, default=0, minimum=0, maximum=365),
    "template_id": _t(40),
    "instructions": _t(2000),
}, unique=(("sequence_id", "position"),), default_order="position asc")

entity("sequence_enrollments", "se", {
    "sequence_id": _t(40, required=True, index=True),
    "contact_id": _t(40, required=True, index=True),
    "campaign_id": _t(40),
    "opportunity_id": _t(40),
    "status": _choice("pending_approval", "active", "paused", "completed", "replied", "bounced",
                      "unsubscribed", "suppressed", "stopped"),
    "current_step": Col("int", required=True, default=0, minimum=0),
    "next_step_at": Col("ts", index=True),
    "variables": _j(),
    "approved_by": Col("uuid"),
    "approved_at": Col("ts"),
}, unique=(("sequence_id", "contact_id"),))

entity("message_events", "me", {
    "enrollment_id": _t(40, index=True),
    "contact_id": _t(40, index=True),
    "campaign_id": _t(40, index=True),
    "event": _choice("queued", "rendered", "sent", "delivered", "opened", "replied", "bounced",
                     "unsubscribed", "blocked", "failed"),
    "provider": _t(60),
    "provider_message_id": _t(300),
    "occurred_at": Col("ts", required=True),
    "data": _j(),
}, append_only=True, default_order="occurred_at desc")

entity("suppressions", "su", {
    "value": _t(320, required=True, search=True),
    "kind": _choice("email", "domain"),
    "reason": _choice("unsubscribe", "bounce", "complaint", "manual", "legal", "customer"),
    "source": _t(100),
}, unique=(("kind", "value"),))

# ---------------------------------------------------------------------------
# Track I: automation engine
# ---------------------------------------------------------------------------

entity("workflows", "wf", {
    "name": _t(200, required=True, search=True),
    "trigger": _t(60, required=True, index=True),
    "conditions": _j([]),
    "actions": _j([]),
    "enabled": Col("bool", required=True, default=False),
    "description": _t(2000),
    "max_runs_per_day": Col("int", minimum=0),
})

entity("workflow_runs", "wr", {
    "workflow_id": _t(40, required=True, index=True),
    "trigger": _t(60, required=True),
    "event_key": _t(300, required=True),
    "status": _choice("pending", "running", "succeeded", "failed", "skipped"),
    "attempts": Col("int", required=True, default=0, minimum=0),
    "input": _j(),
    "steps": _j([]),
    "error": _t(4000),
    "finished_at": Col("ts"),
}, unique=(("workflow_id", "event_key"),), system_write=True)

# ---------------------------------------------------------------------------
# Track J: research agent
# ---------------------------------------------------------------------------

entity("research_runs", "rr", {
    "question": _t(4000, required=True, search=True),
    "intent": _j(),
    "plan": _j([]),
    "status": _choice("planned", "approved", "running", "completed", "failed", "cancelled"),
    "planner": _t(60),
    "task_id": _t(40),
    "progress": _j(),
    "summary": _t(8000),
    "result_count": Col("int", required=True, default=0, minimum=0),
    "proposed_actions": _j([]),
    "estimated_credits": _j(),
    "error": _t(4000),
    "approved_by": Col("uuid"),
})

entity("research_results", "rx", {
    "run_id": _t(40, required=True, index=True),
    "rank": Col("int", required=True, minimum=0),
    "company_id": _t(40),
    "contact_id": _t(40),
    "data": _j(),
    "evidence": _j([]),
    "score": _score(),
}, default_order="rank asc")

# ---------------------------------------------------------------------------
# Monitoring and change detection
# ---------------------------------------------------------------------------

entity("monitors", "mo", {
    "name": _t(200, required=True),
    "target_type": _choice("company", "list", "segment"),
    "target_id": _t(40, required=True),
    "frequency": _choice("daily", "weekly", "monthly"),
    "enabled": Col("bool", required=True, default=True),
    "watch": _tags(),
    "last_run_at": Col("ts"),
    "next_run_at": Col("ts", index=True),
})

entity("change_events", "ch", {
    "company_id": _t(40, required=True, index=True),
    "change_type": _choice("new_job", "job_closed", "hiring_spike", "technology_added", "technology_removed",
                           "leadership_change", "new_contact", "contact_changed", "careers_url_changed",
                           "ats_changed", "status_changed"),
    "detected_at": Col("ts", required=True),
    "before": _j(),
    "after": _j(),
    "summary": _t(1000),
    "source": _t(100),
}, append_only=True, default_order="detected_at desc")

entity("company_snapshots", "cs", {
    "company_id": _t(40, required=True, index=True),
    "taken_at": Col("ts", required=True),
    "data": _j(),
}, append_only=True, system_write=True, default_order="taken_at desc")

# ---------------------------------------------------------------------------
# Background tasks, exports, audit, idempotency
# ---------------------------------------------------------------------------

TASK_KINDS = ("crawl", "discovery", "scraper", "enrichment", "validation", "research", "analytics",
              "workflow", "import_merge", "monitor", "signals", "export", "source_search")

entity("platform_tasks", "tsk", {
    "kind": Col("text", choices=TASK_KINDS, required=True, index=True),
    "status": _choice("queued", "running", "paused", "completed", "failed", "cancelled", "retrying"),
    "params": _j(),
    "result": _j(),
    "progress": _j(),
    "attempts": Col("int", required=True, default=0, minimum=0),
    "max_attempts": Col("int", required=True, default=3, minimum=1, maximum=10),
    "worker_id": _t(200),
    "heartbeat_at": Col("ts"),
    "lease_expires_at": Col("ts"),
    "run_after": Col("ts"),
    "started_at": Col("ts"),
    "finished_at": Col("ts"),
    "cancel_requested_at": Col("ts"),
    "error": _t(4000),
    "idempotency_key": _t(200),
    "entity_type": _t(40),
    "entity_id": _t(40),
}, unique=(("idempotency_key",),), system_write=True)

entity("exports", "ex", {
    "entity_type": _t(40, required=True),
    "format": _choice("csv", "xlsx", "json"),
    "filters": _j(),
    "status": _choice("queued", "completed", "failed"),
    "row_count": Col("int", required=True, default=0, minimum=0),
    "storage_key": _t(512),
    "filename": _t(200),
    "sha256": _t(64),
    "size_bytes": Col("bigint", minimum=0),
    "error": _t(2000),
})

entity("audit_log", "au", {
    "actor_id": Col("uuid"),
    "actor_kind": _choice("user", "system", "agent", "workflow"),
    "action": _t(100, required=True, index=True),
    "entity_type": _t(40, index=True),
    "entity_id": _t(40, index=True),
    "summary": _t(1000),
    "changes": _j(),
    "request_id": _t(100),
}, append_only=True)

entity("idempotency_keys", "ik", {
    "key": _t(200, required=True),
    "method": _t(10, required=True),
    "path": _t(500, required=True),
    "request_hash": _t(64, required=True),
    "status_code": Col("int", required=True),
    "response": _j(),
}, unique=(("key",),), append_only=True)


# ---------------------------------------------------------------------------
# AI Control Room (migration 0004)
# ---------------------------------------------------------------------------

_M4 = {"migration": "0004"}

entity("agent_sessions", "as", {
    "title": _t(300, required=True, search=True),
    "mode": _choice("auto", "research", "prospecting", "hiring", "data", "campaign", "crm", "monitoring"),
    "status": _choice("active", "archived"),
    "working_set": _j(),
    "last_run_id": _t(40),
}, **_M4, description="A Control Room conversation: messages plus the current working result set.")

entity("agent_messages", "am", {
    "session_id": _t(40, required=True, index=True),
    "role": _choice("user", "assistant", "system"),
    "content": _t(20000, required=True),
    "run_id": _t(40),
    "data": _j(),
}, **_M4, append_only=True, default_order="created_at asc")

entity("agent_runs", "ar", {
    "session_id": _t(40, index=True),
    "request": _t(8000, required=True, search=True),
    "mode": _t(40, required=True),
    "intent": _j(),
    "plan": _j([]),
    "status": _choice("planned", "running", "awaiting_approval", "completed", "failed", "cancelled"),
    "planner": _t(60),
    "estimate": _j(),
    "result": _j(),
    "summary": _t(20000),
    "task_id": _t(40),
    "error": _t(4000),
    "progress": _j(),
}, **_M4, description="One request: intent, plan, estimate, execution state and synthesised result.")

entity("agent_steps", "sp", {
    "run_id": _t(40, required=True, index=True),
    "position": Col("int", required=True, minimum=0),
    "tool": _t(80, required=True, index=True),
    "params": _j(),
    "risk": _t(20, required=True),
    "status": _choice("planned", "running", "done", "failed", "skipped", "awaiting_approval", "rejected"),
    "requires_approval": Col("bool", required=True, default=False),
    "estimate": _j(),
    "output": _j(),
    "error": _t(4000),
    "started_at": Col("ts"),
    "finished_at": Col("ts"),
    "duration_ms": Col("float"),
    "reservation_ids": _tags(),
    "idempotency_key": _t(200),
}, **_M4, system_write=True, unique=(("run_id", "position"),), default_order="position asc",
   description="Every tool call: parameters (secrets redacted), risk, credits, output and errors.")

entity("agent_approvals", "ap", {
    "run_id": _t(40, required=True, index=True),
    "step_id": _t(40, required=True, index=True),
    "action": _t(300, required=True),
    "reason": _t(4000),
    "risk": _t(20, required=True),
    "impact": _j(),
    "credits": _j(),
    "status": _choice("pending", "approved", "rejected", "expired"),
    "decided_by": Col("uuid"),
    "decided_at": Col("ts"),
}, **_M4, system_write=True)

entity("agent_results", "ax", {
    "run_id": _t(40, required=True, index=True),
    "entity_type": _t(40, required=True, index=True),
    "entity_id": _t(40),
    "rank": Col("int", required=True, minimum=0),
    "title": _t(500),
    "data": _j(),
    "score": _score(),
    "reasons": _j([]),
    "evidence": _j([]),
}, **_M4, system_write=True, default_order="rank asc")

entity("ai_memory", "mm", {
    "kind": _choice("alias", "preference", "campaign_definition", "saved_pattern", "source_priority",
                    "scoring_preference", "allowed_providers", "default_filter", "preferred_fields"),
    "key": _t(120, required=True, search=True),
    "value": _j(),
    "text": _t(2000, search=True),
    "enabled": Col("bool", required=True, default=True),
}, **_M4, unique=(("kind", "key"),), description="Workspace AI memory. Never holds secrets.")

entity("ai_insights", "in", {
    "kind": _t(60, required=True, index=True),
    "title": _t(500, required=True),
    "detail": _t(4000),
    "severity": _choice("info", "notable", "high"),
    "company_ids": _tags(),
    "evidence": _j([]),
    "status": _choice("new", "seen", "dismissed", "acted"),
    "fingerprint": _t(200, required=True),
    "suggested_request": _t(2000),
}, **_M4, unique=(("fingerprint",),), default_order="created_at desc")

entity("saved_requests", "sv", {
    "name": _t(200, required=True, search=True),
    "request": _t(8000, required=True),
    "mode": _t(40),
    "kind": _choice("request", "view"),
    "config": _j(),
}, **_M4)

# ---------------------------------------------------------------------------
# Real AI providers (migration 0005)
# ---------------------------------------------------------------------------

entity("ai_usage", "ai", {
    "provider": _t(60, required=True, index=True),
    "model": _t(120, required=True),
    "purpose": _t(60, required=True, index=True),
    "success": Col("bool", required=True, default=True),
    "error": _t(1000),
    "prompt_tokens": Col("int", minimum=0),
    "completion_tokens": Col("int", minimum=0),
    "total_tokens": Col("int", minimum=0),
    "estimated_cost_usd": Col("float", minimum=0),
    "request_id": _t(200),
    "latency_ms": Col("float", minimum=0),
    "run_id": _t(40, index=True),
}, migration="0005", append_only=True, system_write=True,
   description="One row per external AI call: provider, model, tokens, estimated cost and request id.")


def entities() -> Iterable[EntitySpec]:
    return ENTITIES.values()


def get_spec(name: str) -> EntitySpec:
    try:
        return ENTITIES[name]
    except KeyError:
        raise KeyError(f"unknown entity {name!r}") from None
