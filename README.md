# Thresy

A self-hosted running coach for one athlete. Thresy syncs your Garmin data (activities with
per-second records, sleep, HRV, steps) into Postgres, and a coding agent (Claude Code, Codex
or Cursor) answers your chat messages and sends a morning briefing and post-run debriefs by
working with that data through plain command-line tools.

The name comes from "threshold", as in threshold runs.

Status: early development. The core (configuration, database schema, migrations, FIT parsing
and the write API) is in place; Garmin sync, analysis and the chat bridge are being built.

## How it fits together

- **Postgres** is the single store, including the raw Garmin payloads.
- **Sync** polls Garmin without any LLM involvement and records events such as "new activity"
  or "sleep arrived".
- **Bridge** turns chat messages and events into agent runs; the agent answers through an
  outbox that a transport (Telegram first) delivers.
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

`compose.yml` runs Postgres, a one-shot migration and two services from one application image:
`sync` (`coach sync poll`) and `bridge` (`coach bridge run`). Copy `.env.example` to `.env`, set
the database passwords and tokens, then `docker compose up -d --build`. The coding-agent CLI in
the image is chosen with the `AGENT_CLI` build argument (`claude`, `codex`, `cursor` or `none`).

## License

MIT, see [LICENSE](LICENSE).
