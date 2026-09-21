# CareerCloud: the control plane

CareerCloud puts CareerCrawler on the web. A signed-in user enters a company or
a list, clicks **Run crawl**, watches each company progress live, and downloads
the results as Excel, CSV or JSON.

**Status: Phase 5D, local worker working; cloud staging not yet deployed.**
Phase 5B built
PostgreSQL/Supabase persistence with row-level security, Supabase Auth, a Redis
job queue, a separate worker with leases, heartbeats, bounded retries and crash
recovery, and a runner that crawls through the **existing** CareerCrawler engine.
Phase 5C added staging isolation guards, S3 result storage, an egress guard,
API hardening, deployment files, a safety report and end-to-end staging tests.
Phase 5D removed the last assumption that the worker lives on a particular
cloud provider, added the `run-worker.bat` / `stop-worker.bat` launchers, worker
heartbeats, the `/status` endpoint and the dashboard's worker indicator. **The
cloud staging deployment still has not happened**, because it needs Supabase,
Upstash and Cloudflare accounts. See
[Staging status](#staging-status-and-blockers).

- [Architecture](#architecture)
- [Isolation from production CareerCrawler](#isolation-from-production-careercrawler)
- [Database](#database)
- [Queue and worker lifecycle](#queue-and-worker-lifecycle)
- [Authentication and multi-tenancy](#authentication-and-multi-tenancy)
- [API](#api)
  - [Health and status](#health-and-status)
- [Local development](#local-development)
- [The local worker](#the-local-worker)
- [Tests](#tests)
- [Known limits](#known-limits)
- **Staging (Phase 5C)**
  - [Status and blockers](#staging-status-and-blockers)
  - [Architecture](#staging-architecture)
  - [Isolation](#staging-isolation-how-staging-is-kept-off-production)
  - [Secrets and configuration](#secrets-and-configuration)
  - [Deployment steps](#staging-deployment-steps)
  - [Running the worker on another host](#running-the-worker-on-another-host)
  - [Test procedure](#staging-test-procedure)
  - [Operations: health, logs, resources, rollback](#operations)
  - [Troubleshooting](#troubleshooting)
  - [Security checks](#security-checks-staging)
  - [Cost and free tiers](#cost-and-free-tier-notes)

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
| GET | `/health` | Public. `{status, version, environment, runner, storage, queue, auth}`. Touches no backend: it says the API process is up and how it is configured, nothing more. |
| GET | `/status` | **Signed in.** Actually checks each dependency: `{status, environment, checked_at, api, database, redis, queue, worker}`. See [Health and status](#health-and-status). |
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

### Health and status

Two endpoints, on purpose.

**`/api/v1/health`** is public and shallow. It reports that the API answered and
how it is configured. It opens no connections, so it stays fast and cannot be
made to hang by a sick database. Use it for uptime checks.

**`/api/v1/status`** requires a signed-in caller and actually checks things:

```jsonc
{
  "status": "degraded",            // ok, or degraded if a dependency is down
  "environment": "staging",
  "checked_at": "2026-09-21T09:14:03Z",
  "api":      { "status": "ok", "backend": "fastapi" },
  "database": { "status": "ok", "backend": "postgres", "latency_ms": 12.4 },
  "redis":    { "status": "ok", "backend": "redis",    "latency_ms": 31.8 },
  "queue":    { "ready": 3, "delayed": 0, "in_flight": 1 },
  "worker": {
    "online": false,
    "count": 0,
    "last_heartbeat": "2026-09-21T08:52:11Z",
    "seconds_since_heartbeat": 1312.4,
    "stale_after_seconds": 90,
    "message": "Crawler worker offline. 3 crawls queued. Start the worker to process new crawls."
  }
}
```

Each component is `ok`, `down` (configured but unreachable) or `disabled` (not
configured — for example `redis` in local development, where jobs run in the API
process). A `down` dependency makes the overall `status` **degraded** but still
answers **200**: a status endpoint that fails when a backend fails is useless
exactly when it is needed.

Queue depth and worker presence are **not** on the public `/health` because they
describe how much work the system is carrying, which anonymous callers have no
business knowing.

**Worker presence** is a heartbeat the worker writes to the queue (a Redis
sorted set, `<prefix>:workers`) every 30 seconds. It lives there rather than in
PostgreSQL deliberately: presence is ephemeral, it has to expire on its own when
a worker is killed, and it must be readable when no job is running. A worker
that stops cleanly removes its own entry; one that is killed goes stale after
`CAREERCLOUD_WORKER_STALE_AFTER_SECONDS` (default 90, three missed beats).

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

### No production credentials, locally

Local development uses **no real account of any kind**: PostgreSQL is embedded
(`pgserver`), Redis is `fakeredis`, auth is locally issued dev tokens, and
results are written to a directory. Nothing reaches the internet except the
career sites a crawl actually visits.

Keep it that way:

- **Never** put the Supabase service-role key anywhere — not in the API's
  environment, not in a `VITE_*` variable, not in a file in this repository. The
  API is built so it never needs it, and `unused_placeholders()` warns if it is
  set.
- **Never** put a database URL, Redis URL or any secret in a `VITE_*` variable.
  Everything `VITE_*` is compiled into the dashboard bundle and served to every
  visitor. Only the Supabase **anon** key belongs there — it is designed to be
  public and is useless without a signed-in session, because RLS is what
  actually protects the data.
- `.env`, `.env.local` and `cloud/deploy/staging/env/*.env` are git-ignored.
  Only the `.example` files are committed, and they contain no real values.
- A remote database or Redis is **refused** in `development` unless you set
  `CAREERCLOUD_ALLOW_REMOTE_SERVICES=1` on purpose, so a pasted production URL
  cannot connect by accident.
- Use a **separate Supabase project** for staging, never the production one.
  `cloud/ops/stamps.py` stamps each database, queue prefix and bucket with the
  environment it belongs to, and a process refuses to start against a resource
  stamped as something else.
- The production CareerCrawler's `secrets/` directory and Google credentials are
  never read by any CareerCloud process; `cloud/tests/test_isolation.py` proves
  it by spying on `open()` during a real job.

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

## The local worker

The worker is the process that actually crawls. The API only records jobs and
puts their ids on the queue; until a worker is running, crawls queue up and the
dashboard says **Crawler worker offline**. That is a normal state, not a fault.

Right now the worker runs on this Windows machine. Nothing else in CareerCloud
knows or cares — see
[Running the worker on another host](#running-the-worker-on-another-host).

### Start and stop

From the repository root (`E:\Crawlers\CareerCrawler-cloud`):

```bat
run-worker.bat
```

and, in another window (or just press Ctrl+C in the worker's window):

```bat
stop-worker.bat
```

`run-worker.bat` reads `cloud\worker\.env`, or a path you pass as the first
argument. Before starting anything it checks, and explains in plain language
what to do if a check fails:

| Check | Why |
|---|---|
| `cloud\.venv` exists | otherwise prints the two commands that create it |
| the env file exists | otherwise points at `cloud\worker\.env.example` |
| `CAREERCLOUD_ENV` is not `production` | a production worker belongs on a managed host, not in a console window someone can close |
| the env file names no SQLite database or `crawler.db` | CareerCloud is PostgreSQL-only; the production crawler's `state\crawler.db` must never be opened by a cloud worker |

It then reports the exit code in words rather than as a number: configuration
error (2), environment-isolation refusal (3), or an unreachable database or
Redis.

**`stop-worker.bat` is the graceful stop.** It writes the file named by
`CAREERCLOUD_STOP_FILE` (default `cloud\.localdev\worker.stop`); the worker
notices within a second, finishes the company it is crawling, puts any
unfinished job back on the queue **with its attempt refunded**, and exits. No
crawl is lost and no retry is burned. Closing the window instead kills the
worker; the job's lease then expires and the reaper requeues it, which also
works but takes a minute or so.

### Operator workflow

| Step | What you do |
|---|---|
| **Start worker** | `run-worker.bat` — leave the window open. The dashboard's sidebar turns to **Worker online** within a few seconds. |
| **Run crawl** | In the dashboard, **New crawl** → enter a company or paste a list → **Run**. |
| **View progress** | The job page updates every few seconds: per-company status, platform, jobs found, and a live timeline. |
| **Download results** | On a finished job, **Results** → Excel, CSV, JSON summary or the crawl log. Downloads are authenticated `fetch` calls, so no token ever appears in a URL. |
| **Stop worker** | `stop-worker.bat`, or Ctrl+C in the worker's window. |

While the worker is stopped you can still create crawls. They queue, the
dashboard says so, and they start on their own when a worker comes back.

### What the worker never touches

Enforced in code and asserted by `cloud/tests/test_isolation.py`, not just by
convention: the production weekly run, `state/crawler.db` and its checkpoint,
`output/`, `input/`, `secrets/`, the Google Sheet, Seamless, and the crawler's
`max_workers` default. The worker reads `CAREERCLOUD_*` variables only, uses
its own venv, and crawls inside `cloud/runtime/<job id>/attempt-<n>/`.


## Tests

```powershell
# CareerCloud: 306 tests (7 skipped without a real Redis). Real PostgreSQL 16 (embedded), Redis Lua via fakeredis, S3 via moto, real HTTP redirects, real crawler engine with fake adapters.
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
| Staging/production isolation: registry, naming, stamps, forbidden configuration, deployment preflight | `test_staging_guards.py` |
| Egress guard with a real HTTP server: redirects to metadata/private/loopback, rebinding, crawler session | `test_egress.py` |
| S3 result storage and namespace (moto) | `test_s3_storage.py` |
| Rate limits, security headers, client-IP trust, JSON logs, redaction | `test_api_hardening.py` |
| systemd units, nftables policy, install script, env templates | `test_deploy_staging.py` |
| Phase 5A suites (API, schemas, lifecycle, runner, repository, settings) | kept |

## Known limits

- **The staging deployment has not been performed.** Every artifact, check and
  test below exists and has been run locally, but the cloud accounts do not
  exist yet. See [Staging status](#staging-status-and-blockers).
- **Real Redis is untested.** The Lua scripts pass on fakeredis and the
  rehearsal used a Redis-compatible stand-in. The 7 real-Redis tests run as
  soon as a staging Redis exists (`CAREERCLOUD_TEST_REDIS_URL`).
- **The in-process egress guard covers urllib3 only.** That means everything
  `requests` does, including redirects and the crawler's own sessions. A
  headless browser is not covered, so browser fallback stays off unless the
  nftables policy is installed and confirmed.
- **Crawler accuracy is unchanged.** The existing generic HTML extractor can
  report a bogus "job" from a marketing homepage (figma.com gave one titled
  "Company"). This is outside Phase 5C's scope, which does not modify the engine.
- **Name-only companies** are recorded and skipped; the engine needs a URL.
- **`weekly_crawl` and `discovery`** are recorded but not run.

---

# Staging (Phase 5C)

## Staging status and blockers

**Nothing has been deployed.** Everything needed to deploy staging is in the
repository and tested. What is missing is external accounts, which were not
available and were not substituted:

| Component | Needed from the account owner | Status |
|---|---|---|
| Supabase staging project | new project (not the production one): ref, region, DB password, anon key, Storage S3 keys, 2 test users | **blocked** |
| Redis | Upstash database (TLS + password), staging only | **blocked** |
| Worker/API host | **none required** — the worker runs on this Windows machine (`run-worker.bat`). Any Linux VM works later; see [Running the worker on another host](#running-the-worker-on-another-host) | **not blocking** |
| HTTPS | Cloudflare zone + tunnel for `api-staging.<domain>` | **blocked** |
| Dashboard hosting | Cloudflare Pages project | **blocked** |

A **local pre-staging rehearsal** ran TESTs 1–6 with the real API, a separate
real worker and the real CareerCrawler engine crawling real public career
sites, against PostgreSQL 16. All passed. Results and the safety report are in
[`deploy/staging/STAGING_SAFETY_REPORT.md`](deploy/staging/STAGING_SAFETY_REPORT.md).

## Staging architecture

```
Internet
  │ HTTPS
  ├── careercloud-staging.pages.dev   Cloudflare Pages: React dashboard (CSP, noindex, STAGING banner)
  │        │ Supabase Auth (email/password) ──► Supabase STAGING project: Auth
  │        │ Bearer token
  └── api-staging.<domain>            Cloudflare edge (TLS, rate-limit rule)
           │ Cloudflare Tunnel (outbound-only; no open inbound port)
           ▼
  Compute host — any Linux VM with systemd (provider does not matter)
  ├── careercloud-staging-tunnel      cloudflared         user ccstg-tunnel
  ├── careercloud-staging-api         uvicorn 127.0.0.1:8180, 2 workers    user ccstg-api
  │        ├── JWT via Supabase JWKS ──────────────► Supabase STAGING: Auth
  │        ├── PostgreSQL (TLS, RLS as authenticated) ► Supabase STAGING: Postgres (schema careercloud, stamped "staging")
  │        ├── enqueue (TLS, password, prefix careercloud:staging) ► Upstash Redis (stamped "staging")
  │        └── stream downloads (S3 keys) ─────────► Supabase STAGING: Storage bucket careercloud-staging-results (private, namespace staging/)
  └── careercloud-staging-worker      python -m cloud.worker          user ccstg-worker
           ├── nftables egress policy (skuid ccstg-worker): public internet only
           ├── in-process egress guard (every urllib3 connection, every redirect hop)
           ├── CareerCrawlerRunner ──► existing CareerCrawler engine (crawl_company)
           ├── scratch: /var/lib/careercloud-staging/runtime/<job>/attempt-<n>/
           └── results ──► private bucket; metadata ──► Postgres
```

Nothing on this VM relates to the production CareerCrawler deployment. The
users, directories, virtualenv, units and firewall table are all separate, and
the two never share a host with production CareerCloud (`install.sh` refuses to
run where that is possible).

## Staging isolation: how staging is kept off production

A deployed process (`CAREERCLOUD_ENV=staging|production`) refuses to start
unless all of these pass. Code: `shared/environment.py`, `ops/stamps.py`,
`api/deployment.py`, `worker/settings.py`.

1. **Resource registry (an allowlist).**
   `/etc/careercloud-staging/resources.json` lists each environment's database
   hosts, Supabase refs, Redis hosts, storage endpoints and buckets, API origins
   and dashboard origins. Staging may use only resources listed under
   `staging`, and it refuses anything listed under `production`, even if the
   same value was also pasted under staging.
2. **Naming rules.** The queue prefix must be `careercloud:staging`, and the
   bucket and results namespace must contain `staging`. A staging identifier
   containing `prod` is refused, and a production identifier containing
   `staging`, `stage`, `dev` or `test` is refused.
3. **Environment stamps.**
   - The database carries a permanent `careercloud.deployment` row, which a
     trigger prevents from being changed or deleted.
   - Redis carries `careercloud:staging:environment`.
   - The bucket carries `staging/meta/environment`.

   A process refuses a resource stamped for another environment. The database
   is only ever stamped explicitly (`migrate stamp --yes`).
4. **Forbidden configuration.** Any of these is refused:
   - a Google Sheets variable (`*SPREADSHEET*`, `GOOGLE_APPLICATION_CREDENTIALS`, …);
   - any `CAREERCLOUD_*` value naming a SQLite file, `crawler.db`, the
     production CareerCrawler checkout, `secrets/` or the Seamless worktree;
   - the crawler's `store`, `sheets`, `weekly_run` or `checkpoint` modules
     being loaded.
5. **Deployment requirements.**
   - Durable backends only: Postgres, Redis and S3 storage.
   - TLS on the database (`sslmode=require`) and on Redis (`rediss://` with a
     password).
   - Supabase auth only: no dev auth or dev secret, and no service-role key on
     the API.
   - HTTPS origins, exact CORS, JSON logs.
   - A worker must run the real runner with the egress guard on; browser
     fallback needs a confirmed firewall.

There is no fallback. A missing or wrong value stops the process and lists
every problem.

## Secrets and configuration

| File on the VM | Owner:group, mode | Contains |
|---|---|---|
| `/etc/careercloud-staging/api.env` | `root:ccstg-api 0640` | DB URL (password), Redis URL (password), Supabase URL, S3 keys |
| `/etc/careercloud-staging/worker.env` | `root:ccstg-worker 0640` | DB URL, Redis URL, S3 keys |
| `/etc/careercloud-staging/resources.json` | `root:root 0644` | identifiers only, no secrets |
| `/etc/careercloud-staging/cloudflared.yml`, `tunnel.json` | `root:ccstg-tunnel 0640` | tunnel credentials |
| Cloudflare Pages env | Pages dashboard | `VITE_*` public values only |

Each service user can read only its own secrets. Templates are in
`deploy/staging/env/*.example`. Never commit filled copies (`.env*` and
`*.json` credentials are git-ignored). **The Supabase service-role key is not
used anywhere.** The API and worker use the database password, and Storage
uses S3 access keys.

## Staging deployment steps

### 1. Supabase staging project (dashboard, one time)

1. Create a **new** project named `careercloud-staging` in your org. Do not
   reuse any production project. Choose a region near the VM.
2. **Authentication → Providers → Email:** enabled. Turn on "Confirm email".
   **Authentication → URL Configuration:**
   - Site URL `https://careercloud-staging.pages.dev`
   - Redirect URLs `https://careercloud-staging.pages.dev/**`
3. **Authentication → Users:** create two test users, `stg-user-a@…` and
   `stg-user-b@…`, with strong passwords. Store the passwords in your password
   manager.
4. **Project Settings → Data API:** make sure *Exposed schemas* is only
   `public, graphql_public`. `careercloud` must **not** be listed.
5. **Storage:** run `deploy/staging/supabase/storage_bucket.sql` in the SQL
   editor, which creates the private bucket. Then go to **Project Settings →
   Storage → S3 access keys** and create a key pair.
6. Note the session-pooler connection string (**Connect → Session pooler**,
   port 5432) and append `?sslmode=require`.

### 2. Migrate and stamp (from your machine)

```powershell
$env:CAREERCLOUD_ENV="staging"
$env:CAREERCLOUD_RESOURCE_REGISTRY="C:\secure\careercloud\resources.json"
$env:CAREERCLOUD_DATABASE_URL="postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
cloud\.venv\Scripts\python -m cloud.db.migrate status
cloud\.venv\Scripts\python -m cloud.db.migrate apply
cloud\.venv\Scripts\python -m cloud.db.migrate stamp --environment staging     # prints the host; re-run with --yes
cloud\.venv\Scripts\python -m cloud.db.migrate stamp --environment staging --yes
```

Then run `deploy/staging/supabase/verify.sql` in the SQL editor. Expect:
- `staging`;
- both migrations;
- RLS `true` on all tables;
- no `careercloud` in `pgrst.db_schemas`;
- no `anon` grants;
- bucket `public = false`.

### 3. Redis (Upstash console)

Create a **Regional** database named `careercloud-staging` with TLS on, in the
region nearest the VM. Copy the `rediss://default:<password>@<host>:6379` URL.
Keep it for staging only; production gets its own database.

Real-Redis tests (TEST 7), from your machine:

```powershell
$env:CAREERCLOUD_TEST_REDIS_URL="rediss://default:<password>@<host>:6379"
cloud\.venv\Scripts\python -m unittest cloud.tests.test_queue -v     # the 7 real-Redis tests now run
```

### 4. Compute host for the API and worker

**You do not need one to get started.** The worker runs on this Windows machine
today (see [The local worker](#the-local-worker)), and the API can be exposed
through a Cloudflare Tunnel from the same machine. This section is for when you
want the API and worker to run unattended on a server instead.

Any Linux VM with systemd will do — a VPS, a cloud instance from any provider,
or hardware you own. Ubuntu 22.04 or 24.04, 1–2 vCPU and 4–8 GB is comfortable;
CareerCloud is not provider-specific and `install.sh` contains nothing tied to
one. Whatever you pick, add **no inbound rules** besides SSH from your own IP:
the tunnel is outbound-only, so the API never needs an open port. Then:

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv nftables git curl
# cloudflared: https://pkg.cloudflare.com/ (apt repo), then:
sudo install -d -m 0750 /etc/careercloud-staging
sudoedit /etc/careercloud-staging/api.env          # from deploy/staging/env/api.env.example
sudoedit /etc/careercloud-staging/worker.env       # from deploy/staging/env/worker.env.example
sudoedit /etc/careercloud-staging/resources.json   # from deploy/staging/env/resources.json.example
```

Build a release archive locally and copy it over, so the branch never needs
pushing:

```powershell
git -C E:\Crawlers\CareerCrawler-cloud archive --format=tar -o careercloud-<commit>.tar <commit>
scp careercloud-<commit>.tar cloud\deploy\staging\install.sh <vm>:/tmp/
```

```bash
sudo bash /tmp/install.sh --release <commit> --archive /tmp/careercloud-<commit>.tar --i-am-deploying-staging
```

`install.sh` performs these steps in order:
1. Creates the service users.
2. Unpacks the release into `/opt/careercloud-staging/releases/<commit>` with
   its own venv.
3. Installs and loads the nftables egress table.
4. Installs the systemd units.
5. Runs the safety report **as each service user** against the new release.
6. Only if both reports pass, switches `current` and restarts the services.

### 5. Cloudflare Tunnel and DNS

```bash
cloudflared tunnel login                                   # the staging zone
cloudflared tunnel create careercloud-staging
cloudflared tunnel route dns careercloud-staging api-staging.<domain>
sudo cp ~/.cloudflared/<uuid>.json /etc/careercloud-staging/tunnel.json
sudo cp deploy/staging/cloudflared/cloudflared.yml.example /etc/careercloud-staging/cloudflared.yml  # fill uuid + hostname
sudo chown root:ccstg-tunnel /etc/careercloud-staging/{tunnel.json,cloudflared.yml} && sudo chmod 0640 $_
sudo systemctl enable --now careercloud-staging-tunnel
```

In the Cloudflare dashboard, add a **rate limiting rule** for
`api-staging.<domain>/api/v1/*`, for example 100 requests per 10 s per IP. The
free plan includes one rule.

Optional: put **Cloudflare Access** (free for up to 50 users) in front of the
dashboard so only invited testers can load it.

### 6. Dashboard (Cloudflare Pages)

```powershell
copy cloud\web\.env.staging.example cloud\web\.env.staging.local   # fill in the public staging values
cd cloud\web; npm run build:staging
npx wrangler pages deploy dist --project-name careercloud-staging --branch staging
```

`build:staging` refuses to build unless:
- `VITE_DEPLOY_ENV=staging`;
- Supabase auth is used, with an `https://<ref>.supabase.co` URL;
- the API URL is https;
- no value looks like production, localhost or a secret key;
- optionally, the values match the resource registry.

It writes a strict CSP whose `connect-src` allows only the staging API and
Supabase, plus noindex headers, `robots.txt` and SPA routing.

### 7. Verify and run the staging tests

```bash
# on the VM
sudo -u ccstg-worker /opt/careercloud-staging/current/.venv/bin/python -m cloud.ops.safety_report \
    --component worker --env-file /etc/careercloud-staging/worker.env
curl -fsS https://api-staging.<domain>/api/v1/health
```

From your machine:

```powershell
$env:CAREERCLOUD_E2E_SUPABASE_URL="https://<ref>.supabase.co"
$env:CAREERCLOUD_E2E_SUPABASE_ANON_KEY="<anon key>"
$env:CAREERCLOUD_E2E_USER_A_EMAIL="stg-user-a@…"; $env:CAREERCLOUD_E2E_USER_A_PASSWORD="…"
$env:CAREERCLOUD_E2E_USER_B_EMAIL="stg-user-b@…"; $env:CAREERCLOUD_E2E_USER_B_PASSWORD="…"
cloud\.venv\Scripts\python -m cloud.ops.staging_e2e --api https://api-staging.<domain> --auth supabase `
  --worker-kill    "ssh <vm> sudo systemctl kill -s KILL careercloud-staging-worker" `
  --worker-start   "ssh <vm> sudo systemctl start careercloud-staging-worker" `
  --worker-restart "ssh <vm> sudo systemctl restart careercloud-staging-worker" `
  --api-restart    "ssh <vm> sudo systemctl restart careercloud-staging-api" `
  --tests 1,2,3,4,5,6 --json-report staging-e2e.json
```

Also check the dashboard manually: sign in, create a job, watch live progress,
use the job list and detail pages, cancel a job, and download results.

## Running the worker on another host

The worker is deliberately provider-neutral. It is a plain Python process whose
entire contract with the rest of the system is:

* **outbound** TLS to PostgreSQL, to Redis, and to object storage;
* **outbound** HTTPS to the career sites it crawls;
* no inbound connections at all — nothing ever calls the worker;
* no shared filesystem with the API; results travel through object storage;
* no local state worth keeping: everything durable is in PostgreSQL.

So moving it is a matter of running the same command somewhere else. Nothing in
the API, the database, the queue or the dashboard changes, and workers on
different hosts can run at the same time — the atomic claim in PostgreSQL is
what stops two of them running the same job.

| Host | How it starts | When it makes sense |
|---|---|---|
| **Windows machine (today)** | `run-worker.bat` | Getting going. Zero cost, zero setup, but crawls only run while the window is open. |
| Any Linux VM or VPS | `careercloud-staging-worker.service` (in `cloud/deploy/staging/systemd/`), installed by `cloud/deploy/staging/install.sh` | Unattended running. Any provider; the unit and installer contain nothing provider-specific. |
| A container host | `python -m cloud.worker` as the entrypoint | If you already run containers. Nothing extra is needed. |

To move it, put the same `CAREERCLOUD_*` values on the new host and start the
process there. The only settings that must match the API are
`CAREERCLOUD_DATABASE_URL`, `CAREERCLOUD_REDIS_URL` and
`CAREERCLOUD_QUEUE_PREFIX`. Then stop the local one with `stop-worker.bat`; the
handover loses nothing, because a worker shutting down releases its job back to
the queue.

On Linux, also install the nftables egress policy
(`cloud/deploy/staging/nftables/`) before enabling browser fallback — the
in-process egress guard covers `urllib3` only.

**Oracle Cloud, specifically.** An Always Free Ampere A1 instance is a
reasonable free option and was the original plan for Phase 5C, but it is *only*
an option and nothing depends on it. Any Linux VM is equivalent, and its
sign-up needs a payment card. There is no Oracle-specific code, configuration
or account in CareerCloud.

*(Unrelated: `adapters/oracle.py` and `adapters/taleo.py` read job postings from
**Oracle Recruiting** career sites. That is crawler functionality and has
nothing to do with where anything is hosted.)*


## Staging test procedure

| # | Test | Pass criteria (automated in `ops/staging_e2e.py`) |
|---|---|---|
| 1 | Single company | a recognised ATS or discovered careers page, ≥3 postings, counters and timestamps persisted, timeline `created→claimed→completed`, CSV downloads with rows |
| 2 | Bulk (5) | every company finishes; job completes even if some companies fail; XLSX and CSV download |
| 3 | Worker crash | kill at ≥2 companies; `reaped_requeued`; completes on attempt ≥2; one result record per kind |
| 4 | Multi-user | B gets 404 on A's job, targets, events, results, cancel, download; anonymous 401; not in B's list |
| 5 | Cancel | running job ends `cancelled` before all companies; `cancel_requested→cancelled`; repeat cancel returns 409 |
| 6 | Restart | API restart, then worker restart mid-job: job released (attempt refunded) or recovered, completes; a queued job completes |
| 7 | Real Redis | `CAREERCLOUD_TEST_REDIS_URL=… python -m unittest cloud.tests.test_queue`: 7 more tests run and pass |

Run the safety report first, every time. `staging_e2e --require-safety-report "<cmd>"` enforces that.

## Operations

**Health.**

```bash
curl -fsS http://127.0.0.1:8180/api/v1/health              # on the VM
curl -fsS https://api-staging.<domain>/api/v1/health        # through the tunnel
systemctl status careercloud-staging-{api,worker,tunnel}
```

**Logs** (journald, JSON lines).

```bash
journalctl -u careercloud-staging-worker -f -o cat | jq .
journalctl -u careercloud-staging-api --since "1 hour ago" -o cat | jq 'select(.status >= 500)'
journalctl -k | grep ccstg-egress-block                     # kernel egress refusals
```

**Resources** (systemd limits; tune in the unit files).

| Unit | Memory | CPU | Tasks | Stop timeout |
|---|---|---|---|---|
| api | 512M | 100% | 128 | 30 s |
| worker | 2G | 200% | 512 | 300 s (graceful: jobs released to the queue) |
| tunnel | 256M | – | – | 30 s |

The worker's disk use is bounded by `/var/lib/careercloud-staging/runtime`.
Workspaces are deleted after each attempt, and anything left by a killed
worker is swept after 6 h.

**Rollback.**

```bash
sudo /opt/careercloud-staging/current/cloud/deploy/staging/rollback.sh                 # previous release
sudo /opt/careercloud-staging/current/cloud/deploy/staging/rollback.sh --release <sha> # a specific one
```

Migrations are additive and forward-only, so code can roll back without
touching the schema. Rollback stops the worker with SIGTERM first, so running
jobs go back to the queue.

**Stop staging completely.**

```bash
sudo systemctl disable --now careercloud-staging-tunnel careercloud-staging-api careercloud-staging-worker
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Service will not start; `refusing to start 'staging'` in the journal | isolation or deployment check failed (the message lists every problem) | correct `/etc/careercloud-staging/*.env` or `resources.json`; nothing falls back |
| `database is not stamped` | new database | `python -m cloud.db.migrate stamp --environment staging --yes` after checking the host |
| `stamped 'production', but this process is 'staging'` | a production URL in a staging secret | **stop**; fix the secret; review how it got there |
| Worker `ExecStartPre` fails on `nft list table` | egress table not loaded | `sudo nft -f /etc/nftables.d/careercloud-staging-egress.nft` |
| All crawls fail with `not a public internet address` | DNS points somewhere private, or the resolver is not 127.0.0.53 | `resolvectl status`; the nft policy allows DNS only to the local stub |
| API 503 `authentication is unavailable` | JWKS fetch to Supabase failed | check `https://<ref>.supabase.co/auth/v1/.well-known/jwks.json` from the VM |
| API 401 on every call | wrong project (token issuer) or expired session | the dashboard and API must use the same staging project |
| Jobs stay `queued` | worker down, Redis prefix mismatch, or the type isn't runnable | Check the dashboard first — it says **Crawler worker offline** when that is the cause. Locally: `run-worker.bat`. On a server: `systemctl status careercloud-staging-worker`. The queue prefix must match in both env files. |
| Upstash free quota exhausted | poll interval too short | `CAREERCLOUD_POLL_INTERVAL=10` or more (see cost notes) |
| Download returns 410 | object deleted from the bucket | results are gone; re-run the job |
| Dashboard says **Crawler worker offline** but the worker window is open | the worker cannot reach Redis, or it is pointed at a different `CAREERCLOUD_QUEUE_PREFIX` than the API | Read the worker window: a connection error is printed there. Then compare `CAREERCLOUD_REDIS_URL` and `CAREERCLOUD_QUEUE_PREFIX` in `cloud\api\.env` and `cloud\worker\.env` — they must be identical. |
| Worker shows offline for ~90 s after starting | presence is a heartbeat, not a connection | Normal only if it lasts a few seconds. `run-worker.bat` beats immediately at startup, so a full 90 s gap means the beat is not reaching Redis. |
| `run-worker.bat` exits 3 straight away | environment-isolation guard refused the configuration | Its database, queue prefix or bucket does not match the environment it claims. Nothing was touched. Compare against `resources.json`. |
| `run-worker.bat` says it will not start a production worker | `CAREERCLOUD_ENV=production` in the env file | Intended. Use `development` or `staging` locally; deploy production to a real host. |
| `stop-worker.bat` seems to do nothing | the worker checks once a second, then finishes the company it is on | Give it a moment; a slow career site can take a while. Its window prints `worker … stopped` when it is done. |

## Security checks (staging)

| Check | Where enforced | Verified by |
|---|---|---|
| No production resource reachable | registry + stamps + naming rules | `test_staging_guards.py`, safety report |
| Crawler redirects to private or metadata addresses refused | urllib3 connection guard (every hop, DNS pinned) | `test_egress.py` (real HTTP server, real redirects, crawler's own session) |
| Kernel backstop for non-urllib3 traffic | nftables `skuid ccstg-worker` | `test_deploy_staging.py`, `ExecStartPre` |
| API not directly exposed | uvicorn on 127.0.0.1; tunnel only; no inbound rules | unit test on `ExecStart` |
| TLS everywhere | `sslmode=require`, `rediss://`, https origins, HSTS | `deployment_problems` |
| Auth | Supabase JWKS; no dev auth, dev secret or service-role key | `test_auth.py`, `test_staging_guards.py` |
| Tenancy | RLS + owner filter; 404 for others' jobs and downloads | `test_rls.py`, `test_ownership.py`, TEST 4 |
| Abuse | per-IP and per-user rate limits, active-job cap, Cloudflare rule | `test_api_hardening.py` |
| No interactive docs or debug | docs, redoc and openapi disabled when deployed; `debug=False` | `create_app` |
| Dashboard | strict CSP, no localhost, production or secret values in the bundle, noindex | `build-staging.mjs` |
| Least privilege | dedicated users; `ProtectSystem=strict`; no capabilities; write access only to the runtime directory | `test_deploy_staging.py` |

## Cost and free-tier notes

These are the providers' published limits at the time of writing. Check them
at sign-up. Nothing here is upgraded or created automatically.

| Component | Free option | Limits that matter | Will it cost money? |
|---|---|---|---|
| Postgres + Auth + Storage | Supabase Free | 2 free projects per org; 500 MB database; 1 GB storage; 5 GB egress; 50K monthly active users; **paused after ~7 days without activity**; no daily backups | Free. Pro ($25/month per project) only if staging must never pause or needs backups. |
| Redis | Upstash Free | ~500K commands/month; 256 MB; TLS. A worker polling every 10 s uses ~260K/month | Free at the template's poll interval. A 1–2 s poll would exceed it and move to pay-as-you-go (~$0.20 per 100K commands). |
| Compute (API, worker, tunnel) | **the Windows machine you already have** | none | Free. A Linux VM is optional — see the note under this table. |
| HTTPS tunnel | Cloudflare Tunnel | – | Free |
| Dashboard hosting | Cloudflare Pages | 500 builds per month | Free |
| Rate limiting / Access | Cloudflare free plan | 1 rate-limit rule; Access free for up to 50 users | Free |
| Domain | an existing domain on Cloudflare | – | Free if you already have one. A new domain costs ~$10–15/year. Avoid domains used for cold email, whose reputation matters. |
| Alternative storage | Cloudflare R2 | 10 GB-month; no egress fees | Free at staging volume |

**Compute is not on the list above because it is not needed yet.** The worker
runs locally, so there is no host to pay for. If you later move it to a server,
the usual options are a small VPS (~$4–6/month), a free-tier cloud VM from any
of the large providers, or a machine you already own. CareerCloud does not
depend on any of them — see
[Running the worker on another host](#running-the-worker-on-another-host).

**Definitely costs money:** only a new domain if none is available, and only
if you choose it. Every other component fits a free tier at staging volume
with the provided settings.
