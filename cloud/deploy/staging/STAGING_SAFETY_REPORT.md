# CareerCloud STAGING SAFETY REPORT

**Status (2026-09-17): real staging has NOT been deployed.** The Supabase
staging project, the Redis instance, the Linux VM, the Cloudflare zone/tunnel
and the storage bucket all need accounts that do not exist yet (see "Blocked on"
below). Nothing was deployed and no production resource was contacted.

What *was* run is a **local pre-staging rehearsal** on the development machine.
It used the real API, a separate real worker, the real CareerCrawler engine
crawling real public career sites, PostgreSQL 16, the egress guard and
multi-user auth. Stand-ins replaced the missing accounts: development auth, a
local Redis-compatible server and local result storage.

This file is the human-readable record. The machine check is:

```
python -m cloud.ops.safety_report --component all --env-file <env file>
```

It runs automatically before each staging service starts (systemd
`ExecStartPre`) and exits non-zero on any failure.

---

## 1. Pre-crawl safety check (local rehearsal, before every real crawl)

| Check | Result | Detail |
|---|---|---|
| Git branch | PASS | `feature/careercloud-mvp` (never `queue-recovery`, `main`, `seamless-integration`) |
| Code location | PASS | `E:\Crawlers\CareerCrawler-cloud` worktree, not the production checkout |
| Target environment | PASS | `development` (rehearsal) |
| Database host | PASS | `127.0.0.1` embedded PostgreSQL 16, stamped `development`, migrations 0001+0002, RLS on all tables |
| Redis host / queue prefix | PASS | `127.0.0.1:6390` local stand-in, prefix `careercloud:development:rehearsal` |
| Storage | INFO | local directory `cloud/.localdev/results` (staging requires the private S3 bucket) |
| Google Sheets | PASS | no Sheets variables; `googleapiclient` not installed in the cloud venv |
| Production SQLite | PASS | no SQLite or `crawler.db` path configured; worker never imports `sqlite3`/`store` |
| CareerCrawler production state | PASS | no reference to the production checkout, `state/`, `output/`, `secrets/` |
| Seamless | PASS | not referenced by any configuration. No Seamless file, process, lock or worktree was opened, listed or inspected |
| Egress guard | PASS | installed in the worker process; browser fallback off |
| nftables policy | SKIP | Windows host, so no kernel egress policy (required and checked on the staging VM) |
| **Result** | **SAFE** | 18 pass, 0 fail, 5 skipped, 10 info |

The first run of this report returned **UNSAFE** because the local database had
never been stamped. The rehearsal did not start until that was corrected. The
check worked as designed.

## 2. Production untouched

| Item | State |
|---|---|
| Production checkout `E:\Crawlers\CareerCrawler` | branch `queue-recovery` @ `46af82d`, 0 changed files |
| `state/crawler.db` | never opened; no cloud code can reach it (tests prove it) |
| Production Google Sheet | never contacted |
| Production worker default (6) | unchanged; the cloud runner never sets `max_workers` |
| "CareerCrawler Weekly" scheduled task | not touched |
| Seamless | not touched, not inspected |

## 3. Rehearsal results (real crawls, local stand-ins for accounts)

| Test | Result | Evidence |
|---|---|---|
| 1 Single company | PASS | `https://supabase.com`: careers page **discovered** (`jobs.ashbyhq.com/supabase/…`), platform Ashby, **60 jobs**, persisted, progress/timeline recorded, **CSV downloaded (60 rows)**. |
| 2 Bulk (5) | PASS | posthog.com, linear.app, vercel.com, supabase.com, sentry.io all completed; 174 postings; XLSX (23.6 KB) and CSV (33.8 KB) downloaded. |
| 3 Worker crash | PASS | 16-company job; worker hard-killed at 3/16; replacement worker's reaper requeued it after lease expiry; completed on **attempt 2** in 78 s; events `created, claimed, reaped_requeued, claimed, completed`; **one record per result kind** (no duplicates). |
| 4 Multi-user | PASS | user B got **404** on A's job, targets, events, results, cancel and download; anonymous download got **401**; A's job absent from B's list. |
| 5 Cancellation | PASS | 16-company job cancelled while running; stopped between companies at **3/16**; 13 targets never started; final `cancelled`; second cancel returns 409. |
| 6 API + worker restart | PASS | API restarted; worker gracefully restarted mid-job; job `released_on_shutdown` (attempt refunded), re-claimed, completed; a job queued during the restart also completed. |
| 7 Real Redis | **BLOCKED** | no real Redis instance available (needs a provider account). The 7 real-Redis queue tests are still skipped. |

Rehearsal quality note: an earlier TEST 1 candidate, figma.com, produced
one bogus "job" titled "Company" from the homepage through the existing crawler's
generic HTML extractor. The test now requires a recognised ATS or a
discovered careers page with several postings. This is a crawler accuracy
finding, left unchanged here because the crawler engine is out of scope.

## 5. Real staging connection (2026-09-21)

