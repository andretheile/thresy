# Thresy

A self-hosted running coach for one athlete. Thresy syncs your Garmin data (activities with
per-second records, sleep, HRV, steps) into Postgres, and a coding agent (Claude Code, Codex
or Cursor) answers your chat messages and sends a morning briefing and post-run debriefs by
working with that data through plain command-line tools.

The name comes from "threshold", as in threshold runs.

Status: early development, not yet run in production. In place: the core (configuration,
database schema, migrations, FIT parsing, write API), the runner layer over the coding-agent
CLIs, Garmin sync, the importer for the bot Thresy replaces, analysis commands, the Telegram
bridge, backups, and the Compose stack.

## How it fits together

- **Postgres** is the single store, including the raw Garmin payloads.
- **Sync** polls Garmin without any LLM involvement and records events such as "new activity"
  or "sleep arrived".
- **Bridge** turns chat messages and events into agent runs; the agent answers through an
  outbox that a transport (Telegram first) delivers.
- The agent is treated as the least trusted part: it works with its own database role
  (read everything, write only plans, races, segments and outgoing messages), runs as its own
  Unix user, and never sees the Garmin, Telegram or database credentials of the services.
- **Backup** dumps the database and uploads it, the FIT files and the coach's memory files to a
  bucket with a create-only credential, optionally encrypted with age.
- Everything runs as one Docker Compose stack with no published ports.

The design, database contract and rules for contributors are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); [AGENTS.md](AGENTS.md) is the short guide for
coding agents working on this repository.

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

## License

MIT, see [LICENSE](LICENSE).
