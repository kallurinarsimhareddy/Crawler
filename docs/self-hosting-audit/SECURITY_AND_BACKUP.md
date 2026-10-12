# Security, backup and recovery

## 1. Current gaps (verified)

| # | Gap | Evidence | Severity |
|---|---|---|---|
| S1 | **No database backups exist for production**, apart from whatever the Supabase plan includes (unknown plan) | no `pg_dump` in the repo; no backup job | **High** |
| S2 | The only local dumps are of the research DB (`E:\Crawlers\sana-backups\sana_wad_pre_0013_2026-10-02.dump`), on the **same physical NVMe** as everything else | disk inventory | High |
| S3 | Production runs with `CAREERCLOUD_ENV=development`, which skips the deployed-only checks (sslmode, resource registry, service-role-key ban) | env names + `deployment.py` | Medium |
| S4 | The public API URL changes on every restart (quick tunnel); the frontend is rebuilt automatically with wrangler credentials stored on the PC | `sync-frontend.ps1` | Medium (availability) |
| S5 | The GitHub repo is **public** (intentional). Env files are git-ignored. Run a secret scan before every push (as done 2026-09-29/30) | memory + `.gitignore` | Medium |
| S6 | Logs grow without rotation while a process runs | `logs/sana-gtm/*.log` | Low |
| S7 | Provider credentials are Fernet-encrypted in the DB with `CAREERCLOUD_PLATFORM_SECRETS_KEY`. **Losing that key makes stored provider keys unreadable; leaking it, together with a DB dump, exposes them** | `providers/registry.py:100-105` | High (key handling) |
| S8 | Webroot SecureAnywhere has deleted Python, bash and build output on this host before | 2026-09-29 incident | Medium (host stability) |

## 2. Security design for the VM

- **Network.**
  - The only ingress is the outbound Cloudflare named tunnel to the API container.
  - PostgreSQL has no published port. The VM firewall (`ufw default deny incoming`) allows SSH only from the host-only or LAN subnet.
  - No router port-forwarding.
- **TLS.**
  - Cloudflare terminates TLS for `api.<domain>`.
  - The cloudflared-to-API hop is on the compose network inside the VM.
  - If the DB is ever reached from outside the VM, use `sslmode=verify-full` with a real certificate.
- **Secrets.**
  - `/etc/sanagtm/sanagtm.env` and `postgres.env` are root:root 0600.
  - The tunnel token is a Docker secret file.
  - Nothing goes in git, images or the frontend bundle. The frontend contains only the API URL and the public Supabase anon key.
  - The installer generates DB passwords and never prints them.
  - Variable names (no values) are listed in §5.
- **DB roles.**
  - `sanagtm_owner` owns the schema and runs migrations.
  - `sanagtm_app` is the login role used by the API and worker. It is a member of `authenticated`, which RLS needs for user-scoped calls, and it uses `SET LOCAL ROLE`.
  - Migration and system tasks need table ownership today (system calls bypass RLS as the owner). Keep `owner = app login` at first, exactly as on Supabase. Splitting the roles is a later hardening item.
- **Auth.**
  - Option A keeps Supabase JWT verification (JWKS, `aud=authenticated`, `iss` checked).
  - No service-role key exists on the VM.
- **Admin access.**
  - SSH keys only, no password login.
  - Optionally, Cloudflare Access (free for up to 50 users) in front of `/api/v1/admin/*`.
- **Updates.**
  - `unattended-upgrades` for Ubuntu security patches.
  - App updates only through `sanagtm update`, which takes a backup first.
  - Pin image tags.
- **Outbound policy.**
  - Crawlers need general HTTPS egress.
  - Email validation needs DNS (UDP/TCP 53).
  - SMTP preflight needs outbound 25. It is off by default, and many ISPs block it.

## 3. Backups

| What | How | Schedule | Retention | Size (E) |
|---|---|---|---|---|
| Database | `pg_dump -Fc` (consistent snapshot, no locks that block writers) | nightly 02:30 | 14 daily, 8 weekly, 6 monthly | about 5–8% of the live DB: about 30 MB now, about 0.5 GB at 8 GB live |
| Files | tar + zstd of `/srv/sanagtm/files` | nightly | same | 0.1–1 GB |
| Config | `sanagtm.env`, encrypted with a passphrase (age/gpg) | on change + nightly | same | KB |
| **Secrets key** | `CAREERCLOUD_PLATFORM_SECRETS_KEY` kept **separately** in a password manager | once | forever | — |
| Off-VM copy | Windows task pulls the newest set to a second physical disk or USB (not `E:` of the same NVMe) | nightly | 30 days | — |
| Optional PITR | WAL archiving (`archive_command`) to the backup disk | continuous | 7 days | about 1–3 GB |

