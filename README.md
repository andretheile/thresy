# Thresy

[![CI](https://github.com/andretheile/thresy/actions/workflows/ci.yml/badge.svg)](https://github.com/andretheile/thresy/actions/workflows/ci.yml)

A self-hosted running coach for one athlete. Thresy syncs your Garmin data (activities with
per-second records, sleep, HRV, steps) into Postgres, and a coding agent (Claude Code, Codex
or Cursor) answers your chat messages and sends a morning briefing and post-run debriefs by
working with that data through plain command-line tools.

The name comes from "threshold", as in threshold runs.

Status: early. Thresy has run in one deployment since November 2026, with Claude Code as the
agent. Expect rough edges, and read "Which agents it works with" and "Credits and disclaimers"
below before relying on it.

Thresy is not a Garmin product and is not affiliated with Garmin. It reads your own data
through an unofficial community library, which can stop working at any time.

## How it fits together

- **Postgres** is the single store, including the raw Garmin payloads.
- **Sync** polls Garmin without any LLM involvement and records events such as "new activity"
  or "sleep arrived".
- **Bridge** turns chat messages and events into agent runs; the agent answers through an
  outbox that a transport (Telegram first) delivers.
- The agent is treated as the least trusted part: it works with its own database role
  (read everything, write only plans, races, segments and outgoing messages), runs as its own
  Unix user, and never sees the Garmin, Telegram or database credentials of the services.
- **Backup** dumps the database and uploads it, the FIT files and the coach's memory files and
  kept scripts to a bucket with a create-only credential, optionally encrypted with age.
- **Routes**: the coach plans running routes with openrouteservice (a free key, or your own
  instance) and hands them over as GPX files with a map picture, reads GPX files you send it, and
  says how much of a route you have run before (computed from your data, locally). You import
  the files into komoot or Garmin Connect yourself ([docs/routes.md](docs/routes.md)).
- Everything runs as one Docker Compose stack with no published ports.

The design, database contract and rules for contributors are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); [AGENTS.md](AGENTS.md) is the short guide for
coding agents working on this repository.

## Which agents it works with

Nothing outside one small package (`thresy.runner`) knows which agent is in use. What the coach
is and knows lives in plain things any agent can work with: a brief in `AGENTS.md`, playbooks
and memory files in a workspace directory, the data in Postgres, and `coach …` commands on the
shell.

| Agent | State |
|---|---|
| Claude Code | Verified with real runs; used in the first deployment, on a Claude subscription |
| Codex CLI, Cursor CLI | Adapters written from each tool's documentation; not yet run against the real CLI ([docs/runners.md](docs/runners.md)) |
| Hosted autonomous agents with their own persistent computer, such as xAI's Grok Bots, Meta's Muse or OpenAI's Dots | Should work, untested (see below) |
| Sandboxes that are thrown away between sessions, such as Claude Code cloud sessions | Not on their own: they cannot hold the data (see "A machine that keeps its disk") |

Thresy began as a coach running on one of those hosted agents, and the workspace is built so it
can go back: an agent that has a shell on a machine with the `coach` commands and a restricted
database URL can read the brief and coach from it. Such a system brings its own chat and its
own scheduling, so it would replace Thresy's bridge and Telegram transport rather than run
under them, and the isolation Thresy sets up around the agent (its own Unix user and database
role) is then yours to arrange on that platform. Nobody has tried this yet with the current
code. If you do, a report or a pull request is welcome.

## A machine that keeps its disk

Whatever runs Thresy has to be a machine whose disk survives between conversations: a small
server, a home machine that stays on, or a hosted agent whose computer is persistent. The
predecessor of this project ran on such a hosted agent, and that persistence is why it worked.
Three things live on that disk and cannot sensibly be fetched fresh each time:

- **The training history.** Per-second recordings add up, and Garmin's unofficial interface
  is slow and easily upset: fetching history from Garmin (`coach sync backfill`) waits between
  requests on purpose and can take hours. Downloading it again for every session is not an
  option.
- **The Garmin session.** Thresy logs in once and then keeps a session token. A machine that
  forgets the token must log in with the password again, and repeated password logins are
  what gets an account locked.
- **What the coach has learned.** Its memory files, the plan, and the analysis scripts it
  kept are what make it one coach over time instead of a new one every day.

An environment that is rebuilt from the repository for each session and can only make web
requests (a typical cloud coding sandbox) therefore cannot host Thresy by itself. It could act
as the agent in front of a persistent Thresy elsewhere, which would need a network interface
to the `coach` commands that does not exist yet.

Persistence does not have to mean one machine. The same three things could live in managed
services instead, for example a hosted Postgres such as Google Cloud SQL plus an object-storage
bucket, and then the compute becomes replaceable: the sync could run as a scheduled container,
and the agent could be anything with a shell and the `coach` commands, including an agentic IDE
such as Google's Antigravity or a command-line agent such as Gemini CLI. Thresy is part of the
way there (one Postgres as the single store, backups already go to a bucket) but not all of it:
the original FIT files, the Garmin session token and the coach's workspace are still files on a
local disk, the sync is a long-running process, and there is no runner adapter for a Google
agent yet. Nobody has built or tried this variant; it is noted here as a direction.

How much disk it takes, measured on the first deployment, a consistent amateur runner:

| | |
|---|---|
| History | 6 years, about 780 runs and 8,700 km (between 650 and 2,400 km a year), plus about 340 other activities |
| Recorded samples | 1,040 activities, about 1,030 hours, 2.9 million samples |
| Sleep and HRV | 2,150 days, with HRV for the 1,400 most recent nights |
| Database | 400 MB, almost all of it the recorded samples |
| Original FIT files | 243 MB |
| Fetching all of it from Garmin | about four hours and 7,000 requests, slow on purpose |
| Importing three years of it from files instead | 7 minutes on a small server |

As a rule of thumb, 1,000 km of running recorded every second is about 40 MB of database and
30 MB of original files (older watches that record less often need less), so a decade of
training fits in a few gigabytes including backups.
Memory is the tighter constraint: the four services used about 0.5 GB while idle on that
deployment, and more while the agent works; [docs/deploy.md](docs/deploy.md) lists the limits.

## Local development

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync                                       # create .venv with all dependencies
cp .env.example .env                          # local settings (match compose.dev.yml)
docker compose -f compose.dev.yml up -d       # Postgres on 127.0.0.1:55432
uv run coach db migrate                       # create the schema
uv run pytest                                 # tests (database tests need the dev Postgres)
uv run ruff check && uv run ruff format --check
```

`coach --help` lists the available commands. Commands print text by default and JSON with
`--json`.

Two scripts run the tests that need Linux or tools a development machine may lack, each in a
throwaway container (they need Docker; nothing is kept): `sh docker/isolation-test.sh` (the
agent's separate Unix user, with the real setuid helper) and `sh docker/backup-test.sh` (backups
with the real `pg_dump`, `pg_restore` and `age`, against the development Postgres).

Continuous integration ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the same
commands on every push to `main` and every pull request: lint and format check, the whole test
suite against the development Postgres, both scripts above, and a build of the application image
without an agent CLI (`AGENT_CLI=none`). It needs no secrets and downloads only images and
packages; it never reaches Garmin, Telegram or a model provider.

## Deployment

[docs/deploy.md](docs/deploy.md) walks through a first deployment step by step. In short:
`compose.yml` runs Postgres, a one-shot migration and three services from one application
image: `sync` (`coach sync poll`), `bridge` (`coach bridge run`) and `backup`
(`coach backup schedule`). Compose only uses `.env` to fill in `compose.yml`; each service
receives just the variables and volumes it needs (Garmin credentials only in `sync`, the Telegram
token only in `bridge`, the backup key only in `backup`). Inside `bridge` the agent CLI runs as a
separate Unix user whose model credential is in a file only that user can read; `coach runner
check --isolation` verifies the separation on the server.

The coding-agent CLI in the image is chosen with the `AGENT_CLI` build argument (`claude`,
`codex`, `cursor` or `none`) and pinned with `AGENT_CLI_VERSION`; upgrade it by changing the
version and rebuilding. With Claude Code, use a subscription token (`claude setup-token`) and
leave `ANTHROPIC_API_KEY` unset. Backups: [docs/backup.md](docs/backup.md).

## Credits and disclaimers

- **Garmin.** Thresy is an independent project. It is not affiliated with, endorsed by or
  supported by Garmin. Garmin's official developer programme is meant for approved business
  integrations, not for an individual reading their own data, so Thresy uses the community
  library [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) by Ron
  Klinkien and its contributors (MIT), which talks to the same web services as Garmin's own
  apps. Thanks to them: without that library this project would not exist. Because the interface is unofficial, it can change or stop
  working without notice. Use Thresy only with your own account, at your own risk, and check
  that this use is compatible with Garmin's terms for you. Garmin and Garmin Connect are
  trademarks of Garmin Ltd. or its subsidiaries.
- **Weather** comes from [Open-Meteo](https://open-meteo.com/), whose data is licensed under
  CC BY 4.0 and whose free service is for non-commercial use.
- **Routes** are computed by [openrouteservice](https://openrouteservice.org/) (© openrouteservice.org
  by HeiGIT; results licensed CC BY 4.0) on map data © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors (ODbL). A route request sends the routing service the coordinates of the start,
  the points to pass and the end (the usual start is often your home), and nothing else: no
  history, names or activity ids. Route pictures use the standard OpenStreetMap tiles by default,
  a few per picture, cached, within the
  [tile usage policy](https://operations.osmfoundation.org/policies/tiles/); the tile server sees
  which area is shown. `COACH_ROUTE_TILE_URL=none` switches them off. Thanks to both projects and
  to everyone who maps.
- **Libraries** this project leans on most: [fitparse](https://github.com/dtcooper/python-fitparse)
  for FIT files, [python-telegram-bot](https://python-telegram-bot.org/) for the chat,
  [psycopg](https://www.psycopg.org/) for Postgres.
- **Other names.** Telegram, Claude, Codex, Cursor, Grok, komoot, Muse and Dots are products of
  their respective owners, with which this project has no affiliation.
- **Not medical advice.** The coach is software that reads training data. It is not a doctor
  or a physiotherapist, and what it says is no substitute for one.

## License

MIT, see [LICENSE](LICENSE).
