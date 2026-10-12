# Self-hosted SANA GTM: package status, tests and checklists (2026-10-09)

**Where:**
- worktree `E:\Crawlers\CareerCrawler-sana-safety`
- branch `fix/production-safety` (off production `0bdcee4`)
- **uncommitted, not pushed, not deployed**

**Production:** unchanged. The only production access was read-only:
- catalog and count queries
- the monitor snapshot and dry run
- the stamp and grant check

## 1. What was built

### Package files

| Path | What |
|---|---|
| `deploy/selfhost/compose.yaml` | postgres:17, one-shot `migrate`, `api`, `worker`, `web` (nginx: dashboard + `/api` proxy), `cloudflared` (profile `tunnel`). No published ports, private `backend` network, health checks, `unless-stopped`, rotated logs, dropped capabilities |
| `deploy/selfhost/Dockerfile` | App image: Python 3.12 slim, pinned requirements, PostgreSQL 17 client tools (PGDG), non-root uid 10001, optional Playwright (`INSTALL_BROWSER=1`). Leaves out `sheets/` and `store/` |
| `deploy/selfhost/web/*` | Dashboard image: Node 22 build → nginx-unprivileged, same-origin API, CSP/HSTS headers, streamed uploads and exports |
| `deploy/selfhost/postgres/initdb/10-sanagtm.sh` | App role `sanagtm` (owns everything), `authenticated` (RLS), database. The password goes in as a psql variable |
| `deploy/selfhost/bin/sanagtm` | Guided CLI: install, start, stop, restart, status, health, logs, backup, backups, restore (test or `--live`), `migrate import`, `migrate verify`, capacity, update, selftest, uninstall |
| `deploy/selfhost/bin/bootstrap-ubuntu.sh` | Ubuntu 24.04 prep: Docker CE + compose plugin, ufw (SSH only), optional empty data disk → `/srv/sanagtm`, code, CLI, timers |
| `deploy/selfhost/systemd/*` | Nightly backup + restore test (02:30), health every 5 min, daily capacity record |
| `deploy/selfhost/windows/check-host.ps1` | Read-only host readiness check (no admin) |
| `deploy/selfhost/windows/pull-backups.ps1` | Copies the VM's dumps off the VM and verifies their SHA-256 |
| `deploy/selfhost/requirements*.txt` | Pinned runtime set. Adds `dnspython` and `python-dotenv`, which the code imports but no requirements file listed. No redis, supabase, or Google libraries |
| `cloud/web/scripts/build-selfhost.mjs` | Same-origin production build; refuses secret keys |
| `deploy/selfhost/README.md` | Installation and operating guide |

### Application changes (tested)

| Change | Detail |
|---|---|
| `CAREERCLOUD_SELF_HOSTED=1` | `cloud/api/settings.py`, `deployment.py`. Production allows local file storage, and no TLS to a database on the private network (bare service name, loopback or private IP). **Everything else stays enforced**: a public database host still needs `sslmode=require` |
| `CAREERCLOUD_TRUSTED_PROXIES` | `middleware.py`. Real client IPs for rate limits behind nginx, believed only from the web container's /32 |
| `cloud/ops/selfhost.py` | `prepare` (wait for the DB → migrations → stamp only a FRESH DB → registry), `stamp-restored`, `registry` (lists the old hosted database as another environment, so the VM refuses it), `health api\|worker`, `info` |
| `deploy/ops/backup/sana_backup.py` | Now also does a restore test into a scratch database on a live server, `restore` into the install (owned by the app role; refuses to overwrite without `--replace --confirm`), `verify` against a manifest, and `prune` (14 daily / 8 weekly / 6 monthly). Credentials go only through PG* variables, never argv |

### Bugs found and fixed while testing

| # | Bug | Fix |
|---|---|---|
| 1 | The worker crashes at start on a lower-case `CAREERCLOUD_LOG_LEVEL` (`info`), which the API accepts | The worker now normalises the case |
| 2 | Production refused the PostgreSQL queue | `settings.py:239` (from the safety pass) |
| 3 | Restoring with `--no-privileges` silently drops the GRANTs that RLS needs | Privileges are kept |
| 4 | Passing a connection URL to `pg_restore --dbname` exposes the password in the process list | Uses PG* variables instead |
| 5 | A non-interactive install could not generate a new secrets key | Fixed |
| 6 | Cancelling a paused job-monitor run blocks the monitor forever | Fixed earlier on this branch |
| 7 | `prepare` migrated an unstamped restored database before refusing it (found by the rehearsal) | It now checks the stamp first; nothing is changed until `stamp-restored` |
| 8 | The restore verifier treated an older-release dump's pending migrations as corruption | Reported as `pending_migrations`; only edited migrations or RLS gaps fail |
| 9 | 30 tests failed inside the image because they need Redis/fakeredis or repository-only files | `cloud/tests/_optional.py` skip markers with stated reasons (no test logic changed) |

