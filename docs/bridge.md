# Bridge, transports and the agent's messaging tools

`coach bridge run` connects a chat transport (Telegram) to the coding agent (through
`thresy.runner`, any harness). It answers the athlete's messages, sends the morning briefing
and post-run debriefs when the sync service reports new data, and delivers what the agent
queues with `coach send`.

| Package | Role |
|---|---|
| `thresy.transport` | `Transport` protocol, message types, `FakeTransport` for tests, reading and writing files in directories the agent controls (`files`) |
| `thresy.transport.telegram` | Telegram (python-telegram-bot, long polling); the only code that knows Telegram |
| `thresy.bridge` | chat loop, sessions, agent runs, outbox delivery, event dispatcher, chat commands |
| `thresy.tools` | `coach send …` and `coach sql`, run by the agent |

## How a message flows

1. The transport receives an update. Chats that are not allow-listed are dropped; the log
   line names the chat id and user id, never the content.
2. The bridge logs the message in `messages` (direction `in`). Files the athlete sends
   (photos, documents, voice and audio messages) are saved under `<workspace>/inbox/<date>/`
   first and recorded as attachments with workspace-relative paths.
3. Commands (`/status`, `/sync`, `/new`, `/help`, `/start`) are answered by the bridge itself,
   at once, even while the agent is working. Other words starting with `/` go to the agent.
4. Everything else goes to the chat's worker. **One agent run at a time per chat:** messages
   that arrive while a run is in progress are collected and handed to the next run together
   ("The athlete sent 2 messages … while you were busy; answer them together").
5. The worker finds or creates the chat's session (below), records the run in `agent_runs`
   (status `running`), links the messages to it, shows "typing" and starts the agent in the
   workspace with a short prompt and an explicit environment.
6. While the agent works it may run `coach send …`, which only inserts `outbox` rows tagged
   with the run id.
7. When the run ends, its record gets status, model, cost, usage and error. The final answer
   goes into the `outbox` after anything the agent queued itself; if the run failed, a plain
   message explaining why goes there instead.
8. The outbox deliverer sends rows in id order and marks each one sent and logs it in
   `messages` (direction `out`) in one transaction.

The bridge never sends interim "working on it" messages; the typing indicator stays on while a
run is in progress. An answer of exactly `NO_REPLY` (or an empty answer after `coach send`)
sends nothing more; an empty answer with nothing sent gets one honest sentence.

### Sessions and context

Conversation continuity belongs to the bridge, not the harness. There is one session per chat
and local day (`COACH_TZ`) in `agent_sessions`:

- The harness's own session id (`native_session_id`) is resumed when the runner supports it and
  the session was started with the same runner (`agent_sessions.runner`).
- A **fresh** harness session starts on a new day, after a runner switch, after `/new`, and
  after a failed resume. Its prompt then begins with recent messages from `messages`: at most
  `COACH_BRIDGE_CONTEXT_MESSAGES` messages and `COACH_BRIDGE_CONTEXT_CHARS` characters, none
  older than `COACH_BRIDGE_CONTEXT_HOURS`, never the bridge's own command replies and
  notifications, and after `/new` nothing from before the command (the cut is remembered by
  message id in `sync_state` key `bridge:reset:<chat>`).
- A resumed run that fails with `other` or `crash` before it queued anything is retried once in
  a fresh session (with context from the log); the old session is marked `failed`.

Session statuses: `active`, `closed` (day over or replaced), `reset` (`/new`), `failed`.
Run statuses: `running`, `ok`, `failed`; `error_kind` is the runner's kind, or `interrupted`
for runs a crash left behind.

### Failures

| Runner `error_kind` | The athlete reads (English; German with `COACH_LANGUAGE=de`) |
|---|---|
| `rate_limit` | "I can't answer right now: the usage limit of the coach's AI subscription has been reached. Please try again later." plus the harness's detail (usually the reset time) |
| `auth` | "I can't answer: the login of the coach's AI agent has expired or is invalid. The owner needs to renew it and restart the bridge." plus the detail |
| `timeout` | "Sorry, that took too long and I stopped without an answer. …" |
| `cancelled` at shutdown | "I was restarted while I was working on your last message … Please send it again." |
| anything else | "Sorry, something went wrong on my side and I have no answer. Please try again." |

