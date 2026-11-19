# Thresy architecture

This document is the contract that every work package implements against. If you need to deviate from it, say so in your final report and explain why. Do not silently change a contract another package depends on.

## What we are building

A self-hosted running coach, one athlete per deployment. It syncs Garmin data into Postgres, and an AI coding agent answers chat messages and sends proactive messages (morning briefing, post-run debrief) by working in a project directory with CLI tools.

The code will be published as open source, so the repository must work for any athlete and contain nothing specific to one. The first deployment replaces a bot that ran on a Grok VM; that bot's export is in `legacy/` and is used only for import and for parity checks.

Code, comments and docs are in English. The language the coach speaks is a deployment setting (`COACH_LANGUAGE`; the first deployment uses German).

## Principles

1. **Harness-agnostic.** The agent is whichever coding CLI is configured: Claude Code (the default), Codex, or Cursor. Nothing outside `src/thresy/runner/` may depend on a specific harness. That means:
   - Capabilities are plain CLI commands (`coach …`), not MCP servers or harness-specific tools.
   - Agent instructions live in `AGENTS.md`. `CLAUDE.md` contains only `@AGENTS.md`.
   - Memory is files in the coach workspace plus Postgres, never a harness's own memory feature.
   - Conversation continuity belongs to the bridge (message log in Postgres). Resuming a harness-native session is an optimisation, not a requirement: after a harness switch or a lost session, the bridge starts a new session and supplies recent context from the log.
2. **Transport-agnostic.** Telegram is the first transport. A private web page comes later. The agent never calls Telegram; it writes to the `outbox` table and a transport delivers.
3. **Deterministic sync, LLM on demand.** Syncing Garmin data never involves the LLM. The sync poller writes rows and raises `events`; the bridge decides which events start an agent run.
4. **Postgres is the single store.** No SQLite, Parquet or DuckDB. Raw Garmin payloads are kept as `jsonb` so derived columns can be recomputed.
5. **Open-source ready: nothing athlete-specific in git.** `legacy/` and `private/` are ignored. Everything that belongs to one athlete comes from configuration, the database, or `private/`: names, places, coordinates, segment definitions, race lists, paces, heart-rate values, memory files, Garmin activity ids. None of it may appear in code, tests, docs, fixtures or commit messages. Fixtures in the repo are synthetic. Tests that need real data read it from `COACH_LEGACY_DIR`, pick their inputs dynamically (for example "the first three FIT files"), compare against the legacy store rather than against hard-coded numbers, and skip when the directory is unset or missing.
6. **Subscription auth.** With Claude Code the model is reached through the subscription token (`CLAUDE_CODE_OAUTH_TOKEN`). Never pass `--bare` (bare mode ignores that token).
7. **Least privilege for the agent.** The agent has a shell and reads the web, so treat it as the least trusted part of the system. It gets its own database role (`coach_agent`), an environment built from an allow-list rather than inherited from the service, and no access to Telegram, Garmin or Google credentials or to the volumes that hold them. Anything with an external side effect (Garmin writes, calendar changes) is carried out by a service on the agent's request, never by the agent directly. Secrets are never stored in database tables.

## Components

| Component | Package | Role |
|---|---|---|
| Core | `thresy.config`, `thresy.db`, `thresy.migrate`, `thresy.fit`, `thresy.store` | Settings, connections, migrations, FIT parsing, upserts |
| Legacy import | `thresy.importer` | One-off import of `legacy/` into Postgres |
| Garmin sync | `thresy.garmin`, `thresy.sync` | Auth, polling, wellness and activity ingest, events |
| Analysis | `thresy.analysis` | Segments, intervals, plots |
| Runner | `thresy.runner` | One interface over Claude Code, Codex and Cursor CLIs |
| Transport | `thresy.transport` | Telegram now, web later |
| Bridge | `thresy.bridge` | Chat loop, sessions, event dispatcher, outbox delivery |
| Tools | `thresy.tools` | Weather, calendar, routes, `coach send` |
| Coach workspace | `workspace/` | Template of the directory the agent runs in: `AGENTS.md`, playbooks, memory skeleton |

## Repository layout

```
AGENTS.md  CLAUDE.md  README.md  pyproject.toml  uv.lock
docs/            architecture and per-package notes
migrations/      NNNN_name.sql, applied in order
src/thresy/      the Python package (see Components)
tests/           pytest; tests/<package>/ mirrors src
workspace/       coach workspace template
docker/          Dockerfile(s)
compose.yml      production stack (db + app), no published ports
compose.dev.yml  local dev database only
legacy/          old export, ignored by git
```

## Tech stack

