# SANA GTM self-hosting audit: executive summary

**Date:** 2026-10-09.

**Code audited:** branch `feature/careercrawler-platform` @ `0bdcee4`, which is what production runs.

**Type:** audit and plan only. No migration, deletion, deployment or architecture change was made. The only things written are the files in this folder.

**Production access:** the production database was queried **read-only**, using catalog statistics and counts only (scripts in `scripts/`).

## What SANA GTM is today

- **Frontend.** React SPA on Cloudflare Pages (`sanagtm.pages.dev`). Sign-in is Supabase Auth in the browser.
- **API and worker.** Python FastAPI API plus **one** platform worker. They run **on this Windows PC** under a PowerShell supervisor (scheduled task "SANA GTM Auto Start") and are exposed through a **cloudflared quick tunnel** whose URL changes on every restart. The frontend is rebuilt automatically each time.
- **Database.** Hosted **Supabase PostgreSQL 17.6**, schema `careercloud`: 91 tables, **451.9 MB**.
- **Queue.** The task queue has run on **PostgreSQL since 2026-10-06**. Upstash Redis is no longer used at runtime.
- **Features in the code.** WeAreDevelopers job monitor with lifecycle and hiring signals, Account Builder, CRM, email validation (local DNS plus optional paid EmailListVerify), AI scraper, 60-adapter ATS crawler, enrichment (Seamless, ZoomInfo), AI (rules / Gemini free / Claude), invitations, roles, audit log, CSV/XLSX import and export.
- **Not running.** No VM exists yet, and VMware, Docker and WSL are not installed. The Windows account is a standard user, so installing VMware needs an administrator once.

## Key numbers

| | Value |
|---|---|
| Production DB | 451.9 MB: 115,682 jobs (238 MB) + 231,360 history rows (147 MB) = 85%; CRM companies 0, contacts 0 |
| Measured cost per job | 3.9 KB in production; 5.1 KB in the local DB that has a fuller history |
| Growth (WAD daily) | 2.2–3k new jobs a day, giving about 11–15 MB/day, 330–450 MB/month, 4–5.5 GB/year |
| Local full WAD history | about 395k jobs in `sana_wad` (2.0 GB DB). **Not in production.** |
| Backup ratio | `pg_dump -Fc` is about 5% of the live DB size (98.9 MB for the 392k-job DB) |
| Recommended VM | 4 vCPU, 8 GB RAM, 30 GB OS disk + 80 GB data disk (thin), on `E:` |
| Recurring paid infrastructure after migration | **$0** (Option A or B). Electricity: about +₹30/month on a PC that is already on 24/7 |

## Urgent findings (production, before any migration)

1. **The daily WAD incremental has not run since 2026-10-02.** The paused full sweep still counts as an active run, and `tick()` silently skips monitors with an active run (`cloud/intel/job_monitor/service.py:335`).
2. **Automatic full sweeps are switched on** (`next_full_sweep_at` = 2026-10-12). They were meant to be off.
3. **The DB is at 452 MB.** If the Supabase plan is Free (500 MB cap; the plan could not be checked from here), any of these crosses the cap: resuming the sweep, an automatic sweep, about 3 weeks of dailies, or a completed full sweep. (Correction: there is no automatic expiry wave; see `PRODUCTION_SAFETY.md`.)
4. **No production backups exist** beyond whatever the Supabase plan provides.

## Recommendation

**Option A first.** Move PostgreSQL, the API, the worker and files to an Ubuntu 24.04 VM, run with Docker Compose, and use a **named** Cloudflare Tunnel for a stable `api.<domain>`. Keep **only Supabase Auth** (Free).

- Almost no code changes.
- Removes the DB-size risk, the tunnel churn and Upstash.
- Rollback is simple.

**Then Option B1.** Self-host GoTrue on the VM, importing users with their existing UUIDs and password hashes, to drop Supabase entirely. This is a separate, tested project.

## Documents