**Restore test.** A weekly timer restores the newest dump into a scratch database, runs `verify_copy.py` (counts, checksums, RLS on), records the result in `health.json`, and drops the scratch DB. A backup is "good" only after it has passed a restore test.

**Recovery objectives.**
- With nightly dumps: RPO ≤ 24 h and RTO about 30 min (fresh VM, install, restore).
- With WAL archiving: RPO ≤ 5 min.

## 4. Disaster scenarios

| Scenario | Recovery |
|---|---|
| Bad update or migration | `sanagtm update` took a pre-update dump. Check out the previous tag and `sanagtm restore --into live` |
| Accidental deletion in the app | Restore the latest dump into scratch and copy the rows back (audit_log helps find them) |
| VM corruption | New VM from the installer → `sanagtm install --restore <dump>` |
| Host NVMe failure | Only the **off-host** copy survives. This is why the off-VM copy must go to a different physical disk or cloud |
| Lost secrets key | Stored provider credentials must be re-entered (the data is otherwise intact) |
| Tunnel or Cloudflare outage | The app is unreachable from outside, and nothing is lost. LAN access via the VM IP can be added for emergencies |

## 5. Required environment variables (names only)

- **Secrets:**
  - `CAREERCLOUD_DATABASE_URL`, `CAREERCLOUD_PLATFORM_SECRETS_KEY`, `TUNNEL_TOKEN`, `POSTGRES_PASSWORD` (VM only)
  - optional: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`, `ZOOMINFO_CLIENT_ID`/`_SECRET`, `CAREERCLOUD_SMTP_PASSWORD`, `CAREERCLOUD_UNSUBSCRIBE_SECRET`, `CAREERCLOUD_WEBHOOK_SIGNING_SECRET`, `CAREERCLOUD_INBOUND_WEBHOOK_SECRET`, OAuth client secrets
  - Option B: `CAREERCLOUD_SUPABASE_JWT_SECRET` or a GoTrue JWT secret
- **Required non-secret:**
  - `CAREERCLOUD_ENV`, `CAREERCLOUD_STORAGE=postgres`, `CAREERCLOUD_QUEUE=postgres`, `CAREERCLOUD_AUTH_MODE`, `CAREERCLOUD_SUPABASE_URL`, `CAREERCLOUD_CORS_ORIGINS`, `CAREERCLOUD_TRUST_PROXY`
  - `CAREERCLOUD_PLATFORM_FILES_DIR`, `CAREERCLOUD_RESULTS_DIR`, `CAREERCLOUD_WORKER_STATE_FILE`, `CAREERCLOUD_DB_USER_ROLE`, `CAREERCLOUD_DB_POOL_MAX`, `CAREERCLOUD_ALLOW_REMOTE_SERVICES`
- **No longer needed on the VM:** `CAREERCLOUD_REDIS_URL`, `CAREERCLOUD_QUEUE_PREFIX`; Pages Function `UPSTASH_REDIS_REST_URL`/`_TOKEN`, `SANA_API_ORIGIN_KEY`, `SANA_API_HOSTS`.
- **Frontend build:** `VITE_API_URL`, `VITE_AUTH_MODE`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` (public), `VITE_DEPLOY_ENV`.

## 6. Operational risks to watch

- **Disk.** `sanagtm capacity` alerts at 80% of the data disk. Thin-provisioned VMDKs do not shrink automatically, so watch host `E:` free space too.
- **Unbounded tables.** `job_postings`, `job_posting_changes`, `audit_log`, `ai_usage`, `email_check_cache` and the stored uploads have no retention. Add retention policies before scenario C.
- **Paid providers.** The credit ledger refuses when the balance is unknown and needs explicit confirmation for paid runs. Keep it that way on the VM.
- **Crawling etiquette.** Keep one crawler per host (enforced in `job_monitor/service.py:300-306`). JobSpy stays disabled.
