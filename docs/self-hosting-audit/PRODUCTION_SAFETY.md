# Production safety runbook (2026-10-09)

**Status: nothing in production has been changed.** All production access in this pass was read-only:
- catalog and count queries,
- the `monitor_settings.py snapshot` command,
- an `auto-sweep off` **dry run**.

The code changes live in worktree `E:\Crawlers\CareerCrawler-sana-safety`, branch `fix/production-safety` (off `0bdcee4`). They are uncommitted and not deployed.

**Constants used below:**

| Name | Value |
|---|---|
| Workspace | `b0e4445d-1150-4605-b258-be421e776e63` |
| Monitor | `jmon_ec14fe0b3380290594595005a04f34dd` (WeAreDevelopers US Jobs) |
| Paused run | `jmr_eba7780b67927ce5fb7a063214bfc368` |
| Paused task | `tsk_7c37cad5d267cec28660e9621922a259` |
| Env file | `E:\Crawlers\CareerCrawler-platform\cloud\worker\.env.sana-cloud` (read by path; never printed) |

## 1. Current state (measured, read-only)

| Item | Value |
|---|---|
| DB size | 451.9 MB |
| Monitor | daily, enabled, `next_run_at` 2026-10-09 17:16 UTC, version 29 |
| Automatic full sweeps | **ON**: `next_full_sweep_at` = 2026-10-12 17:16:33.745417 UTC, `full_sweep_days` 7 |
| Who and when | Two Lifecycle-card saves on 2026-10-05 17:16 UTC by the owner account (audit log). The card sends `stale_after_days`, `visible_window_days`, `gone_checks_per_run` **and** `auto_full_sweep` on every save. The audit rows have empty `changes`, so the earlier values were never recorded. |
| `stale_after_days` / `visible_window_days` / `gone_checks_per_run` | 1 / 60 / 500 |
| Incremental page cap | `max_pages_incremental` 200 (= at most 4,800 jobs per run at 24 per page) |
| Last monitor run | 2026-10-02 19:10 (completed). **No run since.** |
| Paused run | `full`, `running`, page 4,820, 115,680 found; next cursor = listings of 2026-09-03 |
| Paused task | `paused`, attempts 2/5 (reason "database capacity check") |
| Lifecycle | runs daily (last 2026-10-09 09:00 UTC): `became_stale` 0, refreshes 5,645 hiring signals |
| Jobs | 115,680 `stale` + 2 `unknown` |
| History | 115,680 `new` (10-02) + 115,680 `stale` (10-06); about 0.64 KB per row with indexes |

## 2. Disabling automatic full sweeps (item 2) — **needs approval**

**The change.** `JobMonitorService.update_monitor(…, {"auto_full_sweep": false})` sets only `next_full_sweep_at = NULL`. It also bumps the version and writes an audit row. Proven by `test_disabling_auto_sweeps_touches_only_the_sweep_date`.

**Safest path:** `deploy/ops/prod-safety/monitor_settings.py`. It writes a snapshot before any write, goes through the service (audited, version-checked), fails loudly if anything other than `next_full_sweep_at` changed, and has an exact rollback.

```powershell
cd E:\Crawlers\CareerCrawler-sana-safety
$a = '--env-file','E:\Crawlers\CareerCrawler-platform\cloud\worker\.env.sana-cloud',
     '--workspace','b0e4445d-1150-4605-b258-be421e776e63','--monitor','jmon_ec14fe0b3380290594595005a04f34dd'
cloud\.venv\Scripts\python deploy\ops\prod-safety\monitor_settings.py auto-sweep off @a --snapshot E:\SanaBackups\monitor-2026-10-09-before.json           # dry run (done 2026-10-09: OK, nothing written)
cloud\.venv\Scripts\python deploy\ops\prod-safety\monitor_settings.py auto-sweep off @a --snapshot E:\SanaBackups\monitor-2026-10-09-before.json --apply   # APPROVAL
# rollback (exact previous timestamp 2026-10-12 17:16:33.745417 UTC):
cloud\.venv\Scripts\python deploy\ops\prod-safety\monitor_settings.py restore @a --snapshot E:\SanaBackups\monitor-2026-10-09-before.json --apply
```

**UI alternative** (also safe): Monitors → WeAreDevelopers US Jobs → Lifecycle → Edit → untick automatic full sweeps → Save. That save re-sends the other three values unchanged. Rollback in the UI re-ticks it, which gives "now + 7 days" rather than the exact old timestamp.

**Effect:** none until 2026-10-12. Today the paused run blocks every scheduled run anyway. Disabling matters **before** the paused run is cleared: otherwise the next due tick would start a full sweep.

