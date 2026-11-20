#!/bin/sh
# Creates the three application roles when the Postgres data volume is initialised for the
# first time (scripts in /docker-entrypoint-initdb.d run only on an empty data directory).
#
#   coach_app    read/write, owns the database and therefore the schema objects created by
#                `coach db migrate`; used by the services. CREATEDB lets the backup's restore
#                check create (and drop) its scratch database (docs/backup.md).
#   coach_agent  the agent's own `coach` commands: SELECT everywhere, a few writes
#                (grants are in migrations/0002_agent_role.sql)
#   coach_ro     SELECT only (grants are in migrations/0001_init.sql), for `coach sql`; its
#                sessions are read-only by default and statements time out after 30 s
#
# Required environment: POSTGRES_USER, POSTGRES_DB (set by the postgres image),
# DB_APP_PASSWORD, DB_AGENT_PASSWORD and DB_RO_PASSWORD.
#
# On an existing volume this script does not run again: create a missing role by hand with
# the same statements (psql as the superuser), then run `coach db migrate`.
set -eu

: "${DB_APP_PASSWORD:?DB_APP_PASSWORD must be set}"
: "${DB_AGENT_PASSWORD:?DB_AGENT_PASSWORD must be set}"
: "${DB_RO_PASSWORD:?DB_RO_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_password="$DB_APP_PASSWORD" \
  -v agent_password="$DB_AGENT_PASSWORD" \
  -v ro_password="$DB_RO_PASSWORD" \
  -v dbname="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE coach_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_app') \gexec

SELECT format('CREATE ROLE coach_agent LOGIN PASSWORD %L', :'agent_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_agent') \gexec

SELECT format('CREATE ROLE coach_ro LOGIN PASSWORD %L', :'ro_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_ro') \gexec

ALTER ROLE coach_app CREATEDB;
ALTER ROLE coach_ro SET default_transaction_read_only = on;
ALTER ROLE coach_ro SET statement_timeout = '30s';

-- coach_app owns the database; on Postgres 15+ the public schema belongs to
-- pg_database_owner, so coach_app can create the tables. Nobody else may create objects.
ALTER DATABASE :"dbname" OWNER TO coach_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE :"dbname" FROM PUBLIC;
SQL
