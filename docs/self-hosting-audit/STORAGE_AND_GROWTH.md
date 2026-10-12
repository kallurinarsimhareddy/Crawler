# Storage and growth

Measured 2026-10-09. All database numbers come from read-only catalog queries (`pg_total_relation_size`, `pg_stat_user_tables`, `pg_stats`) run inside `SET TRANSACTION READ ONLY`. No row contents were read and nothing was written. The scripts are in `scripts/`; see "How to reproduce" at the end.

**M** = measured, **E** = estimated (the method is given next to each estimate).

## 1. What exists and where

| Dataset | Location | Size | Rows | Kind |
|---|---|---|---|---|
| **Production database** | Hosted Supabase, PostgreSQL 17.6, schema `careercloud` (91 tables) | **451.9 MB** (`pg_database_size`) | 115,682 job postings | M |
| Local WeAreDevelopers (WAD) research DB `sana_wad` | `E:\Crawlers\CareerCrawler-sana-jobmonitor\cloud\.localdev\postgres` (embedded PG; not running) | 2.0 GB database directory (`base/22793`) + 401 MB `pg_wal`; 2.4 GB data dir in total | about 395,169 job postings (from the 2026-10-02 run log) | M (disk) / E (rows) |
| Local job-import file copies | `...sana-jobmonitor\cloud\.localdev\platform-files\job-imports` | 526 MB | none (uploaded CSVs kept forever) | M |
| WAD scraper baseline CSV | `E:\Projects\wearedevelopers_job_scraper\output\jobs_full_2026-09-04.csv` | 80.1 MB | 321,970 jobs | M |
| Logical backup of `sana_wad` | `E:\Crawlers\sana-backups\sana_wad_pre_0013_2026-10-02.dump` (pg_dump, custom format) | 98.9 MB | about 392k jobs | M |
| Production platform files | `cloud/.localdev` in the platform worktree (`CAREERCLOUD_PLATFORM_FILES_DIR`) | 164 MB (76 MB idle embedded PG + 81 MB files) | none | M |

**The large WAD dataset is not in production.** Production holds only 115,682 postings: the 2,856-job first incremental run plus the paused full sweep, which stopped at page 4,820 with 112,344 new jobs. The full 395k-job history exists only in the local `sana_wad` database and its dump.

## 2. Production database by category (measured)

| # | Category | Tables | Rows | Total size | Table + TOAST | Indexes |
|---|---|---|---|---|---|---|
| 1 | Jobs and descriptions | `job_postings` | 115,682 (1,810 dead) | **238.0 MB** | 147.3 MB | 90.7 MB |
| 2 | Job lifecycle / change history | `job_posting_changes` | 231,360 | **147.4 MB** | 80.2 MB | 67.2 MB |
| 5 | Hiring signals and evidence | `hiring_signals` | 5,645 | 26.2 MB | 23.4 MB | 2.9 MB |
| 6 | Account Builder / company review | `job_company_reviews` | 22,674 | 11.9 MB | 5.0 MB | 6.9 MB |
| | Email validation | `email_validation_items`, `email_validations`, `email_check_cache`, `_jobs` | 7,874 | 7.6 MB | | |
| 3 | Companies / CRM | `companies`, `contacts`, `opportunities`, `crm_tasks`, `lists`, … | **0 companies, 0 contacts** | about 0.1 MB each (index pages only) | | |
| 4 | Contacts / prospects | `contacts`, `contact_snapshots` | 0 | under 0.2 MB | | |
| 7 | Task queue + history | `platform_tasks` | 32 | 0.2 MB | | |
| 8 | Research, agent, logs, audit | `agent_*`, `scrape_*`, `audit_log` (218 rows) | under 100 | about 1.5 MB | | |
| 10 | Crawler checkpoints / source metadata | `job_source_monitors`, `job_monitor_runs`, `job_keyword_sets` | 5 | under 0.5 MB | | |
| | Supabase Auth (`auth.*`), Storage, Realtime | | 6 users | 2.0 MB | | |
| | PostgreSQL catalog (`pg_catalog`) | | | 13.4 MB | | |
| **Total** | | | | **451.9 MB** | | |

Jobs plus their history are **85%** of the database. Everything else is noise at today's scale.

**Per-row cost in production (M):**

| Row type | Per row | Breakdown |
|---|---|---|
| Job posting | about 2.06 KB | 1.27 KB heap/TOAST + 0.78 KB across 10+ indexes |
| History row | about 0.64 KB | |

