-- Which environment this database belongs to.
--
-- Written once, by `python -m cloud.db.migrate stamp --environment <env>`, and
-- read by every deployed API and worker at startup. A staging process refuses a
-- database stamped `production` (and vice versa), even if a URL was copied into
-- the wrong secret. The row can never be changed to another environment:
-- a trigger refuses it, whatever role tries.

create table careercloud.deployment (
  singleton    boolean primary key default true check (singleton),
  environment  text not null check (environment in ('development', 'test', 'staging', 'production')),
  stamped_at   timestamptz not null default now()
);

create function careercloud.guard_deployment_stamp() returns trigger
  language plpgsql
  as $fn$
begin
  if tg_op = 'DELETE' or new.environment <> old.environment then
    raise exception 'the deployment environment stamp is permanent (was %)', old.environment
      using errcode = 'check_violation';
  end if;
  return new;
end
$fn$;

create trigger deployment_stamp_is_permanent
  before update or delete on careercloud.deployment
  for each row execute function careercloud.guard_deployment_stamp();

-- Users never read or write it; only the table owner (API/worker system scope) does.
alter table careercloud.deployment enable row level security;
