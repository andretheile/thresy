-- 0001_init: the database contract from docs/ARCHITECTURE.md.
--
-- Runs inside one transaction (see thresy.migrate). Run it as coach_app so that coach_app owns
-- every object. Timestamps are timestamptz in UTC unless the column name ends in _local.

-- updated_at maintenance ---------------------------------------------------------------------

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

-- Activities ---------------------------------------------------------------------------------

CREATE TABLE activities (
  id              bigint PRIMARY KEY,          -- Garmin activityId
  start_utc       timestamptz,
  start_local     timestamp,
  local_date      date NOT NULL,
  type            text NOT NULL,               -- Garmin typeKey
  is_run          boolean NOT NULL,
  name            text,
  distance_m      double precision,
  duration_s      double precision,
  moving_s        double precision,
  elapsed_s       double precision,
  avg_hr          real,
  max_hr          real,
  elev_gain_m     real,
  elev_loss_m     real,
  avg_cadence_spm real,
  avg_power_w     real,
  vo2max          real,
  aerobic_te      real,
  anaerobic_te    real,
  training_load   real,
  calories        real,
  location        text,
  has_records     boolean NOT NULL DEFAULT false,
  fit_path        text,                        -- relative to COACH_DATA_DIR
  raw             jsonb,                       -- Garmin summary payload
  source          text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activities_local_date_idx ON activities (local_date);
CREATE INDEX activities_start_utc_idx ON activities (start_utc);
CREATE INDEX activities_runs_local_date_idx ON activities (local_date) WHERE is_run;
CREATE TRIGGER activities_updated_at BEFORE UPDATE ON activities
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Runs only, with distance in km and pace. Pace uses moving time, falling back to duration
-- when moving time is missing or zero. Note: "a.*" is expanded when the view is created, so a
-- migration that adds columns to activities must recreate this view.
CREATE VIEW runs AS
SELECT a.*,
       a.distance_m / 1000.0 AS distance_km,
       p.pace_s_per_km,
       CASE WHEN p.pace_s_per_km IS NOT NULL THEN
         (floor(p.pace_s_per_km / 60))::int || ':'
         || lpad((floor(p.pace_s_per_km)::bigint % 60)::text, 2, '0') || '/km'
       END AS pace
FROM activities a
CROSS JOIN LATERAL (
  SELECT CASE WHEN a.distance_m > 0 AND coalesce(nullif(a.moving_s, 0), a.duration_s) > 0
              THEN coalesce(nullif(a.moving_s, 0), a.duration_s) / (a.distance_m / 1000.0)
         END AS pace_s_per_km
) p
WHERE a.is_run;

CREATE TABLE activity_records (                -- per-second samples from the FIT file
  activity_id  bigint NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  ts           timestamptz NOT NULL,
  elapsed_s    real NOT NULL,
  lat          double precision,
  lon          double precision,
  alt_m        real,
  dist_m       real,
  speed_mps    real,
  hr           smallint,
  cadence_spm  real,                           -- steps per minute, both feet
  power_w      real,
  resp_rate    real,
  temp_c       real,
  PRIMARY KEY (activity_id, ts)
);

CREATE TABLE activity_laps (
  activity_id  bigint NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  lap_no       smallint NOT NULL,
  start_utc    timestamptz,
  duration_s   real,                           -- timer time
  elapsed_s    real,
  dist_m       real,
  avg_speed_mps real,
  max_speed_mps real,
  avg_hr       real,
  max_hr       real,
  avg_cadence_spm real,
  avg_power_w  real,
  ascent_m     real,
  descent_m    real,
  avg_temp_c   real,
  intensity    text,
  trigger      text,
  wkt_step     smallint,
  PRIMARY KEY (activity_id, lap_no)
);

-- Wellness (keyed by wake date: the calendar day on which the athlete woke up) -----------------

CREATE TABLE wellness_daily (
  wake_date        date PRIMARY KEY,
  sleep_score      smallint,
  sleep_s          integer,
  deep_s           integer,
  light_s          integer,
  rem_s            integer,
  awake_s          integer,
  sleep_start_utc  timestamptz,
  sleep_end_utc    timestamptz,
  hrv_night_avg    real,
  hrv_weekly_avg   real,
  hrv_status       text,
  hrv_balanced_low real,
  hrv_balanced_high real,
  resting_hr       real,
  steps            integer,
  stress_avg       real,
  body_battery_charged smallint,
  sleep_synced_at  timestamptz,                -- when this night's sleep first appeared
  raw_sleep        jsonb,
  raw_hrv          jsonb,
  raw_stats        jsonb,
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER wellness_daily_updated_at BEFORE UPDATE ON wellness_daily
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE hrv_readings (                    -- 5-minute readings during sleep
  ts         timestamptz PRIMARY KEY,
  wake_date  date NOT NULL,
  value_ms   real NOT NULL
);
CREATE INDEX hrv_readings_wake_date_idx ON hrv_readings (wake_date);

CREATE TABLE sleep_levels (
  start_utc  timestamptz PRIMARY KEY,
  end_utc    timestamptz NOT NULL,
  wake_date  date NOT NULL,
  level_raw  smallint NOT NULL,                -- Garmin activityLevel as delivered
  stage      text NOT NULL                     -- deep | light | rem | awake
);
CREATE INDEX sleep_levels_wake_date_idx ON sleep_levels (wake_date);

CREATE TABLE personal_records (
  key text PRIMARY KEY, label text NOT NULL, value double precision,
  activity_id bigint, activity_name text, date date, source text NOT NULL
);

-- Segments -----------------------------------------------------------------------------------

CREATE TABLE segments (
  key text PRIMARY KEY, name text NOT NULL,
  start_lat double precision NOT NULL, start_lon double precision NOT NULL,
  end_lat double precision NOT NULL, end_lon double precision NOT NULL,
  tol_m real NOT NULL DEFAULT 30, dist_min_m real, dist_max_m real,
  max_s real NOT NULL DEFAULT 1800, uphill boolean NOT NULL DEFAULT true, notes text
);
CREATE TABLE segment_efforts (
  segment_key text NOT NULL REFERENCES segments(key) ON DELETE CASCADE,
  activity_id bigint NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  start_utc timestamptz NOT NULL,
  duration_s real, dist_m real, gain_m real, avg_hr real, max_hr real,
  hr_last60 real, pace_s_per_km real,
  PRIMARY KEY (segment_key, activity_id, start_utc)
);
CREATE INDEX segment_efforts_activity_idx ON segment_efforts (activity_id);

-- Planning -----------------------------------------------------------------------------------

CREATE TABLE races (
  id bigserial PRIMARY KEY, date date NOT NULL, name text NOT NULL,
  distance_km real, elev_m real, location text, start_time_local time,
  priority text, goal text, status text NOT NULL DEFAULT 'planned',
  activity_id bigint, result jsonb, url text, notes text
);
CREATE INDEX races_date_idx ON races (date);

CREATE TABLE planned_workouts (
  id bigserial PRIMARY KEY, date date NOT NULL, title text NOT NULL,
  kind text, description text, target jsonb,
  status text NOT NULL DEFAULT 'planned',      -- planned | done | skipped | moved
  activity_id bigint, calendar_event_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX planned_workouts_date_idx ON planned_workouts (date);
CREATE TRIGGER planned_workouts_updated_at BEFORE UPDATE ON planned_workouts
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Agent, chat, outbox, events ----------------------------------------------------------------

CREATE TABLE agent_sessions (
  id uuid PRIMARY KEY, chat_id text NOT NULL, runner text NOT NULL,
  native_session_id text, day date NOT NULL, status text NOT NULL DEFAULT 'active',
  started_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz
);
CREATE INDEX agent_sessions_chat_day_idx ON agent_sessions (chat_id, day);

CREATE TABLE agent_runs (
  id bigserial PRIMARY KEY, session_id uuid REFERENCES agent_sessions(id),
  trigger text NOT NULL,                       -- user_message | morning_briefing | post_run | ...
  runner text NOT NULL, model text, status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  cost_usd real, usage jsonb, error_kind text, error_detail text
);
CREATE INDEX agent_runs_session_idx ON agent_runs (session_id);
CREATE INDEX agent_runs_started_at_idx ON agent_runs (started_at);

CREATE TABLE messages (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  chat_id text NOT NULL, direction text NOT NULL,   -- in | out
  kind text NOT NULL DEFAULT 'text',                -- text | photo | choice | choice_answer | voice
  text text, attachments jsonb, transport text NOT NULL, transport_msg_id text,
  run_id bigint REFERENCES agent_runs(id)
);
CREATE INDEX messages_chat_ts_idx ON messages (chat_id, ts);

CREATE TABLE outbox (
  id bigserial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
  chat_id text,                                -- NULL = the default chat
  kind text NOT NULL,                          -- text | photo | document | choice
  text text, file_path text, options jsonb, run_id bigint,
  sent_at timestamptz, transport_msg_id text, error text
);
CREATE INDEX outbox_unsent_idx ON outbox (created_at, id) WHERE sent_at IS NULL;

CREATE TABLE events (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
  dedupe_key text UNIQUE, processed_at timestamptz, processed_by text
);
CREATE INDEX events_unprocessed_idx ON events (ts, id) WHERE processed_at IS NULL;

CREATE TABLE sync_state (
  key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER sync_state_updated_at BEFORE UPDATE ON sync_state
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Privileges ---------------------------------------------------------------------------------
-- The roles are created outside migrations (docker/postgres/initdb/10-roles.sh, or the test
-- harness), because creating roles needs privileges the application role must not have.
-- coach_ro may read everything and write nothing. Default privileges make tables created by
-- later migrations (run by the same role) readable too. If a role does not exist, the grant is
-- skipped with a notice so that the schema also works on a plain single-user database.

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_ro') THEN
    GRANT USAGE ON SCHEMA public TO coach_ro;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO coach_ro;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO coach_ro;
  ELSE
    RAISE NOTICE 'role coach_ro does not exist; read-only grants skipped';
  END IF;

  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'coach_app')
     AND current_user <> 'coach_app' THEN
    -- Migrations were run by another role (for example a superuser): let coach_app work anyway.
    GRANT USAGE ON SCHEMA public TO coach_app;
    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO coach_app;
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO coach_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO coach_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO coach_app;
  END IF;
END
$$;
