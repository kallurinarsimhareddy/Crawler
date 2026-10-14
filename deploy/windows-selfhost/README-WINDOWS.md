# SANA GTM on Windows (self-hosted)

The complete SANA GTM application runs natively on one Windows machine:

- PostgreSQL 17
- the API and dashboard (one local web server)
- the background worker, which runs every crawler and task through the **PostgreSQL task queue**
- an optional Cloudflare Tunnel

There is **no Docker, WSL2, Redis, Upstash or hosted database**. Sign-in stays on Supabase Auth (free tier), so existing accounts and user IDs keep working.

```
Browser ─https─► Cloudflare ─► Tunnel (outbound only) ─► 127.0.0.1:8100  SanaGtmWeb  (dashboard + /api, one process)
                                                         SanaGtmWorker  (all task kinds: crawlers, imports, email checks …)
                                                         SanaGtmPostgres 127.0.0.1:55432 (never reachable from the network)
```

## Why native Windows services (and not Docker Desktop)

- **The application already runs natively on Windows.** The current production instance runs on a Windows PC, and every test suite passes on Windows.
- **Docker Desktop with WSL2 is a poor fit inside a VM.** It needs *nested* virtualization, which VMware generally cannot offer when the host runs Hyper-V or virtualization-based security, as the current host does. Docker Desktop also starts only after a user signs in, and has licensing terms for larger companies.
- **Native services meet every requirement.** They start at boot without anyone signing in, use less memory, and are managed with standard Windows tools.

## Requirements

| | Minimum | Recommended |
|---|---|---|
| Windows | 10 1809 / Server 2019 (64-bit) | Windows 11 / Server 2022 |
| CPU / RAM | 2 cores / 4 GB | 4 cores / 8 GB |
| Disk | 3 GB program + 20 GB data | 80 GB+ for data (a second disk, e.g. `D:`) |
| Network | outbound HTTPS | + outbound TCP 7844 for the Cloudflare Tunnel |
| Other | Microsoft Visual C++ 2015–2022 x64 runtime (installed automatically in service mode) | |

## What needs an administrator

| Step | Admin? | Why |
|---|---|---|
| Visual C++ runtime, if missing | **yes** (once) | PostgreSQL needs it. Service mode installs it automatically after checking it is signed by Microsoft |
| Registering the 4 Windows services (start at boot) | **yes** | Windows rule |
| `sanagtm start / stop / restart / update / uninstall` on a service-mode install | **yes** | controls services |
| Everything else (download, verify, unpack, Python packages, database creation, migrations, configuration, status, health, logs, backup, restore test) | no | done by the installer and the CLI |

**Without an administrator:** use **User mode** (`-Mode User`). It runs the same programs under your account and starts them at **sign-in**, not at boot. User mode cannot install the Visual C++ runtime. If PostgreSQL then fails with error `0xC0000135` (a missing DLL), an administrator must install the runtime once. Run `-PreflightOnly` first, because it reports this before anything changes.

## Install

1. Copy `SANA-GTM-Windows-Installer.zip` to the VM. Check it against `SANA-GTM-Windows-Installer.zip.sha256`:
   ```powershell
   Get-FileHash .\SANA-GTM-Windows-Installer.zip
   ```
   Then unzip it.
2. Right-click **`INSTALL.cmd`** › **Run as administrator**. Or, in an Administrator PowerShell:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\SANA-GTM-Windows-Installer\Install-SanaGtm.ps1 -DataDir D:\SanaGtmData
   ```
   Check first without changing anything: add `-PreflightOnly`.
3. Answer the prompts:
   - the public hostname
   - the Supabase Auth URL
   - the public anon key
   - the **existing** `CAREERCLOUD_PLATFORM_SECRETS_KEY` (hidden input; Enter = generate a new one)
   - the Cloudflare Tunnel token (hidden input)

   For an unattended install, copy `config\answers.example.env` to a private folder, fill it in, and pass `-AnswersFile`.

The installer then does the rest:

1. Checks Windows, admin rights, memory, disk, ports, the Visual C++ runtime and network access. It stops before changing anything if a check fails.
2. Downloads Python 3.12.10, PostgreSQL 17.11, WinSW 2.12.0 and cloudflared 2026.10.0, and **verifies each SHA-256** (pinned in `components.json`). `-ComponentsDir` uses pre-downloaded copies for offline installs.
3. Installs the Python packages from the pinned `requirements-lock.txt`.
4. Creates PostgreSQL on **127.0.0.1 only** (SCRAM passwords, random 192-bit passwords), the app role and the database, then applies the migrations and the production stamp.
5. Writes `<DataDir>\config\sanagtm.env`, readable only by SYSTEM, Administrators, you and the service account.
6. Registers the services (service mode) or a sign-in task (user mode), starts everything, and runs the health check.

## Where things are

| Path | Contents |
|---|---|
| `C:\Program Files\SANA GTM\` (service mode) or `%LOCALAPPDATA%\SANA GTM\` (user mode) | program: `app\` (source), `web\dist` (dashboard), `runtime\` (Python, PostgreSQL, WinSW, cloudflared), `bin\sanagtm.cmd` |
| `<DataDir>\config\` | **secrets**: `sanagtm.env` (incl. the platform secrets key), `postgres.superuser`, `tunnel.token`; `install.json` |
| `<DataDir>\pgdata\` | the database |
| `<DataDir>\files\`, `results\` | uploads, imports, exports |
| `<DataDir>\backups\` | nightly dumps + manifests, files zips, `config\` (secrets, restricted) |
| `<DataDir>\logs\` | web, worker, tunnel and PostgreSQL logs (rolled at 10 MB) |
| `<DataDir>\run\` | worker heartbeat, health.json, capacity.csv, resource registry |

Upgrading or uninstalling the program never touches `<DataDir>`.

## Operate

```bat
"C:\Program Files\SANA GTM\bin\sanagtm.cmd" status
sanagtm health                       & rem exit code 0 = healthy; writes run\health.json
sanagtm logs web -Follow             & rem web | worker | tunnel | postgres
sanagtm start | stop | restart
sanagtm backup                       & rem dump + manifest + files + config; -Offsite E:\Backups copies + verifies off the data disk
sanagtm backups
sanagtm restore <file.dump>          & rem restore TEST into a scratch database (dropped afterwards)
sanagtm restore <file.dump> -Live    & rem replaces the database (asks for the hostname; safety backup first)
sanagtm capacity
sanagtm update -Package <new zip>    & rem backup, swap program, pip, migrations, restart, health
sanagtm rollback-update              & rem back to the previous program version
sanagtm selftest                     & rem the platform test suite on scratch databases
sanagtm uninstall                    & rem keeps <DataDir>; -PurgeData needs a backup under 24 h and typed confirmation
```

**Nightly backup.** Create a scheduled task as an administrator:

```powershell
schtasks /Create /TN "SANA GTM backup" /SC DAILY /ST 02:30 /RU SYSTEM /TR "\"C:\Program Files\SANA GTM\bin\sanagtm.cmd\" backup -Label nightly"
```

User mode uses the same command without `/RU SYSTEM`; it runs only while you are signed in.

Copy `<DataDir>\backups` to another physical disk or machine regularly. `-Offsite <folder>` does that and verifies each copy.

## Cloudflare Tunnel (secure public access)

1. In the Cloudflare dashboard, open Zero Trust › Networks › Tunnels › **Create a tunnel** (Cloudflared). Copy the token.
2. Add a public hostname `sanagtm.<your-domain>` → service **HTTP** `127.0.0.1:8100`.
3. In **Supabase** › Authentication › URL Configuration, add `https://sanagtm.<your-domain>` as the Site URL and as a redirect URL.

