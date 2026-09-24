# Cross-track service contracts

Every service is constructed as `Service(platform)` and resolved with
`platform.service("<name>")` (see `cloud/intel/platform.py::SERVICES`). All
methods take a `Ctx` first. Rows are plain dicts from the store. Any method that
spends paid credits takes `allow_paid: bool = False` and must refuse (not
silently skip) when a paid call would be needed and `allow_paid` is false.

## Track A — CRM / data engine (`crm`, `imports`, `dedupe`, `exports`)

```python
CrmService.ensure_defaults(ctx) -> None               # default pipeline + stages; idempotent
CrmService.upsert_company(ctx, values: dict, *, source_kind: str, source_name: str,
        source_ref: str | None = None, original: dict | None = None,
        confidence: float | None = None, auto_merge: bool = True
    ) -> dict   # {"company": row|None, "created": bool, "match": {"outcome", "company_id", "reasons"},
                #  "needs_review": bool}
CrmService.upsert_contact(ctx, values: dict, *, source_kind, source_name, source_ref=None,
        original=None, confidence=None) -> dict    # {"contact": row, "created": bool}
CrmService.create_opportunity(ctx, company_id, title, *, signal_ids=(), signal_types=(),
        score=None, score_breakdown=None, reason=None, campaign_id=None, contact_id=None,
        evidence=(), source="manual") -> dict
CrmService.move_stage(ctx, opportunity_id, stage_id) -> dict
CrmService.add_to_list(ctx, list_id, entity_type, ids: list[str], reason=None) -> int
CrmService.company_timeline(ctx, company_id, limit=100) -> list[dict]
CompanyResolver.resolve(ctx, candidate: dict) -> {"outcome": "EXACT|STRONG|PROBABLE|AMBIGUOUS|NONE",
        "company_id": str|None, "reasons": [str], "candidates": [str]}
ExportService.export_rows(ctx, entity_type: str, rows: list[dict], fmt: "csv|xlsx|json",
        filename_stem: str) -> dict   # exports row incl. storage_key
```

## Track B/C/D — intelligence (`jobs`, `signals`, `technology`, `discovery`, `monitoring`)

```python
JobIntelService.ingest_postings(ctx, postings: list[dict], *, source_kind, source_name,
        company_id: str | None = None, crawled_company_ids: set[str] | None = None) -> dict  # stats
JobIntelService.classify(title: str, description: str = "", location: str = "") -> dict
SignalService.detect_for_company(ctx, company_id, *, now=None) -> list[dict]     # hiring_signals rows
SignalService.score_company(ctx, company_id) -> dict   # {"account_score","hiring_score","opportunity_score","breakdown"}
SignalService.score_contact(ctx, contact: dict, company: dict | None = None) -> dict
TechnologyService.detect_in_text(text: str) -> list[{"technology","category","vendor","evidence_text"}]
TechnologyService.record(ctx, company_id, technology, *, source, category=None, evidence_url=None,
        evidence_text=None, confidence=None) -> dict
DiscoveryService.submit_candidates(ctx, candidates: list[dict], *, source_kind, source_name) -> list[dict]
MonitoringService.schedule_due(ctx) -> int
MonitoringService.record_change(ctx, company_id, change_type, *, before=None, after=None, summary=None, source=None)
```

## Track E/G/H — sources, providers, credits, email, contacts

```python
SourceAdapter: name, kind, requires, search(query) , collect(ref), normalize(raw), dedupe(rows),
        health() -> {"status": "ok|not_configured|blocked|error", "detail"}, usage() -> dict
SourceService.list_sources(ctx) -> list[dict]; SourceService.search(ctx, source, query, *, allow_paid=False)
CreditLedger.balance(ctx, provider) -> {"total","reserved","consumed","remaining","last_sync_at"}
CreditLedger.reserve(ctx, provider, amount, *, reason, task_id=None, idempotency_key=None) -> dict
CreditLedger.consume(ctx, reservation_id, actual) -> dict ; CreditLedger.release(ctx, reservation_id) -> dict
CreditLedger.sync(ctx, provider, total, *, source) -> dict
EmailValidationService.validate(ctx, emails: list[str], *, allow_paid=False, max_age_days=30) -> list[dict]
ContactIntelService.gap_analysis(ctx, company_id, functions=("hr","it","executive")) -> dict
ContactIntelService.find_contacts(ctx, company_ids, *, functions=..., allow_paid=False,
        providers=None) -> dict
```

## Track F/J — AI, scraper, research (`ai`, `scraper`, `research`)

```python
AIRegistry.for_ctx(ctx, purpose: str) -> AIProvider   # RulesProvider when external AI not allowed
AIProvider.complete_json(system: str, prompt: str, schema: dict, *, max_tokens=4000) -> dict
AIProvider.name / .external: bool
ScraperService.instruction_to_schema(ctx, instruction) -> dict
ScraperService.start(ctx, urls, instruction) -> dict   # scrape_runs row + task
ResearchService.plan(ctx, question) -> dict ; .approve(ctx, run_id) ; .apply_actions(ctx, run_id, action_ids)
```

## Track I/K — GTM, automation, analytics

```python
CampaignService.ensure_defaults(ctx) -> None   # COX-LITTLE, RISEIT, ITECH US; idempotent
CampaignService.match_campaigns(ctx, company: dict, signals: list[dict], jobs: list[dict]) -> list[
        {"campaign": row, "score": float, "reasons": [str]}]
SequenceService.enroll(ctx, sequence_id, contact_ids, *, campaign_id=None) -> list[dict]  # pending_approval
AutomationEngine.emit(ctx, trigger: str, event_key: str, payload: dict) -> list[dict]  # workflow_runs
AnalyticsService.dashboard(ctx) -> dict
```

Triggers: new_company, hiring_spike, technology_detected, leadership_change, new_contact,
email_validated, job_posted, long_open_job, company_matched, research_completed.
Emitting is best-effort: wrap in try/except so a missing/failed engine never breaks the caller.