## 3. Why the paused sweep blocks the daily incremental (item 3) — **cancel needs approval**

**Mechanism:**
- `tick()` advances `next_run_at`, then skips any monitor with an active run (`service.py:335`).
- `active_run` means a run in `queued` or `running`.
- Pausing stops the **task** (`platform_tasks.status = paused`) but leaves the **run** `running`.
- So every daily tick since 2026-10-03 skipped the monitor silently. `start_run` would also refuse, because only one crawler per site is allowed.

**A second bug found and reproduced on the production code** (in-memory demo):

> **Do not cancel from the current UI or API.**

- **Observed on `0bdcee4`:** after `POST /job-monitor-runs/{id}/cancel` on a paused run, the run stays `running`, the task becomes `cancelled`, and the scheduler starts nothing. Output: `run.status=running task.status=cancelled tick_started=0`.
- **Why:** the route cancels the task, but only marks the run cancelled if it was still `queued`. A paused task has no worker left to finish the run.
- **Consequence:** the monitor would be blocked **permanently**, and only a manual database fix could clear it.

**Fix on `fix/production-safety`:**
- `JobMonitorService.cancel_run` finishes a run whose task is paused, finished or missing.
- The route uses it.
- A user-cancelled sweep waits a whole `full_sweep_days` cycle before the next automatic sweep, instead of "tomorrow".
- `tick()` now posts a warning notification when a paused or stalled run blocks a scheduled run.

**Tests:** 7 new tests, including the reproduction scenario.

**Options and their consequences:**

| Option | Immediate DB effect | Afterwards | Risk |
|---|---|---|---|
| **A. Leave paused** (today) | 0 | No new jobs at all. The daily lifecycle keeps running (cheap; it refreshes 5,645 signals). Data gets older every day. | Data staleness only |
| **B. Resume the sweep** | Reads about 127k more jobs (the listing had 242,391 on 10-02), going back from 2026-09-03 | About +345 MB of postings and "new" rows, plus about +80 MB of stale rows the next day. The close phase expires everything listed before 2026-08-03 (est. 40–60k rows, +25–40 MB). Then about 500 "gone" URL checks. **The DB reaches about 900 MB.** Jobs listed after 10-02 are still missing until the next incremental. | **Exceeds a 500 MB Free cap.** Fine on Pro or the VM |
| **C. Cancel (requires the fix)**, after disabling auto sweeps | 0 (jobs kept, nothing closed; the run becomes `cancelled`; the checkpoint can no longer be resumed) | The next daily tick runs an **incremental** with floor 2026-09-29, read newest-first and capped at 200 pages (≤4,800 jobs). Expect **≤4,800 new jobs/day ≈ +13 MB/day, plus ≤3 MB/day of stale rows**. 7–8 days of listings are waiting (est. 17–38k jobs). Only the newest ≤4,800 arrive in the first run, and later runs move their floor forward, so **a gap of about 5–6 days of listings (10-02 → about 10-08) stays missing** until a manual `since` run with a higher page cap, or a full sweep. | With 48 MB of headroom under 500 MB, **about 3–4 days of dailies crosses a Free cap** |

**Recommended order:**
1. Confirm the Supabase plan.
2. Take a verified backup (§5).
3. Disable auto sweeps (§2).
4. Then choose:
   - On Free: **stay with A** until the database moves to the VM.
   - On Pro (8 GB): deploy the fix, then C, then a one-off `since=2026-10-01` run with a raised page cap to close the gap.

**Cancel commands (after the fix is deployed, or run with the fixed code; needs approval):**

```powershell
# via the fixed API (UI "Cancel run" button does the same)
#   POST /api/v1/w/<workspace>/job-monitor-runs/jmr_eba7780b67927ce5fb7a063214bfc368/cancel
# rollback: none for the run state itself (a cancelled run is final). The data is untouched; a new
# full sweep can be started later (it starts from page 1).
```

## 4. `stale_after_days = 1` (item 4): measured growth risk

**How it works** (`evaluate_lifecycle`, `service.py:854-915`):
- A job is `open` until its active-since date (the earlier of first seen and listing date) is older than `stale_after_days`. It then becomes `stale`, with **one** history row.
- Stale jobs never go back to open through the lifecycle.
- **Expiry and closure never happen in the daily lifecycle.** They happen only in a completed full sweep's close phase.
- **Correction to the audit:** there is no automatic late-November expiry wave.

**Measured:**
- The one-time `stale` transition of 2026-10-06 wrote 115,680 rows, about 74 MB including indexes. The average stale row is 360 B in the heap (`pg_column_size`) and 0.64 KB with its indexes.

**Ongoing, with daily incrementals running:**