Proactive runs prefix the message with their name ("Morning briefing: …"). A failed morning
briefing (any kind) is followed by the **numbers-only briefing**, built straight from the
database: sleep duration and score, HRV against its balanced range, resting heart rate and
yesterday's run(s), or a line saying the night is not synced yet.

### Shutdown and restart

On SIGTERM or SIGINT the bridge stops taking work, cancels running agents through the runner
handle (the run is recorded as `failed`/`cancelled` and the athlete is told), waits up to
`COACH_BRIDGE_SHUTDOWN_GRACE_S`, flushes the outbox for up to 10 s and stops the transport.
Messages arriving meanwhile are logged but not run. Give the container enough time
(`stop_grace_period: 30s` in Compose; Docker's default of 10 s may cut this short).

On start the bridge marks runs left `running` by a crash as `failed`/`interrupted` and tells
the athlete, answers logged messages that never reached a run (younger than
`COACH_BRIDGE_REQUEUE_MINUTES`), delivers unsent outbox rows and picks up unprocessed events.

## The agent's environment and prompts

The runner starts the agent with an allow-listed environment (see docs/runners.md). On top of
it the bridge passes exactly:

| Variable | Value |
|---|---|
| `COACH_DATABASE_URL` | `COACH_AGENT_DATABASE_URL` (role `coach_agent`) |
| `COACH_DATABASE_URL_RO` | `COACH_DATABASE_URL_RO` (role `coach_ro`), if set |
| `COACH_DATA_DIR`, `COACH_WORKSPACE_DIR` | absolute paths |
| `COACH_TZ` and `TZ` | `COACH_TZ` (so `date` in the agent's shell is local time) |
| `COACH_LANGUAGE`, `COACH_NAME` | as configured |
| `COACH_HOME_LOCATION` | as configured, if set (the default place of `coach weather`) |
| `COACH_RUN_ID` | the `agent_runs.id` of this run |

**Model tier and reasoning effort** are chosen per kind of run: runs that answer the athlete
use `COACH_BRIDGE_CHAT_TIER` and `COACH_BRIDGE_CHAT_EFFORT`, the morning briefing and the
debrief (also when they are combined into one run) `COACH_BRIDGE_PROACTIVE_TIER` and
`COACH_BRIDGE_PROACTIVE_EFFORT`. A higher effort gives better answers at the price of time and
subscription usage, and chat answers keep someone waiting on a phone while nobody waits for the
briefing, so the proactive level can sensibly be the higher one. The level is a harness-neutral
hint (`RunRequest.effort`): each runner maps it to its CLI's own setting, a runner without one
(Cursor) ignores it, a level the model lacks never fails the run, and an effort set directly in
the harness's own configuration (for Claude Code `CLAUDE_CODE_EFFORT_LEVEL` in
`COACH_CLAUDE_ENV`) overrides both settings (docs/runners.md, "Reasoning effort"). Each run's
tier and level are in the bridge log ("run 12 (user_message) for chat …: resumed session, tier
smart, effort high").

Never the Telegram token, the read/write database URL or the Garmin credentials;
`tests/bridge/test_agent_env.py` checks this with a real CLI child process. The bridge refuses
to start when `COACH_AGENT_DATABASE_URL` equals `COACH_DATABASE_URL`.

Prompts are short and name the playbook in the workspace to follow when the file exists
(`playbooks/chat.md`, `playbooks/morning-briefing.md`, `playbooks/post-run.md`; for the last
two a one-line fallback instruction is used while the file is missing). Nothing per run goes
into `system_append`. Example, a chat message:

```
The athlete wrote in the chat (telegram, Wed 2030-01-02 08:15, America/Chicago):

<message text>

Follow playbooks/chat.md.
Reply in English. Your final reply is sent to the athlete as one chat message. Send nothing in between unless it carries content (`coach send`, for plots, files or questions with buttons).
```

A proactive run starts with "Nobody wrote: the bridge started this run on its own (…)",
then one `Trigger: …` line per due item, the playbook line(s), the language line, and "If there
is nothing worth sending, reply with exactly NO_REPLY." The exact texts are pinned in
`tests/bridge/test_prompts.py`.

## Proactive messages

Driven by `events` from the sync service. The dispatcher checks every
`COACH_BRIDGE_EVENTS_POLL_S`; all its decisions read the injected clock, so tests never wait.
Handled and skipped events get `processed_at` and `processed_by = bridge:<outcome>` (for
example `bridge:briefing`, `bridge:already-briefed`, `bridge:too-old`, `bridge:rate-limited`).
Events that are still waiting stay unprocessed, so a restart picks them up again.

**Quiet hours** (`COACH_BRIDGE_QUIET_HOURS`, default `22:00-06:00`, `off` to disable): the
briefing, debriefs and notifications wait until the window ends. Answers to the athlete's own
messages are never held back.

### Morning briefing

- Triggered by `sleep_arrived` whose `wake_date` is today's local date, at most once per local
  day (`sync_state` key `bridge:briefing`, set when the run starts, so a restart does not
  repeat it). Events for other dates are skipped; none starts after
  `COACH_BRIDGE_BRIEFING_LATEST` (14:00).
- **Night guard.** A watch sync after waking up in the night can store a partial night and
  raise `sleep_arrived` at 03:00. While the stored night for today
  (`wellness_daily.sleep_end_utc`) ends before `COACH_BRIDGE_NIGHT_END_EARLIEST` (05:00), the
  night counts as possibly unfinished: the briefing waits until a later sync pass moves the
  stored end past that time, or until the fallback time, whichever comes first. Quiet hours
  ending is not enough.
- **Settle delay.** Once the night looks finished, the run starts
  `COACH_BRIDGE_BRIEFING_SETTLE_S` (180 s) later, so HRV and the daily stats of the same sync
  pass are stored as well.
- **Fallback.** If no briefing has started by `COACH_BRIDGE_BRIEFING_FALLBACK` (10:00), one
  starts anyway with whatever is stored; the prompt says the night has not been synced yet
  when there is none, or that the stored night may be incomplete when it ends too early.
- The prompt is built from the data as it is when the run starts, never from what was there
  at trigger time; if HRV for the night is still missing then, the prompt says so.
- When the agent cannot write it, the numbers-only briefing goes out (see Failures).

### Post-run debrief

`new_activity` with `is_run` starts a debrief `COACH_BRIDGE_DEBRIEF_DELAY_S` (180 s) after the
upload; each further upload restarts the delay, capped at `COACH_BRIDGE_DEBRIEF_MAX_WAIT_S`
(900 s) after the first, so uploads in a row become one debrief. Other sports and runs that
started more than `COACH_BRIDGE_DEBRIEF_MAX_AGE_H` (12 h) ago are skipped. If a briefing and a
debrief are due at the same time they become one run and one message
(`trigger = morning_briefing+post_run`).

### Notifications

`garmin_auth_required`, `sync_failed` and `backup_failed` (from the backup service, docs/backup.md;
the message names the last successful backup) become plain messages without an agent run, at
most one per kind per `COACH_BRIDGE_NOTIFY_INTERVAL_H` (12 h, kept in `sync_state` keys
`bridge:notified:<kind>`). A sync failure is not reported within that interval after a Garmin
login notice (the login is the cause). The sync raises `sync_failed` only after several failed
passes in a row, so `COACH_BRIDGE_SYNC_FAILED_THRESHOLD` defaults to 1; the notice names the
last successful sync from the sync's `sync_status` record.

## Chat commands

| Command | Does |
|---|---|
| `/status` | last successful sync and failures in a row (from the sync's `sync_status`), Garmin login state if not ok, newest stored activity and night, runner health (`healthcheck`, no model usage), today's agent runs and cost, waiting and failed outbox rows, whether a run is in progress, quiet hours |
| `/sync` | inserts an `agent_requests` row (`sync_now`), keeps "typing" on while the sync service works, then reports new activities and whether last night's sleep is synced; gives up after `COACH_BRIDGE_SYNC_REQUEST_TIMEOUT_S` |
| `/new` | closes today's session; the next message starts a fresh one without earlier context |
| `/help`, `/start` | lists the commands |

## Outbox delivery

- Everything outgoing goes through `outbox`: the agent's `coach send` rows, final answers, the
  bridge's own replies and notices. `chat_id` NULL means the chat of the run (via
  `agent_runs.session_id`), or the transport's default chat (the first allow-listed id).
- Rows are delivered in id order, polled every `COACH_BRIDGE_OUTBOX_POLL_S` and woken at once
  when the bridge queues something.
- A permanent failure is written to `outbox.error` and the next row follows. A transient one
  stops the pass (the order is kept) and is retried with backoff (5 s doubling, or Telegram's
  flood-control wait); after `COACH_BRIDGE_SEND_ATTEMPTS` the row is marked failed.
- Delivered rows are never sent again; a crash between the transport accepting a message and
  the database commit (milliseconds) would resend that one row after the restart.
- Rows are checked again before delivery because the agent can also insert them with plain
  SQL: known kind, non-empty text, valid options, an allow-listed chat, and a file path under
  `<workspace>/out/` (absolute, or relative to the workspace; no `..`).
- **Files are read once, by the bridge**, and the transport sends those bytes
  (`OutboundItem.data`); it never opens the path. The agent can replace anything under `out/`
  at any moment, also between `coach send`'s check and the delivery, so the bridge
  (`thresy.transport.files`) opens `out` and then each path component relative to the one
  before with `O_NOFOLLOW`: a link anywhere on the way (`out` itself, a parent directory, the
  file) is refused, never followed. It then checks the open file: a regular file with one name
  (no hard link), not empty, within the limit (photos 10 MB, documents 50 MB, also while
  reading), and, where the kernel tells an open file's path (Linux `/proc/self/fd`, macOS
  `F_GETPATH`), still inside `out/`. A refused file is a permanent failure with the reason in
  `outbox.error` ("refused: out/plot.png is a symbolic link; files are never read through
  links").
- Files the athlete sends are written the same way under `inbox/<date>/`: directories and the
  file are created without following links, and an existing name (a planted link included) is
  never written to; a second copy gets a number (`10-2-photo.jpg`).
- A pressed button comes back as an inbound `choice_answer` message ("Pressed the button
  "Rest", answering your question: "…"") and starts a run like any message; a second press of
  the same question is ignored.

## Tools the agent runs

`coach send text|photo|document|choice …` inserts an `outbox` row with the agent's role
(`coach_agent` may INSERT into `outbox`, nothing else there), attributed to `COACH_RUN_ID`.
Files must resolve to a path under `$COACH_WORKSPACE_DIR/out/`; photos up to 10 MB, documents
up to 50 MB, choices 1 to 10 options of at most 64 characters. A role that may not insert gets
"this database role may not queue messages", not a traceback.

`coach sql "<query>"` runs one query as `coach_ro` (read-only session, `--timeout`, default
30 s) through a server-side cursor, prints an aligned table or `--json`, and stops at
`--limit` rows (default 200) with "(showing the first N rows; the query returned more …)".

Both learn their usage from `--help`, which is written for the agent.

## Settings

Core settings used: `COACH_DATABASE_URL`, `COACH_DATABASE_URL_RO`, `COACH_AGENT_DATABASE_URL`,
`COACH_DATA_DIR`, `COACH_WORKSPACE_DIR`, `COACH_TZ`, `COACH_LANGUAGE`, `COACH_NAME`,
`COACH_RUNNER` (plus the runner's own `COACH_<NAME>_*`), `COACH_TELEGRAM_TOKEN`,
`COACH_TELEGRAM_CHAT_IDS`, and `COACH_HOME_LOCATION` (only handed on to the agent; the setting
belongs to `coach weather`, `thresy.tools.weather.WeatherSettings`). The bridge's own
(`thresy.bridge.settings.BridgeSettings`):

| Variable | Default | Meaning |
|---|---|---|
| `COACH_BRIDGE_TRANSPORT` | `telegram` | the only transport so far |
| `COACH_BRIDGE_QUIET_HOURS` | `22:00-06:00` | proactive messages wait; `off` disables |
| `COACH_BRIDGE_BRIEFING` | `true` | send the morning briefing |
| `COACH_BRIDGE_BRIEFING_FALLBACK` | `10:00` | brief anyway at this local time |
| `COACH_BRIDGE_BRIEFING_LATEST` | `14:00` | no briefing starts later |
| `COACH_BRIDGE_NIGHT_END_EARLIEST` | `05:00` | a stored night ending earlier may be unfinished |
| `COACH_BRIDGE_BRIEFING_SETTLE_S` | `180` | delay between a finished-looking night and the run |
| `COACH_BRIDGE_DEBRIEF` | `true` | send post-run debriefs |
| `COACH_BRIDGE_DEBRIEF_DELAY_S` | `180` | wait after an upload |
| `COACH_BRIDGE_DEBRIEF_MAX_WAIT_S` | `900` | cap on that wait |
| `COACH_BRIDGE_DEBRIEF_MAX_AGE_H` | `12` | older runs get no debrief |
| `COACH_BRIDGE_NOTIFY_INTERVAL_H` | `12` | one notification per kind per interval |
| `COACH_BRIDGE_SYNC_FAILED_THRESHOLD` | `1` | `sync_failed` events needed within the window |
| `COACH_BRIDGE_SYNC_FAILED_WINDOW_H` | `6` | |
| `COACH_BRIDGE_RUN_TIMEOUT_S` | `900` | per agent run |
| `COACH_BRIDGE_CHAT_TIER` / `COACH_BRIDGE_PROACTIVE_TIER` | `smart` / `smart` | model tier |
| `COACH_BRIDGE_CHAT_EFFORT` / `COACH_BRIDGE_PROACTIVE_EFFORT` | unset / unset | reasoning effort: `low`, `medium`, `high`, `xhigh`, `max`; unset: the harness's default (below) |
| `COACH_BRIDGE_CONTEXT_MESSAGES` | `20` | context for a fresh session |
| `COACH_BRIDGE_CONTEXT_CHARS` | `6000` | |
| `COACH_BRIDGE_CONTEXT_HOURS` | `36` | |
| `COACH_BRIDGE_REQUEUE_MINUTES` | `60` | after a restart, answer messages that never reached a run if younger |
| `COACH_BRIDGE_OUTBOX_POLL_S` | `2` | |
| `COACH_BRIDGE_EVENTS_POLL_S` | `15` | |
| `COACH_BRIDGE_SEND_ATTEMPTS` | `5` | transient delivery failures before giving up on a row |
| `COACH_BRIDGE_SYNC_REQUEST_TIMEOUT_S` | `300` | `/sync` waits this long |
| `COACH_BRIDGE_SHUTDOWN_GRACE_S` | `20` | wait for a cancelled run on SIGTERM |

## Telegram

### Creating the bot and finding the chat id

1. In Telegram, talk to `@BotFather`, send `/newbot`, choose a name and a user name. BotFather
   replies with the token; put it into `COACH_TELEGRAM_TOKEN` (a secret: never in git).
2. Optionally send BotFather `/setcommands` with the list `status - sync and agent status`,
   `sync - fetch new Garmin data now`, `new - start a fresh conversation`, `help - commands`.
3. Find your chat id: start the bridge with any placeholder in `COACH_TELEGRAM_CHAT_IDS` (for
   example `0`), send the bot a message, and read the bridge log: "dropped an update from chat
   <id> (user <id>): not on the allow-list". For a private chat the chat id is your user id.
   Alternatively, before starting the bridge, send the bot a message and open
   `https://api.telegram.org/bot<token>/getUpdates` in a browser; the id is under
   `message.chat.id`. (Do not run the bridge at the same time: Telegram allows one consumer.)
4. Put the id into `COACH_TELEGRAM_CHAT_IDS` (comma-separated for several chats; the first one
   receives proactive messages) and restart the bridge.

The bridge uses long polling (`getUpdates`), so no port is opened and no webhook may be set
for the bot.

### The token and the log

The token is part of every Bot API URL (`https://api.telegram.org/bot<token>/<method>`), and
the HTTP client logs every request URL at INFO. The bridge's log never shows it: request logging
of the HTTP clients is off, the transport registers the token with the log redaction when it
is created, and every log line, traceback and stored delivery error is redacted (docs/deploy.md,
"Logs and secrets", also for upgrading from a version that leaked it).
`tests/transport/test_telegram_http.py` runs the transport's real HTTP stack against a fake Bot
API server on 127.0.0.1 and checks that the token appears nowhere in the process's output.

### Formatting

The agent writes Markdown. Telegram's Markdown and HTML parse modes reject a whole message for
one unbalanced character, so the transport parses the Markdown itself (markdown-it, CommonMark
plus tables and strikethrough) and sends plain text with explicit message entities (bold,
italic, strikethrough, code, pre, links, blockquote):

- headings become bold lines, lists get `•` / `1.` prefixes, `---` becomes `———`;
- tables, which Telegram cannot show, become an aligned monospace block when they fit a phone
  (at most 36 characters wide) and a list with one line per row otherwise
  ("• Monday: Distance: 10.2 km · Pace: 5:07/km");
- messages longer than 4000 UTF-16 units are split at paragraph, line, sentence or word
  boundaries; entities crossing a cut are split too;
- if Telegram rejects the entities, the same chunk goes out as plain text (already free of
  Markdown syntax); if rendering itself fails, the raw text is split and sent;
- photo captions longer than 1024 units are sent as a separate message after the photo; a
  photo Telegram refuses (size, dimensions) is sent as a document.

Buttons of a `choice` carry `c|<outbox id>|<index>` as callback data; a pressed keyboard is
replaced by one "✓ <label>" button.

## Adding another transport

1. Create `src/thresy/transport/<name>/` implementing `thresy.transport.Transport`:
   `name`, `default_chat_id`, `allows(chat_id)`, `start(on_message)`, `stop()`,
   `send(item) -> message id` (raise `DeliveryError(permanent=…)`), `set_busy(chat_id, busy)`.
2. Turn incoming messages into `InboundMessage` (save files under the workspace and pass them
   as `Attachment`s; button presses as `kind="choice_answer"` with `reply_to` = the item's
   `ref`). Drop chats that are not allow-listed before calling `on_message`.
3. Render `OutboundItem.text` (Markdown) the way the medium needs; deliver `choice` options so
   that a press comes back with the `ref`. Send files from `OutboundItem.data` and never open
   `file_path` (the agent may have replaced it); save incoming files with
   `thresy.transport.files.create_file_under`.
4. Select it in `thresy.bridge.main.build_transport` with a new `COACH_BRIDGE_TRANSPORT`
   value. Nothing in `thresy.bridge` needs to know about it otherwise; test it like
   `tests/transport/test_telegram.py` and use `FakeTransport` for bridge tests.

## What the workspace should provide

- `AGENTS.md` (and `CLAUDE.md` = `@AGENTS.md`): who the coach is, the tools, the rules.
- `playbooks/chat.md`, `playbooks/morning-briefing.md`, `playbooks/post-run.md`.
- An `out/` directory for plots and files (the bridge creates `out/` and `inbox/` at start).
- The playbooks may rely on: the prompt naming the trigger and date; `NO_REPLY` for "nothing to
  send"; `coach send` for plots and buttons; `coach sql` (and later analysis tools) for data;
  files from the athlete under `inbox/<date>/`; the answer language from the prompt.

## Not verified

No real Telegram bot and no real agent CLI were available while this was built.

- **Telegram, never run against the API:** `TelegramTransport.start/stop` (Application
  lifecycle, `start_polling`, bootstrap retries), real update parsing of photos, documents,
  voice and callback queries (tested with `Update.de_json` on synthetic payloads), file
  download (`get_file` + `download_as_bytearray`), `send_message` with entities and
  `LinkPreviewOptions`, `send_photo`/`send_document` with bytes and `filename`, inline
  keyboards and `edit_message_reply_markup`, `send_chat_action`, flood control, and which
  errors Telegram returns for which problem (the permanent/transient mapping is by exception
  class).
- **Agent CLI:** the bridge was only run with the fake runner and, for the environment test,
  a script that imitates the Claude Code stream. How a real harness reacts to the prompts,
  `NO_REPLY` and `coach send` is untested.
- **Reasoning effort:** that the levels from `COACH_BRIDGE_CHAT_EFFORT` and
  `COACH_BRIDGE_PROACTIVE_EFFORT` reach the runner per kind of run is tested with the fake
  runner; that Claude Code applies them was not checked with a real run (docs/runners.md,
  "Checking the effort level", says how).
- **Process behaviour:** SIGTERM handling of `coach bridge run` inside the container and the
  timing against Docker's stop timeout were not exercised end to end.
- **Security:** in the container the agent runs as its own Unix user (`COACH_AGENT_USER`,
  docs/runners.md "The agent's own Unix user"), so it cannot read the bridge's or PID 1's
  environment through `/proc`; `coach runner check --isolation` checks this on the server.
  Without that setting (local development) the agent runs as the bridge's user and the
  environment allow-list is not a boundary against a determined agent. The outbox and inbox
  file handling (see Outbox delivery) is tested on macOS with links swapped in after
  `coach send`; its Linux-only part, locating the open file through `/proc/self/fd`, was not
  run on Linux here.
