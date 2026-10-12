# Architecture inventory

**Audited:** branch `feature/careercrawler-platform` @ `0bdcee4` (the code production runs) in the worktree `E:\Crawlers\CareerCrawler-platform`, on 2026-10-09.

**Method:** imports, registrations, routes and task kinds were traced with grep. The live processes and env files were inspected by variable **name** only.

**Status key:** IMPL = implemented and reachable; PART = partial; STUB = refuses without partner access; LEGACY = superseded but still referenced; DEAD = no importers.

**Action key:** KEEP · MIGRATE TO VM · REPLACE · REMOVE AFTER VERIFICATION · INVESTIGATE.

## Runtime topology today (verified 2026-10-09)

```
Browser ──► https://sanagtm.pages.dev   (Cloudflare Pages, static React build; Supabase Auth in the browser)
   │  API calls go straight to a *.trycloudflare.com quick-tunnel URL baked into the build
   ▼
cloudflared quick tunnel ──► this Windows 11 PC (i3-12100, 32 GB) ── uvicorn API 127.0.0.1:8100
                                                                 └─ platform worker (python -m cloud.intel.tasks.worker)
   supervisor: scheduled task "SANA GTM Auto Start" → deploy\windows\sana-gtm\sana-gtm.ps1 -Action run
   sync-frontend.ps1 rebuilds and redeploys Pages whenever the tunnel URL changes
API + worker ──► Supabase PostgreSQL 17.6 (session pooler :5432, sslmode=require)
                 CAREERCLOUD_QUEUE=postgres (cut over 2026-10-06; Redis rollback env files kept)
Upstash Redis: no longer used by the running API/worker (REDIS_URL still set; the API logs "has no effect")
```

No VMware, Docker or WSL is installed on this PC. The firmware virtualization flag reads `False`, but a hypervisor is present because VBS is running. **The target VM does not exist yet.**

## Component inventory

### Application

| Component | Current location | Purpose | Status | Required? | External dependency | Migration action |
|---|---|---|---|---|---|---|
| FastAPI app | `cloud/api/main.py`, `routes.py`, `settings.py`, `deployment.py` | HTTP API `/api/v1/*`, health `/api/v1/health`, `/status` | IMPL | Yes | none | **MIGRATE TO VM** |
| Platform API routers | `cloud/intel/api/routes_*.py` | CRM, jobs, monitors, scraper, email, sources, admin, AI | IMPL | Yes | none | MIGRATE TO VM |
| Platform worker | `cloud/intel/tasks/worker.py`, `service.py` | Runs all 23 task kinds; leases, retries (30 s → 900 s), pause/resume/cancel, checkpoints, reaper, daily purge | IMPL | Yes | none | MIGRATE TO VM |
| PostgreSQL task queue | `cloud/intel/tasks/pgqueue.py` | `platform_tasks` as the queue, SKIP LOCKED, LISTEN/NOTIFY doorbell | IMPL, **live since 10-06** | Yes | needs a session-mode PG connection | **KEEP** |
| Redis task queue | `cloud/shared/queue.py`, `bootstrap.py:132-150` | Former queue (Upstash) | LEGACY (rollback path) | No | Upstash | REMOVE AFTER VERIFICATION, about 30 days after VM go-live |
| Inline queue | `bootstrap.py:41` | Dev/tests only | IMPL | Dev | none | KEEP |
| In-worker scheduler | `worker.py:338-385` `maintain()` every 30 s | Monitor ticks, lifecycle, outbox, automation, retention, scraper recovery, insights | IMPL | Yes | none; must run 24/7 | MIGRATE TO VM |
| Store layer | `cloud/intel/store/{spec,ddl,postgres,memory,schema_check}.py` | 82 entities, RLS-aware access, boot schema check | IMPL | Yes | none | KEEP |
| Migrations | `cloud/db/migrations` 0001–0009, 0011–0014 (13 files; there is no 0010); `cloud/db/migrate.py` | Schema | IMPL | Yes | `auth.uid()`/`authenticated` shim works on plain PG (0001:20-57) | MIGRATE TO VM |
| Auth verifier | `cloud/api/auth.py:113-171` | Verifies Supabase JWTs (JWKS ES256/RS256 or HS256) | IMPL | Yes | **Supabase Auth** | KEEP in phase 1, **REPLACE** in phase 3 |
| Dev auth | `auth.py:174-212`, `POST /auth/dev-session` | Local development | IMPL | Dev | none | KEEP |
| Tenant isolation | App `Ctx` filters + RLS (`SET LOCAL ROLE authenticated` + `request.jwt.claims`) | Workspace isolation | IMPL | Yes | none (plain PG compatible; `test_rls.py` passes on pgserver) | MIGRATE TO VM |
| Users / roles / invites | `cloud/intel/admin/service.py`, `workspace_members`, `workspace_invitations`, `teams` | owner/admin/manager/member/viewer; app-native invites | IMPL | Yes | invite email needs SMTP (none configured; links only) | MIGRATE TO VM |
| Admin "create user" (Edge Function) | branch `feature/admin-create-user` only | Admin creates sign-in accounts | **Not on prod branch**, not deployed | No | Supabase Edge Functions | INVESTIGATE (redesign without Supabase if still wanted) |
| Frontend SPA | `cloud/web` (React 19, Vite 8) → Cloudflare Pages `sanagtm` | UI, about 39 routes | IMPL | Yes | Cloudflare Pages (free), supabase-js | KEEP on Pages |
| Pages Function proxy | `cloud/web/functions/api/[[path]].js` | Proxies `/api/*` to a tunnel origin read from Upstash REST | Deployed but **unused** (returns 500) | No | Upstash | **REMOVE AFTER VERIFICATION** (or rewrite to a fixed origin) |
| File storage | `LocalFileStorage`, `CAREERCLOUD_PLATFORM_FILES_DIR`; optional S3 (`cloud/shared/s3_storage.py`) | Uploads, exports, import copies | IMPL (local) | Yes | none | MIGRATE TO VM (`/var/lib/sanagtm/files`) |

