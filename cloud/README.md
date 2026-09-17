# CareerCloud: the control plane

CareerCloud puts CareerCrawler on the web. A signed-in user enters a company or
a list, clicks **Run crawl**, watches each company progress live, and downloads
the results as Excel, CSV or JSON.

**Status: Phase 5B.** The following are in place: PostgreSQL/Supabase
persistence with row-level security, Supabase Auth, a Redis job queue, a
separate worker process with leases, heartbeats, bounded retries and crash
recovery, and a runner that crawls through the **existing** CareerCrawler engine.
Nothing is deployed yet. See [Phase 5C](#phase-5c-deployment-checklist).

- [Architecture](#architecture)
- [Isolation from production CareerCrawler](#isolation-from-production-careercrawler)
- [Database](#database)
- [Queue and worker lifecycle](#queue-and-worker-lifecycle)
- [Authentication and multi-tenancy](#authentication-and-multi-tenancy)
- [API](#api)
- [Local development](#local-development)
- [Tests](#tests)
- [Known limits](#known-limits)
- [Phase 5C deployment checklist](#phase-5c-deployment-checklist)

---

## Architecture

```
Browser ── React dashboard (cloud/web) ── Supabase Auth (sign-in, access token)
   │  Authorization: Bearer <access token>
   ▼
FastAPI (cloud/api) ── verifies JWT ── JobService ── PostgresJobRepository ──► PostgreSQL / Supabase
   │                                         (user-scoped: SET ROLE authenticated + RLS)
   │ 1. insert job row (committed)
   │ 2. enqueue job id
   ▼
Redis queue (cloud/shared/queue.py) — ready LIST, delayed ZSET, in-flight ZSET, Lua scripts
   ▼
Worker process (python -m cloud.worker)
   ├─ Worker: reserve → JobExecutor → ack;  maintenance: reap stale leases, re-enqueue orphans, sweep workspaces
   ├─ JobExecutor: atomic claim → heartbeat thread → runner → results → fenced finish / bounded retry
   └─ CareerCrawlerRunner (cloud/worker/careercrawler_runner.py)
          └─ crawler.crawler_engine.CrawlerEngine.crawl_company()   ← the existing engine, unmodified
                 seed selection · careers-page discovery · platform detection · adapters
   ▼
Result files → ObjectStorage (LocalFileStorage now; Supabase Storage/S3 in 5C) + job_results rows
```

| Layer | Module | Notes |
|---|---|---|
| Domain | `shared/models.py`, `shared/schemas.py` | Frozen models and transition table. Requests validated and websites normalised, with non-public hosts refused. |
| Service | `shared/service.py` | The only code that changes a job. It has user actions (owner-scoped), worker actions (fenced on worker and attempt), and recovery (`reap_stale`). |
| Repository | `shared/repository.py` (interface + in-memory), `db/postgres.py` | Same contract, tested against both. |
| Queue | `shared/queue.py` | `JobQueue` interface with `InMemoryJobQueue` and `RedisJobQueue` implementations. |
| Storage | `shared/storage.py` | `ObjectStorage` interface with a `LocalFileStorage` implementation. |
| Auth | `api/auth.py` | `SupabaseTokenVerifier` (JWKS + legacy HS256) and `DevTokenIssuer`. |
| Worker | `worker/worker.py`, `worker/executor.py`, `worker/results.py`, `worker/workspace.py` | Queue loop, execution, result files, and per-attempt scratch space. |
| Runner | `worker/runner.py`, `worker/careercrawler_runner.py`, `worker/fake_runner.py` | `JobRunner` interface, the engine adapter, and a simulated runner. |
| Migrations | `db/migrations/*.sql`, `db/migrate.py` | Checksummed, advisory-locked, applied once. |

## Isolation from production CareerCrawler

The production crawler, meaning the weekly run, `state/crawler.db`, its checkpoint, the
Google Sheet and its 6-worker default, is untouched. It is protected in several
layers:

1. **One adapter module.** `cloud/worker/careercrawler_runner.py` is the only
   cloud module allowed to import the crawler. It may import only
   `crawler.crawler_engine`, `utils.http`, `utils.browser`,
   `exporters.excel_exporter`, `models.job` and `config.settings`.
   `tests/test_isolation.py` fails the build if any cloud module imports
   `store`, `sheets`, `crawler.weekly_run`, `crawler.checkpoint`,
   `crawler.sync`, `sqlite3` or a Google client. It also checks at runtime that
   importing the API loads no crawler module and that the engine loads none of
   those.
2. **No SQLite, ever.** `db/connection.py` accepts only `postgresql://` URLs, so
   a mis-set variable cannot point CareerCloud at `state/crawler.db`. A test
   runs a real cloud job in a subprocess while spying on `open()`, and asserts
   that no `.db`, checkpoint or `secrets/` file is opened and `sqlite3` is never
   imported.
3. **Private scratch space.** Each attempt gets
   `cloud/runtime/<job_id>/attempt-<n>/{state,output,logs}`. The runtime root is
   refused if it overlaps the crawler's `state/`, `output/`, `input/` or
   `secrets/`, and job ids are regex-checked before becoming path segments. The
   workspace is deleted after the attempt. A new attempt removes a dead
   attempt's leftovers, and maintenance sweeps workspaces left by killed
   workers.
4. **Process-wide crawler settings are cloud-safe.** The worker sets the
   crawler's `SETTINGS` once for its own process. Diagnostics are off, and
   `output_dir`/`diagnostics_dir` point inside the cloud runtime root.
   `max_workers` is never touched.
5. **Separate venv and dependencies.** `cloud/.venv` and
   `cloud/*/requirements.txt`. The worker omits the Google clients.
6. **Tests prove it.** They check that the crawler's `state/`, `output/`,
   `input/` and `secrets/` directories are byte-for-byte unchanged after every
   runner test, and that `SETTINGS` is restored.

`weekly_crawl` and `discovery` jobs are **not executed** by the cloud runner.
The production roster run is driven by Sheets and must stay where it is. The API
records such jobs with `runnable: false` and the message "not supported by the
cloud runner yet; this job will not start".

## Database

Schema `careercloud`, created by `db/migrations/0001_careercloud_core.sql`.
Supabase's Data API does not expose this schema by default.

| Table | Purpose | Key columns |
|---|---|---|
| `jobs` | One row per job | `id` (`job_<32 hex>`), `owner_id uuid`, `type`, `status`, `target_count`, `created_at/updated_at/started_at/completed_at`, `error`; **progress:** `total_companies`, `completed_companies`, `failed_companies`, `jobs_found`, `current_company`, `current_phase`, `progress_message`; **execution:** `attempts`, `max_attempts`, `worker_id`, `heartbeat_at`, `lease_expires_at`, `cancel_requested_at` |
| `crawl_targets` | One row per company in a job | `(job_id, position)`, `website`, `company_name`, `status` (pending/running/completed/failed/skipped), `platform`, `outcome`, `jobs_found`, `error`, timestamps |
| `job_events` | The job's timeline | `kind` (created, claimed, retry_scheduled, reaped_requeued, cancel_requested, completed…), `attempt`, `message`, `data jsonb` |
| `job_results` | Result file metadata | `id` (`res_<32 hex>`), `(job_id, owner_id)` FK to `jobs`, `kind` (unique per job), `filename`, `content_type`, `storage_key`, `size_bytes`, `sha256`, `row_count` |
| `schema_migrations` | Applied migrations | `version`, `checksum`, `applied_at` |

**Enforced by the database, not just the code:**

- CHECK constraints on ids, statuses, lengths and storage keys (no `..`, no
  absolute paths). A running job must hold a lease, a finished job must have
  `completed_at`, and `attempts` may not exceed `max_attempts`.
- The `guard_job_update` trigger refuses illegal status moves for every role,
  including the table owner. It also makes `id`, `owner_id`, `type`,
  `created_at` and `target_count` immutable.
- A composite foreign key forces a result's `owner_id` to equal its job's owner.
- **Row-level security** on all four tables. `authenticated` users can:
  - `SELECT` only rows where `owner_id = auth.uid()`;
  - `INSERT` only their own jobs, and only as fresh `queued` jobs;
  - `UPDATE` only to cancel (column-level grants cover only status,
    completion and progress columns, never `attempts`, `worker_id`, leases or
    `error`);
  - never write results;
  - never delete.

  The `anon` role gets nothing.
- On plain PostgreSQL the migration creates Supabase-compatible `authenticated`,
  `auth.uid()` and grants. On Supabase those already exist and the guarded
  blocks do nothing.

## Queue and worker lifecycle

**PostgreSQL is the source of truth; Redis is a doorbell.** A claim is an
atomic update:
`UPDATE jobs SET status='running', attempts=attempts+1, worker_id=…, lease_expires_at=now()+lease WHERE id=… AND status='queued' AND cancel_requested_at IS NULL AND attempts < max_attempts`.
That one statement is what makes execution idempotent.

**Redis keys** (under `CAREERCLOUD_QUEUE_PREFIX`, e.g. `careercloud:production`):

| Key | Type | Holds |
|---|---|---|
| `ready` | LIST | FIFO ids |
| `delayed` | ZSET | ids waiting for a retry delay |
| `waiting` | SET | makes enqueue idempotent |
| `inflight` | ZSET | reserved ids with their visibility deadline |
| `deliveries` | HASH | delivery counts |

Every multi-key step is a Lua script.

```
API: insert job (queued) ─► enqueue id
Worker thread:
  reserve (visibility timeout) ─► claim in DB ──(not claimable: dup / cancelled / held / exhausted)──► ack, done
        │ claimed (attempt n, lease)
        ├─ heartbeat every lease/3: extend DB lease (fenced) + queue visibility; lease lost ⇒ stop, write nothing
        ├─ runner.run(job, context)  — context.update/target_*/is_cancelled (cancel, lost lease, shutdown)
        ├─ completed ─► write summary.json / jobs.csv / jobs.xlsx / crawl.log ─► storage + job_results ─► finish (fenced)
        ├─ runner returned failure (known cause) ─► failed
        ├─ exception / result upload failure ─► attempts < max ? queued + enqueue after 30s·2^(n-1) (cap 15 min) : failed
        ├─ cancel requested ─► cancelled
        └─ worker shutting down ─► queued, attempt refunded, re-enqueued
  ack
Maintenance (every 30 s, on every worker; all writes conditional):
  lease expired ─► cancel requested ? cancelled : attempts left ? requeued (+delay) : failed
  queued & untouched 10 min ─► enqueue again (idempotent), touch
  workspace untouched 6 h ─► delete
```

- **Bounded retries.** `max_attempts` is stored per job (default 3, maximum
  10) and capped again by the worker's policy. No path requeues a job without
  checking its attempt count.
- **Fencing.** Every worker write includes
  `AND worker_id = :me AND attempts = :claimed_attempt`. A worker that stalls,
  loses its lease and later wakes up cannot overwrite the job's new owner.
- **Cancellation.** A queued job is cancelled immediately. A running job gets
  `cancel_requested_at` and its worker stops between companies. A dead worker's
  job with a cancel request is cancelled by the reaper.

## Authentication and multi-tenancy

- **Browser.** Supabase JS signs the user in (email/password) and refreshes the
  session. Every API call sends `Authorization: Bearer <access_token>`. Downloads
  use `fetch` with the header, so tokens never appear in URLs.
- **API** (`api/auth.py`). Checks on every request:
  - signature, with the algorithm pinned to where the key came from: JWKS gives
    ES256/RS256, and HS256 is accepted only when a legacy secret is configured;
  - `exp`/`iat`/`aud`/`iss` required, with 30 s leeway;
  - `role = authenticated` (rejects anon and service_role tokens);
  - `is_anonymous` not true;
  - `sub` is a UUID.

  A failed check returns 401. Unconfigured auth or an unreachable JWKS returns
  503. Auth never fails open.
- **Ownership.** `sub` becomes `owner_id`. Every job endpoint passes it to the
  service. In PostgreSQL each user-scoped transaction runs
  `set_config('request.jwt.claims', …)` and `SET LOCAL ROLE authenticated`, so
  RLS applies as well as the explicit `owner_id` filter. Another user's job, its
  targets, events, results and downloads all return **404**, identical to a
  nonexistent id. A test asserts the API never calls the repository in system
  scope.
- **Abuse limits.** At most `CAREERCLOUD_MAX_ACTIVE_JOBS_PER_USER` queued or
  running jobs per user (429 beyond that). Bulk jobs are capped at 5,000
  companies.
- **Unsafe input.**
  - Websites must be http(s) on ports 80/443/8080/8443, with no credentials.
    Loopback, private, link-local, metadata, CGNAT, multicast and IPv4-mapped
    addresses are refused, including integer/hex spellings, as are
    `localhost` and `.internal`/`.local` names.
  - The worker re-resolves DNS and refuses a host if **any** address is
    non-public.
  - Downloads are by result id only. Storage keys come from the database,
    match a strict pattern and resolve inside the storage root.
  - Scraped cells starting with `= + - @` are neutralised in CSV and XLSX.
- **Development mode** (`CAREERCLOUD_AUTH_MODE=dev`, `VITE_AUTH_MODE=dev`). Sign
  in as any email; the API issues an HS256 token with issuer `careercloud-dev`.
  Settings refuse dev mode outside `development`/`test`, and the
  `/auth/dev-session` endpoint returns 404 in any other mode.

## API

Base `/api/v1`. Every endpoint except `health` and `auth/dev-session` requires a
bearer token.

| Method | Path | Result |
|---|---|---|
| GET | `/health` | Public. `{status, version, environment, runner, storage, queue, auth}` |
| GET | `/me` | `{user_id, email, auth_mode}` |
| POST | `/jobs` | 201 `{job_id, status}` · 422 invalid · 429 too many active jobs |
| GET | `/jobs?status=&limit=&offset=` | Caller's jobs, `total`, `counts` |
| GET | `/jobs/{id}` | Job with progress, `cancel_requested`, `attempts`, `elapsed_seconds`, `runnable` · 404 |
| POST | `/jobs/{id}/cancel` | Queued jobs are cancelled; running jobs get a cancel request · 404 · 409 already finished |
| GET | `/jobs/{id}/targets` | Per-company status, platform, jobs found, error |
| GET | `/jobs/{id}/events` | Timeline |
| GET | `/jobs/{id}/results` | Result files with `download_url` |
| GET | `/jobs/{id}/results/{result_id}/download` | Streams the file (`attachment`, `nosniff`, `no-store`) · 404 · 410 file missing |
| POST | `/auth/dev-session` | Development only: `{email}` returns a token |

## Local development

Everything runs on one Windows machine with **no Docker and no cloud account**.
The embedded PostgreSQL 16 comes from `pgserver`, and a Redis-compatible dev
server from `fakeredis`. Run the commands from the repository root
(`E:\Crawlers\CareerCrawler-cloud`).

### One-time setup

```powershell
py -3.12 -m venv cloud\.venv
cloud\.venv\Scripts\python -m pip install -r cloud\requirements-dev.txt
cd cloud\web; npm install; cd ..\..

copy cloud\api\.env.example    cloud\api\.env
copy cloud\worker\.env.example cloud\worker\.env
copy cloud\web\.env.example    cloud\web\.env.local
# put a random 32+ character value in CAREERCLOUD_DEV_JWT_SECRET in cloud\api\.env:
cloud\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Start (four terminals)

```powershell
# 1. PostgreSQL (embedded) + migrations. It keeps running in the background; stop with `... localpg stop`.
cloud\.venv\Scripts\python -m cloud.devtools.localpg start

# 2. Redis-compatible dev server on 127.0.0.1:6390
cloud\.venv\Scripts\python -m cloud.devtools.fake_redis

# 3. API on :8000
cloud\.venv\Scripts\python -m uvicorn cloud.api.main:app --reload --port 8000 --env-file cloud\api\.env

# 4. Worker (real CareerCrawler engine; set CAREERCLOUD_WORKER_RUNNER=fake for simulated runs)
cloud\.venv\Scripts\python -m cloud.worker --env-file cloud\worker\.env

# 5. Dashboard on :5173
cd cloud\web; npm run dev
```

Open http://localhost:5173 and sign in with any email (dev mode).

**Quickest UI-only mode.** Set `CAREERCLOUD_STORAGE=memory` and
`CAREERCLOUD_QUEUE=inline` in `cloud\api\.env`. The API then runs simulated jobs
itself, and needs no Postgres, Redis or worker.

**Migrations.**

```powershell
cloud\.venv\Scripts\python -m cloud.db.migrate status
cloud\.venv\Scripts\python -m cloud.db.migrate apply   # uses CAREERCLOUD_DATABASE_URL
```

### Using a real Supabase project and Redis

1. Create a Supabase project. Under **Authentication → Providers**, enable
   Email.
2. Run the migrations against it. Use the connection string from **Project
   Settings → Database**; the direct or session-pooler connection is needed
   because migrations use an advisory lock. Its host is remote, so opt in
   explicitly:

   ```powershell
   $env:CAREERCLOUD_ALLOW_REMOTE_SERVICES="1"
   cloud\.venv\Scripts\python -m cloud.db.migrate apply --database-url "postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres"
   ```

3. In `cloud\api\.env`, set:
   - `CAREERCLOUD_AUTH_MODE=supabase`
   - `CAREERCLOUD_SUPABASE_URL=https://<ref>.supabase.co`
   - `CAREERCLOUD_DATABASE_URL=<same URL>`
   - `CAREERCLOUD_ALLOW_REMOTE_SERVICES=1`
   - `CAREERCLOUD_SUPABASE_JWT_SECRET`, only for projects still on the legacy
     HS256 secret. New projects use the JWKS endpoint automatically.
4. In `cloud\web\.env.local`, set:
   - `VITE_AUTH_MODE=supabase`
   - `VITE_SUPABASE_URL=https://<ref>.supabase.co`
   - `VITE_SUPABASE_ANON_KEY=<anon/publishable key>`
5. For a real Redis, set `CAREERCLOUD_REDIS_URL=redis://…` or `rediss://…` on
   both the API and the worker. Give each environment its own
   `CAREERCLOUD_QUEUE_PREFIX`.

Never put the service-role key, database URL or Redis URL in a `VITE_*`
variable, and never give the service-role key to the API.

## Tests

```powershell
# CareerCloud: 258 tests (7 skipped without a real Redis). Real PostgreSQL 16 (embedded), Redis Lua via fakeredis, real crawler engine with fake adapters.
cloud\.venv\Scripts\python -m unittest discover -s cloud\tests -t .

# Dashboard: TypeScript check + production build
cd cloud\web; npm run build

# CareerCrawler's own suite, unchanged (use the crawler's venv)
venv\Scripts\python -m unittest discover -s tests
```

Optional environment variables:
- `CAREERCLOUD_TEST_DATABASE_URL` points the PostgreSQL tests at another
  disposable server.
- `CAREERCLOUD_TEST_REDIS_URL` also runs the queue contract against a real
  Redis. Those 7 tests are skipped otherwise.

| Area | File |
|---|---|
| Repository contract, in-memory **and** PostgreSQL | `test_repository_contract.py` |
| RLS, trigger, grants and migrations, in raw SQL | `test_rls.py` |
| JWT verification: expiry, signature, aud/iss, alg=none, algorithm confusion, anon/service roles | `test_auth.py` |
| Cross-user read, cancel and download through HTTP, on both backends | `test_ownership.py` |
| Queue contract, in-memory and Redis/Lua | `test_queue.py` |
| Retries, crash recovery, heartbeat expiry, fencing, idempotency, cancellation, orphans, shutdown (in-memory and PostgreSQL+Redis) | `test_worker.py` |
| Engine adapter, progress, formula neutralisation, workspace isolation, no SQLite | `test_careercrawler_runner.py` |
| SSRF, storage keys, connection and deployment guards | `test_safety.py` |
| Import allowlist and runtime import checks | `test_isolation.py` |
| Phase 5A suites (API, schemas, lifecycle, runner, repository, settings) | kept |

## Known limits

- **Redirects inside the crawler.** The worker checks that a website resolves
  only to public addresses, but a redirect the crawler follows afterwards
  happens inside the unmodified engine. Close this at the network layer on
  deployment (5C step 4).
- **Result storage is local disk.** `LocalFileStorage` is fine for one worker
  host. Supabase Storage or S3 must replace it before running API and worker on
  separate machines (5C step 3).
- **Real Redis was not exercised on this machine.** The Lua scripts are tested
  on fakeredis's Lua runtime; run the real-Redis variant in CI (5C step 7).
- **Name-only companies** are recorded and skipped; the engine needs a URL.
- **`weekly_crawl` and `discovery`** are recorded but not run (see Isolation).

## Phase 5C deployment checklist

1. **Supabase (production project).** Enable Email auth. Set the site URL and
   redirect URLs to the dashboard domain. Apply migrations with
   `python -m cloud.db.migrate apply` from CI or a one-off job, never by hand.
   Verify `careercloud` is **not** in the Data API's exposed schemas.
2. **Secrets.** Store `CAREERCLOUD_DATABASE_URL` and `CAREERCLOUD_REDIS_URL` in
   the host's secret store: Oracle Vault, systemd `LoadCredential`, or
   environment files owned by root with mode 600. The API also needs
   `CAREERCLOUD_SUPABASE_URL`. The service-role key is not needed anywhere.
3. **Object storage.** Implement `SupabaseStorage` or `S3Storage` against
   `ObjectStorage`. Use a private bucket and server-side reads only, with
   streaming downloads unchanged. Add a retention job for old results.
4. **Worker host** (Oracle VM).
   - Run `python -m cloud.worker` under a dedicated unprivileged user and a
     systemd unit with `Restart=always`, `KillSignal=SIGTERM` and
     `TimeoutStopSec` of at least the time one company takes, so shutdown
     releases jobs.
   - Put the runtime directory on its own disk path.
   - Add an egress firewall that denies RFC1918, 100.64/10, 169.254/16,
     127/8, ::1, fc00::/7 and fe80::/10.
   - Keep it separate from the production CareerCrawler deployment: a
     different user, directory and venv, and it must not run as the
     CareerCrawler service user.
5. **API host.**
   - Run `uvicorn cloud.api.main:app --workers N` behind a reverse proxy
     (Cloudflare Tunnel or Caddy) with TLS.
   - Set `CAREERCLOUD_ENV=production`, `STORAGE=postgres`, `QUEUE=redis`,
     `AUTH_MODE=supabase`, and `CORS_ORIGINS` to the exact dashboard origin.
   - Add rate limiting at the proxy.
6. **Dashboard.** Run `npm run build` with `VITE_API_URL`, `VITE_AUTH_MODE=supabase`,
   `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`, then serve `dist/` from
   Cloudflare Pages. Add a CSP that allows only the API and Supabase origins.
7. **CI.**
   - Run the cloud suite with `CAREERCLOUD_TEST_DATABASE_URL` (a Postgres
     service container) and `CAREERCLOUD_TEST_REDIS_URL` (a Redis service
     container).
   - Run `npm run build` and the CareerCrawler suite.
   - Block merges on all three.
8. **Staging first.** Deploy with a separate Supabase project, Redis prefix and
   bucket. Run a real single-company crawl and a small bulk crawl, kill the
   worker mid-job, and confirm the job recovers. Test that a second account
   cannot see the first account's jobs.
9. **Observability.** Ship worker and API logs. Alert on jobs stuck `queued` for
   more than 15 minutes, on `reaped_*` events, on the failure rate, and on queue
   depth (`JobQueue.stats`).
10. **Go-live gate.** Deploy to production only after the staging checks and a
    security review of the deployed configuration.