- Python 3.13, managed with `uv`. Lint and format with `ruff`. Tests with `pytest`.
- Postgres 16. Driver: `psycopg` 3. No ORM; write SQL.
- Migrations: plain SQL files plus a small runner that records applied files in `schema_migrations`. No Alembic.
- Settings: `pydantic-settings`, environment variables prefixed `COACH_`, optional `.env`.
- CLI: one entry point `coach` built with `typer`. Each package registers its sub-commands.
- FIT parsing: `fitparse` (parity with the old parser). Garmin API: `garminconnect`, pinned.
- Analysis: `pandas`, `numpy`, `matplotlib`.
- The `runner` package uses the standard library only.

## Database contract

All timestamps are `timestamptz` in UTC unless the column name ends in `_local`. Three roles, all created outside migrations (by the Postgres init script and by the test harness) because creating roles needs privileges the application must not have:

| Role | Used by | Rights |
|---|---|---|
| `coach_app` | sync and bridge services, migrations | owns the schema, read/write |
| `coach_agent` | `coach` commands run by the agent | SELECT on everything; INSERT on `outbox`; INSERT, UPDATE, DELETE on `planned_workouts`, `races`, `segments`, `segment_efforts`; nothing else |
| `coach_ro` | `coach sql` | SELECT only, read-only sessions, statement timeout |

Only the owner can create objects, temporary tables included. `coach_agent` also has USAGE on the id sequences of the tables it may insert into.

```sql
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
-- VIEW runs: activities WHERE is_run, plus distance_km, pace_s_per_km
-- (coalesce(moving_s, duration_s) / km) and pace as 'M:SS/km'.

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

CREATE TABLE wellness_daily (
  wake_date        date PRIMARY KEY,           -- see "Wake date" below
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

CREATE TABLE hrv_readings (                    -- 5-minute readings during sleep
  ts         timestamptz PRIMARY KEY,
  wake_date  date NOT NULL,
  value_ms   real NOT NULL
);

CREATE TABLE sleep_levels (
  start_utc  timestamptz PRIMARY KEY,
  end_utc    timestamptz NOT NULL,
  wake_date  date NOT NULL,
  level_raw  smallint NOT NULL,                -- Garmin activityLevel as delivered
  stage      text NOT NULL                     -- deep | light | rem | awake
);

CREATE TABLE personal_records (
  key text PRIMARY KEY, label text NOT NULL, value double precision,
  activity_id bigint, activity_name text, date date, source text NOT NULL
);

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

CREATE TABLE races (
  id bigserial PRIMARY KEY, date date NOT NULL, name text NOT NULL,
  distance_km real, elev_m real, location text, start_time_local time,
  priority text, goal text, status text NOT NULL DEFAULT 'planned',
  activity_id bigint, result jsonb, url text, notes text
);
CREATE TABLE planned_workouts (
  id bigserial PRIMARY KEY, date date NOT NULL, title text NOT NULL,
  kind text, description text, target jsonb,
  status text NOT NULL DEFAULT 'planned',      -- planned | done | skipped | moved
  activity_id bigint, calendar_event_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_sessions (
  id uuid PRIMARY KEY, chat_id text NOT NULL, runner text NOT NULL,
  native_session_id text, day date NOT NULL, status text NOT NULL DEFAULT 'active',
  started_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz
);
CREATE TABLE agent_runs (
  id bigserial PRIMARY KEY, session_id uuid REFERENCES agent_sessions(id),
  trigger text NOT NULL,                       -- user_message | morning_briefing | post_run | ...
  runner text NOT NULL, model text, status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  cost_usd real, usage jsonb, error_kind text, error_detail text
);
CREATE TABLE messages (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  chat_id text NOT NULL, direction text NOT NULL,   -- in | out
  kind text NOT NULL DEFAULT 'text',                -- text | photo | choice | choice_answer | voice
  text text, attachments jsonb, transport text NOT NULL, transport_msg_id text,
  run_id bigint REFERENCES agent_runs(id)
);
CREATE TABLE outbox (
  id bigserial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
  chat_id text,                                -- NULL = the default chat
  kind text NOT NULL,                          -- text | photo | document | choice
  text text, file_path text, options jsonb, run_id bigint,
  sent_at timestamptz, transport_msg_id text, error text
);
CREATE TABLE events (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
  dedupe_key text UNIQUE, processed_at timestamptz, processed_by text
);
CREATE TABLE sync_state (
  key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
```

Add indexes where queries need them (at least `activities(local_date)`, `hrv_readings(wake_date)`, `messages(chat_id, ts)`, unprocessed `events`, unsent `outbox`).

### Wake date

