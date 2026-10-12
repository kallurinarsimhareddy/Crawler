# Migration plan (staged, reversible)

**Ground rules for every stage:**

- Nothing is deleted. That covers the Supabase project, Upstash, the Redis code, the migrations, the Windows supervisor, the quick tunnel, the `sana_wad` local DB and the dumps.
- No full crawler sweep, bulk import or bulk lifecycle operation runs until stage 6 sign-off.
- Each stage has its own approval gate. "Done" means verified, not just executed.

## Stage 0: Stabilise production now (before any migration; each item needs approval)

| # | Action | Why | Risk |
|---|---|---|---|
| 0.1 | Check the Supabase plan and DB size in the dashboard | DB is 451.9 MB; if on Free (500 MB cap), it is at 90% | none |
| 0.2 | Turn **off** automatic full sweeps on the WAD monitor (PATCH monitor `auto_full_sweep=false`, which sets `next_full_sweep_at` NULL) | It is currently on (2026-10-12); a sweep would add about 260 MB | one row update |
| 0.3 | Decide what to do with the paused full sweep run `jmr_eba7780b…`: **cancel** (keeps 115k rows, frees the monitor) or leave paused until the VM exists | It blocks the daily incremental (D1); nothing new has been added since 10-02 | Cancel is irreversible for that run's checkpoint. The data stays. |
| 0.4 | Review `stale_after_days = 1` (D3). It adds one stale history row per new job the day after it is added (see `PRODUCTION_SAFETY.md` §4) | Semantics + about 0.6 KB per new job | settings only |
| 0.5 | Do **not** start the daily incremental again on Supabase if on Free, until 0.1 is answered | about 15 MB/day | |

## Stage 1: Build the VM (no production impact)

1. **An administrator installs VMware Workstation Pro** on the host. This needs admin once; the user account is standard.
2. Create the VM on `E:\VMs\sanagtm`: Ubuntu Server 24.04 LTS, 4 vCPU, 8 GB RAM, disk 1 30 GB, disk 2 80 GB (thin), NAT network.
3. Run the installer (`INSTALLATION_DESIGN.md`) in **staging mode**:
   - empty database, migrations applied by `python -m cloud.db.migrate apply`
   - dev or Supabase auth
   - **no tunnel yet**
4. Run the test suites inside the VM (cloud, crawler and web) against the VM's PostgreSQL, using `CAREERCLOUD_TEST_DATABASE_URL`.

**Gate:** the suites pass, `sanagtm status` is all green, and the boot schema check passes. That check refuses to boot if a migration is pending, a checksum differs, a table is missing, or RLS is off.

## Stage 2: Rehearsal copy of production data (read-only on Supabase)

1. Take a verified backup from Supabase. Use the **session** connection (not the transaction pooler), so the dump is consistent:

   ```bash
   pg_dump --format=custom --no-owner --no-privileges --schema=careercloud \
           --file=sanagtm_supabase_$(date +%F).dump "$SUPABASE_SESSION_URL"
   pg_restore --list sanagtm_supabase_*.dump | wc -l      # sanity check
   ```

   - Expect about 25–40 MB of dump for 452 MB of live data.
   - **Do not** dump the `auth`, `storage` or `realtime` schemas into the app database.
   - Separately, export `auth.users` (id, email, encrypted_password, email_confirmed_at). Keep it **encrypted** for stage 7 (Option B). It is not needed for Option A.
2. Keep two copies of the dump: one on the VM data disk and one on the Windows host on `E:`.
3. Restore into the VM:
   - Create the roles first: the app owner role, plus `authenticated` via migration 0001. Alternatively, apply migrations 0001–0014 to an empty DB and then restore with `--data-only --disable-triggers`.
   - Then `pg_restore --no-owner --role=<app_owner>`.
   - The **preferred method is the schema-from-migrations route**. It guarantees `schema_migrations` checksums match the files on disk, which the boot check requires.

## Stage 3: Verify the copy

The script is `scripts/verify_copy.py` (to be written in implementation; the checks are listed here):

