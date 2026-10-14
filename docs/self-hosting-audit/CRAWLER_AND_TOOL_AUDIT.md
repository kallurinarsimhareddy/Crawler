# SANA GTM: crawler, tool, duplication and unused-code audit

| | |
|---|---|
| Date | 2026-10-14 |
| Repository | `github.com/kallurinarsimhareddy/Crawler`, local worktree `E:\Crawlers\CareerCrawler-sana-safety` |
| Commit inspected | `5ee805d` (= `origin/feature/careercrawler-platform`, verified with `git ls-remote` the same day). Clean working tree. 759 tracked files |
| Also inspected (read-only) | Production legacy crawler checkout `E:\Crawlers\CareerCrawler` (`46af82d`, branch `queue-recovery`); the other worktrees and standalone crawler projects on the PC (section 3.4); the Windows scheduled tasks |
| Production evidence | The verified read-only production backup of 2026-10-14 (`pre-selfhost_careercloud_20261014T082831Z.dump`), restored into a throwaway local PostgreSQL 17 on 127.0.0.1. Measurements in section 8 come from that copy. **Production itself was not queried for this audit** |
| Mode | Read-only. Nothing was deleted, refactored, committed, scheduled or started. No crawler or job ran. No secrets were read except to confirm that variables exist |

**How to read the labels.**
- **CONFIRMED**: proven by code (grep, including dynamic loading and string dispatch), by the production data copy, or by a production log or scheduled task.
- **PROVISIONAL**: follows from code but needs runtime or business confirmation.
- **ESTIMATE**: a number derived from measurements, not measured directly.

---

## 1. Executive summary

1. **SANA GTM has two disconnected job pipelines. The bigger one is outside the platform.**
   - The legacy **CareerCrawler weekly run** is live. It is the scheduled task "CareerCrawler Weekly", every Saturday at 06:00; last run 2026-10-10, result 0. Run 2026-W41 crawled **11,377 companies and 349,317 postings** (46,656 new, 49,991 closed), using about 60 ATS adapters. It writes to **Google Sheets** and a **590 MB SQLite** ledger. CONFIRMED from the production log.
   - None of that reaches the SANA GTM database. SANA holds **115,682 jobs, and all but 2 come from one source (WeAreDevelopers)**. CONFIRMED from the backup.
   - **The biggest consolidation win is to feed the weekly CareerCrawler output into SANA's `job_postings`, not to delete crawlers.**
2. **The platform's downstream GTM workflow has no production data yet.**
   - These tables are **empty**: `companies`, `contacts`, `opportunities`, `sequences`, `outbound_messages`, `research_runs`, `workflows` and 44 more.
   - Only 5 of the 23 task kinds have ever run in production: `job_monitor`, `job_lifecycle`, `job_import`, `scraper`, `email_validation_job`.
   - So most of the platform is **built and tested, but unused in production**. That is not the same as unnecessary; it is the product roadmap. CONFIRMED.
3. **Database storage is dominated by history that repeats other columns.**
   - `job_posting_changes` holds 128 MB (36% of a 354 MB database) in 231,360 rows. They are exactly one `new` row (2026-10-02) and one `stale` row (2026-10-06) for every WAD job. Each says only what `job_postings.first_seen_at` and `stale_at` already say. CONFIRMED.
   - There are no duplicate job URLs: 115,682 rows have 115,682 distinct `url_key` values. CONFIRMED.
4. **Real duplication is in code paths, not in rows.**
   - There are two job write paths with **different URL keys and different side effects**: `jobs/service.ingest_postings` and `job_monitor/service.upsert_batch`. CONFIRMED in code. It has not produced duplicate rows in production yet.
   - The ATS detection, ATS API fetching, careers-page discovery, URL canonicalization, company-name keys and "is this an IT job" classification are each implemented **2–4 times** across the legacy crawler, `vendor/`, `scraper/` and `sources/`. CONFIRMED.
5. **The Redis/Upstash, S3 and old-installer paths are obsolete for the self-hosted target.**
   - CONFIRMED: every platform task kind runs on the PostgreSQL queue, with no Redis.
   - Redis is still needed only by the legacy CareerCloud crawl worker (`cloud/worker`), the old PyInstaller installer, the Pages proxy function and the current production health check's default.
6. **Three defects were found during the audit**, all CONFIRMED:
   - The `careercrawler` external source always fails (wrong task parameters).
   - The platform worker's crawl path has no egress (private-IP redirect) guard. The legacy worker has one.
   - Jobs written by the crawl, external-source and scraper-apply paths never get the keyword relevance class that hiring signals require.

**Summary counts.** Every number maps to an `ID` in the tables below.

| Recommendation | Count | Components |
|---|---|---|
| KEEP | 36 | C1 C2 C3 C5 C11 C14 C15 C23 C25 C26 · U1 U6 U8 U9 U15 U18 U19 · X1 · T1 T2 T5 T8 T12 T14 T17 T27 T28 · I1–I9 |
| KEEP / OPTIONAL | 17 | C7 C10 C12 C16 C20 · T4 T6 T7 T9 T10 T15 T16 T18 T19 · I22 I23 I24 |
| CONSOLIDATE | 22 | C6 C17 C21 C24 · U2 U5 U7 U10 U11 U12 U16 · T3 T11 T13 T20 T21 T23 · I25 · F1–F4 |
| UNUSED | 6 | C8 · U4 U20 · T24 T25 T26 |
| OBSOLETE | 10 | C22 · X2 X3 X4 · I13 I14 I15 I16 I19 I21 |
| REMOVE CANDIDATE | 10 | C19 · U3 U13 U14 U17 · T22 · I10 I11 I17 I20 |
| BLOCKED | 8 | C4 C9 C13 C18 · X5 X6 · I12 I18 |
| **Total inventoried components** | **109** | C = collectors (26), U = shared crawler utilities (20), X = crawler code outside the branch (6), T = tools (28), I = infrastructure (25), F = frontend duplicate groups (4). Defects D1–D7 and duplicate groups G1–G21 are findings, not components |

---

## 2. Repository and commit inspected

- **Main branch content.** Two code generations live in one repository:
  1. **Legacy CareerCrawler** at the repository root: `main.py`, `adapters/` (59 modules), `crawler/`, `sheets/`, `store/`, `utils/`, `models/`, `exporters/`, `config/`, `tests/`. This code is byte-identical to the production checkout `E:\Crawlers\CareerCrawler` (`git diff 46af82d 5ee805d` touches none of these paths; the only differences are the root `.bat` files and line endings).
  2. **SANA GTM platform** under `cloud/`: `api/`, `intel/` (185 files), `worker/`, `shared/`, `db/`, `ops/`, `web/` (React), `deploy/`, `demo/`, `devtools/`, `tests/`.
- **Deployment trees:** `deploy/windows-selfhost` (target), `deploy/selfhost` (Docker/Ubuntu), `deploy/windows/installer` (old PyInstaller), `deploy/windows/sana-gtm` (what runs production today), `deploy/ops` (backup and monitor tools), `cloud/deploy/staging` (Linux staging).
- **What runs where today (CONFIRMED from Windows scheduled tasks and the process list):**
  - "CareerCrawler Weekly" runs `E:\Crawlers\CareerCrawler` with `python -m crawler.weekly_run --workers 6`.
  - "SANA GTM Auto Start" runs the API and platform worker from `E:\Crawlers\CareerCrawler-platform` at `0bdcee4` (`CAREERCLOUD_QUEUE=postgres`) behind a quick Cloudflare tunnel.

---

## 3. Complete crawler inventory

Rec = primary recommendation (section 6 defines each). Tables written are in schema `careercloud` unless they say Sheets or SQLite.

### 3.1 Legacy CareerCrawler (repository root)

