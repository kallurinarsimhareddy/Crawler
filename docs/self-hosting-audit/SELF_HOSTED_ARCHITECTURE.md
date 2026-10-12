# Self-hosted target architecture

## Facts the design starts from (verified 2026-10-09)

- **No VM exists yet.**
  - The PC runs Windows 11 Home Single Language, with an i3-12100 (4 cores / 8 threads), 32 GB RAM and one 512 GB NVMe.
  - `C:` has 6 GB free and `E:` has 177 GB free.
  - VMware, Docker and WSL are not installed.
  - VBS (a hypervisor) is running. VMware Workstation 17.5+ runs on top of it through Windows Hypervisor Platform, with some performance cost.
  - The Windows account is a **standard user**. Installing VMware Workstation needs an administrator once.
- **The application is already queue-independent of Redis.** Production has run on `CAREERCLOUD_QUEUE=postgres` since 2026-10-06. All 23 task kinds have handlers in `cloud/intel/tasks/worker.py:46-70`, and none uses Redis.
- **The scheduler lives inside the worker** (`maintain()` every 30 s). Nothing external (cron) is needed, but the worker must run 24/7.
- **Supabase coupling is Auth only** (GoTrue: password sign-in, sign-up with email confirmation, password reset, and the JWKS the API verifies). The schema, RLS, `auth.uid()` and SECURITY DEFINER functions already run on plain PostgreSQL: the migration 0001 shim, plus `test_rls.py` on the embedded pgserver.

## Target (recommended)

```
                 Internet
                    │
  ┌─────────────────┴──────────────────┐
  │ Cloudflare (free)                  │
  │  • Pages: sanagtm.pages.dev (SPA)  │
  │  • Named Tunnel → api.<your-domain>│ (stable hostname: no more rebuild per restart)
  │  • (optional) Access policy        │
  └─────────────────┬──────────────────┘
                    │ outbound-only tunnel (no inbound ports opened on the router/PC)
  Windows host ─ VMware Workstation ─ Ubuntu Server 24.04 LTS VM  (on E:\VMs\sanagtm)
  ┌────────────────────────────────────────────────────────────────────────────┐
  │  docker compose project "sanagtm"  (restart: unless-stopped)               │
  │   cloudflared ──► api (uvicorn :8100, internal network only)               │
  │                   worker (cloud.intel.tasks.worker, scheduler inside)      │
  │                   postgres:17 (internal network only, never published)     │
  │                   backup (nightly pg_dump → /srv/sanagtm/backups)          │
  │  disk 1 (sda) OS + images        disk 2 (sdb) /srv/sanagtm: pgdata, files,  │
  │                                  backups, logs                              │
  └────────────────────────────────────────────────────────────────────────────┘
  Off-VM copy: backups rsync'd/robocopy'd to a Windows folder on another physical disk (or USB)
```

**Components:**

- **PostgreSQL 17** (the same major version as Supabase today, 17.6). It listens only on the compose network, and the port is never published.
  - The data directory sits on its own virtual disk, so the OS disk can be rebuilt without touching data.
- **API.** The existing FastAPI app, unchanged, reached only through `cloudflared`.
- **Worker.** One process, with concurrency set by `CAREERCLOUD_PLATFORM_CONCURRENCY` (2 recommended). It requires defect D5 to be fixed first; until then, set it on the command line.
  - The PG queue uses `LISTEN/NOTIFY`. Local PostgreSQL is a direct session connection, which is better than the Supabase pooler.
- **cloudflared** with a **named tunnel** (a token in `.env`) and a stable hostname such as `api.<domain>`. This removes `sync-frontend.ps1`, the per-restart frontend rebuild and the 6-deploys-per-day cap.
- **Frontend** stays on Cloudflare Pages. It is rebuilt once, with `VITE_API_URL=https://api.<domain>`, and its CSP keeps `connect-src` for the API and for Supabase Auth (phase 1).
- **Files.** `CAREERCLOUD_PLATFORM_FILES_DIR=/srv/sanagtm/files`, plus a retention job for exports and import copies (fix D7).
- **Backups.** A nightly `pg_dump -Fc` plus a files tarball, with 14 daily, 8 weekly and 6 monthly copies, a weekly automated restore test, and a copy off the VM. See `SECURITY_AND_BACKUP.md`.