The widest posting columns (`pg_stats.avg_width`) are `relevance_reason` 94 B, `job_url` 83 B, `skills` 66 B and `content_hash` 65 B. WAD listing cards carry no long description, so a source that does store full descriptions (the `description` column allows up to 50,000 characters) will cost several KB more per job.

**All-in production cost (M):** 451.9 MB / 115,682 jobs = **about 3.9 KB per job**, including 2 history rows per job.

**All-in local `sana_wad` cost (M/E):** 2.0 GB / about 395k jobs = **about 5.1 KB per job**. This is the realistic figure for a database that has run full sweeps, reconciliation and lifecycle passes: more history rows, about 12k signal clusters, and dead-tuple bloat.

### Where the history rows came from (M)

`job_posting_changes` by `created_at`:

| Date | Rows | What happened |
|---|---|---|
| 2026-10-02 | 115,680 | "new" rows from the first run and the sweep |
| 2026-10-06 | 115,680 | the `job_lifecycle` pass that marked every posting `stale` |

The production monitor has `stale_after_days = 1`. As a result, every job flips to `stale` one day after it is seen, and **each whole-table status transition writes one history row per job** (about 74 MB at 115k jobs).

**Correction (2026-10-09, after reading `evaluate_lifecycle`):** the daily lifecycle only moves `open` to `stale`, once per job. Expiry and closure happen only in `close_missing`, after a **completed full sweep**. There is no automatic late-November expiry wave. A completed full sweep would expire everything listed more than 60 days before the sweep started, writing one history row per expired job. See `PRODUCTION_SAFETY.md` §4.

## 3. Growth model

**Inputs.** These are measured from the source, not guessed:

- WAD shows about 242k US jobs at any time (2026-10-02 full sweep: 242,391 distinct).
- The listing window is about 90 days.
- A 30-day full sweep found 65,364 new jobs (Sep 4 to Oct 2), and one daily incremental found 2,856.
- So the arrival rate is **2,200 to 3,000 new jobs per day**.

**Per new job over its life (E):**

- 1 posting row: about 2.1 KB
- about 3 history rows (new, then stale, then expired or closed): about 1.9 KB
- signals and reviews: about 0.5 KB
- dead-tuple bloat between autovacuums: 20%

That gives **about 5 KB per job**, which matches the measured 5.1 KB of the local DB.

| Horizon | New jobs (WAD only) | Live DB growth |
|---|---|---|
| Per day | 2,200–3,000 | **11–15 MB** |
| Per month | 66k–90k | **330–450 MB** |
| Per year | 0.8M–1.1M | **4–5.5 GB** |

Confidence: medium. The arrival rate is measured. The 3 history rows per job assumes lifecycle settings stay as they are; `stale_after_days = 1` makes it at least 3. Jobs are never deleted (`job_monitor/service.py:16`), and there is no retention on `job_posting_changes`, `audit_log` or `ai_usage`.

**Other categories (E, low confidence because there is no production usage yet):**

| Category | Size | Growth |
|---|---|---|
| CRM, companies, contacts | about 1–3 KB per company or contact | 100k contacts ≈ 0.3 GB |
| Email validation | about 0.8 KB per item | 1M validated rows ≈ 0.8 GB, kept forever |
| Task queue | negligible | auto-purged after 90 days (`CAREERCLOUD_TASK_RETENTION_DAYS`) |
| Uploaded CSV/XLSX copies | 1:1 with upload size | kept forever; the local DB already has 526 MB of them |

## 4. Non-database disk

| Item | Measured |
|---|---|
| Python venv (`cloud/.venv`, includes pandas and Playwright libraries) | 461 MB |
| Playwright Chromium browser (only if the browser scraper fallback is used) | about 0.5 GB (E) |
| Frontend `node_modules` (only needed to build; the built `dist` is 1.2 MB) | 97 MB |
| App source plus original crawler | about 0.3 GB |
| Logs (`logs/sana-gtm`): rotated only on restart, so they grow while a process runs | 0.4 MB now |
| Stale PyInstaller payload `deploy/windows/installer/build` (git-ignored) | 660 MB |
| PostgreSQL WAL (default `max_wal_size` 1 GB; observed 401 MB locally) | 0.5–1 GB |
| PostgreSQL temp files during big sorts or index builds | up to 1× the largest table, briefly |