| Check | How | Pass |
|---|---|---|
| Row counts | `count(*)` for all 91 `careercloud` tables, source vs copy | identical |
| Key relationships | e.g. `job_postings.source_monitor_id` → monitors; `job_posting_changes.job_posting_id` → postings; `workspace_members` → workspaces; `hiring_signals.job_posting_ids` all exist | 0 orphans |
| Content checksum | `md5(string_agg(id::text \|\| version, ',' order by id))` per table | identical |
| Sequences/defaults | `gen_random_uuid()` defaults present; `schema_migrations` = 13 rows with matching checksums | |
| RLS | `relrowsecurity` on for every table; stranger probe (`SET ROLE authenticated` + foreign `sub`) returns 0 rows; insert refused | same as the 2026-10-02 production probe |
| Secrets | Provider connections decrypt with the copied `CAREERCLOUD_PLATFORM_SECRETS_KEY`, via the "Test connection" endpoint for **free** checks only (ELV `/api/credits`, Seamless poll) | verified |
| Auth | Sign in with Supabase in a browser against a Pages **preview** build pointing at the VM API; `/me` and workspace pages load | |
| Worker | One small task end to end, e.g. an email validation of 5 synthetic addresses with paid checks off; pause/resume/cancel; kill the worker mid-task → reaped and retried | |
| Scheduler | The monitor `tick` fires on the VM **with all monitors disabled** except a test monitor on a tiny source | |

## Stage 4: Cutover (short write freeze, about 15–30 minutes)

1. Announce the freeze. Stop the Windows worker (`stop-sana-gtm.bat`; the watchdog must be disabled for the duration, see the known gap). Leave the API serving read-only traffic, or stop it.
2. Run a final `pg_dump` from Supabase, restore into the VM, and repeat the stage 3 count and checksum checks. The data is small, so a full re-copy beats incremental sync.
3. Start the API and worker on the VM. Bring up the named tunnel `api.<domain>`.
4. Rebuild the frontend once with `VITE_API_URL=https://api.<domain>` and deploy to Pages as a **preview** branch. Smoke-test it, then promote it to `main`.
5. Disable the Windows "SANA GTM Auto Start" task. Keep its files.
6. Watch for 48 hours: `sanagtm status`, logs, and the daily lifecycle task. Re-enable the WAD daily incremental, after 0.3 is resolved, and confirm one run completes.

## Rollback (any time within 30 days)

- **Before stage 4 completes:** do nothing. Supabase is still the system of record.
- **After cutover:**
  1. Stop the VM worker.
  2. `pg_dump` the VM database (any new rows), then restore the delta into Supabase if it fits under the cap; otherwise accept the loss of rows created since cutover.
  3. Re-enable the Windows scheduled task, whose env files are unchanged.
  4. Redeploy the previous Pages deployment from the Cloudflare dashboard (instant).
- The Redis rollback env files (`.env.sana-cloud.redis-rollback-20261006`) remain, but Upstash's free quota is exhausted, so the PostgreSQL queue is the only practical queue on either side.

## Stage 5: Hardening (after 2 stable weeks)

- Fix D4, then switch to `CAREERCLOUD_ENV=production`, with TLS or a loopback exemption for the DB URL check.
- Fix D5, D6 and D7. Remove the `redis` hard import (D8).
- Enable the backup timers and the weekly restore test. Copy backups off the VM (`SECURITY_AND_BACKUP.md`).

## Stage 6: Data growth decisions (separate approval)

- Optionally import the local 395k-job `sana_wad` history. This takes about 1.6 GB more on the VM (fine there). Use `job_import` from the 80 MB CSV or a table-level copy. **Not** to Supabase.
- Re-enable automatic weekly full sweeps on the VM once backups are proven.

## Stage 7: Remove Supabase (Option B, separate project)

1. Stand up GoTrue on the VM (B1), with its own `auth` schema. **Order matters:** migration 0001 creates `auth.uid()` only if it is missing.
2. Import `auth.users` with the **same UUIDs and bcrypt hashes**, so no password resets are needed.
3. Point `CAREERCLOUD_SUPABASE_URL` and `VITE_SUPABASE_URL` at `https://auth.<domain>` or a path proxy that exposes `/auth/v1`. Relax `build-staging.mjs:20-23` and the CSP. Update the installer validators.
4. Configure SMTP for confirmation and reset emails.
5. Test sign-in, sign-up, reset and invite acceptance.
6. Run both side by side for 2 weeks.
7. Only then, with explicit approval, pause the Supabase project. Delete it later, after a final export.

## Stage 8: Cleanup (separate approval)

Work through `CLEANUP_CANDIDATES.md`, one PR per item, with the test suites green.