## Why Docker Compose inside a Linux VM (and not the alternatives)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Ubuntu VM + Docker Compose** | One file describes everything. Same versions every install. `docker compose up -d` / `down` / `pull`. Isolated from Webroot, which deleted Python and bash from Windows on 2026-09-29, and from the full `C:` drive. Easy to move to any Linux box later. | About 150 MB RAM for dockerd. Images take 1–2 GB of disk; Playwright's image is about 1.5 GB more if enabled. Docker adds one more layer to learn. | **Recommended** |
| Ubuntu VM, native (apt PostgreSQL + venv + systemd) | Lightest; journald logs; no image builds | More steps in the installer; OS upgrades can move the PostgreSQL or Python version underneath you; harder to reproduce | Good fallback. The installer design supports both because the CLI is the same. |
| Docker Desktop on Windows | No VM to manage | Needs admin + WSL2; licence terms for business use; ties the app to the Windows box with Webroot and a full `C:` | Not recommended |
| Stay native on Windows + local PostgreSQL for Windows | No virtualization at all; the smallest change from today | Windows services need admin; Webroot interference; `C:` is full; the supervisor is PowerShell scheduled-task glue | Possible stop-gap only |

**VM sizing.** Disk numbers come from `STORAGE_AND_GROWTH.md` §6.

| Scenario | vCPU | RAM | Disk 1 (OS) | Disk 2 (data) |
|---|---|---|---|---|
| A. Minimum | 2 | 4 GB | 20 GB | 20 GB |
| **B. Full current (recommended)** | **4** | **8 GB** | **30 GB** | **80 GB** (thin, grows) |
| C. Millions of jobs | 4 (all physical cores) | 12–16 GB | 30 GB | 250–300 GB + external backup disk |

**Memory budget for B, about 8 GB.** The idle API (116 MB) and worker (239 MB) were measured on 2026-10-09. The 200k-row import added about 17 MB of RSS, and the 50k-row email validation about 24 MB.

| Use | Memory |
|---|---|
| Ubuntu + dockerd | about 0.8 GB |
| PostgreSQL `shared_buffers` 2 GB + work memory | about 3 GB |
| API | about 0.2 GB |
| Worker | 0.3–0.5 GB; +0.5–1 GB while a Playwright browser runs |
| Page cache | the rest |

**CPU.** The live API and worker together used about 90 CPU-seconds in 9 hours. Two vCPUs are enough for steady state; four help with full sweeps, imports and VACUUM. The host keeps 4 threads and 24 GB for Windows.

**PostgreSQL settings for 8 GB:**

```
shared_buffers=2GB  effective_cache_size=5GB  work_mem=16MB  maintenance_work_mem=512MB
max_wal_size=2GB  wal_compression=on  checkpoint_timeout=15min
autovacuum_vacuum_scale_factor=0.05 (job_postings, job_posting_changes)  max_connections=50
```

## Option A versus Option B

