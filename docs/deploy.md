# Deploying Thresy

This guide sets up Thresy on a fresh Linux server, step by step, for one athlete. Everything runs
as one Docker Compose stack with no published ports: it only makes outgoing connections (Garmin,
Telegram, the model provider, optionally a backup bucket).

| Service | Does | Has |
|---|---|---|
| `db` | PostgreSQL 16 | the database volume; internal network only |
| `migrate` | applies database migrations, then exits | the read/write database URL |
| `sync` | polls Garmin, stores data, raises events | Garmin login and session, FIT files (read/write) |
| `bridge` | Telegram chat, starts the coding agent | Telegram token, the coach workspace, the agent's home, FIT files (read-only) |
| `backup` | nightly backup | the backup key, read-only FIT files and workspace |

Inside `bridge` the coding agent runs as its own Unix user (`agent`), not as the bridge's
(`coach`): it cannot read the bridge's environment or files, and its model credential lives in a
file only it can read. Commands in this guide run in the checkout on the server unless they say
otherwise. Placeholders look like `<this>`.

## 1. Prerequisites

- A Linux server, amd64 or arm64, with Docker Engine and the Compose plugin
  (`docker compose version` 2.20 or newer), and `git` and `openssl`. You need a user that may run
  `docker` (member of the `docker` group, or `sudo`).
- Memory: the services' limits add up to about 2.7 GB (db 512 MB, sync 384 MB, bridge 1.5 GB,
  backup 256 MB); in normal use they need much less. Change the limits in `.env`
  (`DB_MEM_LIMIT`, `SYNC_MEM_LIMIT`, `BRIDGE_MEM_LIMIT`, `BACKUP_MEM_LIMIT`).
- Disk: about 3 GB for building the image (roughly 1 GB remains), plus the data: the database
  grows by about 0.5 MB per hour of recorded activity, FIT files take their own size.
- Accounts: Garmin Connect; Telegram; a subscription or API key for the coding agent (Claude Code
  with a Pro, Max, Team or Enterprise plan is the default; Codex and Cursor also work, see
  docs/runners.md); optionally a Google Cloud project for off-site backups.

## 2. Clone

```sh
git clone <repository URL> thresy
cd thresy
git checkout <release tag or commit>     # deploy a known version, not whatever main is
```

Keep everything athlete-specific out of this checkout's tracked files: `.env`, `private/` and
`secrets/` are ignored by git.

## 3. Configure `.env`

```sh
cp .env.example .env
chmod 600 .env
```

Generate the four database passwords (letters and digits only, they end up in URLs):

```sh
for v in DB_SUPERUSER_PASSWORD DB_APP_PASSWORD DB_AGENT_PASSWORD DB_RO_PASSWORD; do
  sed -i "s/^$v=.*/$v=$(openssl rand -hex 24)/" .env
done
grep -c '_PASSWORD=.\{48\}' .env      # 4
```

They take effect when the database volume is created (step 6); changing them later needs
`ALTER ROLE`. Then edit `.env`:

- Delete the block "Local development only"; the server does not use it.
- `COACH_TZ`: the athlete's time zone, e.g. `Europe/Berlin`. It decides what "today" and "last
  night" mean.
- `COACH_LANGUAGE` (`en`, `de`, ...) and `COACH_NAME` (what the coach calls itself).
- `COACH_HOME_LOCATION` for `coach weather`, if you want weather in the briefings.
- Leave the Garmin, Telegram and backup values for their steps below.

## 4. Choose and pin the agent CLI

The image contains one coding-agent CLI, chosen by `AGENT_CLI` (`claude`, `codex`, `cursor`) and
pinned by `AGENT_CLI_VERSION`, so that it only changes when you rebuild deliberately. For Claude
Code the version is the package version of its signed apt repository (stable channel). To list
the available versions:

```sh
docker run --rm python:3.13-slim-trixie sh -c '
  apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null &&
  install -d /etc/apt/keyrings &&
  curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o /etc/apt/keyrings/claude-code.asc &&
  echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
    > /etc/apt/sources.list.d/claude-code.list &&
  apt-get update -qq && apt-cache madison claude-code | head -5'
```

Put the chosen version (e.g. `2.1.311-1`) into `AGENT_CLI_VERSION`. Before moving to a newer
version, read docs/runners.md, "Risk: `--bare` becoming the default". For Codex the version is
the release version from its GitHub releases (without `rust-v`); Cursor's installer cannot pin.