### Crawlers and sources

| Component | Current location | Purpose | Status | Required? | External dependency | Migration action |
|---|---|---|---|---|---|---|
| WAD job monitor | `cloud/intel/job_monitor/` (profile `wearedevelopers`, strategies, diff, lifecycle, importer, exports, accounts) | Daily incremental + full sweeps, lifecycle, signals, Account Builder | IMPL; **daily runs blocked since 10-02** (see the finding below) | Yes | wearedevelopers.com (free, public HTTP) | MIGRATE TO VM |
| JobSpy source | `cloud/intel/job_monitor/jobspy_source.py` + optional `requirements-jobspy.txt` | Indeed/LinkedIn/… via an unofficial library | PART, every board off unless `SANA_JOBSPY_BOARDS` is set; library not installed | No | Unofficial endpoints (policy risk) | KEEP disabled; separate venv if ever enabled |
| AI Scraper V1–V4 | `cloud/intel/scraper/` (20 modules) | Any-site job and data extraction, templates, CRM proposals | IMPL | Optional | AI optional; Playwright optional | MIGRATE TO VM (Playwright optional) |
| Original CareerCrawler | top level: `adapters/` (60 ATS adapters), `crawler/`, `store/`, `sheets/`, `main.py` | ATS crawling engine | IMPL | Yes, used by the `crawl` task via the bridge | Public ATS APIs | KEEP (library) |
| CareerCrawler weekly Sheets run | `crawler/weekly_run.py`, `sheets/`, `deploy/careercrawler.{service,timer}`, `E:\Crawlers\CareerCrawler` (queue-recovery) | Older weekly crawl into Google Sheets | Separate product | Not for SANA GTM | Google Sheets API | **INVESTIGATE** (still scheduled on this PC? out of scope for the VM) |
| Platform crawl bridge | `cloud/intel/jobs/careercrawler_bridge.py`, `crawl_task.py` | Task kind `crawl` → CareerCrawler | IMPL | Yes | none | KEEP |
| Legacy CareerCloud crawl worker | `cloud/worker/*` (`python -m cloud.worker`), tables `jobs`, `crawl_targets`, `job_events`, `job_results`, UI `/settings/crawls`, `/new` | Original crawl-job system | **LEGACY**: Redis-only; on the postgres queue its jobs sit queued forever (`main.py:155-160`); still imported by the API (`main.py:35-39`) | No | Redis | **REMOVE AFTER VERIFICATION** (move `neutralise_cell` and the crawl settings helpers first) |
| Source adapters | `cloud/intel/sources/adapters.py` | `ats_public` IMPL; `usajobs`, `adzuna` IMPL (need keys); `ziprecruiter` PART; `linkedin`, `indeed`, `dice`, `wellfound`, `builtin` STUB | mixed | Optional | Per-source keys (none configured) | KEEP |
| Discovery | `cloud/intel/discovery/` | Website/careers/ATS detection | IMPL | Optional | public web | KEEP |
| Change monitors | `cloud/intel/monitoring/` (`monitors`, `change_events`, `company_snapshots`; UI `/change-monitors`) | Company page change monitoring | IMPL; separate concept from job monitors | Optional | public web | KEEP (naming overlap only) |

