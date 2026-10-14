# SANA GTM phase 2: self-hosting readiness and network security

| | |
|---|---|
| Date | 2026-10-14 |
| Worktree / branch | `E:\Crawlers\CareerCrawler-sana-safety`, `fix/production-safety` |
| Base | `9edafcc` (phase 1). Verified on `origin/feature/careercrawler-platform` with `git ls-remote` before this work; the working tree was clean |
| Production | **Not touched.** Production still runs `0bdcee4` from `E:\Crawlers\CareerCrawler-platform`. Nothing was changed: no deploy, configuration, DNS, Supabase redirect URLs, WAD sweep, `stale_after_days`, history pruning or Redis/legacy component. No crawls and no migration |

**Labels.** **CONFIRMED** = proven by a test or command run here. **NOT RUN** = needs an administrator or a VM. **ASSUMPTION** = reasoned only.

---

## 1. Git state (verified)

- `9edafcc` is reachable from `origin/feature/careercrawler-platform` (`git merge-base --is-ancestor`).
- Other worktrees hold unrelated uncommitted work. It is left untouched:
  - `CareerCrawler-sana-jobmonitor`: 2 paths;
  - `CareerCrawler-sana-jobspy`: 11 paths.
- The production checkouts `CareerCrawler-platform` and `CareerCrawler` are clean.

## 2. Network security: every outbound path

