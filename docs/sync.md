# Garmin sync

`thresy.garmin` talks to Garmin Connect; `thresy.sync` writes what it gets into Postgres and raises events. No LLM is involved. The service is `coach sync poll`, running as the `sync` Compose service, the only one with Garmin credentials and the Garmin token volume.

What it fixes compared with the old once-a-day bot:

- **The right night.** Garmin keys sleep by the date you woke up. The sync fetches today's local date (`COACH_TZ`) as "last night", and raises `sleep_arrived` only for that date.
- **No stale mornings.** The poller keeps looking, and looks more often in the morning until last night's sleep has really arrived (not Garmin's empty placeholder).
- **Runs noticed right away.** A new activity is picked up on the next pass (every 20 minutes, or immediately on request) and raises `new_activity` once its FIT file is stored.

## Commands

| Command | What it does |
|---|---|
| `coach sync poll` | The service: passes on a schedule, answers sync requests, stops cleanly on SIGTERM. |
| `coach sync once [--json]` | One pass now. Refused while the poller runs; use `request` then. |
| `coach sync request [--wait SECONDS] [--json]` | Ask the poller for a pass now and print what came in. Works with the agent's database role. |
| `coach sync login [--code CODE] [--restart] [--json]` | Log in to Garmin and save the session (see [Logging in](#logging-in)). |
| `coach sync backfill --since DATE [--until DATE] [--wellness/--no-wellness] [--fit/--no-fit] [--restart] [--json]` | Import history, slowly and resumably, without events. |
| `coach sync status [--json]` | Last success, last error, login and token state, pending requests, whether last night has arrived. |

Exit codes: 0 success, 1 failure, 3 not finished yet (a login waiting for its verification code, or `request --wait` timing out while the request is still pending).

On a database that has not been migrated (a missing table or column), the commands that use it stop with one line instead of a traceback: ``error: database is not migrated (relation "agent_requests" does not exist); run `coach db migrate` ``.

## One pass

1. **Activities.** List the activities of the last `COACH_SYNC_LOOKBACK_DAYS` local days and upsert each (`source = 'garmin'`). For every listed activity that has neither records nor a stored file, download the original, extract the FIT file, save it as `fit/<YYYY>/<activity_id>.fit` under `COACH_DATA_DIR` (`YYYY` = year of the local start) and store records and laps. A download that fails is retried on later passes, at most `COACH_SYNC_FIT_MAX_ATTEMPTS` times; an activity without a file (HTTP 404, e.g. manual entries) or with an unusable file is not retried. A FIT file that cannot be parsed is kept (`fit_path` set, `has_records` false) so it can be parsed again later.
2. **Wellness.** Today's local date: sleep, HRV and daily stats on every pass. Yesterday and up to `COACH_SYNC_RECHECK_DAYS` older days: only what is still incomplete, namely sleep not synced, no HRV, or a step count fetched before the day was over (a day's count is final once fetched `COACH_SYNC_STEPS_FINAL_AFTER_H` hours after it ended). Days older than yesterday are rechecked at most every `COACH_SYNC_RECHECK_INTERVAL_S`. The 15-minute step intervals are fetched only when the daily stats have no step count. Empty answers (Garmin's HTTP 204) are treated as "no data" and never overwrite stored values; the store keeps step counts from decreasing.
3. **Personal records** every `COACH_SYNC_PR_REFRESH_H` hours and after every new run.

A pass needs about 6 to 10 requests. Garmin errors other than "not found" end the pass; everything stored until then stays stored, and the next pass carries on.

## Events

| kind | dedupe key | when |
|---|---|---|
| `sleep_arrived` | `sleep:<wake_date>` | Real sleep (`sleepTimeSeconds > 0`) for **today's** wake date is stored for the first time. Raised in the same transaction as the sleep row, so it can be neither lost nor doubled. Never for yesterday or older nights, never from a backfill. |
| `new_activity` | `activity:<id>` | An activity first inserted by a pass (not by the importer or a backfill), raised once its FIT file is stored or given up, and only if it started at most `COACH_SYNC_NEW_ACTIVITY_MAX_AGE_H` hours ago. A first sync of a long history therefore raises events only for the last two days. Payload `{activity_id, type, is_run}`. |
| `garmin_auth_required` | `garmin_auth:<UTC date>` | Logging in needs a human: no session and no credentials, a password login failed (and further automatic attempts are paused), or Garmin sent a verification code. Payload `{reason}`: a sentence for the athlete, including the command to run. At most one per UTC day. |
| `sync_failed` | `sync_failed:<UTC date>:<UTC hour>` | `COACH_SYNC_FAIL_EVENT_AFTER` passes in a row failed for reasons other than login (network, Garmin errors, rate limits, bugs). At most one per hour. Payload `{error}`. |

## The poller

- One pass every `COACH_SYNC_INTERVAL_S` (default 20 minutes), randomly shifted by up to `COACH_SYNC_JITTER_S`.
- Between `COACH_SYNC_MORNING_START` and `COACH_SYNC_MORNING_END` (local time) every `COACH_SYNC_MORNING_INTERVAL_S` (default 5 minutes) while today's sleep has not arrived. Not while the login is broken.
- After HTTP 429 the next request waits `COACH_SYNC_RATE_LIMIT_BACKOFF_S`, doubling with every further 429 up to `COACH_SYNC_RATE_LIMIT_BACKOFF_MAX_S`. A good pass resets it. Requests during the back-off are answered with an error at once, without calling Garmin.
- Every `COACH_SYNC_REQUEST_CHECK_S` it looks for `sync_now` requests and serves all pending ones with one pass.
- Only one poller works per database: it holds a Postgres advisory lock. A second one waits in standby and takes over when the first stops. `coach sync once` uses the same lock.
- SIGTERM or SIGINT: the current step (one activity, one day of wellness) finishes, the pass stops, a request it was serving is answered as interrupted, and the process exits 0.
- A failing pass never ends the loop. A lost database connection is retried every 30 seconds. Requests left `running` by a poller that died are marked failed when the next one starts.

## Sync requests

The agent cannot reach Garmin. When the athlete says "it's synced now, look again", the agent runs:

```sh
coach sync request --wait 120
# sync done: 1 new activity (…); sleep of 2030-01-02 synced
```

This inserts an `agent_requests` row (`kind = 'sync_now'`) with the agent's own database role and polls it. The poller answers with `status = 'done'` and `result = {new_activities: [ids], sleep_synced: bool, wake_date}`, or `status = 'failed'` and `error`. `new_activities` lists the activities inserted by that pass; `sleep_synced` says whether the night of `wake_date` (today's local date) is in the database now. The bridge can use the same table for a `/sync` chat command (`thresy.sync.agent_requests.create_request` and `wait_for_request`).

## Logging in

The session tokens live in `COACH_GARMIN_TOKEN_DIR` (the `garmin-auth` volume): `garmin_tokens.json`, written by `garminconnect` itself (mode 0600 in a 0700 directory; the library rewrites it when it refreshes the access token). Email and password (`COACH_GARMIN_EMAIL`, `COACH_GARMIN_PASSWORD`) are needed only when there is no usable token; once a token exists they can be removed from `.env`.

Rules, because Garmin locks accounts and rate-limits after repeated password logins:

- The token is always tried first. A network error, a server error or a rate limit during the token login never leads to a password login.
- A password login runs only when the token is missing or rejected and credentials are configured. Before each attempt, automatic password logins are paused for `COACH_SYNC_LOGIN_RETRY_H` hours (default 24); only a success lifts the pause. So one failure means: wait for a human or the long back-off. `coach sync login` by a human is always allowed.

### First set-up on a terminal

```sh
docker compose run --rm sync coach sync login    # or locally: uv run coach sync login
```

On a terminal you are asked for email and password if they are not configured, and for the verification code if Garmin sends one. The session is saved to the token directory.

### On a server without a terminal (two steps)

If the poller (or `docker compose exec -T sync coach sync login`) starts a password login and Garmin asks for a code, the waiting login is saved privately in the token directory (`mfa_pending.json`, mode 0600; it holds the SSO session cookies, so it is as sensitive as a password and never goes into the database). The athlete gets `garmin_auth_required` with the instructions, and finishes with:

```sh
docker compose exec sync coach sync login --code 123456
```

A wrong code leaves the waiting login in place; try again with the right one. `--restart` discards the waiting login and starts a new one. A waiting login is offered for one hour; after that, start a new one.

## Backfill

```sh
coach sync backfill --since 2024-01-01            # activities, FIT files, wellness
coach sync backfill --since 2024-01-01 --no-fit   # summaries and wellness only
```

- Activities are listed in four-week chunks; files are downloaded for activities that have no records or file yet. Wellness is fetched day by day; days that are already complete are skipped without a request.
- Before every request it pauses `COACH_SYNC_BACKFILL_PAUSE_S` (default 2 s). On HTTP 429 it waits with exponential back-off and retries; after five rate limits in a row it stops.
- Progress is saved after every chunk and day. Running the same command again continues where it stopped; `--restart` starts over. Everything is idempotent.
- It raises no events, and it never touches today: `--until` is at most yesterday, so the poller still raises `sleep_arrived` and `new_activity` for today.
- It can run while the poller runs (it has its own lock), but both share Garmin's rate limit.

## Settings

From the core settings: `COACH_TZ` (the athlete's local date decides "today"), `COACH_DATA_DIR` (FIT files), `COACH_GARMIN_EMAIL`, `COACH_GARMIN_PASSWORD`, `COACH_GARMIN_TOKEN_DIR`. The sync's own, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `COACH_SYNC_LOOKBACK_DAYS` | 7 | Activities listed on every pass. |
| `COACH_SYNC_NEW_ACTIVITY_MAX_AGE_H` | 48 | `new_activity` only for activities that started this recently. |
| `COACH_SYNC_INTERVAL_S` | 1200 | Time between passes. |
| `COACH_SYNC_JITTER_S` | 120 | Random shift of each interval, up to this. |
| `COACH_SYNC_MORNING_START` / `_END` | 05:00 / 11:00 | Local morning window. |
| `COACH_SYNC_MORNING_INTERVAL_S` | 300 | Interval in the morning window while today's sleep is missing. |
| `COACH_SYNC_REQUEST_CHECK_S` | 5 | How often the poller looks for sync requests. |
| `COACH_SYNC_RECHECK_DAYS` | 3 | Days before yesterday fetched again while incomplete. |
| `COACH_SYNC_RECHECK_INTERVAL_S` | 10800 | At most this often per such day. |
| `COACH_SYNC_STEPS_FINAL_AFTER_H` | 12 | A step count fetched this long after the day ended is final. |
| `COACH_SYNC_PR_REFRESH_H` | 24 | Personal records refresh interval (also after every new run). |
| `COACH_SYNC_FIT_MAX_ATTEMPTS` | 3 | Downloads of an original file before giving up. |
| `COACH_SYNC_LOGIN_RETRY_H` | 24 | Pause of automatic password logins after an attempt that did not succeed. |
| `COACH_SYNC_RATE_LIMIT_BACKOFF_S` / `_MAX_S` | 300 / 21600 | Back-off after HTTP 429. |
| `COACH_SYNC_FAIL_EVENT_AFTER` | 3 | Failed passes in a row before `sync_failed`. |
| `COACH_SYNC_BACKFILL_PAUSE_S` | 2 | Pause before each backfill request. |

## State

`sync_state` keys: `sync_status` (last attempt, last success, last error and its kind, failures in a row, last result), `garmin_auth` (login status and reason, when automatic password logins may resume), `garmin_rate_limit`, `wellness_fetched` (when each recent day was last fetched), `activities_pending` (new activities whose event waits for the file), `fit_failures`, `personal_records_at`, `backfill` (progress). `coach sync status` reads them; it works with the agent's role, so the agent can answer "when did we last sync?".

## Code

| Module | Contents |
|---|---|
| `thresy.garmin.client` | `GarminClient` (the protocol), `ConnectClient` (over `garminconnect.Garmin`), error translation |
| `thresy.garmin.fake` | `FakeGarminClient`: canned payloads for tests and imports |
| `thresy.garmin.auth` | Token store, token login, password login, verification-code flow |
| `thresy.sync.auth` | When logging in is allowed (`ClientProvider`), `coach sync login` |
| `thresy.sync.passes` | `run_pass`, `sync_once` |
| `thresy.sync.poller` | `Poller`, the advisory lock |
| `thresy.sync.agent_requests` | `sync_now` requests |
| `thresy.sync.backfill` | History import |
| `thresy.sync.fitfiles` | FIT extraction and the file layout |

## What is verified, and what is not

Verified by tests: everything above against the fake client and the test database; the login, token-file and error-handling code of the installed `garminconnect` 0.3.20 with only its HTTP methods replaced (`Client.connectapi`, `download`, `login`, `resume_login`); real payloads from the old bot's export pushed through the pass and compared with the old store.

Never run against the real Garmin service (watch these on the first deployment):

- The password login and the verification-code flow (`Garmin.login(return_on_mfa=True)`, saving `client_state`, `Garmin.resume_login` in a second process). In particular, how long Garmin keeps a waiting login is unknown; one hour is a guess.
- Token refresh during a long-running poller, and that the library's rewritten token file keeps working after a container restart.
- The shape of `download_activity(..., ORIGINAL)` for every activity type (zip with one FIT file is assumed; bare FIT files are accepted too), and which activities answer 404.
- Real rate limits: how many requests per hour Garmin tolerates, and whether the 20-minute and 5-minute intervals are acceptable.
- HTTP 204 answers for days without HRV, and the step-interval endpoint's shape for days without a summary.