### Enrichment, validation, AI and sending

| Component | Current location | Purpose | Status | Required? | External dependency | Migration action |
|---|---|---|---|---|---|---|
| Email validation | `cloud/intel/email/` (engine, jobs, providers, cache) | Format/DNS/SPF/DMARC checks; optional SMTP preflight; paid ELV for "VALID" | IMPL | Yes | DNS (free); EmailListVerify (paid, optional) | MIGRATE TO VM; the VM needs outbound DNS (and port 25 only if SMTP preflight is used) |
| AI providers | `cloud/intel/ai/` (rules, Claude, Gemini, OpenAI-compatible) | Scraper planning, research, agent, scoring | IMPL; default `rules` (no AI) | Optional | Anthropic / Google / any OpenAI-compatible | KEEP; free-only Gemini mode exists |
| Enrichment providers | `cloud/intel/providers/` (Seamless, ZoomInfo, generic partner, credit ledger) | Contact and company enrichment | IMPL with paid gates | Optional | Seamless.AI, ZoomInfo (paid) | KEEP |
| Outbound sending | `cloud/intel/gtm/`, `sending/` | Sequences over SMTP / Gmail / MS Graph / Postmark / SendGrid | IMPL, hard-gated off (`NullSender` unless production + `CAREERCLOUD_ALLOW_EMAIL_SENDING`) | No | mail providers | KEEP gated |
| Integrations | `cloud/intel/integrations/` | Slack, webhook, Google/Outlook calendar | IMPL | Optional | those services | KEEP |

### Deployment and operations

| Component | Current location | Purpose | Status | Required? | External dependency | Migration action |
|---|---|---|---|---|---|---|
| Windows supervisor | `deploy/windows/sana-gtm/*` (`sana-gtm.ps1`, `launch_hidden.py`, `healthcheck.py`, `verify-sana-gtm.ps1`, `sync-frontend.ps1`), root `*-sana-gtm*.bat` | Runs API, worker and tunnel on this PC | IMPL, **live** | Until cutover | cloudflared, wrangler | KEEP until the VM is verified, then retire |
| Frontend auto-sync | `deploy/windows/sana-gtm/sync-frontend.ps1` | Rebuild + redeploy on every new quick-tunnel URL | IMPL, live; fragile (6 deploys/24 h cap) | No, once there is a named tunnel | wrangler | **REMOVE AFTER VERIFICATION** (a stable hostname makes it unnecessary) |
| PyInstaller Windows installer | `deploy/windows/installer/*` (+ 660 MB `build/`) | One-click Windows install | Not deployed; **Redis-era** (requires `CAREERCLOUD_REDIS_URL`, publishes to Upstash); Webroot deleted its build output | No | Upstash | REMOVE AFTER VERIFICATION (replaced by the VM installer) |
| Linux staging bundle | `cloud/deploy/staging/*` (systemd, nftables, named tunnel, Redis) | Earlier VPS plan | Not used; Redis-era | No | Redis | REPLACE (salvage the systemd and nftables parts for the VM) |
| Legacy crawler Linux units | `deploy/careercrawler.{service,timer}`, `deploy/install.sh` | Weekly Sheets crawler | Not SANA GTM | No | Google | INVESTIGATE |
| CI | none (`.github` absent) | none | missing | Should exist | GitHub Actions (free for public repos) | add later |
| Backups | none (no `pg_dump` anywhere in the repo) | none | **missing** | **Yes** | none | **build** (see `SECURITY_AND_BACKUP.md`) |
| Logging | stdout → `logs/sana-gtm/*.log`, rotated only on restart | | IMPL (weak) | Yes | none | REPLACE with journald + logrotate on the VM |

## Supabase dependency summary

