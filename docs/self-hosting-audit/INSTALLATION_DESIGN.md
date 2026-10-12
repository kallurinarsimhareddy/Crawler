# Installation and management design

**This document is a design.** It describes what to build. No VM, installer or script described here exists yet.

## Environment checked first

- The host is Windows 11 Home on an i3-12100 with 32 GB RAM and 177 GB free on `E:`. The account is a standard user.
- VMware, Docker and WSL are not installed, and **no VM exists**.
- The design therefore targets a **Linux guest (Ubuntu Server 24.04 LTS) under VMware Workstation Pro**. The only Windows-side steps are installing VMware (admin, once) and an optional backup copy job.

## The experience

```
1. (admin, once)  Install VMware Workstation Pro; create VM from the documented settings
                  (or import the provided sanagtm-vm.ovf template when we publish one)
2. In the VM:     curl -fsSL https://raw.githubusercontent.com/<org>/Crawler/<tag>/deploy/vm/bootstrap.sh -o bootstrap.sh
                  less bootstrap.sh            # read it first
                  sudo bash bootstrap.sh       # installs docker + git, clones the release tag, runs `sanagtm install`
3. Guided prompts (only what is required):
     • Public API hostname            e.g. api.example.com
     • Cloudflare tunnel token        (paste; hidden input)
     • Auth mode                      [supabase] (Option A) | gotrue (Option B, later)
     • Supabase project URL           https://<ref>.supabase.co      (Option A only)
     • Frontend origin                https://sanagtm.pages.dev
     • Existing CAREERCLOUD_PLATFORM_SECRETS_KEY   (paste to keep stored provider keys readable;
                                                   leave blank on a fresh install → generated)
     • Restore from a dump?           path to .dump or "no"
     • Admin email                    the account that will own the first workspace
4. Installer then automatically:
     • writes /etc/sanagtm/sanagtm.env (0600) — generates DB passwords, never echoes secrets
     • formats/mounts the data disk at /srv/sanagtm (asks before touching any disk that has data)
     • docker compose build && up -d postgres; waits for healthy
     • python -m cloud.db.migrate apply  (or restore + verify, see MIGRATION_PLAN.md)
     • starts api + worker + cloudflared; installs backup + log-rotation timers
     • creates the first workspace for the admin on first sign-in (create_workspace())
       — Option A: the admin signs in with their existing Supabase account; no password ever typed into the installer
     • runs `sanagtm health` and prints:
           SANA GTM is running
           App:     https://sanagtm.pages.dev
           API:     https://api.example.com/api/v1/health  ✓
           DB:      PostgreSQL 17 · 452 MB · 91 tables · RLS on ✓
           Worker:  online · queue postgres · 0 queued
           Backups: nightly 02:30 → /srv/sanagtm/backups (next in 7 h)
```

## Files to add (implementation phase)

```
deploy/vm/
  bootstrap.sh            # apt prerequisites, docker engine from the official repo, clone tag, call sanagtm install
  compose.yaml            # postgres:17, api, worker, cloudflared, (optional) playwright worker profile
  Dockerfile              # python:3.12-slim + cloud/api/requirements.txt + intel extras; non-root user
  sanagtm                 # the CLI below (bash), installed to /usr/local/bin
  postgresql.conf.d/      # tuning from SELF_HOSTED_ARCHITECTURE.md
  systemd/                # sanagtm-backup.{service,timer}, sanagtm-restore-test.{service,timer}
  logrotate/sanagtm
  verify_copy.py          # row counts / checksums / orphans / RLS probe (MIGRATION_PLAN stage 3)
```

`compose.yaml` outline:

```yaml
name: sanagtm
services:
  postgres:
    image: postgres:17
    env_file: /etc/sanagtm/postgres.env
    volumes: [ "/srv/sanagtm/pgdata:/var/lib/postgresql/data",
               "./postgresql.conf.d:/etc/postgresql/conf.d:ro" ]
    healthcheck: { test: ["CMD", "pg_isready", "-U", "postgres"], interval: 10s }
    restart: unless-stopped                 # no "ports:" — never published
  api:
    build: { context: ../.., dockerfile: deploy/vm/Dockerfile }
    command: uvicorn cloud.api.main:app --host 0.0.0.0 --port 8100 --proxy-headers
    env_file: /etc/sanagtm/sanagtm.env
    volumes: [ "/srv/sanagtm/files:/srv/sanagtm/files", "/srv/sanagtm/run:/srv/sanagtm/run" ]
    depends_on: { postgres: { condition: service_healthy } }
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8100/api/v1/health')"] }
    restart: unless-stopped
  worker:
    build: { context: ../.., dockerfile: deploy/vm/Dockerfile }
    command: python -m cloud.intel.tasks.worker --concurrency 2 --state-file /srv/sanagtm/run/worker-state.json
    env_file: /etc/sanagtm/sanagtm.env
    volumes: [ "/srv/sanagtm/files:/srv/sanagtm/files", "/srv/sanagtm/run:/srv/sanagtm/run" ]
    depends_on: { postgres: { condition: service_healthy } }
    stop_grace_period: 60s                   # lets the worker release its task (attempt refunded)
    restart: unless-stopped
  cloudflared:
    image: cloudflare/cloudflared:latest     # pin a version in the real file
    command: tunnel --no-autoupdate run
    environment: [ "TUNNEL_TOKEN_FILE=/run/secrets/tunnel_token" ]
    restart: unless-stopped
```