| File | Content |
|---|---|
| [ARCHITECTURE_INVENTORY.md](ARCHITECTURE_INVENTORY.md) | Every component, its status and its action; Supabase coupling; duplicates; defects D1–D9 |
| [STORAGE_AND_GROWTH.md](STORAGE_AND_GROWTH.md) | Measured sizes by category, growth model, three capacity scenarios |
| [COST_ANALYSIS.md](COST_ANALYSIS.md) | Today vs Option A/B, electricity, zero-paid configuration, unknowns |
| [SELF_HOSTED_ARCHITECTURE.md](SELF_HOSTED_ARCHITECTURE.md) | Target design, Docker vs native, VM sizing, Option A vs B |
| [MIGRATION_PLAN.md](MIGRATION_PLAN.md) | Stages 0–8, verification checks, cutover, rollback |
| [INSTALLATION_DESIGN.md](INSTALLATION_DESIGN.md) | Guided installer and the `sanagtm` install/start/stop/update/backup/restore/health/logs/capacity/uninstall CLI |
| [SECURITY_AND_BACKUP.md](SECURITY_AND_BACKUP.md) | Gaps, VM security, backup and restore-test policy, DR scenarios, env var names |
| [WINDOWS_PACKAGE.md](WINDOWS_PACKAGE.md) | **Windows installer ZIP: architecture, test results, readiness (2026-10-12)** |
| [SELF_HOSTED_PACKAGE.md](SELF_HOSTED_PACKAGE.md) | **The built package: tests, VM spec, prerequisites, migration + rollback checklists (2026-10-09)** |
| [PRODUCTION_SAFETY.md](PRODUCTION_SAFETY.md) | Production safety runbook (auto sweep, paused run, backups) |
| [CLEANUP_CANDIDATES.md](CLEANUP_CANDIDATES.md) | Removal candidates, each with evidence and a precondition |
| `scripts/` | Read-only measurement scripts used for this report |

## Tests run during the audit

All suites ran offline on this PC against `0bdcee4`. The platform suite used an embedded throwaway PostgreSQL (pgserver), never production.

| Suite | Command | Result |
|---|---|---|
| Platform (cloud) | `cloud\.venv\Scripts\python -m unittest discover -s cloud\tests -t .` | **1,884 tests OK**, 18 skipped (Redis/S3/Playwright/live-only), 450 s |
| CareerCrawler | `cloud\.venv\Scripts\python -m unittest discover -s tests` | 1,696 tests, **1 error**: `test_runlock ... releases_on_a_clean_exit` hit a Windows "file in use" error deleting a temp `crawler.lock` during teardown. **Flaky:** `tests.test_runlock` passed 3 of 3 reruns (27 tests OK). Likely a Windows/antivirus file handle. |
| Web | `npm test` in `cloud/web` | **117 OK**, 0 failed |

Not run: the installer suite, the live-network harnesses, and the Redis integration tests (no Redis URL). No production writes were made.

## What could not be verified from here

- The Supabase plan, its backup policy and project usage (no dashboard or management access).
- Paid provider prices: EmailListVerify top-ups, Seamless, ZoomInfo.
- Which domain or Cloudflare zone can host `api.<domain>`.
- The real power draw and the tariff category.
- The VM itself. It does not exist yet, so the installer, Docker and PostgreSQL tuning are untested designs.
- Exact table-level sizes inside the local `sana_wad` DB. It was not started, to avoid crash-recovery writes; only on-disk sizes were measured.

## Missing tests for self-hosting

- An end-to-end install test on a clean Ubuntu VM (compose up, migrate, health).
- A backup → restore → `verify_copy` round trip.
- Worker restart with an in-flight task on a local PostgreSQL connection, plus the LISTEN/NOTIFY reconnect after a PostgreSQL restart.
- The `CAREERCLOUD_ENV=production` + `QUEUE=postgres` configuration (currently refused, D4).
- A scheduler test where a paused run blocks the daily tick (D1). It should surface a warning, not skip silently.
- Auth against a self-hosted GoTrue (Option B).
