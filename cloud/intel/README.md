# CareerCrawler platform (`cloud/intel`)

Company intelligence, hiring intelligence, CRM and GTM automation, built on
CareerCloud (`cloud/`) and the existing CareerCrawler engine. One API, one
worker model and one database, with every record scoped to a workspace.

- [Architecture](#architecture)
- [What was reused, and how](#what-was-reused-and-how)
- [Tracks and modules](#tracks-and-modules)
- [Data model](#data-model)
- [API map](#api-map)
- [UI map](#ui-map)
- [Safety rules](#safety-rules)
- [Integrations and credentials](#integrations-and-credentials)
- [Running it](#running-it)
- [Tests](#tests)

## Architecture

```
React dashboard (cloud/web)                      Supabase Auth / dev tokens
      │  Bearer token, never in URLs
      ▼
FastAPI  (cloud/api/main.py)  ── CareerCloud crawl routes   /api/v1/jobs…
      │                        └─ Platform routes           /api/v1/workspaces, /api/v1/w/{workspace}/…
      │  workspace_ctx: token → membership (RLS) → Ctx(workspace, user, role)
      ▼
Platform container (intel/platform.py) ── services resolved lazily by name
      │  crm · imports · dedupe · exports · discovery · jobs · signals · technology · monitoring
      │  sources · providers · credits · email · contacts · ai · scraper · research
      │  campaigns · sequences · automation · analytics
      ▼
Store (intel/store) ──► PostgreSQL / Supabase   schema careercloud, 47 platform tables + tenancy, RLS on all
      │                  MemoryStore twin for offline tests (same contract suite)
      ▼
TaskService (intel/tasks) ──► Redis queue  <prefix>:platform   (Postgres is truth, Redis is the doorbell)
      ▼
Platform worker  python -m cloud.intel.tasks.worker   (host-neutral)
      ├─ crawl          → jobs/careercrawler_bridge.py → crawler.crawler_engine (existing engine, unmodified)
      ├─ discovery, scraper, enrichment, validation, research, source_search
      ├─ import_merge, export, signals, monitor, analytics, workflow
      └─ maintenance: reap dead leases, re-ring orphans, schedule monitors
      ▼
Object storage (LocalFileStorage in dev, S3/Supabase Storage/R2 elsewhere) for files
```

The CareerCloud crawl worker (`python -m cloud.worker`) keeps running beside
it, unchanged, on its own queue prefix.

## What was reused, and how

| Existing project | Reused | How |
|---|---|---|
| CareerCrawler engine | `crawler.crawler_engine`, ATS detection, careers discovery, 60 adapters, pagination, browser fallback | Called, not copied: `jobs/careercrawler_bridge.py` is the second sanctioned adapter (the first is `cloud/worker/careercrawler_runner.py`), worker-only, allow-listed in `test_isolation.py` |
| CareerCrawler `utils/names.py`, `utils/html.py` | Domain/name normalisation, JSON-LD | Vendored (`vendor/names.py`, `vendor/html.py`) so the API never imports crawler packages |
| CareerCrawler weekly diff | "Failed crawl never closes jobs" | Same rule re-implemented in `jobs/service.py` over the platform's job master |
| CareerCloud | Auth, RLS pattern, Redis queue, leases/heartbeats/fencing, egress guard, storage, settings, stamps | Imported directly |
| Seamless worktree `discovery/identity.py` | Company identity matching (EXACT/STRONG/PROBABLE/AMBIGUOUS) | Vendored `vendor/identity.py`; wrapped by `imports/dedupe.py` |
| Seamless worktree `seamless/` | REST client logic, credit cost model, HR/IT/VP targeting | Client ported into `providers/seamless.py`; `vendor/seamless_credits.py`, `vendor/seamless_targeting.py` |
| Seamless worktree `zerocredit/` | Leadership-page extraction, email rules, confidence tiers | Vendored `vendor/zc_extract.py`, `zc_clean.py`, `email_rule.py`, `confidence.py`; the process-wide requests monkeypatch was **not** reused |
| ZoomInfo automation | Official API OAuth client, rate limiting, ERP catalogue | Ported into `providers/zoominfo.py`; ERP catalogue vendored as `technology/data/erp_catalog.json`. The browser-login automation stays operator-attended and is not run unattended |
| CareerAutomation `ats.py`, `classify.py` | ATS detection and official public job-board APIs; IT/US classification | Vendored `vendor/ats_detect.py`, `vendor/it_classify.py` |
| Job-board crawler (`linkedin_jobs_mvp`) | Salary/skills/experience extractors, skills catalogue | Vendored `vendor/salary_extract.py`, `skills_extract.py`, `job_classify.py` |
| Job-board crawler LinkedIn/Indeed/Monster adapters, proxy/stealth code | — | **Not reused**: proxy rotation, stealth scripts and CAPTCHA/Cloudflare bypass violate the platform's rules. Those sources are connectors that require official or partner access |
| Email verification | No standalone project existed | New `email/` module: local syntax/MX/disposable/role/free checks + EmailListVerify provider |

Every vendored file starts with a `# VENDORED from …` header naming its origin.

## Tracks and modules

| Track | Modules |
|---|---|
| A CRM foundation | `crm/service.py` (companies, contacts, opportunities, pipelines/stages, tasks, notes, activities, tags, lists, segments, custom fields, relationships, merges, timeline), `imports/` (multi-file import engine, dedupe resolver), `exports/` |
| B Company discovery | `discovery/service.py` |
| C Job intelligence | `jobs/classify.py`, `jobs/service.py`, `jobs/crawl_task.py`, `jobs/careercrawler_bridge.py` |
| D Hiring intelligence | `signals/engine.py` (11 signals), `signals/service.py` (aggregation, explainable scores) |
| E External sources | `sources/` (SourceAdapter, ATS public APIs, keyed official APIs, authorization-required connectors) |
| F AI scraper | `scraper/` |
| G Email validation | `email/` |
| H Contact enrichment | `providers/` (registry, ZoomInfo, Seamless, credit ledger, routing, contact intelligence) |
| I GTM automation | `gtm/` (campaigns, sequences, suppression, unsubscribe), `automation/engine.py` |
| J AI research agent | `research/` (intent, planner, tools, service), `ai/` (provider-neutral model layer) |
| K Analytics | `analytics/service.py` |
| L UI | `cloud/web/src/platform/` |
| M API | `api/` (deps, generic CRUD, one router module per track) |
| N Testing/security/deployment | `cloud/tests/test_platform_*.py`, `store/ddl.py` (generated RLS), `core/http.py` (SSRF-safe fetcher), `run-platform-worker.bat` |
| Technology intelligence | `technology/` |
| Monitoring | `monitoring/service.py` |

Cross-track method signatures are in [`CONTRACTS.md`](CONTRACTS.md).

## Data model

`store/spec.py` declares every table once; `python -m cloud.intel.store.ddl --write`
generates `cloud/db/migrations/0003_platform.sql` from it, and a test fails if the
committed migration drifts.

- **Tenancy:** `workspaces`, `workspace_members(role owner|admin|member|viewer)`,
  `careercloud.member_role(ws)` (security definer), `create_workspace()`.
- **Every platform table** has `id` (`<prefix>_<32 hex>`), `workspace_id`,
  `created_at`, `updated_at`, `created_by`, `version`, a guard trigger making
  identity immutable and bumping `version`, and RLS: members read, writers
  write, viewers only read; `append_only` tables (audit, activities, provenance,
  ledgers) have no update/delete grants; `system_write` tables (tasks, credit
  accounts/ledger, workflow runs, snapshots, usage) are written only by the worker.
- **Tables:** companies, company_relationships, contacts, pipelines,
  pipeline_stages, opportunities, crm_tasks, notes, activities, tags, lists,
  list_members, segments, custom_field_defs, source_records, import_batches,
  import_files, import_rows, discovery_candidates, job_postings, hiring_signals,
  company_technologies, provider_connections, credit_accounts, credit_ledger,
  usage_events, email_validations, scrape_runs, scrape_results, campaigns,
  email_templates, sequences, sequence_steps, sequence_enrollments,
  message_events, suppressions, workflows, workflow_runs, research_runs,
  research_results, monitors, change_events, company_snapshots, platform_tasks,
  exports, audit_log, idempotency_keys.
- **Provenance:** every create/merge writes `source_records` with source kind and
  name, import batch/file/row, original values, normalized values, observed time,
  confidence and match rule.

## API map

All under `/api/v1`. Workspace routes are `/w/{workspace_id}/…`; a workspace the
caller is not a member of is a 404. POSTs honour `Idempotency-Key`. Lists take
`limit`, `offset`, `order`, `q` and field filters (`field`, `field__gte`,
`field__in=a,b`, `field__ilike`, `field__isnull`); unknown fields are 422.

| Area | Endpoints |
|---|---|
| Workspaces | `GET/POST /workspaces`, `GET/PATCH /w/{ws}`, `GET/POST /w/{ws}/members`, `/tasks` (+ `/{id}/cancel|pause|resume`), `/audit`, `/provenance/{entity}/{id}` |
| Companies & CRM | `/companies` CRUD (+ `/{id}/contacts|jobs|signals|technologies|opportunities|activities|changes|timeline|sources|relationships`, `POST /companies/merge`), `/contacts`, `/opportunities` (+ `/{id}/stage`), `/pipelines` (+ `/stages`, `/board`), `/pipeline-stages`, `/crm-tasks`, `/notes`, `/activities`, `/tags`, `/custom-fields`, `/lists` (+ `/members`), `/segments` (+ `/results`) |
| Imports & exports | `/imports` (+ `/{id}/files` multipart, `/validate`, `/mapping-suggestions`, `PUT /mapping`, `/merge`, `/rows`), `/exports` (+ `/{id}/download`) |
| Intelligence | `/jobs`, `POST /jobs/ingest`, `POST /crawl`, `/hiring-signals` (+ `/dismiss`), `POST /signals/run`, `/companies/{id}/scores`, `/company-technologies`, `/technology/taxonomy`, `POST /technology/detect`, `/discovery/candidates` (+ approve/reject), `POST /discovery/run`, `/monitors` (+ `/run`), `/change-events` |
| Sources, providers, credits, email, contacts | `/sources`, `POST /sources/{name}/search`, `/providers` (+ `/credentials`, `/settings`, `/verify`), `/credits`, `/credits/ledger`, `/credits/{provider}/sync|grant|limit`, `POST /email/validate`, `/email/validations`, `/companies/{id}/contact-gaps`, `POST /contacts/find`, `/enrichment/plan` |
| AI | `/ai/providers`, `POST /scraper/schema`, `/scraper/runs` (+ results, files), `POST /research/plan`, `/research/runs` (+ results, export, `approve`, `actions`) |
| GTM | `/campaigns` (+ `/match`), `/companies/{id}/campaign-mapping`, `/templates` (+ `/preview`), `/sequences` (+ `/enroll`), `/sequence-steps`, `/enrollments` (+ `/approve`, `/{id}/stop`), `/sequences/process-due`, `/message-events`, `/events/inbound`, `/suppressions`, `/workflows` (+ `/test`), `/workflow-runs`; public `/unsubscribe/{token}` |
| Analytics | `/analytics/dashboard`, `/analytics/timeseries` |

With `CAREERCLOUD_ENV=development` the interactive docs are at `/docs`.

## UI map

Navigation: **Intelligence** (Dashboard, Companies, Contacts, Jobs, Hiring
Intelligence, Discovery, Scraper, Research Agent) · **CRM** (Opportunities,
Tasks, Activities, Lists, Segments) · **GTM** (Campaigns, Sequences, Templates,
Suppression, Workflows, Monitors) · **Data** (Imports, Exports, Sources,
Credits, Analytics) · **System** (Crawls, Background jobs, Settings).

- Company page tabs: Overview (with score explanation), Jobs, Contacts (with
  contact-gap status per function), Technology, Hiring Signals, Activities,
  Opportunities, Sources.
- Contact page tabs: Profile, Company, Role, Email, Validation, Source,
  Activities, Sequences.
- Research page: natural-language input, plan with credit/mutation flags,
  intent and sources, progress, results, export, proposed actions.

## Safety rules

Enforced in code and covered by tests:

1. **Production is untouched.** No platform module imports the crawler's
   `store`, `sheets`, `weekly_run`, `checkpoint` or `sync`, or `sqlite3`; the
   database layer accepts only `postgresql://` URLs; the crawl bridge is
   worker-only.
2. **Workspace isolation** by explicit filter *and* PostgreSQL RLS, including
   raw-SQL tests that bypass the store.
3. **No paid credits without an explicit action.** Paid calls need
   `allow_paid=True` on an approved task plus a ledger reservation within limits.
   Cached validations and existing records are never bought again.
4. **No email is sent** outside production with `CAREERCLOUD_ALLOW_EMAIL_SENDING`,
   a sending-enabled campaign, an approved enrollment and an unsuppressed contact.
5. **No provider controls bypassed.** No CAPTCHA solving, stealth, proxy rotation
   or login automation; blocked pages are reported as blocked.
6. **SSRF protection** on every fetch and webhook (`core/http.py`), plus the
   worker's egress guard.
7. **Secrets** are server-side, encrypted at rest (Fernet), write-only in the API,
   never in `VITE_*`.
8. **AI data policy:** external AI only when the workspace allows it, with
   emails and phones redacted; otherwise deterministic rules.
9. **AI never mutates CRM data directly:** the research agent's CRM changes are
   proposals applied only by an explicit, audited action.

## Integrations and credentials

See the final report for the live status of each integration. In short:
everything is implemented behind its interface and tested offline; each paid or
authenticated provider needs its credential before it can be verified, and is
reported honestly as `not_configured` until then.

### AI providers (Claude, Gemini, OpenAI-compatible)

Adapters live in `ai/` behind one interface — `generate`, `structured_generate`,
`stream`, `health_check`, with per-call usage (tokens, request id, estimated
cost). The AI Control Room uses the configured model for intent interpretation,
research planning, plan explanation, result summarisation and follow-up
understanding; with no provider it plans with the deterministic rules and says
"AI provider not configured".

**Keys** — either saved per workspace in Settings → AI (encrypted with
`CAREERCLOUD_PLATFORM_SECRETS_KEY`, never shown again), or set on the server:

| Provider | Server environment variables | Default model |
|---|---|---|
| Claude | `ANTHROPIC_API_KEY` | `claude-opus-5` |
| Gemini | `GEMINI_API_KEY` | `gemini-2.5-pro` |
| OpenAI-compatible | `OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_BASE_URL` (https), `OPENAI_COMPATIBLE_MODEL` | — |

`CAREERCLOUD_AI_PROVIDER` / `CAREERCLOUD_AI_MODEL` set a server default; each
workspace can override provider, model, enabled, monthly budget
(`max_budget_usd`), allowed AI actions and explicit fallbacks in Settings → AI.
External AI is used only when the workspace's data policy ("Allow external AI
providers") is on. Claude needs `pip install -r cloud/intel/requirements-ai.txt`.

**Safety** — the model only proposes tool calls; the server validates tool,
mode, role, schema, credits and approvals. There is no code/SQL/shell tool.
Prompts carry CRM data as structured fields inside an `<untrusted_data>` block;
free text (descriptions, job text, scraped pages) is never sent. Every call is
recorded in `ai_usage` (provider, model, purpose, tokens, estimated cost,
request id). The only explicit live check is Settings → AI → Test connection.

## Running it

```powershell
# from E:\Crawlers\CareerCrawler-platform
py -3.12 -m venv cloud\.venv
cloud\.venv\Scripts\python -m pip install -r cloud\requirements-dev.txt -r cloud\intel\requirements-ai.txt
cd cloud\web; npm ci; cd ..\..

cloud\.venv\Scripts\python -m cloud.devtools.localpg start          # embedded PostgreSQL + migrations
cloud\.venv\Scripts\python -m cloud.devtools.fake_redis              # dev Redis on :6390
cloud\.venv\Scripts\python -m uvicorn cloud.api.main:app --port 8000 --env-file cloud\api\.env
run-platform-worker.bat                                             # platform tasks
run-worker.bat                                                      # CareerCloud crawl jobs (optional)
cd cloud\web; npm run dev                                           # http://localhost:5173
```

### Persistence

**PostgreSQL is the persistence layer.** Locally that is the embedded server
started by `python -m cloud.devtools.localpg start` (data in
`cloud\.localdev\postgres`, git-ignored, 127.0.0.1 only), selected with:

```ini
CAREERCLOUD_STORAGE=postgres
CAREERCLOUD_DATABASE_URL=localdev
```

Every platform record — companies, contacts, opportunities, jobs, signals,
lists, campaigns, workflows, activities, research runs, AI Control Room plans,
approvals, results and memory, credits and the audit trail — and CareerCloud's
own crawl jobs live in that database and survive API restarts. The API's
storage setting decides for both, so they are always in the same place.

Smallest local setup (no Redis): the two lines above plus
`CAREERCLOUD_QUEUE=inline` in `cloud\api\.env`; platform tasks then run inside
the API process. The embedded server keeps running in the background between
API restarts; stop it with `python -m cloud.devtools.localpg stop`.

**Boot check.** At startup the API and the platform worker verify that every
migration in `cloud\db\migrations` is applied (with matching checksums) and
that all platform tables exist with row-level security. A database that is
missing any of them stops the process with the exact fix, e.g.
`migrations not applied: 0004_ai_control_room … run python -m cloud.devtools.localpg start`.

**In-memory fallback.** `CAREERCLOUD_STORAGE=memory` keeps everything in the
API process for tests and quick UI work; the data disappears on restart and the
API logs a warning saying so. It is never chosen silently: the platform worker,
run with nothing configured, asks for PostgreSQL instead.

None of this touches the production CareerCrawler's SQLite (`state\crawler.db`):
the database layer accepts only `postgresql://` URLs.

## Tests

```powershell
cloud\.venv\Scripts\python -m unittest discover -s cloud\tests -t .      # CareerCloud + platform, offline
E:\Crawlers\CareerCrawler\venv\Scripts\python -m unittest discover -s tests   # CareerCrawler engine
```

PostgreSQL tests use an embedded `pgserver` database per test class (or
`CAREERCLOUD_TEST_DATABASE_URL`, which must be disposable). Real-infrastructure
checks stay separate in `cloud/ops/staging_e2e.py`.
