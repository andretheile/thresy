-- 0002_agent_role: privileges of coach_agent, the role the agent's own `coach` commands use.
--
-- Rights (docs/ARCHITECTURE.md, roles table): SELECT on everything; INSERT on outbox;
-- INSERT, UPDATE, DELETE on planned_workouts, races, segments, segment_efforts; nothing else.
-- The role itself is created outside migrations (docker/postgres/initdb/10-roles.sh, or the
-- test harness). Run as coach_app, the owner of the database.

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_agent') THEN
    GRANT USAGE ON SCHEMA public TO coach_agent;

    GRANT SELECT ON ALL TABLES IN SCHEMA public TO coach_agent;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO coach_agent;

    GRANT INSERT ON outbox TO coach_agent;
    GRANT INSERT, UPDATE, DELETE ON planned_workouts, races, segments, segment_efforts
      TO coach_agent;
    -- nextval() for the bigserial ids of the tables it may insert into
    GRANT USAGE ON SEQUENCE outbox_id_seq, planned_workouts_id_seq, races_id_seq TO coach_agent;
  ELSE
    RAISE NOTICE 'role coach_agent does not exist; agent grants skipped';
  END IF;

  -- No role but the owner may create temporary tables either (CREATE on the public schema is
  -- already revoked from PUBLIC on Postgres 15+ and by the init script).
  EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END
$$;
