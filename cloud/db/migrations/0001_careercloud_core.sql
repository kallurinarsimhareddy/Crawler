-- CareerCloud core schema: jobs, their companies, their timeline, their results.
--
-- Applied by `python -m cloud.db.migrate`, never by hand, so the checksum in
-- careercloud.schema_migrations stays meaningful. Written to run unchanged on
-- Supabase and on a plain local PostgreSQL:
--
--   * On Supabase, the `authenticated` role, the `auth` schema and `auth.uid()`
--     already exist and the guarded blocks below do nothing.
--   * On plain PostgreSQL they are created with the same meaning Supabase gives
--     them, so row-level security behaves identically in local tests.
--
-- Nothing here touches CareerCrawler's SQLite database. It lives in its own
-- schema, `careercloud`, which Supabase's Data API does not expose by default:
-- the only way in for a user is through the CareerCloud API.

-- ---------------------------------------------------------------------------
-- Supabase compatibility (no-ops on Supabase)
-- ---------------------------------------------------------------------------

do $compat$
begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;

  if not exists (select 1 from pg_namespace where nspname = 'auth') then
    create schema auth;
    grant usage on schema auth to authenticated;
  end if;

  if not exists (
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'auth' and p.proname = 'uid'
  ) then
    -- Same contract as Supabase's auth.uid(): the `sub` claim of the verified
    -- JWT the request runs under, or NULL when there is none.
    create function auth.uid() returns uuid
      language sql stable
      as $fn$
        select nullif(
          coalesce(
            current_setting('request.jwt.claim.sub', true),
            nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'
          ),
          ''
        )::uuid
      $fn$;
    grant execute on function auth.uid() to authenticated;
  end if;

  -- The API connects as one database user and switches to `authenticated` per
  -- request. On Supabase `postgres` is already a member; locally, grant it.
  if not pg_has_role(current_user, 'authenticated', 'MEMBER') then
    execute format('grant authenticated to %I', current_user);
  end if;
end
$compat$;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create schema if not exists careercloud;
grant usage on schema careercloud to authenticated;

create table careercloud.jobs (
  id                   text primary key check (id ~ '^job_[0-9a-f]{32}$'),
  owner_id             uuid not null,
  type                 text not null
                         check (type in ('single_company', 'bulk_companies', 'weekly_crawl', 'discovery')),
  status               text not null default 'queued'
                         check (status in ('queued', 'running', 'completed', 'failed', 'cancelled')),
  target_count         integer not null default 0 check (target_count >= 0),
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  started_at           timestamptz,
  completed_at         timestamptz,
  error                text check (char_length(error) <= 4000),

  -- progress
  total_companies      integer check (total_companies >= 0),
  completed_companies  integer not null default 0 check (completed_companies >= 0),
  failed_companies     integer not null default 0 check (failed_companies >= 0),
  jobs_found           integer not null default 0 check (jobs_found >= 0),
  current_company      text check (char_length(current_company) <= 300),
  current_phase        text check (char_length(current_phase) <= 40),
  progress_message     text check (char_length(progress_message) <= 500),

  -- execution: claims, leases, cancellation
  attempts             integer not null default 0 check (attempts >= 0),
  max_attempts         integer not null default 1 check (max_attempts between 1 and 10),
  worker_id            text check (char_length(worker_id) <= 200),
  heartbeat_at         timestamptz,
  lease_expires_at     timestamptz,
  cancel_requested_at  timestamptz,

  constraint jobs_attempts_bounded check (attempts <= max_attempts),
  constraint jobs_running_is_leased
    check (status <> 'running' or (worker_id is not null and lease_expires_at is not null)),
  constraint jobs_finished_has_time
    check (status not in ('completed', 'failed', 'cancelled') or completed_at is not null),
  -- lets job_results prove its owner matches the job's
  constraint jobs_id_owner unique (id, owner_id)
);

create index jobs_owner_newest on careercloud.jobs (owner_id, created_at desc, id desc);
create index jobs_running_by_lease on careercloud.jobs (lease_expires_at) where status = 'running';
create index jobs_queued_by_update on careercloud.jobs (updated_at) where status = 'queued';

create table careercloud.crawl_targets (
  job_id        text not null references careercloud.jobs (id) on delete cascade,
  position      integer not null check (position >= 0),
  website       text check (char_length(website) <= 2048),
  company_name  text check (char_length(company_name) <= 200),
  status        text not null default 'pending'
                  check (status in ('pending', 'running', 'completed', 'failed', 'skipped')),
  platform      text check (char_length(platform) <= 100),
  outcome       text check (char_length(outcome) <= 100),
  jobs_found    integer not null default 0 check (jobs_found >= 0),
  error         text check (char_length(error) <= 2000),
  started_at    timestamptz,
  completed_at  timestamptz,
  primary key (job_id, position),
  check (website is not null or company_name is not null)
);

create table careercloud.job_events (
  id          bigint generated always as identity primary key,
  job_id      text not null references careercloud.jobs (id) on delete cascade,
  created_at  timestamptz not null default now(),
  kind        text not null check (kind ~ '^[a-z_]{1,40}$'),
  attempt     integer,
  message     text check (char_length(message) <= 2000),
  data        jsonb not null default '{}'::jsonb
);