Garmin keys sleep and HRV by the calendar date on which the athlete woke up. "Last night" is the row whose `wake_date` is today. The old bot repeatedly reported the night before because it read yesterday's key. Every reader and writer uses `wake_date` with this meaning, and `sleep_synced_at` records when a night first arrived so the coach can say "not synced yet" instead of showing stale data.

### Store rules

All writes go through `thresy.store`. Its rules:

- A missing or error payload never overwrites a stored value.
- `steps` never decreases (the current day's count is partial).
- `sleep_synced_at` is set once, the first time real sleep data arrives (`sleepTimeSeconds > 0`; Garmin's pre-sync placeholder does not count).
- Store functions do not insert events. Callers decide from the return values, so importing history raises none.
- Garmin `sleepLevels.activityLevel` maps to 0 = deep, 1 = light, 2 = rem, 3 = awake. This was established from the data; the old bot treated 2 as awake, which is wrong.

### Events

The sync poller inserts; the bridge consumes and sets `processed_at`. `dedupe_key` makes inserts idempotent. Dates and hours inside dedupe keys are UTC, except `wake_date`, which is Garmin's calendar date.

| kind | dedupe_key | payload |
|---|---|---|
| `sleep_arrived` | `sleep:<wake_date>` | `{wake_date}` |
| `new_activity` | `activity:<id>` | `{activity_id, type, is_run}` |
| `garmin_auth_required` | `garmin_auth:<date>` | `{reason}` |
| `sync_failed` | `sync_failed:<date>:<hour>` | `{error}` |

## Runner contract

`thresy.runner` hides the coding CLI. Standard library only.

```python
@dataclass(frozen=True)
class RunRequest:
    prompt: str
    cwd: Path
    session_id: str | None = None            # harness-native id to resume; None starts a new session
    model_tier: Literal["fast", "smart"] = "smart"
    timeout_s: float = 900
    env: Mapping[str, str] = field(default_factory=dict)   # added to the child environment
    system_append: str | None = None         # extra system instructions; prepended to the prompt if the harness has no such option

@dataclass(frozen=True)
class Started:  session_id: str | None
@dataclass(frozen=True)
class Progress: kind: Literal["text", "tool"]; text: str
@dataclass(frozen=True)
class Final:    result: RunResult
RunEvent = Started | Progress | Final

@dataclass(frozen=True)
class RunResult:
    ok: bool
    text: str                                 # the agent's final answer
    session_id: str | None
    error_kind: Literal["rate_limit", "auth", "timeout", "crash", "cancelled", "other"] | None
    error_detail: str | None
    cost_usd: float | None
    usage: dict | None
    duration_s: float
    runner: str
    model: str | None

class AgentRunner(Protocol):
    name: str
    capabilities: RunnerCapabilities          # supports_resume, supports_streaming, supports_system_append
    def run(self, req: RunRequest) -> RunHandle: ...   # an AsyncIterator[RunEvent] with cancel(), result(), aclose()
    async def healthcheck(self) -> Health      # binary found, version, auth looks usable
```

Rules:

- **Events.** `Started` comes at most once and may be absent when the run fails early. The stream ends with exactly one `Final`, including on timeout, crash and `handle.cancel()`. `run` never raises for agent-side failures. If the consuming task itself is cancelled, the run is stopped and `CancelledError` propagates without a `Final`.
- **Stopping.** On timeout or cancellation the whole child process group is stopped (SIGINT, then SIGTERM, then SIGKILL). Once the harness has reported its final result, the runner waits only a short, configurable time for the process to exit before stopping it, so a lingering child cannot hold back an answer that is already there.
- **Environment.** The child does not inherit the service's environment. It gets an allow-list (basic process variables, the harness's own auth and configuration variables, explicitly configured pass-through names) plus the adapter's configured additions and `req.env`. The bridge uses `req.env` to hand the agent its own database URLs and settings.
- **No personal text on the command line.** Prompts and appended system instructions reach the harness through stdin or a private temporary file, not through arguments, because arguments are visible to every user on the host. Where a harness offers no alternative, the adapter's docs say so.
- **Cost** is per run, not cumulative over a resumed session.
- **Selection.** `get_runner(name, config)` returns the adapter for `claude`, `codex`, `cursor` or `fake`. Model tiers map to concrete model names per runner through config. Unattended-permission flags are adapter config because the container is the sandbox.

## Transport contract

```python
class Transport(Protocol):
    name: str
    async def start(self, on_message: Callable[[InboundMessage], Awaitable[None]]) -> None
    async def send(self, item: OutboundItem) -> str      # returns transport message id
    async def set_busy(self, chat_id: str, busy: bool) -> None   # e.g. typing indicator
```

Only allow-listed chat ids are accepted; everything else is dropped.

## CLI contract

`coach db migrate` · `coach import legacy [--limit N]` · `coach sync once|poll|backfill` · `coach sql "<select>"` (read-only role) · `coach send text|photo|choice …` (writes `outbox`) · `coach bridge run` · `coach runner check`. Commands print human-readable text by default and JSON with `--json`.

## Configuration

`COACH_DATABASE_URL`, `COACH_DATABASE_URL_RO`, `COACH_DATA_DIR` (FIT files, plots), `COACH_WORKSPACE_DIR`, `COACH_TZ` (default `UTC`), `COACH_LANGUAGE` (default `en`), `COACH_NAME` (default `Thresy`, the name the coach uses for itself), `COACH_RUNNER` (default `claude`), `COACH_TELEGRAM_TOKEN`, `COACH_TELEGRAM_CHAT_IDS`. `COACH_AGENT_DATABASE_URL` is the `coach_agent` URL the bridge hands to the agent as its `COACH_DATABASE_URL`. `COACH_LEGACY_DIR` points tests and the importer at the old export.

Per runner (`<NAME>` is `CLAUDE`, `CODEX` or `CURSOR`): `COACH_<NAME>_BIN`, `COACH_<NAME>_ARGS`, `COACH_<NAME>_PERMISSION_ARGS`, `COACH_<NAME>_MODEL_FAST`, `COACH_<NAME>_MODEL_SMART`, `COACH_<NAME>_ENV`. A package that needs more settings defines its own settings class from `thresy.config.SETTINGS_CONFIG`.

Garmin (sync only): `COACH_GARMIN_EMAIL`, `COACH_GARMIN_PASSWORD`, `COACH_GARMIN_TOKEN_DIR`. Model credential (bridge only), by harness: `CLAUDE_CODE_OAUTH_TOKEN`, `CODEX_API_KEY`, `CURSOR_API_KEY`. In `Settings` an empty value counts as unset.

Compose-level variables, never passed into app containers: `DB_SUPERUSER_PASSWORD`, `DB_APP_PASSWORD`, `DB_AGENT_PASSWORD`, `DB_RO_PASSWORD`, `AGENT_CLI`, `AGENT_CLI_VERSION` (mandatory; `latest` opts out of pinning), `DB_MEM_LIMIT`, `SYNC_MEM_LIMIT`, `BRIDGE_MEM_LIMIT`.

## Deployment

One Compose stack on the server, no published ports:

- `db`: postgres:16-alpine, named volume, internal network only.
- `migrate`: one-shot, applies migrations; the other app services start after it succeeds.
- `sync` and `bridge`: the same app image, on the internal network plus an egress network. Non-root user, `init: true` so the agent's stray child processes are reaped, a memory limit on every service because the host is shared and has no swap.

Each service gets only the environment and volumes it needs; there is no blanket `env_file`. Garmin credentials and tokens exist only in `sync` (own volume). The Telegram token and the model credential exist only in `bridge`. The agent's workspace and home directory are volumes of `bridge`.

Volumes: `db-data`; `app-data` (`COACH_DATA_DIR`, original FIT files: read/write in `sync`, read-only in `bridge`, so the agent cannot alter source data); `garmin-auth` (`sync` only); `workspace` and `agent-home` (`bridge` only). Files the agent produces, such as plots, go under `out/` in the workspace, and `coach send` accepts paths only from there. `bridge` holds all three database URLs and hands the agent only the `coach_agent` and `coach_ro` ones. `ANTHROPIC_API_KEY` must stay unset where subscription auth is used, because it takes precedence over the subscription token. The agent CLI version is pinned in the image and upgraded deliberately.

Backups (planned): nightly Postgres dump, original FIT files and the coach's memory files to an object-storage bucket, written by a credential that can create objects but not read or delete them.

## Rules for work packages

- Stay inside the paths your package owns. If you need a change elsewhere, report it instead of making it.
- Shared files have one owner, the foundation package: `pyproject.toml`, `uv.lock`, `.env.example`, `compose.yml`, `compose.dev.yml`, `docker/`, `migrations/`, `tests/conftest.py`, `src/thresy/config.py`, `src/thresy/cli.py`. Other packages list what they need from these in their report (a dependency, a variable, a volume, a table change).
- Do not push to any remote, and do not touch the server.
- The development Mac has about 6 GB of free disk. Do not build the app image locally, do not pull large images, and do not run the full legacy import locally (use `--limit`).
- Every package ships tests, and `uv run ruff check` and `uv run pytest` pass before you report.
- Report honestly: what you verified by running it, what you could not verify, and every deviation from this document.
