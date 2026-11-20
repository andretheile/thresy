# Thresy

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

How much disk it takes, measured on the first deployment, a consistent amateur runner:

| | |
|---|---|
| History | 6 years, about 780 runs and 8,700 km (between 650 and 2,400 km a year) |
| With per-second data | 473 runs, 492 hours, 1.8 million samples |
| Database | 246 MB, almost all of it the per-second samples |
| Original FIT files | 156 MB |
| Import of that history from files | 7 minutes on a small server |

As a rule of thumb, 1,000 km of running with per-second data is about 40 MB of database and
30 MB of original files, so a decade of training fits in a few gigabytes including backups.
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
- **Libraries** this project leans on most: [fitparse](https://github.com/dtcooper/python-fitparse)
  for FIT files, [python-telegram-bot](https://python-telegram-bot.org/) for the chat,
  [psycopg](https://www.psycopg.org/) for Postgres.
- **Other names.** Telegram, Claude, Codex, Cursor, Grok, Muse and Dots are products of their
  respective owners, with which this project has no affiliation.
- **Not medical advice.** The coach is software that reads training data. It is not a doctor
  or a physiotherapist, and what it says is no substitute for one.

## License

MIT, see [LICENSE](LICENSE).