## 2. Verified test results

All runs were on this PC, without Docker.

| Test | What it proves | Result |
|---|---|---|
| Clean venv from `deploy/selfhost/requirements*.txt` only | The image's dependency set installs | **OK**, 52 packages |
| `image_suite_native.py`: the whole platform suite from the **image's staged file set** with that venv and an external PostgreSQL (= `sanagtm selftest`) | Dependencies and file set are complete; `sheets/` and `store/` are not needed | **1,925 OK, 70 skipped** (every skip has a reason: Redis-queue tests, Windows launchers, staging templates, Playwright) |
| `fresh_install_native.py`: real `sanagtm install` → real initdb script (password auth) → `migrate` → `api` + `worker` (the compose commands) → HTTP use → restart → backup → restore test → verify | An end-to-end fresh install in production self-hosted mode | **28/28 checks** |
| `migration_rehearsal.py` on the 392k-job research dump (pre-0013): backup → restore test → restore into the install → explicit stamp → pending migrations 0013/0014 on real data → an existing member signs in with the **original user id** → stranger sees nothing → source unchanged | The Phase 2 procedure, identity preservation, rollback path | **13/13**: 82.2 MB dump of 514,282 rows / 89 tables; restore + verify 35–52 s; migrations 0013/0014 applied in 3 s; the member's 392,134 jobs visible |
| `sana_backup.py selftest` | Backup, restore-test (throwaway cluster + scratch database), live restore owned by the app role, overwrite refusal, `--replace --confirm`, tamper refusal | **6/6** |
| `cloud.tests.test_selfhost` (15) | Profile rules, client IP trust, registry/legacy refusal, worker health, `prepare` and stamping, **API boots in self-hosted production mode** on a migrated PostgreSQL, refusal of a wrongly stamped database | **15 OK** (incl. provider credentials readable only with the copied secrets key) |
| `cloud.tests.test_selfhost_package` (22) | Compose/image invariants; the real CLI against a fake docker: config written, **no secret in docker argv**, refusals (reinstall, existing data, secret anon key, staging hostname, overwrite, purge without backup), backup/restore wiring, key generation | **22 OK** |
| Dashboard build `build-selfhost.mjs` | The same-origin bundle builds; a secret key is refused | **OK** |
| Full platform suite on the branch (dev venv) | No regressions | **1,950 OK** (18 skipped); CareerCrawler **1,696 OK**; web **117 OK** |
| `check-host.ps1` on this PC | Phase 3 readiness | **2 blockers, 2 warnings** (§4) |

**Not verifiable here** (no Docker or VM):
- `docker compose build/up`
- the nginx config at runtime
- the PGDG apt step
- the cloudflared tunnel
- PostgreSQL **17** specifically (tests used PostgreSQL 16 tools; production is 17.6)
- systemd timers
- the Ubuntu bootstrap

The first `sudo sanagtm install` + `sudo sanagtm selftest` + `sudo sanagtm health` on the VM is the acceptance test for these.

## 3. VM specification

| | Value |
|---|---|
| OS | Ubuntu Server 24.04 LTS, VMware Workstation Pro, NAT network |
| CPU / RAM | **4 vCPU / 8 GB** (the host has 4 cores / 8 threads and 31.7 GB) |
| Disk 1 | 30 GB (OS + images) |
| Disk 2 | 80 GB thin (`/srv/sanagtm`) |
| Location | `E:\VMs\sanagtm` (E: has 176 GB free; C: has 6 GB) |
| Growth path | The data disk can grow; millions of jobs need 250–300 GB plus an external backup disk |

## 4. Host readiness (Phase 3, measured by `check-host.ps1`)

| Check | Result |
|---|---|
| Administrator rights | **BLOCKER**: standard user. An administrator must install VMware Workstation once |
| VMware Workstation | **BLOCKER**: not installed |
| Virtualization | OK. A hypervisor is running (VBS), so VT-x is on; VMware 17.5+ runs on top of it via WHP |
| CPU / RAM | OK: i3-12100 4C/8T, 31.7 GB (13.9 GB free at the time) |
| Disk | E: 176 GB free (OK). C: 6 GB (warning: keep everything off C:) |
| Physical disks | **Warning**: only one NVMe. Off-machine backups need a USB disk or another machine |
| Sleep on AC | Never (OK) |
| Network | download.docker.com, github.com, archive.ubuntu.com, Cloudflare Tunnel edge TCP 7844, supabase.co: **all reachable** |

## 5. Remaining manual prerequisites

