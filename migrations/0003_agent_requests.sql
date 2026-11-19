-- 0003_agent_requests: requests from the agent (or the bridge) to a service that may reach the
-- outside world, see "Agent requests" in docs/ARCHITECTURE.md.
--
-- A `coach` command inserts a row; the owning service (sync for `sync_now`) claims it, sets
-- status, and writes result or error. Run as coach_app, the owner of the database.

CREATE TABLE agent_requests (
  id bigserial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'pending',      -- pending | running | done | failed
  result jsonb, error text, run_id bigint,
  started_at timestamptz, finished_at timestamptz, handled_by text
);
-- Services poll for their pending kinds every few seconds.
CREATE INDEX agent_requests_pending_idx ON agent_requests (kind, created_at, id)
  WHERE status = 'pending';

-- Privileges (roles table in docs/ARCHITECTURE.md): coach_agent may read requests and create
-- them, but not change or delete them. INSERT is granted on the request columns only, so the
-- agent cannot create a row that already looks claimed or finished (status, result,
-- handled_by, ...) or pick its own id; everything else takes its default.
-- coach_ro reads it through the default privileges of 0001_init.

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_agent') THEN
    GRANT SELECT ON agent_requests TO coach_agent;
    GRANT INSERT (kind, payload) ON agent_requests TO coach_agent;
    GRANT USAGE ON SEQUENCE agent_requests_id_seq TO coach_agent;
  ELSE
    RAISE NOTICE 'role coach_agent does not exist; agent grants skipped';
  END IF;
END
$$;