## 5. Build

```sh
docker compose build
```

The first build takes several minutes. It compiles the small user-switch helper, installs the
PostgreSQL 16 client (for backups, from the PostgreSQL project's repository after checking its
key), the agent CLI (likewise), the Python dependencies from `uv.lock`, and copies the coach
workspace template (`workspace/`). Check the result:

```sh
docker compose run --rm --no-deps bridge claude --version     # or codex --version
docker image ls thresy-app
```

## 6. Database

```sh
docker compose up -d db migrate
docker compose logs migrate          # "applied 0001_init.sql" ... and exit code 0
docker compose ps -a                 # db healthy, migrate exited (0)
```

The first start of `db` creates the roles `coach_app`, `coach_agent` and `coach_ro` with the
passwords from `.env` (`docker/postgres/initdb/10-roles.sh`).

## 7. Garmin login

Garmin locks accounts after repeated failed password logins, so log in once by hand; afterwards
the session token in the `garmin-auth` volume is used and refreshed (docs/sync.md, "Logging in").

```sh
docker compose run --rm sync coach sync login
```

It asks for email and password (unless `COACH_GARMIN_EMAIL` and `COACH_GARMIN_PASSWORD` are set in
`.env`) and for the verification code if Garmin sends one. Then:

```sh
docker compose run --rm sync coach sync status     # login ok, token stored
```

If you cannot answer interactively, set email and password in `.env`, run
`docker compose run --rm -T sync coach sync login`, and when it says a code was sent:
`docker compose run --rm -T sync coach sync login --code <code>` within the hour.

Once the login works, remove `COACH_GARMIN_EMAIL` and `COACH_GARMIN_PASSWORD` from `.env` (or leave
them empty): the sync service then has no password at all. With a password configured, the
sync may log in again on its own when the token stops working, at most once per
`COACH_SYNC_LOGIN_RETRY_H` hours; without it, the athlete gets a "Garmin needs attention"
message and you repeat this step.

## 8. History (optional)

Do this before the sync service starts for the first time.

- **The old bot's export** (only if you are moving from it; docs/import.md):

  ```sh
  SYNC_MEM_LIMIT=1g docker compose run --rm -v /path/to/legacy:/legacy:ro sync \
    coach import legacy --dir /legacy
  ```

- **Garmin history** (`coach sync backfill`, slow on purpose and resumable; it can run for hours
  and may run while the sync service runs):

  ```sh
  docker compose run -d --name thresy-backfill sync coach sync backfill --since <YYYY-MM-DD>
  docker logs -f thresy-backfill        # Ctrl+C stops following, not the backfill
  docker rm thresy-backfill             # when it has finished
  ```

## 9. The model credential

The bridge starts the agent CLI as the user `agent` through a setuid helper (docs/runners.md,
"The agent's own Unix user"). The agent's model credential belongs in its credential file,
`/home/agent/.thresy/credentials.env` in the `agent-home` volume: lines `NAME=value`, readable
only by the agent user. The helper adds them to the agent's environment; the bridge never sees
them. (Setting `CLAUDE_CODE_OAUTH_TOKEN` in `.env` also works, but then it is in the bridge's
environment too, and `coach runner check --isolation` warns about it.)

**Claude Code with a subscription.** Create a long-lived token (valid for a year). Either on a
computer where Claude Code is installed, or in a one-off container on the server:

```sh
docker compose run --rm --no-deps bridge claude setup-token
```

It prints a URL: open it in a browser, log in with the subscription's account, paste the code
back; it prints the token. Write it into the agent's credential file, typed or pasted at a hidden
prompt:

```sh
docker compose run --rm --no-deps --user agent bridge sh -c '
  umask 077
  printf "Token: "; stty -echo; read -r token; stty echo; echo
  printf "CLAUDE_CODE_OAUTH_TOKEN=%s\n" "$token" > /home/agent/.thresy/credentials.env
  echo saved'
```

Use `docker compose run` for this, **never `docker compose exec --user agent bridge`**: `exec`
starts a process in the running bridge container with the bridge's environment (Telegram token,
database URL) as the user `agent`, whose other processes could read it. `run` starts a separate
container. Never set `ANTHROPIC_API_KEY` anywhere: it takes precedence over the subscription.

Check it (this asks the CLI, as the agent, whether it is logged in; it spends no usage):