Use a **named** tunnel with a stable hostname. A `*.trycloudflare.com` quick tunnel changes its address on every restart, so never configure or build against one.

**Keeping the Cloudflare Pages dashboard.** The install serves its own dashboard at `https://sanagtm.<your-domain>`. To also keep the Pages site (for example `https://sanagtm.pages.dev`) working against this machine, do two things:
- Add `EXTRA_CORS_ORIGINS=https://sanagtm.pages.dev` to the answers file. Only https origins are accepted; separate several with commas.
- Rebuild the Pages site with `VITE_API_URL=https://sanagtm.<your-domain>`.

Supabase must list every dashboard origin as a redirect URL.

The tunnel only makes **outbound** connections. No inbound firewall rule or router port is opened. PostgreSQL listens only on 127.0.0.1, and its `pg_hba.conf` accepts only local SCRAM logins. The token is stored in `config\tunnel.token`, which the service reads with `--token-file`, so it never appears on a command line.

## Moving the existing data (Supabase → this machine)

1. Install with the **existing** `CAREERCLOUD_PLATFORM_SECRETS_KEY`. It is in the current install's `cloud\api\.env.sana-cloud`. Supply it via `PLATFORM_SECRETS_KEY_FILE` or the hidden prompt.
2. Stop the old installation's worker (no new writes). Then take a read-only backup of the hosted database with the bundled PostgreSQL 17 tools:
   ```
   sanagtm migrate dump-hosted C:\secure\supabase.env
   ```
   `supabase.env` holds one line, `CAREERCLOUD_DATABASE_URL=<the Supabase session-pooler URL>`. The URL is read from that file, never from the command line. Delete the file afterwards.
3. Run:
   ```
   sanagtm migrate import D:\SanaGtmData\backups\supabase-final_careercloud_<time>.dump
   ```
   It does a restore test, the verified restore, the production stamp and the **provider-credential check**, then starts the dashboard. **The worker stays off.**
4. Run `sanagtm migrate verify <manifest>`, then check your data in the browser. Then `sanagtm start`.

**The WAD job monitor after an import.** The imported data includes any job-monitor run that was open in the old installation. A full sweep that was **paused** there stays open here. While it is open, no scheduled run of that monitor starts, and each skipped day raises a "scheduled run skipped" notification. Before `sanagtm start`, decide one of the following:
- **Resume it:** Control Room › Automation › *Resume task*, or `POST /api/v1/w/<workspace>/tasks/<task id>/resume`. It continues from its checkpoint page.
- **Cancel it:** open the run and choose *Cancel run*. Stored jobs are kept, and a cancelled sweep closes nothing.

The daily incremental run reads at most 200 pages (about 4,800 jobs). After a long gap, one completed full sweep is what brings the job list back in line with the source.

## Rollback

- **Program update:** `sanagtm rollback-update`. If the update applied a migration, also run `sanagtm restore <pre-update dump> -Live`.
- **Bad data change:** `sanagtm restore <dump> -Live`. It takes a safety backup first.
- **Migration:** until you switch over, the hosted database is untouched. To go back, stop this machine (`sanagtm stop`) and re-enable the previous installation.

## Verification

This package was tested on Windows 11 without administrator rights. The results are in `docs\self-hosting-audit\WINDOWS_PACKAGE.md`.
