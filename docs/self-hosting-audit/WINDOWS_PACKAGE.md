# SANA GTM for Windows: package status (2026-10-12)

**Deliverable:** `SANA-GTM-Windows-Installer.zip`, built by `deploy/windows-selfhost/Build-Package.ps1` into `E:\Crawlers\sana-win-build\out\`, with a `.sha256` file alongside. The ZIP is a build output: git-ignored and never committed.

**Source:**
- `deploy/windows-selfhost/` (installer, `sanagtm` CLI, PowerShell module, pinned components, lock file, docs, tests)
- `cloud/ops/webapp.py` (dashboard + API on one origin)
- `cloud/web/src/runtimeConfig.ts` (the dashboard reads its auth settings at run time)

## Architecture decision

**Native Windows services**, not Docker Desktop or WSL2:

- **The app already runs on Windows.** Production runs on a Windows PC today, and all suites pass on Windows.
- **Docker Desktop in a VM needs nested virtualization.** VMware generally cannot offer it while the host runs Hyper-V or VBS, which this host does (measured).
- **Docker Desktop starts only after a user signs in.** That breaks "start after a reboot without login". It also carries business licensing terms.

**Components:**

| Service | Program | Account | Listens |
|---|---|---|---|
| SanaGtmPostgres | PostgreSQL 17.11 (`pg_ctl register`) | NetworkService | **127.0.0.1**:55432, SCRAM, `pg_hba` local only |
| SanaGtmWeb | uvicorn `cloud.ops.webapp` (dashboard + `/api` in one process) | LocalService | 127.0.0.1:8100 |
| SanaGtmWorker | `cloud.intel.tasks.worker` (all crawlers / task kinds, PostgreSQL queue) | LocalService | none |
| SanaGtmTunnel | cloudflared `--token-file` | LocalService | outbound only |

- **Wrapper:** the web, worker and tunnel run under WinSW 2.12.0.
- **Start-up:** all four use delayed automatic start, restart on failure, and logs rolled at 10 MB. They start at boot without anyone signing in.
- **User mode (no administrator):** the same programs run under your account and start at sign-in through a scheduled task.

## Results on this PC

This PC runs Windows 11 Home, the account has no administrator rights, and no VM exists yet.

| Test | Result |
|---|---|
| Pinned downloads: SHA-256 recorded; Authenticode checked | cloudflared signed by Cloudflare, python.exe signed by the PSF. WinSW and EDB's `postgres.exe` are unsigned upstream, so they are pinned by SHA-256 |
| PostgreSQL runtime needs | the binaries import `vcruntime140`, `vcruntime140_1` and `msvcp140`. Preflight checks for them; service mode installs the Microsoft-signed redistributable |
| `cloud.tests.test_webapp` | 8/8 |
| `cloud.tests.test_windows_package` (scanner, pins, ASCII + parse of scripts, no secret on argv) | 10/10 |
| PowerShell parse of all scripts | 0 errors |
| Package build + secret scan (500 files, against 12 real local secret values) | 0 problems |
| WinSW parses the generated service definitions (`status` → NonExistent) | ok |
| **Real end-to-end, User mode, from the ZIP** (`tests/e2e_user_mode.py`) | **39/39 checks in 261 s**: preflight; install (secrets never printed, config ACL excludes Users/Everyone, data outside the program folder, PostgreSQL local SCRAM only); dashboard + runtime config + same-origin API; Supabase-format auth; workspace; encrypted provider credential; CSV import and export **through the PostgreSQL queue by the worker**; isolation; `status`, `backup`, restricted config backup, `health`, `restore` test, `migrate verify`, `verify-secrets`, `logs`, `capacity`, `restart`, `update -Package`, `rollback-update`; uninstall keeps data; reinstall `-ReuseData`; purge refused without confirmation; `uninstall -PurgeData`; fresh install with the same key, then `migrate import` (credential readable, worker stays off, data visible to the original identity) |
| Full platform suite / CareerCrawler / web / installer | **1,968 OK** (18 skipped) / **1,696 OK** / **117 OK** / **79 OK** |

**Found and fixed by these runs:**

| # | Defect | Fix |
|---|---|---|
| 1 | `.Count` on an empty result aborted the installer under strict mode | Wrapped in `@()` |
| 2 | The worker's state-file replace failed on Windows when a health check had the file open (`WinError 32`) | Retried briefly. It was already non-fatal |
| 3 | The package scanner's real-value list counted a password-less `redis://127.0.0.1` URL as a secret | Only password-bearing URLs count |
| 4 | The test harness hung on inherited pipe handles | Output now goes to files. Test-only issue |
| 5 | **Reinstalling over existing data left `install.json` with an EMPTY access list**: inheritable grants applied file by file | The folder is protected, then its contents reset to inherit. Verified: 3 entries, no Users |
| 6 | Antivirus briefly locks freshly extracted executables | Extraction retries; uninstall retries the folder removal |
| 7 | The scanner test held a credential-shaped fake string, and the scanner refused to ship it | The fake is assembled at run time |

## Not verified here: needs an administrator and the VM

- **Service mode end to end.** `pg_ctl register`, WinSW `install` with the LocalService / NetworkService accounts, delayed auto-start, start after a **reboot without sign-in**, and the service-mode permissions.
- **The Visual C++ redistributable auto-install.** This PC already has the runtime.
- **Cloudflare Tunnel with a real token, and the public health check.**
- **Sign-in with real Supabase tokens.** The tests used HS256 tokens in Supabase's format; the VM uses Supabase's JWKS, exactly as production does today.
- **Probe side effect, 2026-10-12.** Checking WinSW's `install` on this PC raised a UAC prompt, which was approved, so a stopped test service `SanaGtmProbeWeb` was registered here. Remove it from an Administrator prompt:
  ```
  sc.exe delete SanaGtmProbeWeb
  ```
  Then delete `E:\Crawlers\sana-win-build\winsw-probe-122039352`.

## Production readiness

| Area | Ready? |
|---|---|
| Application code on Windows (API, dashboard, worker, migrations, backups, restore) | **Yes**: verified end to end in user mode on Windows 11 |
| Installer + CLI, user mode | **Yes**: verified from the ZIP |
| Installer + CLI, service mode (the recommended production mode) | **Built and statically checked, not run**: needs the VM + administrator |
| Migration from Supabase | Procedure verified on rehearsal data. The real `migrate dump-hosted` + `import` has not been run and needs your approval |
| Go-live | After a service-mode install on the VM passes `health` + `selftest` and survives a reboot |
