-- Read-only verification of a CareerCloud Supabase STAGING project.
-- `python -m cloud.ops.safety_report` runs the same checks automatically.

-- 1. The database knows it is staging.
select environment from careercloud.deployment;                             -- expect: staging

-- 2. Migrations applied.
select version from careercloud.schema_migrations order by version;          -- expect: 0001…, 0002…

-- 3. RLS on every CareerCloud table.
select c.relname, c.relrowsecurity
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'careercloud' and c.relkind = 'r'
order by 1;                                                                   -- expect: true for all but schema_migrations

-- 4. The Data API (PostgREST) does not expose the careercloud schema.
select rolname, rolconfig from pg_roles where rolname = 'authenticator';     -- pgrst.db_schemas must not list careercloud

-- 5. anon has no privileges on CareerCloud tables.
select table_name, privilege_type from information_schema.role_table_grants
where table_schema = 'careercloud' and grantee = 'anon';                     -- expect: no rows

-- 6. The results bucket is private.
select id, public from storage.buckets where id = 'careercloud-staging-results';  -- expect: public = false
