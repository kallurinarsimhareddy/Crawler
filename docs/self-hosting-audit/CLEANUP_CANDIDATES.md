# Cleanup candidates

**Nothing here has been deleted.** Each item lists the evidence, which was traced with `git grep` across imports, routes, task kinds and scripts. Each item also lists the precondition before removal. Remove one item per PR, with all suites green, after separate approval.

## A. Remove after the VM cutover is verified

| # | Candidate | Evidence it is unused or superseded | Precondition |
|---|---|---|---|
| A1 | Upstash Redis (account/database) | Production on `CAREERCLOUD_QUEUE=postgres` since 2026-10-06; the API logs "CAREERCLOUD_REDIS_URL is set but has no effect" | 30 days stable on the PG queue; remove the `CAREERCLOUD_REDIS_URL`/`QUEUE_PREFIX` lines first |
| A2 | Redis queue code `cloud/shared/queue.py`, Redis branch of `cloud/intel/bootstrap.py:132-150`, `fakeredis` dev dependency, `cloud/devtools/fake_redis.py`, `test_queue.py` Redis parts | Only selected when `CAREERCLOUD_QUEUE=redis` | A1 done; first fix `settings.py:239` (production requires redis) and the `main.py:29` hard import |
| A3 | Legacy CareerCloud crawl worker `cloud/worker/` (+ `run-worker.bat`, tables `jobs`, `crawl_targets`, `job_events`, `job_results`, UI `/new`, `/settings/crawls`, `web/src/api/client.ts:112-129`) | Redis-only (`cloud/worker/__main__.py:89,99`); on the PG queue its jobs stay queued forever (`main.py:155-160`); superseded by task kind `crawl` (`intel/jobs/crawl_task.py`) | Move `cloud.worker.results.neutralise_cell` (used by `intel/exports/service.py:7`, `research/service.py:208`, `scraper/exports.py:88`) and the crawl settings helpers used by `careercrawler_bridge.py:94-110` into `cloud/shared`. Check the 3 legacy `jobs` rows in production. Keep the tables (a migration is needed to drop them; that is a separate decision). |
| A4 | Pages Function `cloud/web/functions/api/[[path]].js` | Returns 500 (unconfigured); the SPA calls the API URL directly | Named tunnel live; or rewrite it as a fixed-origin proxy if same-origin `/api` is preferred |
| A5 | `deploy/windows/sana-gtm/sync-frontend.ps1` + frontend auto-deploy bits | Only exists because quick-tunnel URLs change | Stable `api.<domain>` live |
| A6 | Windows supervisor `deploy/windows/sana-gtm/*`, root `*-sana-gtm*.bat`, `run-sanagtm-*.bat`, scheduled task "SANA GTM Auto Start" | Replaced by the VM | 30 days on the VM; keep the files in git history |
| A7 | PyInstaller installer `deploy/windows/installer/` (code) and `deploy/windows/installer/build/` (**660 MB, git-ignored, stale payload missing `job_monitor`**) | Redis-era (requires `CAREERCLOUD_REDIS_URL`, publishes to Upstash); never shipped; Webroot deleted its output | The `build/` folder can go **now** with approval (git-ignored artifact, regenerable). The code goes once the VM installer exists. |
| A8 | `cloud/deploy/staging/*` (Linux VPS systemd, nftables, Redis) and `cloud/ops/staging_env.py` (Supabase pooler URL generator) | Not used by the live setup | Salvage the systemd and nftables ideas into `deploy/vm/`, then remove |
| A9 | Supabase project `<supabase-project-ref>` | Only Auth after Option A | Option B done + 2 weeks side by side + final export; **explicit approval** |

## B. Dead code (no importers on the production branch)

| # | Candidate | Evidence |
|---|---|---|
| B1 | `cloud/intel/vendor/it_classify.py` | no importers; only mentioned in `cloud/intel/README.md:60-62` |
| B2 | `cloud/intel/vendor/zc_clean.py` | same |
| B3 | `utils/browser_pool.py`, `utils/robots.py` | imported only by their own tests (`tests/test_browser_pool.py`, `tests/test_robots.py`). **Check** whether the separate CareerCrawler Weekly product (`E:\Crawlers\CareerCrawler`) needs them before removing. |

## C. Keep, but document or rename (overlaps, not waste)

- **Two job writers.** `JobIntelService.ingest_postings` writes `change_events`; `JobMonitorService.upsert_batch` writes `job_posting_changes`. Document which history table a UI reads, or unify later.
- **"Monitors" naming.** `monitors` (company change monitors, `/change-monitors`) versus `job_source_monitors` (`/monitors`).
- **Source stubs** (`linkedin`, `indeed`, `dice`, `wellfound`, `builtin`) refuse without partner feeds. Keep them as extension points, but label them "partner access required" in the UI.
- **JobSpy** (`job_monitor/jobspy_source.py`). It is off by default and its library is not installed. Keep it disabled. Policy risk: unofficial endpoints.

## D. Workspace and disk hygiene (outside the repo; user decision)

| Item | Size | Note |
|---|---|---|
| Worktrees `CareerCrawler-sana-final`, `-sana-invites`, `-sana-emailval` | small (venv/node_modules are junctions) | Branches already merged into production; `git worktree remove` after confirming they are clean (all are clean) |
| `CareerCrawler-sana-jobmonitor` | 3.1 GB (2.4 GB `sana_wad` PG + 526 MB import copies) | **Holds the only full 395k-job WAD history.** Keep it until it is imported into the VM or dumped and verified. It also has an uncommitted `worker.py` edit and an untracked `test_task_kinds_matrix.py`. |
| `CareerCrawler-sana-users` (`feature/admin-create-user`, Supabase Edge Function design), `CareerCrawler-sana-jobspy` (11 dirty files) | — | Unmerged work; decide to merge, redesign or archive |
| `CareerCrawler-seamless` (40 dirty files), `CareerCrawler-cloud` (2 dirty files; the user's own `staging_env.py` work) | 0.5 GB | Do not touch without the owner |
| `E:\Crawlers\CareerCrawler` (branch `queue-recovery`) + scheduled task **"CareerCrawler Weekly"** (next run Sat 2026-10-10 06:00, last run 2026-10-07) | 1.6 GB | A separate product (Google Sheets weekly crawl), not part of SANA GTM. Decide separately whether it moves to the VM. |
| `deploy/windows/installer/build` | 660 MB | see A7 |