## 5. Backups (measured ratio)

A custom-format `pg_dump` of the 392k-job database was **98.9 MB**. That is about **5% of live size**, because indexes are not dumped and the data compresses well. For planning, take a nightly dump at about 5–8% of live DB size:

- 14 dailies + 8 weeklies + 6 monthlies ≈ **28 dumps**.
- At the "full current" size that is about 28 × 0.15 GB ≈ **4 GB**.

A restore test needs about **1.3× the live DB** of free space while it runs.

## 6. Capacity scenarios

"Live DB" is the PostgreSQL data directory. "Disk" is the whole VM disk with headroom: OS, app, WAL, temporary space for VACUUM FULL / REINDEX / restore test (about 1× the largest table), backups, and 30% free.

| Scenario | What it holds | Live DB | Backups | VM disk to provision |
|---|---|---|---|---|
| **A. Minimum viable** | Current production data (115k jobs), daily WAD incremental, auto full sweep off, CRM/email at current scale, no Playwright | 0.5 GB now, 1 GB after 3 months | about 1 GB | **40 GB** thin-provisioned (OS 8–10 GB, app 2 GB, DB 1–2 GB, rest headroom) |
| **B. Full current functionality** | Import the local 395k-job WAD history, weekly full sweeps, lifecycle, signals, Account Builder, email validation, Playwright installed, one year of growth | 2 GB now, 7–8 GB after a year | 4–6 GB | **80–100 GB** |
| **C. Growth: millions of jobs** | 5M jobs with history (multiple sources or several years) | 5M × 5 KB = 25 GB; 10M = 50 GB. With full job descriptions add about 3–5 KB per job, giving 40–100 GB | 3–8 GB of dumps, or WAL archiving | **200–300 GB**; put backups on a second virtual disk or an external drive |

**Host reality (M).** This PC has one 512 GB NVMe. `C:` has **6 GB free** (it has filled up repeatedly) and `E:` has 177 GB free. The VM must live on `E:`. Scenario C does not fit alongside the current `E:` contents and on-host backups; it needs another disk. Backups on the same physical NVMe protect against mistakes, not against disk failure. See `SECURITY_AND_BACKUP.md`.

## 7. Near-term risk on hosted Supabase (M + E)

The production DB is **451.9 MB**. The Supabase plan could not be verified (there is no dashboard access from this machine). If it is the Free plan (500 MB database cap; over the cap the project goes read-only), it is at **90%**. Any one of the following crosses the cap:

- **Resuming the paused full sweep:** about 127k more jobs, roughly +260 MB.
- **Automatic full sweeps:** `next_full_sweep_at` is set to 2026-10-12, which means the setting is on (`service.py:1313`).
- **A completed full sweep's close phase:** one expiry or closure history row per affected job. The lifecycle itself only adds one stale row per new job.
- **About 3 weeks of daily incrementals:** about 15 MB/day.

Do not resume the sweep. Check the plan before anything else.

## 8. How to reproduce

```powershell
# Read-only DB size statistics (prints no secrets; reads the URL from the env file)
cloud\.venv\Scripts\python docs\self-hosting-audit\scripts\db_stats.py cloud\worker\.env.sana-cloud prod_stats.json
cloud\.venv\Scripts\python docs\self-hosting-audit\scripts\db_widths.py cloud\worker\.env.sana-cloud job_postings job_posting_changes hiring_signals
cloud\.venv\Scripts\python docs\self-hosting-audit\scripts\db_runs.py cloud\worker\.env.sana-cloud   # task/run timeline + changes per day
cloud\.venv\Scripts\python docs\self-hosting-audit\scripts\db_mon.py  cloud\worker\.env.sana-cloud   # monitor schedule state
```

```bash
# Disk (Git Bash)
du -sh /e/Crawlers/CareerCrawler-platform/cloud/.venv /e/Crawlers/CareerCrawler-sana-jobmonitor/cloud/.localdev/* \
       /e/Crawlers/sana-backups /e/Projects/wearedevelopers_job_scraper
du -sh /e/Crawlers/CareerCrawler-sana-jobmonitor/cloud/.localdev/postgres/base/* \
       /e/Crawlers/CareerCrawler-sana-jobmonitor/cloud/.localdev/postgres/pg_wal
```

On the VM the same numbers come from `sanagtm capacity` (see `INSTALLATION_DESIGN.md`).