| Supabase feature | Used? | Evidence | Self-host action |
|---|---|---|---|
| PostgreSQL | **Yes** | `CAREERCLOUD_DATABASE_URL` → Supabase session pooler | Move to VM PostgreSQL 17 |
| Auth (GoTrue): sign-in, sign-up, password reset, sessions, JWKS | **Yes; the only hard coupling** | `web/src/auth/AuthProvider.tsx:104-158`, `api/auth.py` | Phase 1 keep; phase 3 replace (self-host GoTrue or app-native) |
| `auth.uid()`, RLS | Yes, but self-contained | the 0001 shim recreates them on plain PG | none |
| Storage | No (one-off `deploy/staging/supabase/storage_bucket.sql`, never used) | `STAGING_SAFETY_REPORT.md:184` | none |
| Realtime | No | no channels in `web/src` | none |
| Edge Functions | No (only on an unmerged branch) | `cloud/supabase/` absent on prod branch | none |
| Service-role key | No (forbidden when deployed, `deployment.py:48`) | | none |

## Duplicates and overlaps found

1. **Two task systems.** Legacy `cloud/worker` (Redis, `careercloud.jobs`) and platform `cloud/intel/tasks` (`platform_tasks`). Only the platform one runs.
2. **Three queue backends.** inline / redis / postgres. Production uses postgres.
3. **Two writers into `job_postings`.** `JobIntelService.ingest_postings` (crawl, source search, scraper → `change_events`) and `JobMonitorService.upsert_batch` (→ `job_posting_changes`). They are intentional but have different history semantics.
4. **Two "monitor" concepts.** Company change monitors (`monitors`) and job source monitors (`job_source_monitors`).
5. **Four "company → jobs" paths.** Crawl bridge, `ats_public`, scraper discovery, and the `ai_scraper` monitor strategy.
6. **Three deployment kits.** Windows supervisor (live), PyInstaller installer (Redis-era, unused), Linux staging systemd (Redis-era, unused).
7. **Eleven worktrees** under `E:\Crawlers`. Several branches are already merged (`sana-final-pass`, `user-invitations`, `email-validation-hardening`). `feature/admin-create-user` and `feature/jobspy` are unmerged. `CareerCrawler-sana-jobmonitor` has an uncommitted `worker.py` edit and an untracked test.

## Defects found during the audit (not fixed; for review)

| # | Finding | Evidence | Impact |
|---|---|---|---|
| D1 | **The daily WAD incremental has been silently skipped since 2026-10-02.** `tick()` skips a monitor that has an active run, and the full sweep paused on 10-02 is still `running`. | `job_monitor/service.py:335`; prod `job_monitor_runs` (last completed 10-02); monitor `last_run_at` 2026-10-02 | No new jobs for 7 days. Only lifecycle runs. |
| D2 | **Automatic full sweeps are ON in production** (`next_full_sweep_at` = 2026-10-12), contrary to the 10-02 decision "auto_full_sweep OFF". | `service.py:1313`; prod monitor row | Once the paused run clears, a full sweep starts automatically. That is about +260 MB against a possible 500 MB cap. |
| D3 | `stale_after_days = 1` on the production monitor, so every job turns stale after one day and each mass transition writes one history row per job (+74 MB on 10-06). | prod monitor row; `job_posting_changes` by day | History bloat; the setting may be unintended |
| D4 | `CAREERCLOUD_ENV=production` refuses `CAREERCLOUD_QUEUE=postgres`. `deployment.py:45` accepts it. | `cloud/api/settings.py:239-240` | The VM cannot run as `production` until this is fixed. Production currently runs as `development` (which also skips the TLS/registry checks). |
| D5 | The worker reads `CAREERCLOUD_PLATFORM_CONCURRENCY`, `CAREERCLOUD_WORKER_STATE_FILE` and the retention days before `--env-file` is loaded. | `worker.py:188, 477-485` | Env-file values for these are ignored |
| D6 | A DB outage during `maintain()` → `list_workspace_ids()` kills the worker instead of degrading. | `worker.py:349, 402-404` | Restart loops during DB blips |
| D7 | Uploaded files and exports are never cleaned up. | only email ingest blobs are deleted (`email/jobs.py:606,852`) | Disk growth (526 MB locally) |
| D8 | `redis` is still a hard import of the API even on the postgres queue. | `main.py:29` | Extra dependency |
| D9 | `run-sanagtm-staging.bat` does not set `CAREERCLOUD_WORKER_STATE_FILE`. | `sana-gtm.ps1:163` sets it | `/status` shows the worker offline when started by the .bat |
