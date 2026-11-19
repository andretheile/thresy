# Importing the old bot's export

`coach import legacy` copies the export of the bot Thresy replaces into Postgres. It is meant
to run once, before the first Garmin sync, but it is safe to run again at any time: it is
idempotent, resumable, and never overwrites data that another source (the Garmin sync) wrote.
It raises no events, so importing years of history does not wake the coach.

## Running it

```sh
# locally: a few dozen activities into the dev database (needs the legacy group for nothing;
# the importer only uses the core dependencies)
uv run coach db migrate
uv run coach import legacy --dir ./legacy --limit 30

# on the server: mount the export read-only into a one-off container of the sync service,
# which has COACH_DATA_DIR (the FIT volume) and the read/write database URL
docker compose run --rm -v /path/to/legacy:/legacy:ro sync \
  coach import legacy --dir /legacy
```

Options:

| Option | Meaning |
|---|---|
| `--dir DIR` | The export (default `COACH_LEGACY_DIR`). |
| `--data-dir DIR` | Where the FIT copies go (default `COACH_DATA_DIR`). |
| `--only PART` | Repeatable: `activities` (summaries), `records` (FIT files: per-second records and laps), `wellness` (daily reports), `prs` (personal records). Default: all four. |
| `--limit N` | Only the N most recent activities (after the filters below). |
| `--id ID` | Only this activity (repeatable). |
| `--since D`, `--until D` | Only activities in this local-date range. |
| `--quiet` | No progress lines on stderr. |
| `--json` | The summary as JSON on stdout. |

The filters restrict activities and therefore FIT files; wellness and personal records are
always imported in full (they are small). The exit code is 1 when any file failed (a FIT file
that cannot be parsed, a payload that contradicts its own date); everything else is imported
anyway and the errors are listed in the summary.

## What it reads

All paths are relative to the export directory.

| Part | Files | Notes |
|---|---|---|
| activities | `overviews/year-*.json`, `overviews/ytd-*.json`, `overviews/activities-*.json`, `activities/*.json` | Garmin activity summaries. The union is imported with `store.upsert_activity(source="legacy")`; when an activity appears in several files the later one in this order wins (the old store's order). |
| records | `tracks/<activity id>.fit` | Parsed with `thresy.fit.parse_fit`, copied to `fit/<YYYY>/<id>.fit` under the data directory (`YYYY` = year of the local start), records and laps stored with `store.replace_activity_records`, which also sets `has_records` and `fit_path`. CSV and GPX files in `tracks/` are ignored. |
| wellness | `daily/20*.json` | Each report holds sleep, HRV, stats and step payloads for two days (`yesterday`, `today`). Every payload is keyed by its **own** calendar date (`dailySleepDTO.calendarDate`, `hrvSummary.calendarDate`, `calendarDate`; step lists keep the report's day), and the report's `synced_at` is passed to `store.upsert_wellness` as the night's `sleep_synced_at`. Reports are applied in file-name order. Error payloads and empty lists are skipped; the pre-sync placeholder (a sleep object with a date but no sleep time) is passed on and the store ignores it, so it neither creates a night nor sets `sleep_synced_at`. `latest*.json` are ignored. |
| prs | `overviews/personal-records.json`, `overviews/pr-hm-marathon.json` | Garmin's list (keys `garmin_<typeId>`) and the old bot's computed half-marathon and marathon bests (keys `computed_<name>`, source `computed`), as the old store kept them. |

## Idempotency, resuming and other sources

- Activities whose stored `raw` payload already equals the legacy payload are skipped
  (`unchanged`); the store's own upsert would not change them either.
- A FIT file is skipped when `fit_path` already points at its copy and the copy is
  byte-identical. Copies are written to a temporary name and renamed, and a file is only
  recorded after its records are stored, so an interrupted run simply continues.
- Processed daily reports are remembered in `sync_state` under `legacy_import.wellness`
  (file name -> SHA-256, plus the wake dates the importer created). A run starts at the first
  report that is new or changed and re-applies every report after it, so later reports still
  win, exactly as in a full run.
- Data from another source is left alone: activities whose `source` is not `legacy`
  (`skipped_other_source`), wellness days the importer did not create, a different FIT file
  already at the destination (`skipped_other_file`), and personal records whose key exists.
  Import before the first Garmin sync to get everything.

## Verifying it

The summary lists counts per part. Then compare with the old store:

```sh
uv run coach sql "select count(*), count(*) filter (where is_run), min(local_date), max(local_date) from activities"
uv run coach sql "select count(*) from activities where has_records"   # = number of FIT files
uv run coach sql "select count(*), min(wake_date), max(wake_date) from wellness_daily"
uv run coach sql "select key, label, value from personal_records order by key"
sqlite3 legacy/runs.db "select count(*), sum(is_run), min(date), max(date) from activities"
sqlite3 legacy/runs.db "select count(*), min(date), max(date) from wellness_daily"
```

The automated checks against the real export run with the legacy tests:

```sh
COACH_LEGACY_DIR=./legacy uv run --group legacy pytest tests/importer tests/analysis -m legacy
```

They import dynamically chosen activities and compare summaries, records, laps and FIT copies
with `runs.db` and the FIT files, import all wellness reports and compare every day with the
old store (and each night's `sleep_synced_at` with the first report that contained it), and
compare personal records.

## Size and duration

Parsing dominates: about one FIT file per second on a laptop (fitparse is pure Python), so an
export with 470 FIT files takes 8 to 10 minutes; summaries, wellness and records together take
a few seconds. A second run takes a few seconds. Per FIT file the database grows by roughly
0.5 MB (records, laps and the primary-key index), and the FIT copies take as much space as the
files in `tracks/`.