| ID | Component | Paths | Purpose / output | Source & access | Entry / caller | Queue / schedule | Writes | Consumers | Creds / deps / cost | Tests | Runtime evidence | Rec |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C1 | **Weekly run** | `crawler/weekly_run.py` (WeeklyRun, `main`), `crawler/checkpoint.py`, `utils/runlock.py` | Weekly crawl of the company roster: new, reopened and closed jobs | via C2/C3 | Scheduled task **CareerCrawler Weekly** | Sat 06:00, 200-company batches, checkpoint + run lock | Sheets: MASTER_COMPANIES, CURRENT_JOBS, JOB_HISTORY, NEW_LAST_WEEK, WEEKLY_RUNS, DASHBOARD, FAILURES. SQLite `state/crawler.db`: `jobs`, `companies`, `crawl_attempts` | Human operators via Sheets. **Not SANA** | Google service account, Sheets quota; free | test_weekly_run, _queue, _filters, test_incremental_persistence, test_runlock, test_production_hardening | **CONFIRMED live**: W41 349,317 postings, finished 2026-10-11 10:27 | KEEP |
| C2 | **Crawler engine** | `crawler/crawler_engine.py` (`ADAPTER_MODULES` dict, `build_registry` loads by `importlib`) | Seed, platform detection, adapter, browser rescue, outcome | — | C1, `main.py`, cloud bridge (C21), legacy runner (C22) | per-host `_HostThrottle`, browser budget | — | all crawl paths | — | test_crawler_engine, test_concurrency, test_browser_budget | CONFIRMED live | KEEP |
| C3 | **ATS adapter family** (59 modules) | `adapters/*.py` | `fetch_jobs(url, company, session)` returns Jobs | **JSON/REST APIs:** workday, greenhouse, lever, ashby, smartrecruiters, oracle, workable, breezyhr, rippling, pinpoint, manatal, comeet, eightfold, recruitee, bamboohr, ultipro (+UKG), adp, taleo, ukg_ready, asure, personio (XML). **API then HTML/browser:** cornerstone, phenom. **HTML:** jobvite, peopleadmin, teamtailor, successfactors, dayforce, icims (+browser for WAF). **Paginated HTML helper** (`_paginated_html`): applicantpro, applicantstack, avature, careerplug, clearcompany, fountain, gem, gohire, hireology, homerun, hrmdirect, indeed, isolved, jazzhr, jobscore, join, neogov, oleeo, paycom, paycor, paylocity, radancy, recruiterbox, silkroad, talentreef, zoho_recruit. **Browser only:** bullhorn, adp_rm. **Fallback:** generic.py | C2 registry, picked by `platform_detector` | — | — | C2 | Playwright Chromium for fallbacks; free | test_adapters, _v2, test_workday, test_icims_browser, test_peopleadmin | CONFIRMED live. W41 by platform: Workday 9,663, Oracle 2,191, SmartRecruiters 1,013, Generic HTML 952, Oleeo 848… | KEEP |
| C4 | Indeed company-page adapter | `adapters/indeed.py` | Jobs from `indeed.com/cmp/...` pages | HTML (no proxy, no stealth; shared Chrome UA) | C3 registry | — | — | C2 | — | indirect | No outcome counts found for Indeed in the W41 summary line | BLOCKED (need per-adapter yield and ToS decision) |
| C5 | Careers-page / board resolution | `crawler/resolve.py`, `crawler/career_finder.py` | Website → careers page → ATS board | HTTP | C1, C2 | — | — | C1 | — | test_resolve, test_career_finder | CONFIRMED live (log: "Discovery: no careers page found…") | KEEP |
| C6 | ATS discovery CLI | `crawler/ats_discovery.py` | Batch-fills the "IT Link" column in MASTER_COMPANIES | HTTP + browser | manual `python -m crawler.ats_discovery --apply` | none | Sheets MASTER_COMPANIES | C1 reads `it_link` | Google SA | test_ats_discovery | Manual; no recent output file found | CONSOLIDATE (into C5) |
| C7 | Board filter detection | `crawler/job_filters.py` | Department/location facets | HTML/browser | C1 only with `--filters` (the scheduled task does **not** pass it) | — | Sheets columns | — | — | test_job_filters, test_weekly_filters | Off in production | KEEP / OPTIONAL |
| C8 | One-shot CLI + report exporters + diagnostics | `main.py`, `exporters/failure_exporter.py`, `exporters/report_exporter.py`, `crawler/diagnostics.py`, `crawler/csv_reader.py` (pandas) | CSV in → XLSX/CSV/JSON reports | via C2 | manual `python main.py` | none | `output/jobs.xlsx`, CSVs, `summary.json` | operator | pandas, openpyxl | test_*_exporter, test_locked_output, test_encoding | Last output 2026-08-19 (production `output/`) | UNUSED (provisional: manual tool) |
| C9 | SQLite lease queue path | `store/queue.py` (CrawlQueue), `crawler/sync.py`, `crawler/status.py`, `crawler/retry.py` | Durable work queue for `weekly_run --queue` | SQLite | manual / `--queue` | lease queue | SQLite `crawl_queue` | C1 in queue mode | — | test_store, test_sync, test_weekly_queue, test_resilience | Not used by the scheduled task | BLOCKED (documented migration path; owner decision) |
| C10 | Benchmark | `crawler/benchmark.py` | Measures cost per worker count | via C2 | manual | — | `output/bench-*.json` | operator | psutil | test_benchmark | Last used 2026-09-08 | KEEP / OPTIONAL |

### 3.2 SANA GTM platform collectors (`cloud/intel`)