create index job_events_by_job on careercloud.job_events (job_id, id);

create table careercloud.job_results (
  id            text primary key check (id ~ '^res_[0-9a-f]{32}$'),
  job_id        text not null,
  owner_id      uuid not null,
  kind          text not null check (kind in ('summary_json', 'jobs_csv', 'jobs_xlsx', 'crawl_log')),
  filename      text not null check (filename ~ '^[A-Za-z0-9._-]{1,100}$'),
  content_type  text not null check (char_length(content_type) <= 100),
  storage_key   text not null check (
                  char_length(storage_key) <= 512
                  and storage_key ~ '^[a-z0-9][a-z0-9_/.\-]*$'
                  and storage_key !~ '\.\.'
                ),
  size_bytes    bigint not null check (size_bytes >= 0),
  sha256        text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  row_count     integer check (row_count >= 0),
  created_at    timestamptz not null default now(),
  unique (job_id, kind),
  foreign key (job_id, owner_id) references careercloud.jobs (id, owner_id) on delete cascade
);

-- ---------------------------------------------------------------------------
-- The status machine, enforced by the database as well as the service
-- ---------------------------------------------------------------------------

create function careercloud.guard_job_update() returns trigger
  language plpgsql
  as $fn$
begin
  if new.id <> old.id or new.owner_id <> old.owner_id or new.type <> old.type
     or new.created_at <> old.created_at or new.target_count <> old.target_count then
    raise exception 'job identity, owner, type, targets and creation time are immutable'
      using errcode = 'check_violation';
  end if;

  if new.status <> old.status and not (
       (old.status = 'queued'  and new.status in ('running', 'cancelled'))
    or (old.status = 'running' and new.status in ('completed', 'failed', 'cancelled', 'queued'))
  ) then
    raise exception 'illegal job status change % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;

  if new.attempts < old.attempts - 1 then
    raise exception 'attempts may only be refunded one at a time'
      using errcode = 'check_violation';
  end if;

  new.updated_at := now();
  return new;
end
$fn$;

create trigger jobs_guard_update
  before update on careercloud.jobs
  for each row execute function careercloud.guard_job_update();

-- ---------------------------------------------------------------------------
-- Row-level security
--
-- Users act as `authenticated`, with their verified user id in the request's
-- JWT claims. They can see only their own rows, create only their own queued
-- jobs, and change a job only to cancel it. Everything else — claiming,
-- progress, results — is the worker's, which connects as the table owner and so
-- is not subject to these policies.
-- ---------------------------------------------------------------------------

alter table careercloud.jobs          enable row level security;
alter table careercloud.crawl_targets enable row level security;
alter table careercloud.job_events    enable row level security;
alter table careercloud.job_results   enable row level security;

create policy jobs_select_own on careercloud.jobs
  for select to authenticated
  using (owner_id = auth.uid());

create policy jobs_insert_own on careercloud.jobs
  for insert to authenticated
  with check (
    owner_id = auth.uid()
    and status = 'queued'
    and attempts = 0
    and worker_id is null
    and started_at is null
    and completed_at is null
    and cancel_requested_at is null
  );

create policy jobs_cancel_own on careercloud.jobs
  for update to authenticated
  using (owner_id = auth.uid())
  with check (
    owner_id = auth.uid()
    and cancel_requested_at is not null
    and status in ('running', 'cancelled')
  );

create policy targets_select_own on careercloud.crawl_targets
  for select to authenticated
  using (exists (select 1 from careercloud.jobs j where j.id = job_id and j.owner_id = auth.uid()));

create policy targets_insert_own on careercloud.crawl_targets
  for insert to authenticated
  with check (
    status = 'pending'
    and exists (select 1 from careercloud.jobs j
                where j.id = job_id and j.owner_id = auth.uid() and j.status = 'queued')
  );

create policy events_select_own on careercloud.job_events
  for select to authenticated
  using (exists (select 1 from careercloud.jobs j where j.id = job_id and j.owner_id = auth.uid()));

create policy events_insert_own on careercloud.job_events
  for insert to authenticated
  with check (
    kind in ('created', 'cancel_requested', 'cancelled')
    and exists (select 1 from careercloud.jobs j where j.id = job_id and j.owner_id = auth.uid())
  );

create policy results_select_own on careercloud.job_results
  for select to authenticated
  using (owner_id = auth.uid());

-- Table privileges: the minimum the policies above need. Column-level UPDATE
-- keeps a user from rewriting progress, attempts, leases or errors even on
-- their own job.
grant select, insert on careercloud.jobs to authenticated;
grant update (status, completed_at, cancel_requested_at, updated_at,
              total_companies, completed_companies, failed_companies, jobs_found,
              current_company, current_phase, progress_message)
  on careercloud.jobs to authenticated;
grant select, insert on careercloud.crawl_targets to authenticated;
grant select, insert on careercloud.job_events to authenticated;
grant select on careercloud.job_results to authenticated;

do $anon$
begin
  -- Supabase's anonymous role gets nothing here, explicitly.
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on all tables in schema careercloud from anon;
    revoke usage on schema careercloud from anon;
  end if;
end
$anon$;
