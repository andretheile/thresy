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


def test_next_time_is_local(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    zone = ZoneInfo("Europe/Berlin")
    bs = BackupSettings(backup_at="03:30")
    before = datetime(2030, 1, 2, 1, 0, tzinfo=UTC)  # 02:00 local
    assert schedule.next_time(before, bs, zone) == datetime(2030, 1, 2, 2, 30, tzinfo=UTC)
    after = datetime(2030, 1, 2, 3, 0, tzinfo=UTC)  # 04:00 local
    assert schedule.next_time(after, bs, zone) == datetime(2030, 1, 3, 2, 30, tzinfo=UTC)


def test_due_rules():
    now = datetime(2030, 1, 10, tzinfo=UTC)
    assert schedule.catch_up_due({}, now)
    recent = {"last_success_at": (now - timedelta(hours=20)).isoformat()}
    assert not schedule.catch_up_due(recent, now)
    old = {"last_success_at": (now - timedelta(hours=30)).isoformat()}
    assert schedule.catch_up_due(old, now)
    assert schedule.restore_check_due({}, 7, now)
    assert not schedule.restore_check_due({}, 0, now)
    fresh = {"restore_check": {"at": (now - timedelta(days=2)).isoformat(), "ok": True}}
    assert not schedule.restore_check_due(fresh, 7, now)
    failed = {"restore_check": {"at": (now - timedelta(days=2)).isoformat(), "ok": False}}
    assert schedule.restore_check_due(failed, 7, now)
    stale = {"restore_check": {"at": (now - timedelta(days=8)).isoformat(), "ok": True}}
    assert schedule.restore_check_due(stale, 7, now)