| Path | Before | Now | Evidence |
|---|---|---|---|
| `SafeFetcher` (scraper, sources, discovery, job monitor, public-web contacts) | URL checked per hop, then `requests` **resolved again** (DNS rebinding window) | Every request runs in `netguard.guarded()`: resolve, check every address, connect to **that** address | `test_platform_egress.ScopedGuard.test_rebinding_between_check_and_connect_is_refused` (resolver flips public→loopback; refused at connect, 2 resolutions) |
| Platform worker, any urllib3 traffic (CareerCrawler adapters' own sessions, providers, AI) | process guard (phase 1) | unchanged, now implemented once in `cloud/shared/netguard.py` (`cloud/worker/egress.py` is a thin wrapper) | `test_egress` (legacy worker), `test_platform_egress` |
| **API process** | no guard | process-wide guard **by default where deployed** (staging, production, `CAREERCLOUD_SELF_HOSTED=1`); refused off in staging and production; development and test processes unchanged unless `CAREERCLOUD_EGRESS_GUARD` is set | `ScopedGuard.test_api_guard_defaults_and_infrastructure_hosts`; importing the API in a test process leaves the guard off (checked) |
| Webhooks (`integrations._post`, automation webhook action) | `check_url`, then `requests.post` (rebinding window) | the post runs in `netguard.guarded()` with the same resolver | `ScopedGuard.test_webhook_delivery_is_pinned` |
| **Scraper browser** (`scraper/fetcher.PlaywrightRenderer`) | URL checked per request, then Chromium connected itself; WebSockets, service workers and downloads uncontrolled | Chromium **never connects**. Each request is URL-checked, then fetched by Python through `netguard.guarded()` and fulfilled. Redirects are followed in Python, each hop checked; HTML gets `<base href>` to the final URL. Chromium runs with a **dead proxy** (`127.0.0.1:9`, loopback bypass removed), so anything not intercepted (WebSockets, service-worker fetches) fails. Also: service workers blocked, downloads off, WebRTC non-proxied UDP off, no DNS prefetch | `test_browser_containment` (real Chromium, local site): direct private IPs, integer/hex IPv4, `[::1]`, metadata, a name resolving to loopback, a popup, `fetch()` to 10.x, a redirect into private space, a WebSocket, a service worker, and DNS rebinding are all refused; the server only ever saw the page and the redirect hop; a public redirect still works |
| **Legacy CareerCrawler browser** (`utils/browser.py`, used by a platform `crawl` with `browser_fallback`) | could run inside the platform worker with no containment | refused unless `CAREERCLOUD_EGRESS_FIREWALL_CONFIRMED=true`, the same rule as the legacy crawl worker; the crawl runs without the browser and the result says why | `test_platform_crawl.test_browser_fallback_needs_a_confirmed_firewall` |
| SMTP preflight (email engine) | resolves the MX, refuses non-public, connects to that exact address | unchanged (already correct) | `test_platform_email_engine` |
| SMTP relay (`gtm/sequences`, mailboxes) | host from the operator's server environment only | unchanged: operator-controlled, not user input | — |
| DNS lookups (dnspython) | queries the configured resolver | unchanged: no connection to the looked-up target | — |
| Health probe (`cloud/ops/selfhost.py`) | `urlopen` to `127.0.0.1` | unchanged: fixed local target | — |

**Alternate IP spellings and hostname tricks.** The URL check (`cloud.shared.urls`) refuses all of the following:
- integer, hex and octal IPv4 (`2130706433`, `0x7f000001`, `0x7f.0.0.1`, `017700000001`) and short forms (`127.1`);
- IPv4-mapped IPv6, `[::1]`, `[fe80::1]`, `0.0.0.0`, 169.254.169.254;
- `localhost.`, `*.local`, `*.corp`, `*.internal`;
- credentials in URLs, non-http(s) schemes, and ports other than 80/443/8080/8443.

These cases are in `ScopedGuard.test_alternate_ip_spellings_and_local_names_are_refused_by_the_url_check`. Chromium itself rewrites integer and hex IPv4 hosts to dotted form before requesting (observed); they are refused either way.

**Limitations that cannot be fully enforced in-process (documented, not hidden):**
1. **The legacy CareerCrawler browser** (`utils/browser.py`) is not contained. It stays off on the platform unless an outbound firewall is confirmed (see section 4, admin item 5).
2. **A service-worker registration can report success** in the scraper browser. Its script and fetches go to the dead proxy (the test shows the server never received `/sw.js`).
3. **Raw sockets from third-party libraries** (none found besides SMTP/DNS above) would not be covered by the urllib3 guard. An OS outbound firewall rule is the complete control.
4. **Response cookies in the scraper browser** are passed on as a combined header. Sites relying on many cookies may behave differently. The scraper does not log in anywhere.

## 3. Windows package validation

| Requirement | Result |
|---|---|
| PostgreSQL bound to localhost only | **CONFIRMED** in the installer: `listen_addresses = '127.0.0.1'`; `pg_hba.conf` only `127.0.0.1/32` and `::1/128` with SCRAM. The e2e checks "PostgreSQL accepts only local SCRAM logins" |
| API, worker and tunnel configuration | **CONFIRMED** (e2e): API + dashboard one process (`cloud.ops.webapp`), worker with `--env-file` and `--state-file`, tunnel with `--token-file` |
| PostgreSQL queue without Redis | **CONFIRMED**: the clean venv built from the Windows lock has no `redis`; the e2e runs CSV import and export through the PostgreSQL queue |
| Data, uploads, logs, config and backups persist across restarts | **CONFIRMED in user mode** (e2e: restart, update, rollback, uninstall keeping data, reinstall `-ReuseData`). **Reboot: NOT RUN** |
| Credential encryption uses the existing `CAREERCLOUD_PLATFORM_SECRETS_KEY` | **CONFIRMED** (e2e: a fresh install with the same key imports the first install's backup and the stored credential is readable) |
| Supabase Auth stays enabled | **CONFIRMED** in code: `CAREERCLOUD_AUTH_MODE=supabase`; JWKS verification; the dashboard uses `supabase.auth.*` only. A real sign-in against a self-hosted install is **NOT RUN** (rehearsal step 7) |
| Secrets not in logs, command lines, installer output or Git | **CONFIRMED**: e2e "no secret printed during install"; service XML holds only non-secret environment entries; the tunnel token is passed by file; the PostgreSQL password by `--pwfile`; the package secret scan compares against 7 real values; the commit range is secret-scanned (section 6) |
| **Health checks fail when a dependency is down** | **Fixed.** `health api` used the static `/api/v1/health`, so an API whose database was down looked healthy. It now also requires a database round-trip. `health worker` now also fails when the worker reports `queue_unavailable`. Tests: `HealthProbeTests` (database down → unhealthy, no URL in the output; degraded worker → unhealthy; API down → exit 1) |
| **Backup and restore failure behaviour** | **CONFIRMED**: `sana_backup.py selftest` with the PostgreSQL 17.11 tools passed. It covers a throwaway restore test, a scratch-database restore test, a live restore owned by the app role, refusing to overwrite without `--replace --confirm`, and refusing a tampered copy |
| **No duplicate workers after shutdown or recovery** | **Fixed.** The PID-file check in user mode cannot stop a second worker after a crash or a manual start, and two workers would both run the schedulers (a due monitor could start twice). The worker now holds a PostgreSQL **session advisory lock**. A second worker waits on standby and takes over when the first exits or dies. A watchdog re-takes the lock after a database restart, or stops this worker if another took it. `CAREERCLOUD_WORKER_SINGLETON=false` disables it for deliberate multi-worker setups. Tests: `WorkerSingletonTests` (real PostgreSQL) |
| Service mode (WinSW services, start at boot, restart on failure) | **NOT RUN**: needs an administrator |

## 4. Exact administrator requirements

1. **Create the Windows VM.** Minimum 2 vCPU / 4 GB; recommended 4 vCPU / 8 GB / 80 GB+ data disk. Windows 10 1809+ or Server 2019+.
2. **Install the Microsoft Visual C++ 2015–2022 x64 runtime** if `Install-SanaGtm.ps1 -PreflightOnly` reports it missing. Service mode installs it automatically, but only as administrator.
3. **Run the installer in Service mode** (`INSTALL.cmd`, *Run as administrator*). This registers `SanaGtmPostgres`, `SanaGtmWeb`, `SanaGtmWorker` and `SanaGtmTunnel` (delayed automatic start, restart on failure).
4. **Reboot the VM once and run `sanagtm health`.** This is the only way to validate start-at-boot and recovery.
5. **Optional but recommended: an outbound Windows Firewall rule** for the worker's Python and Chromium that blocks 10/8, 172.16/12, 192.168/16, 169.254/16, 100.64/10, 127/8 (except the local PostgreSQL port), fc00::/7 and fe80::/10. Then set `CAREERCLOUD_EGRESS_FIREWALL_CONFIRMED=true` if the legacy browser fallback is wanted.
6. **On this office PC: `sc.exe delete SanaGtmProbeWeb`** (a leftover stopped test service).

## 5. Migration rehearsal procedure (repeatable; never touches live production)

The rehearsal runs on the **VM's own PostgreSQL**, an isolated copy. The live production database (Supabase) is only **read** (a dump). Its data, auth settings and the office PC are not changed.

| # | Step | Commands / checks | Pass criteria | If it fails |
|---|---|---|---|---|
| 1 | Fresh backup + checksum (office PC) | `python deploy/ops/backup/sana_backup.py backup --env-file <prod worker env> --pg-bin <pg17>\bin --out E:\SanaBackups --local-only`, then `... restore-test --dump <dump> --pg-bin ...` | `OK … sha256 …` and `RESTORE TEST PASSED` with tables/rows equal to the manifest | Stop. Do not continue with an unverified backup |
| 2 | Copy to another physical disk | `... backup ... --offsite <USB or network share>` (copies and verifies the SHA-256), or copy the `.dump` + `.manifest.json` and compare `Get-FileHash` with the manifest | hashes equal | Recopy. The office PC has one physical disk (C: and E: share an NVMe) |
| 3 | Install on the VM | copy `SANA-GTM-Windows-Installer.zip` + `.sha256`, check `Get-FileHash`, `INSTALL.cmd` as admin; answers: a **rehearsal** hostname or `-NoTunnel`, Supabase URL + anon key, the **existing** `CAREERCLOUD_PLATFORM_SECRETS_KEY` via `PLATFORM_SECRETS_KEY_FILE` | installer ends with "installed"; `sanagtm health` OK except "backup newer than 36 h" | `logs\web.err.log`, `sanagtm logs worker`; `sanagtm uninstall` keeps data |
| 4 | Restore the copy into the VM database | `sanagtm migrate import <dump copied in step 2>` | restore test passes; credential check reports all readable; dashboard starts, **worker stays off** | A wrong secrets key shows here: reinstall with the existing key |
| 5 | Keep the worker stopped | `sanagtm status` shows the worker stopped. **Before any worker start, pause the imported WAD monitor in the VM copy** (Monitors → the WAD monitor → **Pause**, which sets `enabled = false`), so the VM never crawls in parallel with production | the monitor shows "Paused" in the VM UI | — |
| 6 | Verify the copy | `sanagtm migrate verify <manifest>`; spot-check 5 jobs, the keyword set, provider connections (Settings → Providers shows "verified" without testing paid calls), the paused run `jmr_eba7780b…` | exact table counts and checksums; representative records match production UI | Record the table and row; restore again |
| 7 | Supabase sign-in, without changing redirect settings | open `http://127.0.0.1:8100` on the VM and sign in with **email + password** (password sign-in needs no redirect URL; magic links and OAuth would, so do not use them) | the existing account opens its existing workspaces (same user id) | `CAREERCLOUD_SUPABASE_URL` / anon key answers |
| 8 | APIs, uploads, job saving, history, queue | `sanagtm start` **after step 5**; upload a 3-row job CSV (Jobs → Import), export it, open a job's history; Tasks shows `job_import` / `job_export` completed | rows saved once (re-import → duplicates counted, no new rows); history shows the import; tasks completed through the PostgreSQL queue | `sanagtm logs worker` |
| 9 | One controlled crawler task | one **official public API** read, e.g. Sources → `ats_public` with `https://boards.greenhouse.io/<an approved company>`, or the scraper on one approved page with `max_pages=1`. **Not** the WAD monitor | postings saved once with `source_name=ats_public`; no request to a private address (`egress blocked` absent from the log unless a test target asks for one) | stop the worker; record |
| 10 | Backup, restore and service recovery | `sanagtm backup -Offsite <folder>`; `sanagtm restore <that dump>` (restore test); **reboot the VM** (admin); `sanagtm health` | backup verified; restore test passed; all services back after reboot; exactly one worker active (a second `sanagtm start` leaves the first worker holding the lock) | `sanagtm rollback-update` / reinstall `-ReuseData` |
| 11 | Record | a rehearsal log: each step, timestamps, failures, fixes | complete | — |

**Rollback of a rehearsal:**
- `sanagtm uninstall -PurgeData` on the VM (it needs a backup under 24 h and a typed confirmation).
- Production was never changed, so nothing else needs rolling back.

## 6. Test results (offline, 2026-10-14)

| Suite | Result |
|---|---|
| Platform `cloud/tests` | **2,017 run, 0 failed**, 18 skipped (all need a real Redis) |
| CareerCrawler `tests` | **1,683 run, 0 failed** |
| Old PyInstaller installer `deploy/windows/installer/tests` | 79 run, 0 failed |
| Web `npm test` | 117 run, 0 failed |
| Windows package e2e, User mode, no admin, package built from these changes (510 files) | **40/40** (install, app use, CORS, PostgreSQL-queue worker with the new singleton lock, backup, restore test, update/rollback, uninstall/reinstall, import with the same key). Package secret scan: 0 problems against 7 real secret values |
| Backup tool `sana_backup.py selftest` (PostgreSQL 17.11 tools) | **SELFTEST PASSED**, 6/6 (restore tests, live restore, overwrite refusal, `--replace --confirm`, tampered copy refused) |
| New security tests | `test_platform_egress` 11 (incl. rebinding, scoped guard, webhook pinning, alternate IP spellings, API defaults); `test_browser_containment` 2 (real Chromium); `test_platform_crawl` browser-fallback gate 1; `test_worker_singleton_pg` 6 (lock, standby, takeover, watchdog; health probes) |

**Failures during development, all fixed before the final runs:**
- A synchronous Playwright WebSocket route deadlocked. WebSockets are now left to the dead proxy.
- Playwright does not route the request that follows a fulfilled redirect. Redirects are now followed in Python, each hop checked.
- A scripted edit duplicated a block in `fetcher.py`; it was found by the tests and removed.

**Not run:** Windows Service mode, reboot recovery, a real Supabase sign-in against a self-hosted install, anything against production, live crawls.

## 7. Remaining risks and production cutover prerequisites

**Risks:**
1. Service mode and reboot recovery are untested (admin).
2. A real Supabase sign-in against a self-hosted install is untested (rehearsal step 7).
3. The legacy browser containment needs an OS firewall (section 2, limitation 1).
4. The WAD scheduler is still blocked by the paused sweep; nothing ran since 2026-10-02.
5. The office PC has no off-machine backup copy yet.

**Cutover prerequisites (each needs explicit approval):**
- a verified fresh backup copied off-machine;
- a completed rehearsal (section 5) with no open failures;
- a decision on the paused WAD sweep (resume or cancel);
- a named Cloudflare Tunnel and stable hostname;
- the Supabase Site URL / redirect URLs updated **at** cutover;
- stopping the office PC worker, a final dump, import and verify;
- `stale_after_days` back to 30;
- optionally `CAREERCLOUD_JOB_HISTORY_RETENTION_DAYS=30`;
- retiring Upstash, the Pages proxy and the office PC autostart afterwards.