Staging is connected to real managed services for the first time. **No secret
value appears in this file**: project refs, hostnames and bucket names are
identifiers, not credentials, and are already listed in `resources.json`.
Passwords, tokens and keys live only in `cloud/.env.staging-secrets`, which is
git-ignored, and in the three env files generated from it.

### What is connected

| Component | Resource | Verified |
|---|---|---|
| Database | Supabase project `zqbbcaehvpstunxphxxj`, region `ap-south-1`, session pooler on `aws-0-ap-south-1.pooler.supabase.com:5432`, `sslmode=require` | authenticated as `postgres.<ref>` |
| Auth | same project, JWKS at `/auth/v1/.well-known/jwks.json`, one **ES256** signing key | API rejects absent, malformed and forged tokens |
| Queue | Upstash `usable-anchovy-289368.upstash.io:6379`, TLS (`rediss://`), Redis 8.4.0, prefix `careercloud:staging` | PING, Lua scripting, 18/18 queue tests |
| Result storage | private bucket `careercloud-staging-results` (0 policies) | `storage.buckets.public = false` |
| Worker | this Windows machine, `run-worker.bat cloud/worker/.env.staging` | heartbeat visible in `careercloud:staging:workers` |

The direct database endpoint `db.<ref>.supabase.co` is **IPv6-only** on this
project and does not resolve from this network, so the session pooler is the
only route. That is also what migrations need, because they take an advisory
lock.

### Migration

`0001_careercloud_core` and `0002_deployment_environment` applied. Verified
against the live database:

- **Tables**: `jobs`, `crawl_targets`, `job_events`, `job_results` all present,
  all with row-level security **ON**. (`schema_migrations` has RLS off by
  design; `deployment` has it on.)
- **Policies**: 8, all scoped to `authenticated` — select/insert on jobs,
  targets and events, select on results, and a cancel-only update on jobs.
  There is no delete policy anywhere.
- **Triggers**: `jobs_guard_update` → `guard_job_update()` and
  `deployment_stamp_is_permanent` → `guard_deployment_stamp()`.
- **`anon` privileges on schema `careercloud`: none.** Zero rows in
  `information_schema.role_table_grants`, so an anonymous caller cannot read
  job data even before RLS is consulted.
- `careercloud.jobs.owner_id` is a plain `uuid` with no foreign key to
  `auth.users`, so deleting a user never cascades into job history.

### Authentication, as enforced by the running API

| Request | Result |
|---|---|
| `GET /health`, no token | 200 — public by design, touches no backend |
| `GET /status`, no token | 401 `sign in required` |
| `GET /status`, malformed token | 401 `token is invalid` |
| `GET /status`, **forged HS256 token** with correct issuer and audience | 401 `token is invalid` |
| `POST /jobs`, forged token | 401 `token is invalid` |

The forgery is refused because the verifier pins the algorithm to where the key
came from: a JWKS key is ES256, so an HS256 token signed with an attacker's
secret is never even considered. Auth does not fail open.

### Secrets

- The built dashboard bundle (311 KB) was searched for the database password,
  the Upstash token, the Upstash hostname and the pooler hostname. **None
  present.**
- Only `SUPABASE_ANON_KEY` is ever written to a `VITE_` variable. This project
  issues Supabase's newer key format, so the generator refuses both
  `service_role` and `sb_secret_` keys in that field; either bypasses RLS.
- `cloud/.env.staging-secrets` and all generated env files are git-ignored,
  asserted by `cloud/tests/test_staging_env.py`.

### Production untouched

`state/crawler.db`, its checkpoint, `output/`, `input/`, `secrets/`, the Google
Sheet, Seamless and the weekly run's worker count were not read or written. The
cloud worker used only `cloud/runtime/` and the staging Supabase/Upstash
resources above. No Oracle resource is involved anywhere.

### Still outstanding

1. **One real single-company crawl has not been run.** It needs an
   authenticated API call, and the staging API verifies real Supabase Auth.
   Signing in needs the test user's password, which has not been supplied;
   `mailer_autoconfirm` is off, so a fresh signup cannot self-confirm either.
2. **Supabase Storage S3 keys** are not issued, so results are written to local
   files. The private bucket exists and is ready.
3. **Cloudflare Pages is not connected to the API.** See
   cloud/README.md → Running the worker on another host, and the tunnel steps.

## 6. Blocked on (manual, needs the account owner)

1. **Supabase staging project**, separate from any production project. Needed:
   project ref and region, database password (session-pooler URL), anon key,
   S3 access keys for Storage, and two staging test users.
2. **Redis**: an Upstash (or equivalent) database with TLS and a password, for
   staging only.
3. **A host for the worker and API**: either a Windows machine running
   `run-worker.bat` (the current arrangement), or any Linux VM with systemd and
   SSH access. No particular provider is required.
4. **Cloudflare**: a zone/domain for `api-staging.<domain>`, a tunnel, and a
   Pages project for the dashboard.
5. **Resource registry**: fill `resources.json` with the staging identifiers
   above and the production identifiers (or placeholders) so they are refused.

Once these exist, follow **cloud/README.md → Staging deployment** and run the
real staging tests with `python -m cloud.ops.staging_e2e --auth supabase`.
