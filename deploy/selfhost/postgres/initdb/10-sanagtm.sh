#!/bin/bash
# Runs once, as the PostgreSQL superuser, when the data directory is first created.
#   sanagtm        login role; owns the database and every table (runs migrations; the API and
#                  worker connect as it and switch to "authenticated" per user request for RLS)
#   authenticated  NOLOGIN role every RLS policy is written for (same name as on Supabase)
# The password comes from SANAGTM_DB_PASSWORD (postgres.env, 0600) via a psql variable, so it is
# never interpolated into SQL text or shown in a process list.
set -euo pipefail
: "${SANAGTM_DB_PASSWORD:?SANAGTM_DB_PASSWORD is not set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v app_password="$SANAGTM_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE sanagtm LOGIN PASSWORD %L', :'app_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sanagtm') \gexec
SELECT 'CREATE ROLE authenticated NOLOGIN NOINHERIT'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') \gexec
GRANT authenticated TO sanagtm;
SELECT 'CREATE DATABASE sanagtm OWNER sanagtm'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sanagtm') \gexec
REVOKE ALL ON DATABASE sanagtm FROM PUBLIC;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname sanagtm <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
