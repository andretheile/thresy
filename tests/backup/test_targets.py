"""Targets, encryption, settings and the schedule: everything that needs no database."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from thresy.backup import crypto, schedule
from thresy.backup.settings import BackupSettings
from thresy.backup.targets import GcsTarget, LocalTarget, TargetError, gcs_client, open_target

# -- local target ---------------------------------------------------------------------------------


def test_local_target_creates_and_never_overwrites(tmp_path):
    target = LocalTarget(tmp_path / "backups")
    first = tmp_path / "a"
    first.write_bytes(b"first")
    second = tmp_path / "b"
    second.write_bytes(b"second")
    assert target.put("runs/1/thresy.dump", first) is True
    assert target.put("runs/1/thresy.dump", second) is False
    assert (tmp_path / "backups" / "runs" / "1" / "thresy.dump").read_bytes() == b"first"
    leftovers = [p.name for p in (tmp_path / "backups" / "runs" / "1").iterdir()]
    assert leftovers == ["thresy.dump"]  # no partial files


@pytest.mark.parametrize("name", ["/abs", "../up", "a/../../b", "dir/", ""])
def test_object_names_are_checked(tmp_path, name):
    source = tmp_path / "f"
    source.write_bytes(b"x")
    with pytest.raises(TargetError, match="invalid object name"):
        LocalTarget(tmp_path / "t").put(name, source)


def test_open_target(tmp_path, gcs):
    local = open_target(f"file://{tmp_path}/backups")
    assert isinstance(local, LocalTarget) and local.root == tmp_path / "backups"
    remote = open_target("gs://example-bucket/thresy/", client=gcs)
    assert isinstance(remote, GcsTarget)
    assert remote.bucket_name == "example-bucket" and remote.prefix == "thresy"
    assert remote.url == "gs://example-bucket/thresy"
    for bad in ("file://relative/path", "http://example.org/x", "gs:///no-bucket", "/plain"):
        with pytest.raises(TargetError):
            open_target(bad, client=gcs)


# -- Google Cloud Storage (fake client) -----------------------------------------------------------


def test_gcs_target_only_creates(tmp_path, gcs):
    target = open_target("gs://example-bucket/thresy", client=gcs)
    source = tmp_path / "f"
    source.write_bytes(b"payload")
    assert target.put("runs/1/manifest.json", source) is True
    assert gcs.objects == {("example-bucket", "thresy/runs/1/manifest.json"): b"payload"}
    # A second create of the same name: 412 from GCS means "already there".
    assert target.put("runs/1/manifest.json", source) is False
    kinds = {call[0] for call in gcs.calls}
    assert kinds == {"bucket", "blob", "upload"}
    uploads = [call[2] for call in gcs.calls if call[0] == "upload"]
    assert all(u["if_generation_match"] == 0 and u["checksum"] == "crc32c" for u in uploads)


def test_gcs_target_without_prefix(tmp_path, gcs):
    target = open_target("gs://example-bucket", client=gcs)
    source = tmp_path / "f"
    source.write_bytes(b"x")
    target.put("fit/2030/1.fit", source)
    assert ("example-bucket", "fit/2030/1.fit") in gcs.objects


def test_gcs_permission_errors_are_explained(tmp_path, gcs):
    source = tmp_path / "f"
    source.write_bytes(b"x")
    gcs.refuse = "forbidden"
    with pytest.raises(TargetError, match=r"roles/storage\.objectCreator"):
        open_target("gs://example-bucket/p", client=gcs).put("a", source)
    gcs.refuse = "notfound"
    with pytest.raises(TargetError, match="not found"):
        open_target("gs://example-bucket/p", client=gcs).put("a", source)


def test_gcs_client_rejects_a_broken_key_file(tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"type": "service_account"}))
    with pytest.raises(TargetError, match="cannot use the service-account key"):
        gcs_client(key)


# -- encryption (stand-in for age) ----------------------------------------------------------------


def test_encrypt_and_decrypt_round_trip(tmp_path, fake_age):
    plain = tmp_path / "plain"
    plain.write_bytes(b"memory notes")
    sealed = tmp_path / "sealed.age"
    crypto.encrypt(plain, sealed, ["age1recipientone", "age1two"], binary=str(fake_age))
    assert b"memory notes" not in sealed.read_bytes()
    identity = tmp_path / "key.txt"
    identity.write_text("age1two\n")
    out = tmp_path / "out"
    crypto.decrypt(sealed, out, identity, binary=str(fake_age))
    assert out.read_bytes() == b"memory notes"
    identity.write_text("age1someoneelse\n")
    with pytest.raises(crypto.CryptoError, match="no identity matched"):
        crypto.decrypt(sealed, tmp_path / "out2", identity, binary=str(fake_age))


def test_missing_age_is_explained(tmp_path):
    source = tmp_path / "f"
    source.write_bytes(b"x")
    with pytest.raises(crypto.CryptoError, match="age tool was not found"):
        crypto.encrypt(source, tmp_path / "o", ["age1x"], binary="thresy-no-such-age")
    with pytest.raises(crypto.CryptoError, match="no age recipient"):
        crypto.encrypt(source, tmp_path / "o", [], binary="age")


def test_real_age_round_trip(tmp_path, real_age):
    import subprocess

    age, keygen = real_age
    key = tmp_path / "key.txt"
    subprocess.run([keygen, "-o", str(key)], check=True, capture_output=True)
    recipient = next(
        line.split(": ", 1)[1]
        for line in key.read_text().splitlines()
        if line.startswith("# public key: ")
    )
    plain = tmp_path / "plain"
    plain.write_bytes(b"x" * 100_000)
    crypto.encrypt(plain, tmp_path / "sealed.age", [recipient], binary=age)
    crypto.decrypt(tmp_path / "sealed.age", tmp_path / "back", key, binary=age)
    assert (tmp_path / "back").read_bytes() == plain.read_bytes()


# -- settings -------------------------------------------------------------------------------------


def test_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COACH_BACKUP_AGE_RECIPIENT", "age1one, age1two\nage1three")
    monkeypatch.setenv("COACH_BACKUP_AT", "4:05")
    bs = BackupSettings()
    assert bs.recipients == ["age1one", "age1two", "age1three"]
    assert bs.at == time(4, 5)
    monkeypatch.setenv("COACH_BACKUP_AT", "25:00")
    with pytest.raises(ValueError):
        BackupSettings()


# -- schedule -------------------------------------------------------------------------------------


BERLIN = ZoneInfo("Europe/Berlin")


def berlin(*args: int) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


def backup_settings(monkeypatch, tmp_path, **values) -> BackupSettings:
    monkeypatch.chdir(tmp_path)  # no .env
    for name in ("COACH_BACKUP_AT", "COACH_BACKUP_WEEKDAYS", "COACH_BACKUP_RESTORE_CHECK_DAYS"):
        monkeypatch.delenv(name, raising=False)
    return BackupSettings(**values)


def test_next_time_is_local_and_daily_by_default(monkeypatch, tmp_path):
    bs = backup_settings(monkeypatch, tmp_path, backup_at="03:30")
    assert bs.weekdays == frozenset(range(7))
    before = datetime(2030, 1, 2, 1, 0, tzinfo=UTC)  # 02:00 local
    assert schedule.next_time(before, bs, BERLIN) == datetime(2030, 1, 2, 2, 30, tzinfo=UTC)
    after = datetime(2030, 1, 2, 3, 0, tzinfo=UTC)  # 04:00 local
    assert schedule.next_time(after, bs, BERLIN) == datetime(2030, 1, 3, 2, 30, tzinfo=UTC)
    assert schedule.previous_time(after, bs, BERLIN) == datetime(2030, 1, 2, 2, 30, tzinfo=UTC)
    assert schedule.previous_time(before, bs, BERLIN) == datetime(2030, 1, 1, 2, 30, tzinfo=UTC)
    assert schedule.describe(bs, BERLIN) == "daily at 03:30 (Europe/Berlin)"


def test_one_weekday(monkeypatch, tmp_path):
    bs = backup_settings(monkeypatch, tmp_path, backup_weekdays="SUN")
    # 2030-01-02 is a Wednesday.
    wednesday = berlin(2030, 1, 2, 12, 0)
    assert schedule.next_time(wednesday, bs, BERLIN) == berlin(2030, 1, 6, 3, 30)
    assert schedule.previous_time(wednesday, bs, BERLIN) == berlin(2029, 12, 30, 3, 30)
    sunday_early = berlin(2030, 1, 6, 3, 0)
    assert schedule.next_time(sunday_early, bs, BERLIN) == berlin(2030, 1, 6, 3, 30)
    sunday_late = berlin(2030, 1, 6, 3, 30)  # exactly then: the next one is a week later
    assert schedule.next_time(sunday_late, bs, BERLIN) == berlin(2030, 1, 13, 3, 30)
    assert schedule.previous_time(sunday_late, bs, BERLIN) == berlin(2030, 1, 6, 3, 30)
    assert schedule.describe(bs, BERLIN) == "sun at 03:30 (Europe/Berlin)"


def test_several_weekdays(monkeypatch, tmp_path):
    bs = backup_settings(monkeypatch, tmp_path, backup_weekdays="thursday, Mon", backup_at="22:15")
    assert bs.weekdays == {0, 3}
    moment = berlin(2030, 1, 2, 12, 0)  # Wednesday
    planned = []
    for _ in range(4):
        moment = schedule.next_time(moment, bs, BERLIN)
        planned.append(moment.astimezone(BERLIN))
    assert planned == [
        berlin(2030, 1, 3, 22, 15),  # Thursday
        berlin(2030, 1, 7, 22, 15),  # Monday
        berlin(2030, 1, 10, 22, 15),
        berlin(2030, 1, 14, 22, 15),
    ]
    assert schedule.describe(bs, BERLIN) == "mon, thu at 22:15 (Europe/Berlin)"


def test_weekdays_setting_is_checked(monkeypatch, tmp_path):
    for value in ("daily", "Daily", "mon tue", "sat,sun", " sun , "):
        backup_settings(monkeypatch, tmp_path, backup_weekdays=value)
    for value in ("sundays", "1", ",", "weekly"):
        with pytest.raises(ValueError, match="weekday"):
            backup_settings(monkeypatch, tmp_path, backup_weekdays=value)


def test_a_week_with_a_change_of_daylight_saving_time(monkeypatch, tmp_path):
    bs = backup_settings(monkeypatch, tmp_path, backup_weekdays="sun")
    # Clocks go forward on Sunday 2030-03-31 and back on Sunday 2030-10-27 in Berlin.
    spring = schedule.next_time(berlin(2030, 3, 24, 4, 0), bs, BERLIN)
    assert spring == berlin(2030, 3, 31, 3, 30)
    assert spring - berlin(2030, 3, 24, 3, 30) == timedelta(days=6, hours=23)
    autumn = schedule.next_time(berlin(2030, 10, 20, 4, 0), bs, BERLIN)
    assert autumn == berlin(2030, 10, 27, 3, 30)
    assert autumn - berlin(2030, 10, 20, 3, 30) == timedelta(days=7, hours=1)
    assert schedule.previous_time(berlin(2030, 3, 31, 12, 0), bs, BERLIN) == spring
    # A time the change skips (02:30 on the spring Sunday) runs an hour later on the clock.
    skipped = backup_settings(monkeypatch, tmp_path, backup_weekdays="sun", backup_at="02:30")
    moment = schedule.next_time(berlin(2030, 3, 30, 12, 0), skipped, BERLIN)
    assert moment.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M %Z") == "2030-03-31 03:30 CEST"


def test_catch_up_only_after_a_missed_scheduled_run(monkeypatch, tmp_path):
    daily = backup_settings(monkeypatch, tmp_path)
    weekly = backup_settings(monkeypatch, tmp_path, backup_weekdays="sun")
    now = berlin(2030, 1, 9, 12, 0)  # Wednesday
    assert schedule.catch_up_due({}, now, weekly, BERLIN)  # never backed up
    after_sunday = {"last_success_at": berlin(2030, 1, 6, 3, 41).isoformat()}
    assert not schedule.catch_up_due(after_sunday, now, weekly, BERLIN)  # 3 days old, fine
    assert schedule.catch_up_due(after_sunday, now, daily, BERLIN)  # daily: today's is missing
    week_before = {"last_success_at": berlin(2029, 12, 30, 3, 41).isoformat()}
    assert schedule.catch_up_due(week_before, now, weekly, BERLIN)  # Sunday's was missed
    retried = {"last_success_at": berlin(2030, 1, 6, 4, 45).isoformat()}  # after a retry
    assert not schedule.catch_up_due(retried, now, weekly, BERLIN)


def test_restore_check_rules():
    now = datetime(2030, 1, 10, 3, 30, tzinfo=UTC)
    assert schedule.restore_check_due({}, 7, now)
    assert not schedule.restore_check_due({}, 0, now)
    fresh = {"restore_check": {"at": (now - timedelta(days=2)).isoformat(), "ok": True}}
    assert not schedule.restore_check_due(fresh, 7, now)
    failed = {"restore_check": {"at": (now - timedelta(days=2)).isoformat(), "ok": False}}
    assert schedule.restore_check_due(failed, 7, now)
    stale = {"restore_check": {"at": (now - timedelta(days=8)).isoformat(), "ok": True}}
    assert schedule.restore_check_due(stale, 7, now)
    # The last check ended a few minutes after last week's backup began: a week old all the same.
    last_week = {
        "restore_check": {"at": (now - timedelta(days=7, minutes=-6)).isoformat(), "ok": True}
    }
    assert schedule.restore_check_due(last_week, 7, now)
    # Across a change to daylight saving time the week is an hour shorter.
    assert schedule.restore_check_due(
        {"restore_check": {"at": (now - timedelta(days=6, hours=23)).isoformat(), "ok": True}},
        7,
        now,
    )


# -- the service loop, with an injected clock -----------------------------------------------------


class FakeTime:
    """The clock of ``run_forever`` and its stop event: waiting moves the clock, nothing sleeps."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.stopped = False

    def __call__(self) -> datetime:
        return self.now

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def wait(self, timeout: float) -> bool:
        self.now += timedelta(seconds=timeout)
        return self.stopped


