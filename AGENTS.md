# Thresy (developer guide for coding agents)

A personal running coach: Garmin data in Postgres, and a coding agent that answers chat messages. Read `docs/ARCHITECTURE.md` before changing anything; it is the contract between packages.

## Rules

- This repository will be open source. Never commit anything specific to one athlete: names, places, coordinates, paces, heart-rate values, Garmin activity ids. `legacy/` and `private/` hold such data and are ignored by git. Fixtures in the repo must be synthetic.
- Nothing outside `src/thresy/runner/` may depend on a specific coding harness (Claude Code, Codex, Cursor).
- Code, comments and docs are in English. The coach's language is the `COACH_LANGUAGE` setting.
- Do not push to a remote or change the server unless the user asks.

## Commands

```sh
uv sync                                     # set up .venv (dev tools included)
uv sync --group legacy                      # plus pyarrow, only for the legacy parity tests
cp .env.example .env                        # local settings matching compose.dev.yml
docker compose -f compose.dev.yml up -d     # start the dev Postgres (127.0.0.1:55432)
docker compose -f compose.dev.yml down      # stop it (add -v to delete its data)
uv run coach db migrate                     # apply migrations/ (coach db status shows state)
uv run pytest                               # all tests; database tests skip if Postgres is down
COACH_LEGACY_DIR=./legacy uv run --group legacy pytest tests/legacy_parity   # parity with the old bot
uv run ruff check                           # lint
uv run ruff format                          # format (CI-style check: ruff format --check)
```

Tests create a fresh database per session on the dev Postgres and drop it afterwards
(override the server with `COACH_TEST_ADMIN_URL`). Do not build the app image locally; check
Compose files with `docker compose -f compose.yml config`.