```sh
docker compose run --rm --no-deps bridge coach runner check
# OK   claude: claude 2.1.311: auth ok: logged in via oauth_token; token validity is only checked by a model request
```

To renew the token, repeat both commands (the file is replaced). The runner logs a warning when
the token expires within three days.

**Codex**: `CODEX_API_KEY=<key>` in the same file, or log in as the agent with
`docker compose run --rm --no-deps --user agent bridge codex login --device-auth` (stores
`~/.codex/auth.json` in the agent's home). **Cursor**: `CURSOR_API_KEY=<key>` in the file. Set
`COACH_RUNNER` in `.env` accordingly.

## 10. Telegram

1. In Telegram, talk to `@BotFather`: `/newbot`, choose a name and a user name. It replies with
   the bot token: put it into `COACH_TELEGRAM_TOKEN` in `.env`.
2. Optionally send BotFather `/setcommands` with
   `status - sync and agent status`, `sync - fetch new Garmin data now`,
   `new - start a fresh conversation`, `help - commands`.
3. Find the chat id: send the bot any message, then open
   `https://api.telegram.org/bot<token>/getUpdates` in a browser on your own computer; the id is
   `message.chat.id` (for a private chat, your user id). Alternatively start the bridge with
   `COACH_TELEGRAM_CHAT_IDS=0`, send the bot a message and look for "dropped an update from chat
   <id>" in `docker compose logs bridge`.
4. Put it into `COACH_TELEGRAM_CHAT_IDS`. Several ids are comma-separated; **the first one
   receives the proactive messages** (morning briefing, run debriefs, warnings).

## 11. The coach workspace and the athlete's memory

The bridge's start script runs `coach workspace init` before the chat loop: on the first start it
copies the template from the image (`AGENTS.md`, `CLAUDE.md`, `playbooks/`, a `memory/` skeleton,
`out/`, `inbox/`, an empty `scripts/`) into the `workspace` volume; on later starts it updates
only `AGENTS.md`, `CLAUDE.md` and `playbooks/` and never touches `memory/`, `scripts/` or
anything else the agent created.

The athlete's own memory files (profile, physiology and paces, preferences, goals; written for
this athlete, never committed) go into `memory/`. Keep them on the server outside git, for
example in `private/memory/` in the checkout, and import them; files of the same name replace
the skeletons:

```sh
tar -C private/memory -c . | docker compose run --rm -T --no-deps bridge \
  coach workspace init --memory-from -
docker compose run --rm --no-deps bridge ls -l /workspace /workspace/memory
```

From then on the agent maintains `memory/` itself; the backup includes it.

## 12. Start

```sh
docker compose up -d
docker compose ps
docker compose logs -f bridge sync        # Ctrl+C to stop following
```

## 13. Backups

Without further settings the `backup` service writes a nightly backup into the `backups` volume
on the same server, which helps after a mistake but not after losing the server. For off-site,
encrypted backups follow docs/backup.md ("Setting up the bucket", "Encryption with age"); in
short:

```sh
# .env
COACH_BACKUP_URL=gs://<bucket>/<prefix>
COACH_BACKUP_GCS_KEY_FILE=./secrets/gcs-backup-key.json
COACH_BACKUP_AGE_RECIPIENT=age1...        # public key; the private key stays off the server
```

```sh
docker compose up -d backup
docker compose exec backup coach backup run --restore-check
docker compose exec backup coach backup status
```

## 14. Verify

```sh
docker compose exec bridge coach runner check --isolation
```

must end with `RESULT: PASS`. It starts a probe exactly like an agent run and reports, one line
each: it runs as `agent`, with `no_new_privs`; it cannot read the environment of the bridge,
PID 1 or any other process of another user, cannot signal the bridge, cannot read a private file
of the bridge, cannot run the helper; its environment is the runner's allow-list plus the
credential file's variables and holds no value of the bridge's other variables; it can write only
to the workspace, its home and `/tmp` (not to the directory `coach` reads a `.env` file from); the workspace exchange works both ways; and the bridge
stops its whole process group, including processes that ignore SIGINT and SIGTERM. Any `FAIL`
means the agent is not isolated: see Troubleshooting.

Then:

```sh
docker compose exec bridge coach runner check     # OK ... auth ok
docker compose exec sync coach sync status         # last success, login ok, last night synced?
docker compose exec backup coach backup status     # after the first backup
docker stats --no-stream                           # memory use against the limits
```