| # | Prerequisite | Who | Blocking |
|---|---|---|---|
| 1 | Install VMware Workstation Pro (free) | an administrator on this PC | the VM |
| 2 | Create the VM and install Ubuntu (README §1) | you | install |
| 3 | A domain on Cloudflare + a named tunnel + public hostname → `web:8080` (README §2) | you (Cloudflare dashboard) | public access |
| 4 | Supabase Auth Site URL / redirect URLs → `https://sanagtm.<domain>` | you (Supabase dashboard) | password-reset links |
| 5 | The current `CAREERCLOUD_PLATFORM_SECRETS_KEY` (it is in the Windows env files; the installer asks for it, hidden input) | you | reading stored provider credentials |
| 6 | A USB disk or second machine for off-machine backups | you | disaster recovery |
| 7 | **For the migration:** PostgreSQL **17** client tools to dump the 17.6 production database. Either inside the VM (the app image has them: run `sana_backup.py backup` there) or a portable PG17 zip on Windows | approval | migration |
| 8 | Approval to commit/push this branch, or use the git-bundle route (README §3) | you | getting the code onto the VM |

## 6. Migration checklist (Supabase → VM)

Nothing here has been run against production.

1. ☐ VM installed: `sudo sanagtm health` all ok, and `sudo sanagtm selftest` passes.
2. ☐ Disable automatic full sweeps on production. One audited row, with an exact rollback:
   ```
   monitor_settings.py auto-sweep off … --apply
   ```
   This needs approval; see `PRODUCTION_SAFETY.md` §2.
3. ☐ Freeze writes: stop the Windows worker and API. Disable the "SANA GTM Auto Start" task, but **do not delete it**.
4. ☐ Production backup, from inside the VM (the app image has PG17 tools; the Supabase URL is read from an env file, never argv):
   ```
   docker compose … run --rm -v /srv/sanagtm/backups:/backups -v /root/supabase.env:/supabase.env:ro migrate \
     python deploy/ops/backup/sana_backup.py backup --env-file /supabase.env --pg-bin /usr/lib/postgresql/17/bin \
     --out /backups --label supabase-final --local-only
   ```
   Then copy it off the machine (`pull-backups.ps1`). Delete `/root/supabase.env` afterwards.
5. ☐ `sudo sanagtm migrate import /srv/sanagtm/backups/supabase-final_careercloud_….dump`. This does:
   - a restore test into a scratch database
   - the restore into the install, verified against the manifest
   - the explicit production stamp
   - start of the API and dashboard; **the worker stays off**
6. ☐ `sudo sanagtm migrate verify /srv/sanagtm/backups/….manifest.json`: rows, checksums and RLS per table.
7. ☐ Browser checks as your own account: workspaces, jobs (115,682), signals, CRM, email validation history, Settings › providers. Run **Test connection** (free checks only). The provider keys decrypt only with the copied secrets key.
8. ☐ Second account: invitations and roles behave as before. Spot-check the audit log.
9. ☐ Decide the paused full-sweep run **before** starting the worker. On the VM there is no 500 MB cap. Cancel it with the fixed code, or let it resume.
10. ☐ `sudo sanagtm start` (worker on). Watch `sudo sanagtm logs worker -f` and `sudo sanagtm health` for 48 h.
11. ☐ First nightly backup: the restore test passed and the backup was pulled off the machine.

## 7. Rollback checklist (valid for 30 days)

1. **Before step 5 completes:** nothing to undo. Supabase is untouched and remains the system of record.
2. **After cutover:**
   1. `sudo sanagtm stop` on the VM.
   2. Re-enable the Windows task "SANA GTM Auto Start". Its env files, tunnel and Pages project are unchanged.
   3. Rows created on the VM after cutover: export with `sudo sanagtm backup`, then decide case by case. They are not merged back automatically.
3. **Do not delete during the 30 days:**
   - the Supabase project
   - Upstash
   - the Windows supervisor files
   - the Redis rollback env files
   - the final Supabase dump
   - `E:\Crawlers\sana-backups`
   - the `sana_wad` research database

## 8. Production safety: the October 12 automatic full sweep

- **What happens on 10-12 if nothing changes:** nothing. `tick()` checks for an active run **before** it checks whether a sweep is due (`service.py:335`). The paused full sweep is still an active run, so the 10-12 sweep cannot start while it stays paused. It has blocked every scheduled run since 10-02.
- **Minimum safe action:** keep the run paused. No change is required.
- **Defence in depth (recommended, needs approval):** this protects against someone cancelling or resuming the paused run before migration. It writes one audited row; the rollback restores the exact timestamp.
  ```
  monitor_settings.py auto-sweep off … --apply
  ```
- **Do not cancel the paused run from the current production UI.** The production code leaves the monitor blocked forever (reproduced). The fix is on this branch.
