#!/bin/sh
# Creates the two application roles when the Postgres data volume is initialised for the first
# time (scripts in /docker-entrypoint-initdb.d run only on an empty data directory).
#
#   coach_app  read/write, owns the database and therefore the schema objects created by
#              `coach db migrate`
#   coach_ro   SELECT only (grants are in migrations/0001_init.sql); its sessions are read-only
#              by default and statements time out after 30 s
#
# Required environment: POSTGRES_USER, POSTGRES_DB (set by the postgres image),
# DB_APP_PASSWORD and DB_RO_PASSWORD.
set -eu

: "${DB_APP_PASSWORD:?DB_APP_PASSWORD must be set}"
: "${DB_RO_PASSWORD:?DB_RO_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_password="$DB_APP_PASSWORD" \
  -v ro_password="$DB_RO_PASSWORD" \
  -v dbname="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE coach_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_app') \gexec

SELECT format('CREATE ROLE coach_ro LOGIN PASSWORD %L', :'ro_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_ro') \gexec

ALTER ROLE coach_ro SET default_transaction_read_only = on;
ALTER ROLE coach_ro SET statement_timeout = '30s';

-- coach_app owns the database; on Postgres 15+ the public schema belongs to
-- pg_database_owner, so coach_app can create the tables.
ALTER DATABASE :"dbname" OWNER TO coach_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
