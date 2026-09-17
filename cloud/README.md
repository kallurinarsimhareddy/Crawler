# CareerCloud: the control plane

This is the start of CareerCrawler as a web product. Someone opens the site,
enters a company or a list, clicks **Run crawl**, watches the job, and (later)
downloads the results.

**Phase 5A status:** the API, the dashboard and the job lifecycle work end to
end. The runs are **simulated**. Jobs live in memory and disappear when the API
restarts. The only runner is `FakeRunner`, which opens no connection, starts no
browser and writes no file.

## Isolation from the crawler

Nothing under `cloud/` imports the crawler, its adapters, `state/crawler.db`,
SQLite, Playwright or the Google Sheets client. `cloud/tests/test_isolation.py`
checks this both statically and at runtime. The cloud code also has its own
dependencies (`cloud/api/requirements.txt`) and its own venv (`cloud/.venv`), so
the weekly run's `venv/` is untouched.

The crawler's test suite (`python -m unittest discover -s tests`) never reaches
`cloud/tests`. If the cloud tests are run under the crawler venv, they report as
skipped instead of failing.

## Architecture

```
 web/ (React + Vite)
   │  HTTP, JSON
   ▼
 api/ (FastAPI) ── routes.py ──► shared/service.py  JobService       ◄── the only thing that changes a job
                                   │
                                   ▼
                                 shared/repository.py  JobRepository   (InMemoryJobRepository today)
   │
   │ dispatcher.dispatch(job_id)
   ▼
 worker/dispatcher.py  JobDispatcher  ── InlineDispatcher (5A, in-process threads)
                                      └─ NullDispatcher   (jobs stay queued)
   ▼                                    [5B: RedisDispatcher → queue → worker process]
 worker/executor.py    JobExecutor    ── start → runner.run → completed / failed / cancelled
   ▼
 worker/runner.py      JobRunner      ── FakeRunner (5A)
                                         [5B: CareerCrawlerRunner → existing crawler engine]
```

| Module | Role |
|---|---|
| `shared/models.py` | `Job`, `JobType`, `JobStatus`, `JobProgress`, `CompanyTarget` and the transition table. All models are frozen. |
| `shared/schemas.py` | The request and response contract. Requests are a discriminated union on `type` and reject unknown fields. Websites are normalised here. |
| `shared/repository.py` | The `JobRepository` interface and `InMemoryJobRepository`. Every write goes through `compare_and_set(job, expected_status=...)`, which becomes `UPDATE … WHERE status = :expected` in SQL. |
| `shared/service.py` | `JobService` handles create, start, progress, complete, fail and cancel, and enforces the transition table. |
| `worker/runner.py` | `JobRunner`, `RunContext` (report progress, check for cancellation) and `RunResult`. |
| `worker/fake_runner.py` | `FakeRunner`: one simulated step per target, with configurable delay, failure or exception. |
| `worker/executor.py` | `JobExecutor`: however the runner ends, the job ends in a terminal status. A cancel that arrives while a job is running is kept. |
| `worker/dispatcher.py` | The point where a job queue plugs in. |
| `api/` | `settings.py` (environment only), `routes.py`, `main.py` (`create_app()` for tests, `app` for uvicorn). |
| `web/` | The dashboard. |

### Job lifecycle

```
queued ──► running ──► completed
   │          ├──────► failed
   └──────────┴──────► cancelled
```

Terminal statuses are final. A move that isn't allowed raises
`InvalidTransitionError`, which the API returns as HTTP 409.

## API

Base path `/api/v1`. Interactive docs are at `http://127.0.0.1:8000/docs`.

| Method | Path | |
|---|---|---|
| GET | `/health` | `{status, service, version, environment, runner, storage}` |
| POST | `/jobs` | Create a job. Returns **201** `{job_id, status: "queued"}`. Returns **422** on invalid input. |
| GET | `/jobs` | `?status=&limit=1..200&offset=`. Returns `{jobs, total, counts}`. `counts` covers all jobs, whatever the filter. |
| GET | `/jobs/{job_id}` | The full job. Returns **404** if unknown. |
| POST | `/jobs/{job_id}/cancel` | queued/running → cancelled. Returns **409** if the job is already final. |

Request bodies:

```json
{"type": "single_company", "website": "https://example.com"}
{"type": "discovery",      "company_name": "Acme", "website": "acme.com"}
{"type": "bulk_companies", "companies": [{"website": "a.com"}, {"company_name": "Bee"}]}
{"type": "weekly_crawl"}
```

`single_company` and `discovery` need a `website` or a `company_name`.
`bulk_companies` accepts 1–5000 companies. `weekly_crawl` accepts no target.

## Running locally

Run these from the repository root. On Windows, use `cloud\.venv\Scripts\python`.

```bash
# one-time: API
py -3.12 -m venv cloud/.venv
cloud/.venv/Scripts/python -m pip install -r cloud/api/requirements-dev.txt

# one-time: dashboard
cd cloud/web && npm install && cd ../..

# terminal 1: API on :8000
cloud/.venv/Scripts/python -m uvicorn cloud.api.main:app --reload --port 8000
#   optional: copy cloud/api/.env.example to cloud/api/.env, then add --env-file cloud/api/.env

# terminal 2: dashboard on :5173 (proxies /api to :8000)
cd cloud/web && npm run dev
```

Open http://localhost:5173.

## Tests

```bash
cloud/.venv/Scripts/python -m unittest discover -s cloud/tests -t .   # cloud: API, validation, lifecycle, runner, repository, isolation
cd cloud/web && npm run build                                          # dashboard: tsc typecheck + production build
venv/Scripts/python -m unittest discover -s tests                      # crawler suite, unchanged
```

## Configuration and secrets

* `cloud/api/.env.example`, `cloud/web/.env.example` and `cloud/worker/.env.example`
  list every variable. The real `.env` files are git-ignored by the root
  `.gitignore`.
* The database, Supabase and Redis variables are placeholders for Phase 5B. If
  one is set, the API logs a warning that it has no effect yet.
* Every `VITE_*` value is built into public JavaScript. Only the Supabase **anon**
  key may go there, never the service-role key.

## Phase 5B

Planned for Phase 5B:

1. **PostgreSQL/Supabase `JobRepository`** with a `jobs` table and a
   conditional-update `compare_and_set`, plus migrations. Add a `job_results` table.
2. **Redis queue:** a `RedisDispatcher` in the API and a `cloud/worker` process
   that consumes job ids and calls `JobExecutor`. Add heartbeats and requeue a
   running job whose worker died.
3. **Authentication:** Supabase Auth in the dashboard and JWT verification in
   the API.
4. **Multi-user isolation:** `owner_id` on every job, repository queries scoped
   to the caller, and row-level security.
5. **`CareerCrawlerRunner`:** a `JobRunner` that calls the existing engine,
   reports per-company progress and honours cancellation. It uses its own
   scratch database and output directory, and never touches `state/crawler.db`
   or the production Sheet.
6. **Progress and results:** real counters and downloadable CSV/XLSX through the
   existing exporters.