def run_service(monkeypatch, settings, *, start, status, backups=3):
    """Run the backup loop until it has made ``backups`` backups; return (time, restore check)
    of each."""
    import contextlib
    from types import SimpleNamespace

    clock = FakeTime(start)
    made: list[tuple[datetime, bool]] = []

    def fake_backup(config, *, restore_check, should_stop):
        made.append((clock.now.astimezone(BERLIN), restore_check))
        clock.now += timedelta(minutes=5)  # how long a backup takes; recorded when it ends
        status["last_success_at"] = clock.now.isoformat()
        if restore_check:
            status["restore_check"] = {"at": clock.now.isoformat(), "ok": True}
        if len(made) >= backups:
            clock.set()
        return SimpleNamespace(run_id="r", summary=lambda: "ok")

    monkeypatch.setattr(
        schedule, "db", SimpleNamespace(connect=lambda url: contextlib.nullcontext())
    )
    monkeypatch.setattr(schedule, "get_status", lambda conn: dict(status))
    monkeypatch.setattr(schedule, "run_backup", fake_backup)
    config = SimpleNamespace(database_url="postgresql://unused")
    schedule.run_forever(config, settings, BERLIN, stop=clock, now=clock)
    return made


def test_service_runs_weekly_and_checks_restores_every_run(monkeypatch, tmp_path):
    weekly = backup_settings(monkeypatch, tmp_path, backup_weekdays="sun")
    recent = {"last_success_at": berlin(2030, 1, 6, 3, 35).isoformat()}
    made = run_service(monkeypatch, weekly, start=berlin(2030, 1, 9, 12, 0), status=recent)
    assert made == [
        (berlin(2030, 1, 13, 3, 30), True),  # no catch-up: Sunday's backup is there
        (berlin(2030, 1, 20, 3, 30), True),  # a 7-day restore check comes with every run
        (berlin(2030, 1, 27, 3, 30), True),
    ]


