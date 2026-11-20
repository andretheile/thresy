# Backups

The `backup` service (`coach backup schedule`) backs up on a schedule (every night by default,
or on chosen weekdays) what cannot be fetched again: the database, the original FIT files, and
the coach's memory files and kept analysis scripts. It writes to a Google Cloud Storage bucket (or a local directory) with a credential
that can **create objects and nothing else**, so a stolen key can neither read nor delete the
backups. Optionally everything is encrypted with [age](https://age-encryption.org) before it
leaves the server.

## What is backed up, and what is not

| Backed up | As | When |
|---|---|---|
| The whole database (activities, records, wellness, plans, races, chat log, events, sync state) | `runs/<run id>/thresy.dump`: `pg_dump --format=custom` (compressed), of one consistent snapshot | every run |
| The coach's memory: the workspace's `memory/` | `runs/<run id>/memory.tar.gz` | every run |
| The analysis scripts the coach keeps: the workspace's `scripts/` (docs/workspace.md) | `runs/<run id>/scripts.tar.gz` | every run while `scripts/` exists; runs made before this was added have none |
| Original FIT files under `COACH_DATA_DIR/fit/` | `fit/<YYYY>/<activity id>.fit`, the same path as on the server | once per file |
| A manifest: row counts of every table in the dumped snapshot, PostgreSQL and pg_dump versions, objects with size and SHA-256, FIT counts, restore-check result | `runs/<run id>/manifest.json`, written last | every run |

With encryption every object gets the suffix `.age`. A run id looks like
`20300102T023000Z-1a2b3c` (UTC time plus a random part), so names never repeat. Links inside
`memory/` and `scripts/` are skipped, never followed, and so is either directory when it is a
link (with a warning in the run's result): the agent owns the workspace and could otherwise
point the backup at a file it must not copy, such as the bucket key.

Not backed up, on purpose:

- **Secrets**: `.env`, the database passwords, the Telegram token, the Garmin session
  (`garmin-auth` volume), the agent's model credential and the backup key itself. Nothing secret
  is stored in database tables, so the dump holds none either. After a loss you log in again.
- **The agent's home** (`agent-home` volume: the CLI's settings and session transcripts; the
  conversation that matters is in the database's `messages` table) and the rest of the
  workspace: `AGENTS.md`, `CLAUDE.md` and `playbooks/` come from the template in the image;
  `scratch/` holds throwaway files; `out/` holds generated plots; `inbox/` holds files the
  athlete sent in the chat, which are also in the chat itself.
- Docker images and logs.

## How a run works

1. A read-only transaction exports its snapshot and counts the rows of every table; `pg_dump`
   dumps exactly that snapshot, so the manifest's counts describe the dump.
2. The dump, the memory and scripts archives, every FIT file that has not been uploaded yet and
   the manifest are encrypted (if configured) and uploaded with "create only" semantics: for a
   bucket an upload with `ifGenerationMatch=0`, for a directory an exclusive create. Nothing is
   ever listed, read, overwritten or deleted.
3. Which FIT files are uploaded is remembered in the database (`sync_state` key `backup_fit`:
   path, size and modification time per file), not looked up in the bucket. If that record is
   lost, or the target or the encryption setting changes, every file is offered again; files
   already in the bucket are answered with "precondition failed" and counted as already there.
4. The outcome goes to `sync_state` (`backup_status`). A failure raises a `backup_failed` event
   (at most one per UTC day), and the bridge tells the athlete, like a failing Garmin sync.
5. Every `COACH_BACKUP_RESTORE_CHECK_DAYS` days (7) the run also restores its dump into a
   scratch database on the same server, compares every table's row count with the manifest, and
   drops the scratch database again (see [Testing a restore](#testing-a-restore)).

### Schedule

The service runs at `COACH_BACKUP_AT` local time (default 03:30, `COACH_TZ`) on the days in
`COACH_BACKUP_WEEKDAYS`: `daily` (the default), or weekday names such as `sun` for once a week
or `mon,thu` for twice (English, three letters or in full, any case; anything else stops the
service at start with a message). Changes to and from daylight saving time keep the local time;
a time that does not exist on such a day (02:30 when the clocks jump from 02:00 to 03:00) runs
an hour later on the clock.

Right after it starts, the service runs once (after a minute) if there is no successful backup
since the last scheduled time: on a new deployment, or when the service was down or failed when
a run was due. A restart between two scheduled times changes nothing, also with weekly runs. A
failed run is retried after an hour, twice; after that the next scheduled time is awaited.

A restore check can only happen during a backup. A run includes one when the last successful
check is `COACH_BACKUP_RESTORE_CHECK_DAYS` days old, counted with half a day's tolerance so that
runs at the same time N days apart qualify. With a value no larger than the gap between two
runs (the default 7 with weekly runs, for example), every run includes one; with 14 and weekly
runs, every other run does; with daily runs and 7, one run a week.

`coach backup status` shows the schedule and the next planned run. Retention is not the
application's business: the bucket's lifecycle rules delete old runs (below).

`pg_dump` and `pg_restore` must have the database's major version (PostgreSQL 16 in
`compose.yml`): the image installs `postgresql-client-16` from the PostgreSQL project's
repository (`docker/install-pg-client.sh`), and the backup refuses other versions. When you
change the `db` image's major version, change `PG_MAJOR` in `compose.yml` with it.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `COACH_BACKUP_URL` | `file:///backups` (in `compose.yml`) | `gs://<bucket>/<prefix>` or `file:///<absolute path>`. The default is the `backups` volume on the same server: it protects against mistakes, not against losing the server. |
| `COACH_BACKUP_GCS_KEY_FILE` | none | Host path of the service-account key (Compose mounts it read-only at `/run/secrets/gcs-backup-key.json`, which is `COACH_BACKUP_GCS_CREDENTIALS` inside the container). |
| `COACH_BACKUP_AGE_RECIPIENT` | none | age public key(s), comma-separated. Set: everything is encrypted. |
| `COACH_BACKUP_AT` | `03:30` | Local time (`COACH_TZ`) of the scheduled run. |
| `COACH_BACKUP_WEEKDAYS` | `daily` | Days of the scheduled run: `daily` or weekday names (`sun`, `mon,thu`). |
| `COACH_BACKUP_RESTORE_CHECK_DAYS` | `7` | A run includes a restore check when the last one is this many days old (0: never); see [Schedule](#schedule). |
| `COACH_BACKUP_PG_BIN_DIR`, `COACH_BACKUP_AGE_BIN`, `COACH_BACKUP_WORK_DIR` | auto, `age`, system temp | Only for running `coach backup` outside the image. The work directory needs about twice the size of a dump. |
| `BACKUP_MEM_LIMIT` | `256m` | Memory limit of the service. |

The service uses the `coach_app` database role, which needs `CREATEDB` for the restore check.
`docker/postgres/initdb/10-roles.sh` grants it on a new database volume; on an older volume run
`docker compose exec db psql -U postgres -c 'ALTER ROLE coach_app CREATEDB'` once.

## Commands

```sh
docker compose exec backup coach backup status          # schedule and next run, last success
                                                        # (with memory files and kept scripts),
                                                        # failures, restore check
docker compose exec backup coach backup run             # back up now (add --restore-check)
docker compose exec backup coach backup restore-check   # restore a fresh dump into a scratch DB
docker compose logs -f backup
```

`coach backup run` exits 1 on failure and prints the error; `--json` prints the result.

## Setting up the bucket

You need a Google Cloud project with billing and the `gcloud` CLI, logged in as someone who may
create buckets and service accounts. The data is health data, so keep it in the EU (or in the
region where the athlete lives): the commands use `europe-west3` (Frankfurt); any single EU
region or the `EU` multi-region works. Placeholders: `<PROJECT_ID>`, `<BUCKET>` (globally
unique, e.g. `thresy-backup-` plus a random suffix), `<PREFIX>` (e.g. `thresy`).

```sh
gcloud config set project <PROJECT_ID>

# 1. The bucket: uniform access, never public, objects kept 7 days after deletion.
gcloud storage buckets create gs://<BUCKET> \
  --location=europe-west3 \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --public-access-prevention \
  --soft-delete-duration=7d

# 2. A service account that may only create objects in this bucket.
gcloud iam service-accounts create thresy-backup \
  --display-name="Thresy backup (create objects only)"
gcloud storage buckets add-iam-policy-binding gs://<BUCKET> \
  --member="serviceAccount:thresy-backup@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/storage.objectCreator"

# 3. Its key, for the server.
gcloud iam service-accounts keys create gcs-backup-key.json \
  --iam-account=thresy-backup@<PROJECT_ID>.iam.gserviceaccount.com
```

Do not grant the service account anything else, in particular not `roles/storage.objectAdmin`
or `objectUser`: `objectCreator` contains `storage.objects.create` but not `get`, `list` or
`delete`, and overwriting an existing object would need `delete`. The project's own owners can
still read and delete everything; protect those accounts.

### Lifecycle rules (retention)

Runs (`<PREFIX>/runs/`) are deleted after 90 days; FIT files are uploaded once and must be kept,
so they get no delete rule, only a cheaper storage class after 30 days (they are read only for a
restore). Save as `lifecycle.json`:

```json
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 90, "matchesPrefix": ["<PREFIX>/runs/"]}
    },
    {
      "action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
      "condition": {"age": 30, "matchesPrefix": ["<PREFIX>/fit/"], "matchesStorageClass": ["STANDARD"]}
    }
  ]
}
```

```sh
gcloud storage buckets update gs://<BUCKET> --lifecycle-file=lifecycle.json
gcloud storage buckets describe gs://<BUCKET> --format=json   # shows the lifecycle rules
```

What the runs take is one dump (plus the small memory and scripts archives) times the number of
runs the delete rule keeps: with 90 days, nightly runs keep 90 dumps, a few hundred MB each, so
tens of GB; weekly runs (`COACH_BACKUP_WEEKDAYS=sun`) keep 13. Lower the age if that is too
much. Nearline has a 30-day minimum storage duration and charges for reads, which only happen in
a restore. If you want protection even against a compromised owner account, add a bucket
retention policy (`gcloud storage buckets update gs://<BUCKET> --retention-period=30d`); once
locked it cannot be shortened, so read Google's documentation on bucket lock first.

### Storage class for a small deployment

For a deployment of a few GB, the cheapest choice is a cold storage class as the bucket's
default, such as Archive: create the bucket with `--default-storage-class=ARCHIVE` instead of
`STANDARD`, or change it later with `gcloud storage buckets update gs://<BUCKET>
--default-storage-class=ARCHIVE` (that applies to objects uploaded afterwards). Every object the
backup uploads then lands in Archive. What that means:

- **Minimum storage duration.** Google's documentation lists 30 days for Nearline, 90 for
  Coldline and 365 for Archive (none for Standard). An object deleted, replaced or rewritten
  earlier is charged as if it had been stored for the minimum, and time in soft delete counts
  towards it. So with Archive the delete rule for runs should be no shorter than 365 days (a
  shorter one deletes earlier but saves nothing); with weekly runs that is 52 dumps. The rule
  that moves FIT files to Nearline has nothing left to do and can go.
- **Reading costs extra.** Cold classes charge a retrieval fee per GB read and more per
  operation than Standard. The backup service never reads, so this only matters in a restore,
  a drill (see [Testing a restore](#testing-a-restore)) and when an owner lists or downloads
  objects. Archive data in Cloud Storage is available within milliseconds, like the other
  classes, not after hours.
- **The backup works the same.** The create-only credential and the create-only uploads
  (`ifGenerationMatch=0`) behave alike in every class, and so does the "already uploaded" case:
  the backup recognises it from the refused upload, without reading the object.

For larger amounts, or if you expect to restore often, compare the classes on Google's pricing
page before choosing.

### Putting the key on the server

```sh
install -d -m 0700 secrets                       # in the checkout; secrets/ is ignored by git
sudo install -m 0400 -o 10001 -g 10001 gcs-backup-key.json secrets/gcs-backup-key.json
shred -u gcs-backup-key.json                     # remove the other copy
```

The container runs as uid 10001 (`coach`), which must be able to read the file. Without `sudo`,
`install -m 0444 gcs-backup-key.json secrets/` also works: the `secrets/` directory (0700)
keeps other users of the host out. Then in `.env`:

```sh
COACH_BACKUP_URL=gs://<BUCKET>/<PREFIX>
COACH_BACKUP_GCS_KEY_FILE=./secrets/gcs-backup-key.json
```

and `docker compose up -d backup`, followed by `docker compose exec backup coach backup run`.

## Encryption with age

```sh
age-keygen -o thresy-backup.key      # on your own computer, NOT on the server
# Public key: age1...
```

Put the public key into `.env` as `COACH_BACKUP_AGE_RECIPIENT=age1...` (several keys,
comma-separated, if more than one person should be able to restore) and restart the service.
Keep `thresy-backup.key` in at least two safe places (a password manager, a printed copy):
**without the private key the encrypted backups cannot be restored, by anyone**, and it must
never be stored on the server, so a compromise of the server does not expose old backups.

The server encrypts with the public key only. The automatic restore check therefore tests the
dump before it is encrypted; testing an encrypted backup needs the private key and happens where
the key is (below). Turning encryption on later uploads all FIT files again, encrypted; the
earlier unencrypted copies stay in the bucket until an owner deletes them
(`gcloud storage rm "gs://<BUCKET>/<PREFIX>/fit/**/*.fit"`, which leaves the `.fit.age` objects).

## Testing a restore

Three levels, from automatic to a full drill:

1. **Automatic, every `COACH_BACKUP_RESTORE_CHECK_DAYS` days** (with the next backup after
   that, see [Schedule](#schedule)): the backup service restores its fresh dump into a scratch
   database
   (`thresy_restore_check_<time>`), compares the row count of every table with the snapshot it
   dumped, and drops it. The result is in `coach backup status`; a failure is a backup failure.
   It proves that the dump is complete and restorable with the stack's PostgreSQL version. It
   needs free space in the database volume of about the database's size while it runs.
2. **On demand:** `docker compose exec backup coach backup restore-check` does the same with a
   fresh dump. `--dir <run directory>` checks a run directory instead (for example one under a
   `file://` target, or one downloaded from the bucket), compared with its manifest; add
   `--identity <key file>` when it is encrypted.
3. **A drill from the bucket** (a few times a year, and after changing anything about backups):
   download the newest run with an account that may read the bucket, decrypt it on your own
   computer and restore it into a throwaway PostgreSQL:

```sh
gcloud storage ls gs://<BUCKET>/<PREFIX>/runs/            # pick the newest <RUN_ID>
mkdir drill && cd drill
gcloud storage cp -r "gs://<BUCKET>/<PREFIX>/runs/<RUN_ID>" .
cd <RUN_ID>
for f in *.age; do age -d -i ~/thresy-backup.key -o "${f%.age}" "$f"; done   # if encrypted

docker run -d --name thresy-drill -e POSTGRES_PASSWORD=drill -v "$PWD:/restore:ro" postgres:16-alpine
until docker exec thresy-drill pg_isready -U postgres; do sleep 1; done
docker exec thresy-drill createdb -U postgres thresy
docker exec thresy-drill pg_restore -U postgres --no-owner --no-privileges --exit-on-error \
  -d thresy /restore/thresy.dump
docker exec thresy-drill psql -U postgres -d thresy -c \
  "select 'activities', count(*) from activities union all
   select 'activity_records', count(*) from activity_records union all
   select 'wellness_daily', count(*) from wellness_daily union all
   select 'messages', count(*) from messages"
python3 -c "import json; print(json.load(open('manifest.json'))['row_counts'])"   # must match
tar -tzf memory.tar.gz                                    # the memory files
tar -tzf scripts.tar.gz                                   # the kept scripts (newer runs only)
docker rm -f thresy-drill && cd ../.. && rm -rf drill     # the drill holds health data
```

## Restoring after a loss

On a new server, follow docs/deploy.md up to and including "Build", with the same
`DB_*_PASSWORD` values or new ones, then:

```sh
# 1. Download and decrypt the newest run and all FIT files (on your computer or the server).
gcloud storage cp -r "gs://<BUCKET>/<PREFIX>/runs/<RUN_ID>" restore/
gcloud storage cp -r "gs://<BUCKET>/<PREFIX>/fit" restore/
cd restore
for f in <RUN_ID>/*.age; do age -d -i ~/thresy-backup.key -o "${f%.age}" "$f" && rm "$f"; done
find fit -name '*.age' -exec sh -c 'age -d -i ~/thresy-backup.key -o "${1%.age}" "$1" && rm "$1"' _ {} \;
cd ..

# 2. Start only the database (creates the roles and the empty database), restore into it as
#    coach_app, then let the migrations add anything newer than the dump.
docker compose up -d db
docker compose cp restore/<RUN_ID>/thresy.dump db:/tmp/thresy.dump
docker compose exec db pg_restore -U postgres --role=coach_app --no-owner --exit-on-error \
  -d thresy /tmp/thresy.dump
docker compose exec db rm /tmp/thresy.dump
docker compose run --rm migrate

# 3. FIT files into the app-data volume.
docker compose run --rm --no-deps -v "$PWD/restore/fit:/restore-fit:ro" sync \
  sh -c 'mkdir -p /data/fit && cp -R /restore-fit/. /data/fit/'

# 4. Memory files and kept scripts into the workspace (the first command seeds the template if
#    the volume is new). A run made before scripts were backed up has no scripts.tar.gz: skip
#    the last two lines.
tar -xzf restore/<RUN_ID>/memory.tar.gz -C restore
tar -C restore/memory -c . | docker compose run --rm -T --no-deps bridge \
  coach workspace init --memory-from -
tar -xzf restore/<RUN_ID>/scripts.tar.gz -C restore
tar -C restore/scripts -c . | docker compose run --rm -T --no-deps bridge \
  coach workspace init --scripts-from -

# 5. Start everything, log in to Garmin again (docs/deploy.md), check.
docker compose up -d
docker compose exec sync coach sync status
```

Delete `restore/` afterwards; it holds health data. FIT files that arrived after the dump are
fetched again by the sync (it lists the last `COACH_SYNC_LOOKBACK_DAYS` days); for a longer gap
run `coach sync backfill --since <date of the dump>`.

## Code

| Module | Contents |
|---|---|
| `thresy.backup.core` | `run_backup`, `restore_check`, the snapshot dump, state and manifest |
| `thresy.backup.targets` | `LocalTarget`, `GcsTarget` (create only), `open_target` |
| `thresy.backup.pg` | finding `pg_dump`/`pg_restore` of the server's version, running them with the connection in `PG*` variables (never on the command line), scratch databases |
| `thresy.backup.crypto` | `age` encryption and decryption |
| `thresy.backup.schedule` | the service loop |

Tests: `tests/backup/` against a local directory and a fake storage client that only offers
"create", with stand-ins for `pg_dump` and `age`; the tests with the real `pg_dump`,
`pg_restore` and `age` run with `sh docker/backup-test.sh`, which installs them like the image
does in a throwaway container and runs the suite against the development database.

## Not verified

- No real bucket was used. Unverified against Google Cloud Storage: that an upload with
  `ifGenerationMatch=0` by an `objectCreator`-only account succeeds without any read
  permission, and that it answers 412 (not 403) when the object exists. If it is 403, a lost
  FIT record makes every FIT upload fail until the record is rebuilt; check it once by running
  `coach backup run` twice after deleting the `backup_fit` record
  (`DELETE FROM sync_state WHERE key = 'backup_fit'`, as `coach_app`).
- The gcloud commands were checked against `gcloud --help`, not run. The storage-class
  paragraph follows Google's documentation on storage classes and pricing; no bucket with
  Archive as its default class was tried.
- The schedule (weekdays, start-up catch-up, restore-check cadence, changes of daylight saving
  time) is tested with an injected clock; the service has not run for weeks in a container.
- Timing and size on real data: how long a dump, the first upload of all FIT files and a
  restore check take, and how much the database volume grows during a restore check.
