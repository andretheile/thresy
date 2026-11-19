# Laufcoach (developer guide for coding agents)

A personal running coach: Garmin data in Postgres, and a coding agent that answers chat messages. Read `docs/ARCHITECTURE.md` before changing anything; it is the contract between packages.

## Rules

- This repository will be open source. Never commit anything specific to one athlete: names, places, coordinates, paces, heart-rate values, Garmin activity ids. `legacy/` and `private/` hold such data and are ignored by git. Fixtures in the repo must be synthetic.
- Nothing outside `src/laufcoach/runner/` may depend on a specific coding harness (Claude Code, Codex, Cursor).
- Code, comments and docs are in English. The coach's language is the `COACH_LANGUAGE` setting.
- Do not push to a remote or change the server unless the user asks.

## Commands

The foundation package fills this section in (setup, tests, lint, local database).