def test_service_catches_up_after_a_missed_weekly_run(monkeypatch, tmp_path):
    weekly = backup_settings(
        monkeypatch, tmp_path, backup_weekdays="sun", backup_restore_check_days=14
    )
    missed = {
        "last_success_at": berlin(2029, 12, 30, 3, 35).isoformat(),
        "restore_check": {"at": berlin(2029, 12, 30, 3, 35).isoformat(), "ok": True},
    }
    start = berlin(2030, 1, 9, 12, 0)
    made = run_service(monkeypatch, weekly, start=start, status=missed)
    assert made == [
        (start + timedelta(seconds=schedule.START_DELAY_S), False),  # Sunday's run was missed
        (berlin(2030, 1, 13, 3, 30), True),  # back to Sundays; 14 days since the last check
        (berlin(2030, 1, 20, 3, 30), False),  # every other week
    ]


def test_service_keeps_the_daily_rhythm_by_default(monkeypatch, tmp_path):
    daily = backup_settings(monkeypatch, tmp_path)
    recent = {
        "last_success_at": berlin(2030, 1, 9, 3, 35).isoformat(),
        "restore_check": {"at": berlin(2030, 1, 9, 3, 35).isoformat(), "ok": True},
    }
    start = berlin(2030, 1, 9, 12, 0)
    made = run_service(monkeypatch, daily, start=start, status=recent, backups=7)
    assert made == [(berlin(2030, 1, day, 3, 30), day == 16) for day in range(10, 17)]