| ID | Component | Paths | Purpose / output | Source & access | Entry / caller | Task kind / schedule | Writes | Consumers | Creds / deps / cost | Tests | Runtime evidence (backup) | Rec |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C11 | **Job monitor: WeAreDevelopers profile** | `job_monitor/profiles/wearedevelopers.py`, `strategies.py` (SiteProfileStrategy), `runner.py`, `service.py` | Daily incremental and periodic full sweep of WAD US jobs | Server HTML + turbo-stream cursor; no browser, no AI | `/job-monitors/{id}/run`; `JobMonitorService.tick` | `job_monitor`; daily/weekly per monitor; full sweep every `full_sweep_days` | job_source_monitors, job_monitor_runs, job_postings (`upsert_batch`), job_posting_changes, job_company_reviews | /jobs feed, Hiring companies, signals, exports | free | test_job_monitor(_sweep), test_job_csv | **CONFIRMED**: 115,680 jobs; 3 runs. Paused full sweep `jmr_eba7780b…` has blocked scheduling since 2026-10-02 | KEEP |
| C12 | Job monitor: generic strategy | `job_monitor/strategies.py` (ScraperStrategy) | Any listing page with the scraper's extractor, plain HTTP | HTML | same as C11 | `job_monitor` | same as C11 | same as C11 | free | test_job_monitor | No production monitor uses it (1 monitor = WAD) | KEEP / OPTIONAL |
| C13 | JobSpy source | `job_monitor/jobspy_source.py`; `cloud/intel/requirements-jobspy.txt` | Indeed (and other boards) via `python-jobspy` | **Unofficial/private endpoints** (Indeed mobile GraphQL with the app's embedded key) | monitor with `strategy="jobspy"`; boards off unless `SANA_JOBSPY_BOARDS` | `job_monitor` | same as C11 | same as C11 | separate venv; ToS/legal risk | thin (test_job_relevance) | Not installed in production; 0 rows | BLOCKED (policy decision) |
| C14 | Job CSV/XLSX import | `job_monitor/importer.py` | Bulk job upload | file | `/job-imports` (+validate, start); JobImport.tsx | `job_import` | job_imports, job_postings (status UNKNOWN), job_posting_changes | same as C11 | 250 MB / 2M rows | test_job_monitor, test_job_csv | CONFIRMED: 1 import (2 rows) | KEEP |
| C15 | Job lifecycle | `job_monitor/service.py` (`evaluate_lifecycle`, `schedule_lifecycle`) | Stale, expired and closed transitions; job signals | DB only | tick (daily per monitor); `/job-monitors/{id}/lifecycle` | `job_lifecycle` | job_postings, job_posting_changes, hiring_signals | signals, UI | — | test_job_lifecycle(_pg) | CONFIRMED: 10 completed tasks, last 2026-10-14 | KEEP |
| C16 | **AI Scraper** | `scraper/*` (runner, crawler, fetcher, discovery, pagination, detail, extractor, planner, templates, dedupe, normalizer, validator, crm, exports, toolkit) | Generic site extraction with optional AI; proposals to CRM | HTML via PageFetcher→SafeFetcher; JSON-LD; official ATS APIs (`vendor/ats_detect.api_endpoints`); optional Playwright | `/scraper/runs` (+actions); agent tool; research `scrape_jobs` (inline) | `scraper`; `scraper.recover` every 30 s | scrape_runs/results/pages/proposals/templates, files; applied proposals → companies/contacts/job_postings (via C21's `ingest_postings`) | Scraper page, agent, research, automation | Playwright optional; AI capped by `max_ai_calls` | test_platform_scraper(_v2) | CONFIRMED: 6 runs, 9 results (2026-09-29…10-01) | KEEP / OPTIONAL |
| C17 | External sources: official ATS APIs | `sources/adapters.py` (ATSPublicAdapter: Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Workday, Teamtailor) | Board search | Official public APIs | `/sources/{source}/search` (**tests only**; no frontend caller) | `source_search` | job_postings via `ingest_postings` | job feed | free | test_platform_sources(_api, _support) | 0 production runs | CONSOLIDATE (with C3 / ats_detect) |
| C18 | External sources: keyed / partner / licensed | `sources/adapters.py` (USAJobs, Adzuna, ZipRecruiter, LinkedIn, Indeed, Dice, Wellfound, BuiltIn) | Board search | Keyed or partner APIs (untested with real keys) | as C17 | `source_search` | as C17 | as C17 | API keys, some paid | as C17 | 0 runs; no keys configured | BLOCKED (needs keys and a product decision) |
| C19 | External source `careercrawler` | `sources/service.py:102-108` | Hands a crawl to the CareerCrawler engine | — | as C17 | submits `crawl` with `{"companies": …}` | none: **always fails** (`crawl_task._targets` accepts only company_ids, list_id or filters, `crawl_task.py:38-47`) | — | — | test only asserts the kind (`test_platform_sources.py:130`) | 0 runs | REMOVE CANDIDATE (or fix) |
| C20 | Company discovery | `discovery/service.py` | Candidate companies verified from homepage, careers page and ATS | HTML (SafeFetcher) + `ats_detect`; ZoomInfo CompanySearch | `/discovery/*`; Intel and ZoomInfo pages; agent | `discovery` | discovery_candidates, companies (on approve), source_records | CRM | ZoomInfo connection optional | test_platform_discovery, test_platform_zoominfo | 0 rows | KEEP / OPTIONAL |
| C21 | **Platform CareerCrawler crawl** | `jobs/crawl_task.py`, `jobs/careercrawler_bridge.py`, `jobs/service.py` (`ingest_postings`) | Crawl CRM companies' career sites into SANA | Legacy engine C2 + adapters C3 | `POST /crawl` ("Crawl careers" button); agent `run_career_crawler` | `crawl` | job_postings, companies (ats, careers_url), change_events, company_technologies, hiring_signals | job feed, signals | **No egress guard** in the platform worker (see 7.3) | test_platform_crawl, test_isolation | 0 runs (0 companies in production) | CONSOLIDATE (write path → `upsert_batch`) |
| C22 | Legacy CareerCloud crawl | `cloud/worker/*` (worker, executor, dispatcher, careercrawler_runner, runner, fake_runner, egress, results), `cloud/api/routes.py` `/api/v1/jobs*`, `cloud/db/postgres.py`, tables from 0001 | Crawl to XLSX for the old CareerCloud UI | C2 | `/new`, `/settings/crawls` pages; `python -m cloud.worker` | **Redis queue only**; on the PostgreSQL queue these jobs never run (`NullDispatcher`, `api/main.py:155-160`) | jobs, crawl_targets, job_events, job_results + files; **never `job_postings`** | legacy pages only | Redis | test_careercrawler_runner, test_worker, test_egress, test_fake_runner, test_queue | 3 jobs / 12 results from staging era (2026-09) | OBSOLETE |
| C23 | Research run | `research/*` | Multi-step research (query, signals, contacts, nested scrape) | DB + providers + inline scraper | `/research/plan`, `/approve`; automation plans only | `research` | research_runs, research_results | Tools page | AI and paid providers behind approval | test_platform_research | 0 rows | KEEP |
| C24 | Change monitors (not a fetcher) | `monitoring/service.py` | Diffs DB snapshots of companies; **fetches nothing**, despite the agent tool text "careers-page and ATS changes" (`agent/tools.py:757`) | DB only | CRUD `/monitors`, `/monitors/{id}/run` (tests only); `/change-monitors` page | `monitor`; `schedule_due` every 30 s | company_snapshots, change_events, hiring_signals | agent insights, automation | — | test_platform_discovery | 0 monitors | CONSOLIDATE (duplicate "hiring spike" rule; misleading name) |
| C25 | Signals | `signals/engine.py`, `jobsignals.py`, `service.py`, `outcomes.py` | Hiring clusters, stack migrations, departures, per-company rules | derived | `/signals/run`, `/signals/jobs/run`; lifecycle, crawl, monitor | `signals` | hiring_signals, contact_snapshots, company scores | Intel, accounts, scoring | — | test_platform_signals, test_job_lifecycle | CONFIRMED: 5,594 HIRING_CLUSTER + 51 STACK_MIGRATION | KEEP |
| C26 | Technology detection | `technology/service.py`, `data/erp_catalog.json` | Technologies from text | derived | jobs/scraper services | — | company_technologies | Companies | — | indirect | 0 rows. `record_from_text`, `run_provider`, `remove` and its `TechnologyProvider` ABC have **zero callers** | KEEP (remove the dead methods: see U17) |

### 3.3 Shared crawler utilities

| ID | Utility | Paths | Used by | Duplicates | Rec |
|---|---|---|---|---|---|
| U1 | Legacy HTTP session | `utils/http.py` (`build_session`, BoundedRetry, Chrome UA) | C1–C10, C21, C22 | `cloud/intel/core/http.py` (different policy: honest bot UA, robots.txt) | KEEP |
| U2 | Legacy browser | `utils/browser.py` | adapters, engine, cloud runner | `scraper/fetcher.PlaywrightRenderer` | CONSOLIDATE (one Playwright wrapper) |
| U3 | Browser pool | `utils/browser_pool.py` | **tests only** (`tests/test_browser_pool.py`). CONFIRMED by grep | — | REMOVE CANDIDATE |
| U4 | Robots cache | `utils/robots.py` | **tests only** (`tests/test_robots.py`). CONFIRMED. The legacy crawler never checks robots.txt | `core/http.py` robots handling | UNUSED |
| U5 | Per-host politeness | `crawler/ratelimit.py` (DomainLimiter, off by default) and engine `_HostThrottle` (active) | C2 | each other | CONSOLIDATE |
| U6 | Embedded-JSON job discovery | `utils/discovery.py` | 14 adapters | `scraper/extractor` JSON-LD paths (partial) | KEEP |
| U7 | ATS detection | `crawler/platform_detector.py` vs `cloud/intel/vendor/ats_detect.py` (vendored from another project; different labels; knows brassring and appone) | legacy vs platform | each other; `ats_detect.find_ats_in_text` duplicates `resolve.ats_link_on` | CONSOLIDATE |
| U8 | SSRF-safe fetcher | `cloud/intel/core/http.py` (SafeFetcher) | scraper, sources, discovery, job monitor | U1 | KEEP |
| U9 | Page fetcher | `cloud/intel/scraper/fetcher.py` (PageFetcher wraps SafeFetcher) | scraper, job monitor | — (correctly layered) | KEEP |
| U10 | URL canonicalization (4 versions) | `jobs/service.canonical_job_url`, `job_monitor/schema.job_url_key` (adds the site-profile key), `scraper/normalizer.canonical_url`, `sources/base._key` (lower-case only), legacy `utils/urlkey.url_key` | all paths | each other. **Same job can get different keys** | CONSOLIDATE |
| U11 | Company name keys | `utils/names.py` vs vendored copy `cloud/intel/vendor/names.py` (`company_key` bodies match) | legacy vs platform | each other | CONSOLIDATE |
| U12 | IT/tech job classification | `crawler/tech_filter.py` + `crawler/keywords.py` (legacy, live) vs `job_monitor/relevance.py` (platform, keyword sets) vs campaign rules in `jobs/service.py` (`is_relevant`) | legacy C1; platform C11/C14 vs C21/C17 | three classifiers | CONSOLIDATE |
| U13 | `vendor/it_classify.py` | — | **no importer anywhere, tests included**. CONFIRMED | U12 | REMOVE CANDIDATE |
| U14 | `vendor/zc_clean.py` | — | **no importer anywhere**. CONFIRMED | — | REMOVE CANDIDATE |
| U15 | Job diff | `crawler/weekly_diff.py` (full-week set compare + re-link) vs `job_monitor/diff.py` (per record) | C1 vs C11 | different granularity | KEEP |
| U16 | Careers-link ranking | `crawler/career_finder.score_candidate` vs `scraper/discovery.rank_candidates` vs `scraper/extractor.careers_candidates` vs `discovery/service._page_facts` | C5, C16, C20 | four rankers | CONSOLIDATE |
| U17 | Unused technology methods | `technology/service.py:117,126,142` + its `TechnologyProvider` ABC (name clash with `providers/base.TechnologyProvider`) | **zero callers**. CONFIRMED | `providers/base` | REMOVE CANDIDATE |
| U18 | Legacy storage: Sheets | `sheets/*` | C1 (live) | — | KEEP (while C1 runs) |
| U19 | Legacy storage: SQLite | `store/*` (`jobs`, `companies`, `crawl_attempts` live; `discovery_queue` never filled) | C1 | — | KEEP |
| U20 | Never-used Sheets tabs and queue | NEW_COMPANY_DISCOVERY and DISCOVERY_CONFIG tabs (seeded only by `sheets/init.py`), SQLite `discovery_queue` (read only by `crawler/status.py` and a test) | none | — | UNUSED |

### 3.4 Crawler code outside this branch (read-only)

| ID | Location | What it is | Status | Rec |
|---|---|---|---|---|
| X1 | `E:\Crawlers\CareerCrawler` (`queue-recovery`, `46af82d`) | **Production** legacy crawler (C1) | Live, weekly | KEEP (it is C1–C10) |
| X2 | `E:\Crawlers\CareerCrawler-seamless` (`seamless-integration`, `ce67e08`, 40 uncommitted paths) | Early Seamless.AI enrichment inside the legacy crawler (`seamless/`, `zerocredit/`) | Superseded by `cloud/intel/providers/seamless.py` + `vendor/seamless_*`. It holds 6.7 GB of `state/` | OBSOLETE (provisional; the owner should confirm the state data is not needed) |
| X3 | `E:\Crawlers\cl-crawl-job-board-crawler-49f0e6c54da3` | LinkedIn/Indeed/Monster job-board crawler with proxy rotation, stealth and CAPTCHA handling | Deliberately **not reused** by SANA (memory and README safety rule) | OBSOLETE (for SANA) |
| X4 | `C:\Users\Narsimha Reddy\wearedevelopers_job_scraper` | Standalone Playwright WAD scraper; produced the 2026-09-04 baseline CSV | Replaced by C11 | OBSOLETE |
| X5 | `E:\Crawlers\zoominfo-erp-automation` | Operator-attended ZoomInfo browser automation (separate project) | Not part of SANA; ZoomInfo API connector T7 is the platform path | BLOCKED (separate product and ToS decision) |
| X6 | `E:\Crawlers\CareerCrawler-sana-jobspy` (11 uncommitted paths) | JobSpy keyword and relevance work beyond C13 | Uncommitted | BLOCKED (same decision as C13) |

---

## 4. Complete non-crawler tool inventory

| ID | Tool | Paths | Why / feature | Required? | Paid? | Alternative in repo | Self-host needs it? | If removed | Rec |
|---|---|---|---|---|---|---|---|---|---|
| T1 | Provider registry + Fernet secrets | `providers/registry.py` | Per-workspace encrypted credentials (`CAREERCLOUD_PLATFORM_SECRETS_KEY`); also used by mailboxes and integrations | required | free | — | **yes** | every provider, mailbox and integration | KEEP |
| T2 | Credit ledger | `providers/credits.py` | Reserve/consume budget for paid calls | required with paid providers | free | `vendor/seamless_credits.CreditBudget` (dead, T22) | yes | budget guard | KEEP |
| T3 | Credit-aware routing / plan | `providers/routing.py` | Plan preview before spending | required (agent, research) | free | `EnrichmentService.priority`, `ContactIntelService` order | yes | plan previews | CONSOLIDATE (three provider orders disagree: routing prefers Seamless and checks `configured()`; enrichment prefers ZoomInfo and checks `enabled()`; contacts uses caller order) |
| T4 | Company enrichment (public + partner) | `providers/enrichment.py` | Firmographics | optional | partner paid | ZoomInfo | public part yes | enrichment tab | KEEP / OPTIONAL |
| T5 | Contact intelligence | `providers/contacts.py` | Contact gaps and finding | core GTM | paid steps | — | yes | contact finding | KEEP |
| T6 | Seamless.AI connector | `providers/seamless.py`, `vendor/seamless_targeting.py`, `vendor/seamless_credits.py` | Contact search | optional | **paid** | ZoomInfo (contacts) | optional | paid contacts | KEEP / OPTIONAL |
| T7 | ZoomInfo GTM API | `providers/zoominfo.py` | Company/technology search (credit-free), enrichment (paid) | optional | **paid** enrich | Seamless | optional | firmographics fallback to public | KEEP / OPTIONAL (see 9: server-env credential fallback) |
| T8 | AI registry + rules provider | `ai/registry.py`, `ai/rules.py`, `ai/base.py` | Provider choice, PII redaction, budgets, free-only Gemini mode; deterministic default | required | — | — | yes | all AI features | KEEP |
| T9 | Claude client | `ai/claude.py` (anthropic SDK) | LLM | optional | **paid** | Gemini, OpenAI-compatible | optional | — | KEEP / OPTIONAL |
| T10 | Gemini + OpenAI-compatible | `ai/rest.py` (requests) | LLM; Gemini is the only free tier | optional | free tier / paid | Claude | optional | — | KEEP / OPTIONAL |
| T11 | AI Control Room agent | `agent/*` | Plan, approve, run | UI feature | uses paid tools after approval | research (T12) | yes | agent UI | CONSOLIDATE (two planners; `/research` history lists `/agent/runs`) |
| T12 | Research engine | `research/*` | Research UI and automation action | UI feature | approval-gated | agent | yes | research UI | KEEP |
| T13 | Email validation (CRM path) | `email/service.py`, `email/providers.py` (LocalValidator, `dns_has_mail`) | Contact email check | required | — | T14 | yes | contact validation | CONSOLIDATE (second DNS stack) |
| T14 | Email evidence engine + bulk jobs | `email/engine.py`, `email/jobs.py` | Bulk CSV validation (DNS, SPF, DMARC, opt-in SMTP preflight) | required | — | T13 (one row can trigger both DNS stacks, `jobs.py:1257`) | yes | Email Validation page | KEEP (canonical) |
| T15 | EmailListVerify | `email/providers.py` (ELV) | Paid verification of unknowns | optional | **paid** | — | optional | VALID verdicts | KEEP / OPTIONAL |
| T16 | Sending: mailboxes, outbox, events | `sending/*` | Gmail API, MS Graph, Postmark, SendGrid, SMTP | optional (off by default) | SendGrid/Postmark paid | `gtm/sequences.SmtpSender` | optional | outbound | KEEP / OPTIONAL |
| T17 | Sequences + suppression | `gtm/sequences.py`, `gtm/suppression.py` | Sequences, unsubscribe, suppression lists | required if sending | free | T16 | yes | compliance | KEEP |
| T18 | Integrations (Slack, signed webhooks, calendars) | `integrations/service.py` | Notification fan-out | optional | free | automation webhook action (different signature header and secret) | optional | — | KEEP / OPTIONAL |
| T19 | Automation engine | `automation/engine.py` | Workflows | optional | can trigger paid steps (admin-saved only) | — | optional | workflows | KEEP / OPTIONAL |
| T20 | Exports (5 writers, 4 formula-neutralise copies) | `exports/service.py`, `scraper/exports.py`, `job_monitor/exports.py`, `email/jobs.py`, `analytics/reports.py`, `research/service.py`; legacy `exporters/excel_exporter.py` (used by C22) | Files | required | free | each other | yes | exports | CONSOLIDATE |
| T21 | Imports (canonical + ad hoc) | `imports/parse.py` (canonical), ad-hoc readers in `scraper/service.py`, `gtm/suppression.py`, `job_monitor/importer.py`, `job_monitor/relevance.py` | Uploads | required | free | each other | yes | imports | CONSOLIDATE |
| T22 | Dead Seamless helpers | `vendor/seamless_credits.CreditBudget`/`CreditCeilingReached`, `seamless_targeting.validate_departments` | none: self-references only. CONFIRMED | — | — | T2 | no | nothing | REMOVE CANDIDATE |
| T23 | Scoring (three places) | `scoring/service.py`, `agent/scoring.py`, `signals/service.py` | Company and signal scores | required | free | each other | yes | scores | CONSOLIDATE |
| T24 | Task kind `analytics` | `analytics/service.py:run_analytics_task` | handler with **no production submitter** (only `test_task_kinds_matrix`). CONFIRMED | — | — | analytics reads run synchronously | no | nothing observed | UNUSED |
| T25 | Task kind `integration` | `integrations/service.py:run_integration_task` | handler with **no production submitter**. CONFIRMED | — | — | direct delivery | no | nothing observed | UNUSED |
| T26 | Automation trigger `company_matched` | `automation/engine.py:69` | **never emitted** (grep of all `emit` calls). CONFIRMED | — | — | — | — | nothing | UNUSED |
| T27 | CRM, analytics/reports, admin/notifications, gtm campaigns/bridge, imports dedupe | respective `service.py` | Core product | required | free | — | yes | core UI | KEEP |
| T28 | Supabase Auth (JWKS verification + `supabase.auth.*` in the dashboard) | `cloud/api/auth.py`, `web/src/auth/AuthProvider.tsx` | Sign-in; existing user IDs | **required** | free tier | none yet | **yes** (temporarily) | all sign-in | KEEP |

---

## 5. Workflow and dependency maps

### 5.1 Task kinds (23, all with handlers: `tasks/worker.py:46-70`)

| Used in production (backup) | Defined, never run in production | No production trigger at all |
|---|---|---|
| job_monitor (3), job_lifecycle (10), job_import (1), scraper (7), email_validation_job (15) | crawl, discovery, enrichment, validation, research, workflow, workflow_resume, import_merge, monitor, signals, export, source_search, email_send, scoring, internal_data, job_export | analytics, integration |

### 5.2 Schedulers (CONFIRMED in code)

| Scheduler | Cadence | Triggers |
|---|---|---|
| Windows task "CareerCrawler Weekly" | Sat 06:00 | C1 (legacy, outside SANA) |
| Platform worker `maintain()` | every 30 s | reap leases; `monitoring.schedule_due`; daily `platform_tasks` purge (90 d); `scraper.recover`; hourly insights; ticks of outbox, automation and job_monitors |
| `JobMonitorService.tick` | per monitor daily/weekly | C11/C12/C13 incremental or full sweep, then `schedule_lifecycle` (daily, idempotent per day) |
| `MonitoringService.schedule_due` | daily/weekly/monthly | C24 |
| `AutomationEngine.tick` | ≥ 5 min buckets | workflows |
| `Outbox.tick` | each maintain | sends (no-op unless production and `ALLOW_EMAIL_SENDING`) |
| Legacy `cloud/worker` maintenance | 30 s | C22 (Redis only) |

**Overlap findings:**
- **CONFIRMED:** `evaluate_lifecycle` calls the **workspace-wide** `detect_job_signals` once per monitor per day (`job_monitor/service.py:945`). With N monitors that is N identical scans. Production has 1 monitor, so the cost is zero today.
- **PROVISIONAL:** two platform workers could both start the same due monitor. Run idempotency is per run id, with no slot lock (`service.py:312`). A single-worker deployment (the self-host default) is safe.
- **CONFIRMED:** `/crawl`, the agent and legacy `/api/v1/jobs` can crawl the same company concurrently. There is no per-company guard.

### 5.3 End-to-end flows

```
LEGACY  Sat 06:00 task -> weekly_run -> Sheets roster -> resolve/career_finder -> engine -> 59 adapters
        -> tech filter -> SQLite ledger (590 MB) -> weekly_diff -> Google Sheets tabs -> humans
        (no link to SANA GTM)

SANA    tick / UI -> job_monitor task -> WAD profile -> upsert_batch (url_key = job_url_key)
        -> job_postings + job_posting_changes -> lifecycle (stale/expired) -> hiring_signals
        -> Hiring companies (accounts build -> companies) -> contacts (Seamless/ZoomInfo, paid, approval)
        -> email validation (built-in, ELV paid) -> campaigns/sequences -> outbox -> outcomes

        UI "Crawl careers" -> crawl task -> bridge -> legacy engine -> ingest_postings
        (url_key = canonical_job_url; NO relevance_class; change_events, not job_posting_changes)

        UI scraper -> scraper task -> scrape_* tables + files -> proposals -> ingest_postings / CRM upserts
```

### 5.4 Writers with no consumer, and consumers with no data

- **No production data:** companies, contacts, sequences, outbound messages, research, workflows and 45 more tables are empty (section 8.4). The workflow **Jobs → Signals** is populated; **Companies → … → Outcome** has never run in production.
- **C22** writes `careercloud.jobs`/`job_results` files that only the legacy pages read.
- **C1** writes Sheets and SQLite that SANA never reads.
- **Backend routes with no frontend caller** (CONFIRMED by grep of `cloud/web/src`; some are used by tests or external callbacks):
  - **Used only by tests:**
    - `POST /jobs/ingest`, `POST /monitors/{id}/run`, `POST /sources/{source}/search`
    - `GET /job-monitor-runs`, `GET /job-monitor-runs/{id}`
    - `POST /scraper/schema`, `GET /scraper/runs/{id}/results`
    - `GET /imports/{id}/rows`, `POST /imports/{id}/rows/{id}/resolve`
    - `POST /companies/{id}/enrich`
    - `GET /scores/model`, `POST /scores/rescore`
    - `POST /sequences/process-due`, `POST /events/inbound`
    - `GET /analytics/timeseries`, the company child-list routes (`routes_crm.py:63`)
    - `GET /w/{id}`, `POST /w/{id}/members`, `GET /w/{id}/audit`
  - **No caller found at all:**
    - `PATCH /admin/teams/{id}`, `DELETE /agent/ai/key/{provider}`, `GET /research/runs/{id}/export`
    - `GET`/`POST /companies/{id}/relationships`, `POST /companies/merge`, `GET /segments/{id}/results`
    - `POST /campaigns/{id}/match`, `POST /enrollments/{id}/stop`, `POST /workflows/emit`, `POST /sequence-enrollments/{id}/signal`
    - `PUT /enrichment/priority`, `POST /hiring-companies/add-to-list`, `GET`/`PATCH /scraper/templates/{id}`
    - `DELETE /mailboxes/{id}` (the UI uses `/disconnect`), `GET /inbound-events`, `POST /suppression/add`
    - `DELETE /providers/{p}/credentials`, `PATCH /providers/{p}/settings`, `POST /credits/{p}/grant`, `POST /credits/{p}/limit`
  - **Called by external systems** (keep): the OAuth callback, the inbound webhook and the unsubscribe links.
  - These routes were found by static search of the frontend source; scripts or users calling the API directly cannot be ruled out (PROVISIONAL).
- **Dead UI code:** `web/src/pages/Dashboard.tsx` (not imported), `AnalyticsSection` (`Sections.tsx:491`).

---

## 6. Duplicate and overlapping groups

| Group | Members | Classification | Keep | Why |
|---|---|---|---|---|
| **G1. Job write paths** | `job_monitor/service.upsert_batch` (C11, C12, C13, C14) vs `jobs/service.ingest_postings` (C17, C19, C21, scraper apply) | **Functional overlap, divergent behaviour** | `upsert_batch` | It has the diff, lifecycle, history and review queue. `ingest_postings` uses a different URL key (no site-profile key, `jobs/service.py:153`), overwrites monitor-owned `status`/`source_*` without a history row (`:206-218`), never sets `relevance_class` (so its jobs are invisible to hiring signals), and logs to `change_events`, not `job_posting_changes` |
| G2. CareerCloud crawl runners | `jobs/careercrawler_bridge._crawl_one` vs `worker/careercrawler_runner._crawl_one` | **Near-exact duplicate** | bridge | It feeds `job_postings`; the runner feeds only legacy files |
| G3. ATS job fetching | legacy adapters C3 vs `sources/adapters.ATSPublicAdapter` (C17) vs `scraper/extractor.ats_api_jobs` | Functional overlap (same Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Workday CXS endpoints) | **C3** for breadth (59 platforms, proven at 349k postings/week); `ats_detect.api_endpoints` as the platform's single endpoint table | C3 has production yield evidence; the cloud versions have none |
| G4. ATS detection | `crawler/platform_detector` vs `vendor/ats_detect` | Functional overlap | One table; keep `platform_detector` enums for the engine and map `ats_detect` labels to them | Two label sets diverge (e.g. "Breezy HR") |
| G5. Careers-page discovery | `career_finder` + `resolve` + `ats_discovery` (legacy) vs `scraper/discovery` vs `extractor.careers_candidates` vs `discovery/service._page_facts` | Functional overlap (six rankers) | `resolve` (legacy, live) and `scraper/discovery` (platform) | Live evidence vs scored and guess-limited |
| G6. URL canonicalization | U10 (five functions) | Shared utility to consolidate | `job_url_key` | Already the production key; a superset of `canonical_job_url` |
| G7. IT-job classification | legacy `tech_filter`/`keywords`, platform `relevance`, campaign rules, dead `it_classify` | Functional overlap | `job_monitor/relevance` (keyword sets, versioned, rescorable) | Delete `it_classify`; map legacy keywords into a keyword set |
| G8. Company keys | `utils/names` vs `vendor/names` | Exact duplicate (vendored) | one shared module | Identical bodies |
| G9. Browser wrappers | `utils/browser` vs `scraper/fetcher.PlaywrightRenderer` (+ unused `browser_pool`) | Functional overlap | `utils/browser` (live) | — |
| G10. Per-host politeness | `ratelimit.DomainLimiter` vs engine `_HostThrottle` | Functional overlap | `_HostThrottle` (the active one) | — |
| G11. Hiring-spike rule | `signals/engine` HIRING_SPIKE vs `monitoring.diff_snapshots` hiring_spike | Two definitions | signals engine | — |
| G12. History tables | `job_posting_changes` (G1 path 1) vs `change_events` (path 2, CRM monitors) | Functional overlap | `job_posting_changes` for jobs | — |
| G13. Email validation DNS | `email/providers.dns_has_mail` vs `email/engine.DnsPythonClient` | Shared utility to consolidate | engine | — |
| G14. Exports and formula neutralising | T20 | Shared utility to consolidate | `exports/service` helpers | — |
| G15. Imports | T21 | Shared utility to consolidate | `imports/parse` | — |
| G16. Scoring | T23 | Functional overlap | `scoring/service` | — |
| G17. Agent vs research planners | T11 vs T12 | Functional overlap | Uncertain: needs product decision | Both are in the UI |
| G18. Health tooling | `deploy/windows/sana-gtm/healthcheck.py`, `verify-sana-gtm.ps1`, installer `manager/verify.py`, `cloud.ops.selfhost health`, `SanaGtm.psm1` health, selfhost systemd timers | Functional overlap (five implementations) | `/api/v1/status`, `cloud.ops.selfhost health`, `SanaGtm.psm1` | Target-architecture tools |
| G19. Deployments | `deploy/windows-selfhost` vs `deploy/selfhost` vs `deploy/windows/installer` vs `deploy/windows/sana-gtm` vs `cloud/deploy/staging` vs `cloud/ops/local_stack.py` vs root `.bat` files | Functional overlap | `deploy/windows-selfhost` | Matches the target; 40/40 e2e |
| G20. Queues and workers | `tasks/pgqueue.py` + platform worker vs `shared/queue.RedisJobQueue` + `cloud/worker` vs `InlineTaskQueue` | Functional overlap | PostgreSQL queue + platform worker | Every platform kind runs on it |
| G21. Legitimately different | WAD profile (C11) vs legacy adapters (C3): different sources (job board vs employer ATS). `weekly_diff` vs `job_monitor/diff`: set compare vs per record. Signals engine vs jobsignals | Legitimately different | both | — |

---

## 7. Unused, obsolete and unverified components

### 7.1 Infrastructure inventory and classification

| ID | Component | Paths | Evidence | Rec |
|---|---|---|---|---|
| I1 | API app | `cloud/api/*` | live | KEEP |
| I2 | Platform store + migrations | `cloud/intel/store/*`, `cloud/db/{connection,migrate}.py`, `migrations/0001-0014` (0010 deliberately absent) | live | KEEP |
| I3 | PostgreSQL queue + platform worker | `cloud/intel/tasks/*` | live in production (`CAREERCLOUD_QUEUE=postgres`) | KEEP |
| I4 | Local file storage | `cloud/shared/storage.py` | default | KEEP |
| I5 | Environment isolation guard | `cloud/shared/environment.py`, `config.py`, `logs.py` | boot checks | KEEP |
| I6 | Ops: selfhost, webapp, stamps | `cloud/ops/{selfhost,webapp,stamps}.py` | used by windows-selfhost | KEEP |
| I7 | Backup and monitor-settings tools | `deploy/ops/*` | used 2026-10-14 | KEEP |
| I8 | Native Windows self-host | `deploy/windows-selfhost/*` | 40/40 user-mode e2e | KEEP |
| I9 | Embedded dev PostgreSQL | `cloud/devtools/localpg.py` | tests and dev | KEEP |
| I10 | Redis queue + helpers | `cloud/shared/queue.py:123-147, 302-499` (RedisJobQueue), `bootstrap.py:139-150`, `api/main.py:143-150`, `connection.resolve_redis_url`, `stamps.ensure_redis_stamp` | Only the legacy worker, old installer and staging need it. The **base classes in queue.py stay** (pgqueue subclasses them) | REMOVE CANDIDATE |
| I11 | fakeredis dev server | `cloud/devtools/fake_redis.py` | only for I10 | REMOVE CANDIDATE |
| I12 | S3 result storage | `cloud/shared/s3_storage.py` | only with `STORAGE_BACKEND=s3`; removing it makes `SELF_HOSTED=1` mandatory for production | BLOCKED (decide whether any non-self-host production remains) |
| I13 | Legacy job layer | `cloud/api/routes.py:337-490`, `cloud/db/postgres.py`, `cloud/shared/{repository,service,models,schemas}.py`, 0001 tables, pages `/new`, `/settings/crawls`, `/dashboard` | On the PostgreSQL queue these crawls never run (`NullDispatcher`) | OBSOLETE (C22) |
| I14 | Legacy worker entry | `cloud/worker/{__main__,worker,settings,egress,dispatcher}.py` | Redis only. **Keep** `results.neutralise_cell`, `careercrawler_runner` settings helpers and `workspace` until moved (imported by intel and api) | OBSOLETE |
| I15 | Linux staging + staging ops | `cloud/deploy/staging/*`, `cloud/ops/{staging_env,staging_e2e,safety_report}.py` | Redis/Upstash/S3 design | OBSOLETE |
| I16 | Local stack | `cloud/ops/local_stack.py` | starts the Redis worker | OBSOLETE |
| I17 | Old PyInstaller installer | `deploy/windows/installer/*` | hard-codes `QUEUE=redis` (`sanagtm_common.py:61`); publishes the tunnel URL to Upstash; build blocked by antivirus | REMOVE CANDIDATE |
| I18 | Current production supervisor | `deploy/windows/sana-gtm/*` + root `start/stop/status-sana-gtm.bat`, `install/uninstall-sana-gtm-autostart.bat` | **runs production today** | BLOCKED (until cutover) |
| I19 | Dev launch scripts | `run-sanagtm-staging.bat`, `run-sanagtm-worker.bat`, `run-platform-worker.bat` | manual equivalents of the services | OBSOLETE |
| I20 | Legacy worker scripts | `run-worker.bat`, `stop-worker.bat` | drive I14 | REMOVE CANDIDATE |
| I21 | Pages Function proxy | `cloud/web/functions/api/[[path]].js` | reads the origin from Upstash REST; allows only `*.trycloudflare.com` | OBSOLETE |
| I22 | Docker/Ubuntu self-host | `deploy/selfhost/*` | valid Linux alternative, shares `cloud/ops/selfhost.py` | KEEP / OPTIONAL |
| I23 | Demo page | `cloud/demo/*` | static, harmless | KEEP / OPTIONAL |
| I24 | Legacy deploy units for the weekly crawler | `deploy/careercrawler.{service,timer}`, `install.sh` | other product (C1 on Linux) | KEEP / OPTIONAL |
| I25 | Stale `.env.example` defaults | `cloud/api/.env.example:31-32`, `cloud/worker/.env.example:13` (QUEUE=redis); `bootstrap.py` docstring | documentation drift | CONSOLIDATE (doc fix) |

### 7.2 Frontend duplicates (CONFIRMED in `cloud/web/src/App.tsx`, `nav.ts`)

| ID | Duplicate | Rec |
|---|---|---|
| F1 | Job postings shown three ways: `/jobs` (`/job-feed`), `/hiring?tab=jobs` (CRUD `/jobs`), `/settings/crawls` (legacy crawl jobs) | CONSOLIDATE |
| F2 | Four ways to start a crawl: `/new` (legacy), "Crawl careers" (C21), `/monitors` (C11), `/scraper` (C16) | CONSOLIDATE (drop `/new` with C22) |
| F3 | `/monitors` (job monitors) vs `/change-monitors` (C24), same nav icon | CONSOLIDATE |
| F4 | Hiring companies: `/jobs?tab=hiring` vs `/hiring?tab=companies`; Signals component mounted 3 times; Settings tabs repeating whole routes; `/analytics` vs `/dashboard`; legacy `web/src/pages/*` beside `web/src/platform/*`; unused `pages/Dashboard.tsx` | CONSOLIDATE |

### 7.3 Defects found (not removals; fix candidates)

| ID | Defect | Evidence | Rec |
|---|---|---|---|
| D1 | `careercrawler` source always fails | `sources/service.py:107` submits `{"companies": …}`; `crawl_task.py:38-47` refuses it | (C19) REMOVE CANDIDATE or fix |
| D2 | No egress guard on the platform worker's crawl path | Only `cloud/worker/__main__.py:108-115` installs `install_egress_guard`; the bridge checks the first hop's DNS only | KEEP (fix: install the guard in the platform worker for `crawl`) |
| D3 | Jobs from `ingest_postings` get no `relevance_class` and fire no `job_posting_changes` | G1 | part of the G1 CONSOLIDATE |
| D4 | Lifecycle runs workspace-wide signals once per monitor | `job_monitor/service.py:945` | part of the G1 CONSOLIDATE (provisional cost) |
| D5 | Platform worker logs are text only (ignores `LOG_FORMAT=json`) | `tasks/worker.py:493-494` | fix candidate |
| D6 | `dnspython` (and explicit `cryptography`) not declared in `cloud/api` and `cloud/worker` requirements | the email engine imports it lazily | fix candidate |
| D7 | ZoomInfo server-env credential fallback is available to every workspace | `providers/zoominfo.py:105-106` vs "private to the workspace" (`registry.py:6`) | fix candidate (provisional) |

Defects are not counted in the component totals; D1 is counted once, as C19.

---

## 8. Database duplication and storage findings

All figures come from the 2026-10-14 production backup restored locally, CONFIRMED unless labelled. The fresh restore is **354 MB**. The live Supabase database was reported at about 452 MB on 2026-10-09 (`STORAGE_AND_GROWTH.md`). The difference is ESTIMATED to be dead tuples and bloat that a restore does not carry.

### 8.1 Where the space is

| Table | Rows | Total | Heap | Indexes |
|---|---|---|---|---|
| job_postings | 115,682 | 178 MB | 130 MB | 49 MB (26 indexes) |
| job_posting_changes | 231,360 | 128 MB | 81 MB | 48 MB |
| hiring_signals | 5,645 | 14 MB | 9 MB | 1.6 MB |
| job_company_reviews | 22,670 | 9.8 MB | 5 MB | 4.7 MB |
| email_validation_items | 6,330 | 4.7 MB | | |
| everything else (86 tables) | | < 5 MB | | |

### 8.2 Duplicates

- **Job URL duplicates: none.** 115,682 rows have 115,682 distinct `url_key` values; uniqueness is enforced per workspace (`job_postings_uq_url_key`).
- **Possible reposts:** 113,404 distinct (company, title, location) combinations for 115,682 rows, so about **2,278 rows** share all three with another row under a different URL. They may be legitimate multi-postings. Not verified.
- **No cross-source duplicates yet:** 115,680 rows come from WAD and 2 from an import. The G1 key divergence is a code risk, not a data problem today.
- **`hiring_signals`:** 5,645 rows with 5,645 distinct fingerprints, so no duplicates. A signal's fingerprint changes when its job set changes, which creates a new row and expires the old one. Growth comes from churn, not duplication.

### 8.3 History, retention and redundant storage

- **`job_posting_changes` = 115,680 `new` rows (all on 2026-10-02) + 115,680 `stale` rows (all on 2026-10-06).** Nothing else.
  - The `new` row repeats `job_postings.first_seen_at`; the `stale` row repeats `stale_at`.
  - The cause is a first-run bulk insert plus `stale_after_days = 1`.
  - ESTIMATE: not writing bulk `new` and `stale` rows, or pruning them after N days, would save most of the table's 128 MB. The JSON payload is only 24 MB; the rest is row overhead and three indexes.
- **The only retention job is the `platform_tasks` purge (90 days).** Nothing prunes `job_posting_changes`, `audit_log`, `ai_usage`, `notifications`, `source_records`, `discovery_candidates`, `scrape_*`, `research_*`, export files or uploads. CONFIRMED in code.
- **Columns that are always empty in production** (0 of 115,682 rows): `description`, `posted_at`, `domain`, `company_id`, `external_id`, `employment_type`, `industry`, `ats`, `years_experience_min`, `closed_at`, `keyword_4`, `keyword_5`, `expired_at`, `reopened_at`, `closure_reason`, `gone_*`, `last_changed_*`, `created_by`. Empty columns cost almost nothing in PostgreSQL. What costs space is the **indexes on them**.
- **`job_postings` has 24 single-column `(workspace_id, col)` indexes**, about 0.8 MB each at today's size, including indexes on always-empty columns (`ats`, `domain`, `company_id`, `expired_at`, `closed_run_id`, `last_changed_run_id`). ESTIMATE: about 15–20 MB today, growing linearly with jobs, and every insert updates all 26 indexes.
- **About 25 indexes elsewhere repeat the leading columns of a unique index** (e.g. `email_check_cache_by_ws_subject_hash`, `scrape_pages.run_id`). Small today.
- **Overlapping columns in `job_postings`:**
  - two relevance systems (`is_relevant`/`relevance_reasons`/`campaign_keys` vs `relevance_score`/`relevance_class`/`matched_keywords`);
  - four source columns (`source_kind`, `source_name`, `source`, `source_board`);
  - `skills` copies `keyword_1-5`.
- **Content stored more than once:**
  - Import rows: the upload file, `import_rows.original` and `source_records.original`.
  - Scraper records: `scrape_results.data` plus 5–6 files.
  - Email results: 3–4 places.
  - No table stores raw HTML.
- **Outside SANA:** the legacy crawler's SQLite ledger is 590 MB, and its crawl logs in `output/` exceed 180 MB.

### 8.4 Tables with no production rows (51 of 91)

activities, agent_approvals, agent_results, ai_insights, ai_memory, ai_usage, change_events, companies, company_relationships, company_snapshots, company_technologies, contact_snapshots, contacts, crm_tasks, custom_field_defs, deployment, discovery_candidates, email_templates, idempotency_keys, import_batches, import_files, import_rows, inbound_events, integration_deliveries, list_members, mailboxes, message_events, monitors, notes, oauth_states, opportunities, outbound_messages, research_results, research_runs, saved_reports, saved_requests, score_snapshots, scrape_proposals, scrape_templates, segments, sequence_enrollments, sequence_steps, sequences, signal_outcomes, source_records, suppressions, tags, team_members, teams, workflow_proposals, workflow_runs, workflows.

An empty table is **not** evidence that a feature is unnecessary. Most of these back the Companies → Outcome stages, which production has not reached yet.

---

## 9. Paid and optional dependency findings

**Paid services that can be invoked:**
- Seamless.AI
- ZoomInfo (enrich only)
- EmailListVerify
- Anthropic, and Gemini beyond the free tier
- OpenAI-compatible endpoints
- Partner API
- SendGrid and Postmark

**Guards (all default off):**
- `allow_paid=False` everywhere.
- The ledger refuses to reserve without a synced balance and enforces a per-task cap of 500.
- ELV needs a verified key and a job budget.
- Paid automation steps run only when an admin saved them.
- Research and the agent need approval.
- AI defaults to `rules`.
- Sending is off unless production and `ALLOW_EMAIL_SENDING`.

CONFIRMED in code.

**Production connections (backup):** zoominfo, emaillistverify and seamless are **verified**; claude is not configured. `ai_usage` and `credit_ledger` show essentially no spend (3 ledger rows).

**Python packages:**

| Package | Status for the self-hosted SANA runtime |
|---|---|
| fastapi, starlette, uvicorn, pydantic, PyJWT, cryptography, psycopg, psycopg-pool, python-multipart, python-dotenv, requests, openpyxl, beautifulsoup4, lxml, dnspython | Required |
| loguru | Required (the CareerCrawler engine, used by C21) |
| anthropic, playwright | Optional |
| python-jobspy | BLOCKED (C13) |
| **redis, boto3** | Not needed. Still listed in `cloud/api/requirements.txt` (I10, I12); already absent from the Windows lock |
| **pandas, numpy** | Not needed by SANA at runtime: only `crawler/csv_reader.py` imports pandas, and only the legacy CLI path (C8) and the Sheets import tools load it. Still in `deploy/windows-selfhost/requirements-lock.txt`. Correction to an agent finding: pandas **is** used by the legacy crawler, so it must stay in the root `requirements.txt` |
| httpx2, httpcore2, psutil | In the Windows production lock but test or dev only; needed only if `sanagtm selftest` runs on the server (provisional) |
| google-api-python-client, google-auth* | Legacy crawler only (C1, Sheets). Not part of SANA |
| fakeredis, moto, pgserver | Dev only; fakeredis and moto only for I10/I12 |

**Infrastructure to replace:**
- **Supabase PostgreSQL:** replaced by the self-hosted PostgreSQL. The migrations already create the `authenticated` role and an `auth.uid()` shim on plain PostgreSQL.
- **Upstash Redis:** no longer used by production (`QUEUE=postgres`).
- **Supabase Auth:** must stay; the backend only verifies JWKS and the dashboard uses `supabase.auth.*` only. No Supabase Storage, Edge Functions or RPC calls exist in code.

---

## 10. Recommended keep / consolidate / remove plan

Removal candidates, with the evidence required by the brief (all paths relative to the repository root):

| ID | Remove | Evidence | Callers checked | Risk | Regression tests needed | Status |
|---|---|---|---|---|---|---|
| U3 | `utils/browser_pool.py` (+ its test) | grep: tests only | imports, strings, CLI, .bat, scheduled task | none known | `tests/` suite | CONFIRMED |
| U13 | `cloud/intel/vendor/it_classify.py` | no importer anywhere | imports, strings | none | platform suite | CONFIRMED |
| U14 | `cloud/intel/vendor/zc_clean.py` | no importer anywhere | imports, strings | none | platform suite | CONFIRMED |
| U17 | `technology/service.py` dead methods + ABC | zero callers | imports, routes, agent tools | none | platform suite | CONFIRMED |
| T22 | dead Seamless helpers | self-references only | imports | low; the vendored "keep identical" note must be updated | test_platform_contacts, credits | CONFIRMED |
| C19 | `careercrawler` source branch | always fails | route (tests only), frontend (none) | none; or fix it to submit company records the crawl accepts | test_platform_sources | CONFIRMED |
| I10/I11 | Redis queue, helpers, fakeredis, `redis` and `boto3` requirements | only legacy C22, I15–I17, I21 use them | bootstrap, api main, worker, healthcheck default `redis` (`deploy/windows/sana-gtm/healthcheck.py:57`) | **production health check defaults to Redis when `CAREERCLOUD_QUEUE` is unset**: remove only after cutover | test_queue, test_worker*, test_staging_guards, test_pg_queue_ops | PROVISIONAL |
| I17 | `deploy/windows/installer/*` | superseded by I8; never shipped | tests in its own folder; docs | low | windows package tests | PROVISIONAL |
| I20 | `run-worker.bat`, `stop-worker.bat` | drive the legacy worker | test_deploy_units | low | test_deploy_units | PROVISIONAL |

**Obsolete (remove after the stated precondition):**
- C22 + I13 + I14 (legacy CareerCloud crawl stack). Precondition: move `neutralise_cell` and the crawler settings helpers out of `cloud/worker`, and drop the `/new` and `/settings/crawls` routes. **Never edit migration 0001**; drop tables with a new migration.
- I15, I16, I19, I21; X2, X3, X4 (outside the branch; owner decision on their data).

**Consolidate (one implementation each):** G1 (highest priority), G3–G10, G12–G16, U5, U10–U12, U16, T3, T11/G17 (needs a product decision), T13, T20, T21, T23, C6, C17, C24, F1–F4, I25.

---

## 11. Target architecture (lean)

```
Collectors (essential)      CareerCrawler engine + ATS adapters (C2/C3)  -- employer ATS, weekly
                            Job monitor + WAD profile (C11)              -- job board, daily
                            Job CSV import (C14)
Collectors (optional)       AI scraper (C16), generic monitor strategy (C12), discovery (C20),
                            official ATS source APIs (C17, after G3)
One write path              job_monitor.upsert_batch, key = job_url_key
                            -> job_postings (+ job_posting_changes for real changes only)
Derivation                  lifecycle (C15) -> signals (C25) -> accounts -> companies
GTM                         contacts (T5 + Seamless/ZoomInfo optional) -> email validation (T14 + ELV optional)
                            -> sequences/suppression (T17) -> sending (T16 optional) -> outcomes
Platform                    FastAPI (I1) + PostgreSQL 17 store (I2) + PostgreSQL task queue + one
                            platform worker (I3) + local files (I4), Windows services (I8),
                            Cloudflare named tunnel, Supabase Auth (T28), Fernet key (T1)
```

**Rules:**
- **One scheduler:** the platform worker's `maintain()` loop, plus the legacy weekly task until C1 moves into a platform task kind (for example a `crawl` run over the roster as a scheduled job monitor). Then Google Sheets becomes an export, not the system of record.
- **One identity per record:**
  - Jobs: `job_url_key` per workspace (requisition id where the adapter has one, as the legacy `job_identity` does).
  - Companies: registrable domain, then a normalized name, confirmed by review.
  - Contacts: email, then (name, company).
- **One relevance engine:** keyword sets (`job_monitor/relevance`), seeded from the legacy IT_KEYWORDS tab.
- **Retention:**
  - Do not log bulk first-seen and lifecycle transitions as per-job history, or prune them after 30–90 days.
  - Keep `platform_tasks` at 90 days.
  - Add pruning for `notifications`, `audit_log` (by policy), `discovery_candidates`, `scrape_*` and stored export and upload files.
  - Set `stale_after_days` back to the default of 30 at cutover.
- **Shared utilities:**
  - `core/http` SafeFetcher with the egress guard for every platform fetch.
  - One Playwright wrapper.
  - One URL canonicalizer.
  - One company key.
  - One export/neutralise module.
  - One import parser.
- **No new paid infrastructure.** Every replacement above is local code or PostgreSQL.

**Must remain for authentication, encryption, deployment and rollback:**
- Supabase Auth (T28)
- `CAREERCLOUD_PLATFORM_SECRETS_KEY` + `providers/registry` (T1)
- migrations 0001–0014, including the 0001/0002 auth shim and the environment stamp in `deployment`
- `deploy/ops` backup tools (I7)
- `deploy/windows/sana-gtm` until cutover (I18)

---

## 12. Prioritized cleanup plan

| # | Action | Type | Risk | Precondition |
|---|---|---|---|---|
| 1 | Fix D1 (careercrawler source) or remove C19; fix D2 (egress guard in the platform worker); fix D6 (declare dnspython) | fix | low | none |
| 2 | **G1:** route `ingest_postings` through `upsert_batch` (one URL key, relevance class, history) | consolidate | medium (central path) | tests in section 13 |
| 3 | Retention: stop bulk `new`/`stale` history rows, or add a pruning job; restore `stale_after_days` | storage | low | production approval for the settings change |
| 4 | Remove the CONFIRMED dead code: U3, U13, U14, U17, T22 | remove | none | full suites green |
| 5 | Drop unused `job_postings` single-column indexes (new migration) after checking `pg_stat_user_indexes` on the self-hosted database | storage | low | runtime index usage evidence (BLOCKED until self-hosted) |
| 6 | After cutover: remove I10, I11, I15, I16, I17, I19, I20, I21, then C22/I13/I14 with a new migration and UI route removal; then I18 | remove | medium | cutover done, "SANA GTM Auto Start" unregistered, Upstash retired |
| 7 | Bring C1 into SANA: a platform task that runs the weekly roster through C2/C3 into `job_postings`; Sheets becomes an export | consolidate | high (largest data flow) | product decision; capacity check (349k postings/week) |
| 8 | Consolidate the shared utilities (U2, U5, U7, U10, U11, U12, U16, T13, T20, T21, T23) and the frontend duplicates F1–F4 | consolidate | low–medium | per-item tests |
| 9 | Decide C4, C13/X6, C18, X5, G17 | decide | — | business and legal decisions |

---

## 13. Tests and validation needed before removals

- **Always:** the full suites at the commit being changed: platform `cloud/tests` (1,975 at `c64676b`), CareerCrawler `tests` (1,696), web `npm test` (117), and the Windows user-mode e2e for anything under `deploy/` or `cloud/ops`.
- **G1:**
  - A WAD URL with a slug, ingested by both paths, gives **one** row.
  - `ingest_postings` keeps monitor-owned status and source fields.
  - Crawl-sourced jobs get a `relevance_class` and appear in HIRING_CLUSTER.
  - History lands in `job_posting_changes`.
- **Retention:** a lifecycle pass over N jobs writes only the expected rows; pruning never deletes `reopened` or `closed` evidence needed by signals.
- **Redis removal:**
  - Boot API and worker with no `redis` package installed.
  - `test_isolation` still passes.
  - The production health check works with `CAREERCLOUD_QUEUE=postgres` set explicitly.
- **Legacy job layer removal:**
  - The new migration applies on a restored copy of the production backup.
  - `/api/v1/status` and `/health` still pass.
  - No frontend route 404s.
- **Index drops:** compare query plans for the job feed and filters on the restored backup before and after.
- **D2:** a crawl that redirects to `127.0.0.1` or `10.x` is refused in the platform worker.

---

## 14. Unknowns requiring runtime or production evidence

1. **Per-adapter yield and error rates** for the 59 legacy adapters: only the W41 summary line was read. The SQLite `crawl_attempts` table and the FAILURES tab hold the data. Needed before touching C4 or any adapter.
2. **Index usage:** `pg_stat_user_indexes` from the live database. Statistics are not in a dump.
3. **Live database bloat:** the reported 452 MB vs the restored 354 MB, never measured directly.
4. **Whether anyone uses the Google Sheets tabs** produced by C1, and which ones. This is a business question.
5. **Whether the legacy `/settings/crawls` and `/new` pages** are used by anyone. Production has 3 legacy jobs from September.
6. **Product decisions:**
   - JobSpy (C13/X6) and the Indeed adapter (C4): ToS.
   - Keyed or licensed sources (C18).
   - Agent vs research (G17).
   - The ZoomInfo browser tool (X5).
7. **Whether the 6.7 GB `state/` of the Seamless worktree (X2)** holds data the owner wants to keep.
8. **Platform features with no production data** (companies → outcomes): their absence shows they are unused **so far**, not that they are unnecessary.