| | Per new job | At ≤4,800 new/day (page cap) | At 2,500/day |
|---|---|---|---|
| Posting row | about 2.06 KB | ≤9.9 MB/day | 5.2 MB/day |
| "new" history row | about 0.64 KB | ≤3.1 MB/day | 1.6 MB/day |
| "stale" row the next day (because the setting is 1) | about 0.64 KB | ≤3.1 MB/day | 1.6 MB/day |
| **Total** | **about 3.3 KB** | **≤16 MB/day (≈ 480 MB/month)** | **about 8.4 MB/day** |

**What changing the setting to 30 (the code default) does and doesn't do:**
- It writes **one row** (the monitor). Raising the value never triggers a mass transition, and the 115,680 jobs already stale stay stale.
- It does **not** materially reduce storage. Without completed full sweeps nothing closes, so every job still gets its stale row eventually, 29 days later. It only saves rows for jobs that close or expire within 30 days.
- Its value is **meaning**: "stale" would mean "listed more than 30 days ago" instead of "everything". Today the dashboard shows 0 active jobs.
- **Lowering** the value later would trigger a mass transition. Don't.

**Real storage levers** (code changes; proposals, not done):
- Record the stale transition as a status and timestamp only, without a history row. This saves about 0.64 KB per job.
- Retention for `stale` history rows.
- Moving off the 500 MB cap (the VM).

**Hiring signals:** the daily lifecycle updates all 5,645 signal rows. That makes about 10 MB/day of dead tuples, which autovacuum reclaims, so the table stays bounded at about 2× (about 50 MB worst case).

**No lifecycle job was run and no setting was changed in this pass.** Changing `stale_after_days` to 30 needs approval. It is one UI save (Lifecycle card) and its rollback is saving 1 again; both are reversible with no data effect.

## 5. Verified backup procedure (item 5)

**Tool:** `deploy/ops/backup/sana_backup.py`.

**`backup`:**
1. Takes a consistent `pg_dump -Fc` of schema `careercloud` inside an exported `REPEATABLE READ, READ ONLY` snapshot.
2. Records exact per-table row counts, RLS flags and id/version checksums **from the same snapshot**.
3. Writes the SHA-256 and checks the table of contents (every table has a data entry).
4. Copies the dump and manifest to `--offsite` and re-verifies the SHA-256 there.
5. Refuses if there is no off-site folder, or if it is on the same physical disk.

The password goes through PG* environment variables, never through the command line or the output.

**`restore-test`:**
1. Verifies the SHA-256.
2. Starts a **throwaway** PostgreSQL cluster (127.0.0.1, random port).
3. Applies the migration-0001 compatibility block and pre-creates the roles named in the dump's GRANTs.
4. Runs `pg_restore --exit-on-error`.
5. Compares counts, checksums and RLS per table with the manifest.
6. Runs the app's boot schema check and an RLS stranger probe.
7. Deletes the cluster.

**Verified locally on 2026-10-09 (`selftest`, PostgreSQL 16 tools, synthetic data, no production access):**
- 91 tables / 151 rows dumped and restored identically.
- 90 RLS tables matched; `schema_migrations` 13; boot schema check 85/85; stranger probe 0 rows.
- A tampered copy was refused.
- Everything was cleaned up.

**Findings fixed during the test:**
- `--no-privileges` silently dropped the grants the app's RLS path needs. Privileges are now kept.
- `pg_ctl start` hung on Windows with captured pipes.

**Prerequisites — each needs approval:**

| # | Prerequisite | Why |
|---|---|---|
| P1 | **PostgreSQL 17 client tools.** Only pg_dump 16.2 exists here (pgserver), and it **refuses a 17.6 server**. Proposed: the official EDB "PostgreSQL 17.x Windows x64 binaries" zip, unpacked to `E:\Tools\pgsql17` (no admin, no service). | Third-party download onto this PC. Webroot has deleted binaries before. |
| P2 | **An off-machine destination.** `C:` and `E:` are on the **same NVMe** (disk 0), and no USB disk is attached. Options: a USB disk (preferred), another PC's share, or a cloud folder. | The user must choose |
| P3 | **Encryption for the off-site copy.** The dump contains workspace data (e.g. email validation addresses). Windows 11 Home cannot create BitLocker To Go volumes, and 7-Zip is not installed. Options: install 7-Zip and create an AES-256 `.7z` (password typed by you), or `age`. | Software install + passphrase custody |
| P4 | **Running the backup against production.** Read-only. It reads about 452 MB through the pooler, produces an estimated 25–40 MB dump, and takes a few minutes. If Supavisor refuses snapshot import, re-run with `--no-snapshot` (counts are then taken just before the dump, and small drift is possible if the lifecycle runs at that moment; avoid 09:00 and 18:55 UTC). | Production read |