## The `sanagtm` command

| Command | What it does | Safety |
|---|---|---|
| `sanagtm install` | Guided first install (above) | Refuses if `/srv/sanagtm/pgdata` already has data, unless `--reuse-data` |
| `sanagtm start` / `stop` / `restart [service]` | `docker compose up -d` / `stop` / `restart` | `stop` waits for the worker's grace period |
| `sanagtm status` | Containers, `/api/v1/health`, `/status` (worker presence, queue depth), DB size, last backup age, disk free | read-only |
| `sanagtm health` | `status` + schema check + RLS check + tunnel reachability from outside (`https://api.<domain>/api/v1/health`) | read-only; non-zero exit on failure |
| `sanagtm logs [api\|worker\|postgres\|tunnel] [-f] [--since 1h]` | `docker compose logs` (json-file driver, 10 MB × 5 per container) | |
| `sanagtm update [--to <tag>]` | Backup → `git fetch` + checkout tag → build → `migrate status` shows pending → **asks** → migrate apply → restart → health. On failure: checkout the previous tag, restore the pre-update dump if a migration ran | Always takes a backup first |
| `sanagtm backup [--label x]` | `pg_dump -Fc` + tar of `/srv/sanagtm/files` + a copy of `sanagtm.env` encrypted with a passphrase → `/srv/sanagtm/backups/YYYY-MM-DD_HHMM/`; writes `MANIFEST` (sizes, sha256, row counts) | |
| `sanagtm restore <dir> [--into scratch]` | Default: restore into a **scratch** database and verify (counts, checksums). `--into live` requires typing the hostname, stops the worker, and takes a backup of current live data first | Never overwrites live data without a fresh backup + explicit confirmation |
| `sanagtm capacity` | DB size per category (the `db_stats.py` logic), largest tables, dead tuples, WAL size, disk free per mount, growth over the last 7/30 days (from a daily `capacity.csv` the timer appends), days until 80% full | read-only |
| `sanagtm uninstall` | Stops and removes containers, images and timers. **Keeps** `/srv/sanagtm` (data, backups) and `/etc/sanagtm`. `--purge-data` additionally requires typing `DELETE <hostname>` **and** refuses unless a backup from the last 24 h exists and was copied off the VM | Data survives an uninstall by default |

## Native (no Docker) variant

The same CLI, with different internals:

- PostgreSQL 17 comes from the PGDG apt repository.
- The app runs from `/opt/sanagtm/app` with a venv at `/opt/sanagtm/venv`.
- systemd units `sanagtm-api.service`, `sanagtm-worker.service` and `cloudflared.service` replace the containers. They use `Restart=always`, `User=sanagtm`, `ProtectSystem=strict` and `ReadWritePaths=/srv/sanagtm`.
- Logs go to journald.

Use this variant if Docker's roughly 150 MB overhead or the image builds become a problem.

## Windows-side helpers (optional)

- `deploy\windows\vm\backup-copy.ps1` runs from a per-user scheduled task (no admin needed). It pulls `/srv/sanagtm/backups` from the VM over SSH with `scp`, a key-only account and a read-only backups user, then copies it to another disk or USB.
- VMware autostart: on Workstation Pro this needs the "Shared VMs/auto-start" feature or a logon task running `vmrun start E:\VMs\sanagtm\sanagtm.vmx nogui`. Because the user is standard and there is no autologon, **the VM starts at user logon, not at power-on**. This is the same limit as today. An admin could configure a boot-time task.

## Health checks and recovery after restarts

- All containers use `restart: unless-stopped`, and docker starts at VM boot.
- The worker's `maintain()` reaps expired leases (120 s lease) on start, so interrupted tasks resume from their checkpoints, as verified on the job monitor 2026-10-01/02.
- The API refuses to boot with pending migrations (boot schema check). `sanagtm update` therefore runs migrations before restarting.
- `sanagtm-health.timer` (every 5 min) runs `sanagtm health --quiet` and writes `/srv/sanagtm/run/health.json`. An optional push to a free notification channel can be added later.