| | **A. Minimal-risk (recommended first)** | **B. Fully self-hosted** |
|---|---|---|
| Database | Moves to VM PostgreSQL | Moves to VM PostgreSQL |
| Queue | PostgreSQL (already live) | PostgreSQL |
| Auth | **Stays on Supabase Auth (Free)**. The API keeps verifying the JWKS at `<project>.supabase.co/auth/v1`. User UUIDs are unchanged, so `workspace_members.user_id` and every `created_by` stay valid. | Replaced by either (B1) self-hosted **GoTrue** (`supabase/auth` container on the VM's PostgreSQL), or (B2) **app-native auth** (users table + argon2 + own JWT issuer) |
| Code changes | Essentially none: env values + rebuild the frontend with the new API URL. Optional: fix D4 to run as `production`. | B1: config + serve GoTrue under `/auth/v1`, relax `build-staging.mjs:22` and the CSP, migrate `auth.users` with password hashes. B2: about 300–600 lines (backend endpoints, a third `CAREERCLOUD_AUTH_MODE`, replace the 5 supabase-js calls in `AuthProvider.tsx`), plus a password-reset email path, which needs SMTP |
| Recurring cost | $0 | $0 |
| Risk | Low; rollback = point `DATABASE_URL` back at Supabase | Medium: sign-in, sessions, reset emails and invite acceptance all change |
| Remaining external dependency | Supabase Auth, Cloudflare | Cloudflare (+ an SMTP sender for password resets) |

**Recommendation.** Do **A** now. It removes the 500 MB cap risk, Upstash, the quick-tunnel churn and the Supabase DB bill with almost no code change.

Then do **B1 (self-hosted GoTrue)** as a separate project. B1 keeps the frontend's supabase-js calls and the JWT shape, so the API verifier works by pointing `CAREERCLOUD_SUPABASE_URL` at it. This is less custom code than B2, and existing password hashes can be imported, so nobody has to reset their password.

Choose B2 only if GoTrue's operational weight (another service plus its own schema migrations) proves annoying.

## What changes in configuration (Option A)

| Variable | Today | VM |
|---|---|---|
| `CAREERCLOUD_DATABASE_URL` | Supabase session pooler, `sslmode=require` | `postgresql://sanagtm_app@postgres:5432/sanagtm`. Compose-internal network; `sslmode` is only enforced when deployed (`deployment.py:51-56`). Running as `production` needs either TLS on PostgreSQL or a code change for loopback/compose-internal hosts. |
| `CAREERCLOUD_QUEUE` | `postgres` | `postgres` |
| `CAREERCLOUD_REDIS_URL`, `CAREERCLOUD_QUEUE_PREFIX` | set (unused) | **removed** |
| `CAREERCLOUD_AUTH_MODE` / `CAREERCLOUD_SUPABASE_URL` | `supabase` / project URL | unchanged |
| `CAREERCLOUD_PLATFORM_FILES_DIR`, `CAREERCLOUD_RESULTS_DIR` | `E:\…` | `/srv/sanagtm/files`, `/srv/sanagtm/results` |
| `CAREERCLOUD_CORS_ORIGINS` | `https://sanagtm.pages.dev` | unchanged |
| `CAREERCLOUD_TRUST_PROXY` | set | set (cloudflared is the proxy) |
| `CAREERCLOUD_PLATFORM_SECRETS_KEY` | set | **copy the same value**: it decrypts the provider credentials stored in `provider_connections` (Fernet) |
| `CAREERCLOUD_ENV` | `development` | `staging` at first. `production` needs D4 fixed (and TLS or a loopback exemption). |
| `CAREERCLOUD_WORKER_STATE_FILE` | `logs/sana-gtm/worker-state.json` | `/srv/sanagtm/run/worker-state.json`, shared by the API and the worker so `/status` sees the worker |
| `TUNNEL_TOKEN` (new) | — | named-tunnel token (secret) |
| `VITE_API_URL` (build) | quick-tunnel URL | `https://api.<domain>` |

## Security posture of the target

- PostgreSQL has no published port.
- The API listens only on the compose network, and the only ingress is the outbound Cloudflare tunnel. No router port-forwarding is needed.
- The VM's SSH is on a host-only adapter, or LAN with key-only authentication. Optionally, admin routes sit behind Cloudflare Access.
- Secrets live in `/etc/sanagtm/sanagtm.env` (mode 600, root-owned), outside the git checkout. Nothing is baked into images or the frontend; the Supabase anon key is public by design.
- Details are in `SECURITY_AND_BACKUP.md`.