**Commands:**

```powershell
cd E:\Crawlers\CareerCrawler-sana-safety
$env:PYTHONPATH = (Get-Location).Path
# 0. self-test the tool locally (done 2026-10-09 with PG16 tools; repeat with PG17 tools once P1 is approved)
cloud\.venv\Scripts\python deploy\ops\backup\sana_backup.py selftest --pg-bin E:\Tools\pgsql17\bin
# 1. backup (APPROVAL P1–P4)
cloud\.venv\Scripts\python deploy\ops\backup\sana_backup.py backup --env-file E:\Crawlers\CareerCrawler-platform\cloud\worker\.env.sana-cloud `
    --pg-bin E:\Tools\pgsql17\bin --out E:\SanaBackups --offsite F:\SanaBackups
# 2. encrypt the off-site copy (example with 7-Zip; prompts for the password), then delete the plaintext there
& "C:\Program Files\7-Zip\7z.exe" a -t7z -mhe=on -p F:\SanaBackups\<name>.7z F:\SanaBackups\<name>.dump F:\SanaBackups\<name>.manifest.json
# 3. restore test in an isolated throwaway cluster (local, 127.0.0.1, deleted afterwards)
cloud\.venv\Scripts\python deploy\ops\backup\sana_backup.py restore-test --dump E:\SanaBackups\<name>.dump --pg-bin E:\Tools\pgsql17\bin
```

**A backup counts only after step 3 prints `RESTORE TEST PASSED`.**

**Supabase Auth users** (the `auth` schema) are deliberately **not** in this dump. They are not needed for Option A. Export them separately and encrypted only when Option B is scheduled.

## 6. PostgreSQL queue refused in production mode (item 6)

**Fix on `fix/production-safety`:**
- `cloud/api/settings.py:239` now accepts `CAREERCLOUD_QUEUE=redis` **or** `postgres` in production, and still refuses `inline`.
- This matches `deployment.py:45`.

**Tests:**
- `test_production_accepts_the_postgres_queue_without_redis`
- `test_production_refuses_the_inline_queue`
- The existing Redis-production test still passes.

**This is necessary but not sufficient.** For `staging` and `production`, boot also enforces `deployment_problems()` (`deployment.py:34-70`):
- S3 file storage (`CAREERCLOUD_STORAGE_BACKEND=s3`)
- `sslmode=require` on the DB URL
- an https `CAREERCLOUD_API_ORIGIN`
- `CAREERCLOUD_RESULTS_NAMESPACE`
- the isolation registry (`enforce_isolation`)

**Proposal (not built; needs approval):** a "self-hosted" deployment profile. It would allow local file storage on a dedicated data disk, and loopback or compose-internal DB hosts without TLS, while keeping every other production check.

**Live impact of the fix: none.** Production runs with `CAREERCLOUD_ENV=development`, and the fix is not deployed.

## 7. What needs approval vs what is safe

| Action | Production effect | Approval |
|---|---|---|
| Read-only inspection, snapshot, dry runs | none | done |
| Local tests, selftest on throwaway clusters, code on `fix/production-safety` | none | done |
| Commit `fix/production-safety` (local) | none | **yes** (you asked for no commits without review) |
| Download PG17 binaries (P1) / choose the off-site disk (P2) / encryption tool (P3) | none | **yes** |
| Run the production backup (P4) | read-only load | **yes** |
| Disable automatic full sweeps (§2) | 1 row + 1 audit row | **yes** |
| Change `stale_after_days` 1 → 30 (§4) | 1 row + 1 audit row | **yes** |
| Deploy the cancel/tick/settings fixes (merge, restart API and worker; no migration) | code change, about 10 s restart | **yes** |
| Cancel the paused run (§3 option C), only after the fix is deployed | run row + task row | **yes** |
| Resume the sweep (§3 option B) | about +450 MB | **yes**; not recommended on a 500 MB plan |
| Run lifecycle or bulk jobs, migrations, deletion | — | not proposed |

## 8. Test results on `fix/production-safety`

| Suite | Result |
|---|---|
| Platform | `cloud\.venv\Scripts\python -m unittest discover -s cloud\tests -t .` → **1,913 tests OK** (18 skipped) |
| New tests | 2 settings, 7 job monitor (pause/cancel/schedule/HTTP), 4 backup-tool refusals, 3 monitor-settings tool |
| Backup selftest | PASSED (§5) |

During development, an extra "postgres queue needs postgres storage" check broke two existing tests that use that combination on purpose. It was removed.