and in Telegram send `/status` (sync, agent, outbox), then an ordinary message.

## 15. Upgrading

```sh
git fetch
git diff <current tag>..<new tag> -- .env.example compose.yml docs/   # new settings?
git checkout <new tag>
docker compose build
docker compose up -d                  # migrate runs before the other services start
docker compose exec bridge coach runner check --isolation
docker image prune                    # removes only dangling images
```

To upgrade only the agent CLI, change `AGENT_CLI_VERSION` in `.env`, then
`docker compose build bridge && docker compose up -d`. The `migrate`, `sync` and `backup`
services use the same image and are recreated as well.

## 16. Restoring

docs/backup.md, "Restoring after a loss", restores the database, the FIT files and the memory
files on a new server; "Testing a restore" shows how to check a backup without touching the
running system.

## 17. Troubleshooting

Look first at `docker compose ps -a` and `docker compose logs --tail 100 <service>`.

| Symptom | Cause and fix |
|---|---|
| `migrate` fails with `password authentication failed` | The database volume was created with other passwords. Set them in the database: `docker compose exec db psql -U postgres -c "ALTER ROLE coach_app PASSWORD '<value from .env>'"` (likewise `coach_agent`, `coach_ro`), or start over with `docker compose down -v` if there is no data yet. |
| `sync`, `bridge` or `backup` never start | They wait for `migrate`; see its log. |
| `bridge` restarts: `COACH_TELEGRAM_TOKEN is not set` / `COACH_TELEGRAM_CHAT_IDS is not set` | Step 10. |
| `bridge` restarts: `COACH_AGENT_USER='agent': no such user` | The image was built from an older `docker/Dockerfile`; rebuild. |
| Agent runs fail with `run-as-agent: setgroups: Operation not permitted` | The bridge container lacks the `SETUID`/`SETGID` capabilities or has `no-new-privileges`; compare the `bridge` service with `compose.yml` (`cap_add`, `security_opt: []`). |
| `run-as-agent: not permitted for user id 0` | A command was run as root (`exec -u root`); run it as the default user. |
| `--isolation`: FAIL "runs as another user ... COACH_AGENT_USER is not set" | `COACH_AGENT_USER` is set to an empty value in `.env`; remove that line. |
| `--isolation`: FAIL "workspace exchange ... missing" | Run `docker compose run --rm --no-deps bridge coach workspace init`. |
| `--isolation`: FAIL "writes the service's working directory" | `coach` was run from a directory the agent can write (`-w /workspace`, or an image with another `WORKDIR`); a `.env` file there would configure the bridge. Run `coach` from the default `/app`. |
| `--isolation`: FAIL "stops ...: only zombie processes are left" | The container runs without an init process; keep `init: true`. |
| `--isolation`: WARN "model credential in the bridge's environment" | Move the token from `.env` into the agent's credential file (step 9) and `docker compose up -d bridge`. |
| `coach runner check`: "not logged in" | The credential file is missing or wrong: step 9. |
| The coach answers that its login expired or is invalid | The token was revoked or expired: step 9. |
| The coach answers that the usage limit is reached | The subscription's limit; it resets by itself (the message says when). |
| "Garmin sync needs attention" in the chat | The Garmin session is gone: step 7. Several failed password logins in a row can lock the Garmin account for a while; wait instead of retrying. |
| Garmin rate limit (HTTP 429) in `coach sync status` | The sync backs off by itself; stop a running backfill (`docker stop thresy-backfill`) and let it recover. |
| A service was OOM-killed (`docker inspect --format '{{.State.OOMKilled}}' <container>`) | Raise its limit in `.env` (for example `BRIDGE_MEM_LIMIT=2g`) and `docker compose up -d`. |
| "The backup failed" in the chat | `docker compose exec backup coach backup status` shows the error. `pg_dump for PostgreSQL 16 is needed`: keep `PG_MAJOR` in `compose.yml` equal to the `db` image. `may not create the scratch database`: `ALTER ROLE coach_app CREATEDB` (docs/backup.md). `403 ... objectCreator`: the service account's role on the bucket. `cannot use the service-account key ... Permission denied`: the key file must be readable by uid 10001. |
| Times in messages are off by hours | `COACH_TZ` is not the athlete's time zone. |
| Disk filling up | `docker system df`; the `db-data` volume, old images (`docker image prune`), and the build cache (`docker builder prune`). Logs are capped at 30 MB per container. |
