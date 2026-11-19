"""ConnectClient over the real garminconnect.Garmin (network methods replaced), and the fake."""

from __future__ import annotations

from datetime import date

import garminconnect
import pytest
import requests

from tests import synthetic
from tests.garmin.offline import OfflineGarmin, api_error, sequence
from thresy.garmin import (
    ConnectClient,
    GarminAuthError,
    GarminClient,
    GarminError,
    GarminNotFound,
    GarminRateLimited,
)
from thresy.garmin.client import none_if_empty, translate_error
from thresy.garmin.fake import FakeGarminClient

D = date(2030, 1, 2)


@pytest.fixture
def offline(monkeypatch) -> OfflineGarmin:
    return OfflineGarmin(monkeypatch)


@pytest.fixture
def client(offline) -> ConnectClient:
    garmin = garminconnect.Garmin()
    garmin.display_name = "synthetic-user"  # what login() would have loaded
    return ConnectClient(garmin)


def test_both_clients_satisfy_the_protocol(client) -> None:
    assert isinstance(client, GarminClient)
    assert isinstance(FakeGarminClient(), GarminClient)


def test_activities_by_date_pages_until_empty(offline, client) -> None:
    seen = []

    def page(_path, kwargs):
        seen.append(dict(kwargs["params"]))
        return [synthetic.activity(1)] if kwargs["params"]["start"] == "0" else []

    offline.routes["/activitylist-service/activities/search/activities"] = page
    items = client.get_activities_by_date(D, date(2030, 1, 9))
    assert [a["activityId"] for a in items] == [1]
    assert seen[0]["startDate"] == "2030-01-02" and seen[0]["endDate"] == "2030-01-09"
    assert [p["start"] for p in seen] == ["0", "20"]


def test_download_original_uses_the_original_format(offline, client) -> None:
    offline.downloads["/download-service/files/activity/42"] = b"PK-zip"
    assert client.download_original(42) == b"PK-zip"
    with pytest.raises(GarminNotFound):
        client.download_original(43)


def test_wellness_calls_pass_iso_dates_and_empty_answers_become_none(offline, client) -> None:
    offline.routes["/wellness-service/wellness/dailySleepData/"] = synthetic.sleep(D)
    offline.routes["/hrv-service/hrv/2030-01-02"] = {}  # HTTP 204 comes back as {}
    offline.routes["/usersummary-service/usersummary/daily/"] = synthetic.stats(D)
    offline.routes["/wellness-service/wellness/dailySummaryChart/"] = []
    assert client.get_sleep_data(D)["dailySleepDTO"]["calendarDate"] == "2030-01-02"
    assert client.get_hrv_data(D) is None
    assert client.get_stats(D)["totalSteps"] == 8000
    assert client.get_steps_data(D) is None
    assert "/hrv-service/hrv/2030-01-02" in offline.paths()


def test_empty_daily_summary_is_no_data(offline, client) -> None:
    offline.routes["/usersummary-service/usersummary/daily/"] = {}  # the library raises
    assert client.get_stats(D) is None
    offline.routes["/usersummary-service/usersummary/daily/"] = api_error(500)
    with pytest.raises(GarminError):
        client.get_stats(D)


def test_personal_records_list(offline, client) -> None:
    offline.routes["/personalrecord-service/personalrecord/prs/"] = [
        {"typeId": 3, "value": 1500.0},
        "garbage",
    ]
    assert client.get_personal_records() == [{"typeId": 3, "value": 1500.0}]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (api_error(429), GarminRateLimited),
        (api_error(404), GarminNotFound),
        (api_error(401), GarminAuthError),
        (api_error(503), GarminError),  # after the library's own retries
        (requests.ConnectionError("network down"), GarminError),
    ],
)
def test_errors_are_translated(offline, client, error, expected) -> None:
    offline.routes["/hrv-service/hrv/"] = error
    with pytest.raises(expected) as info:
        client.get_hrv_data(D)
    if expected is GarminError:
        assert not isinstance(info.value, GarminAuthError | GarminRateLimited | GarminNotFound)


def test_server_errors_are_retried_by_the_library(offline, client) -> None:
    offline.routes["/hrv-service/hrv/"] = sequence(api_error(502), synthetic.hrv(D))
    assert client.get_hrv_data(D)["hrvSummary"]["calendarDate"] == "2030-01-02"
    assert offline.paths().count("/hrv-service/hrv/2030-01-02") == 2


def test_auth_error_caused_by_the_network_is_not_an_auth_error() -> None:
    try:
        try:
            raise requests.ConnectionError("network down")
        except requests.ConnectionError as cause:
            raise garminconnect.GarminConnectAuthenticationError(
                "Failed to retrieve social profile"
            ) from cause
    except garminconnect.GarminConnectAuthenticationError as exc:
        translated = translate_error(exc)
    assert type(translated) is GarminError
    plain = translate_error(garminconnect.GarminConnectAuthenticationError("401 Unauthorized"))
    assert type(plain) is GarminAuthError
    assert isinstance(translate_error(ValueError("bad date")), ValueError)


def test_none_if_empty() -> None:
    assert none_if_empty({}) is None and none_if_empty([]) is None and none_if_empty(None) is None
    assert none_if_empty({"a": 1}) == {"a": 1}


# The fake ---------------------------------------------------------------------------------------


def test_fake_filters_by_local_date_newest_first_and_copies() -> None:
    fake = FakeGarminClient()
    fake.add_activity(synthetic.activity(1, start_local="2030-01-01 07:00:00"))
    fake.add_activity(synthetic.activity(2, start_local="2030-01-03 07:00:00"))
    fake.add_activity(synthetic.activity(3, start_local="2030-01-09 07:00:00"))
    items = fake.get_activities_by_date(date(2030, 1, 1), date(2030, 1, 3))
    assert [a["activityId"] for a in items] == [2, 1]
    items[0]["activityName"] = "changed"
    assert fake.activities[2]["activityName"] == "Morning Run"


def test_fake_downloads_wellness_and_failures() -> None:
    fake = FakeGarminClient()
    fake.add_activity(synthetic.activity(1), original=b"zip")
    fake.add_activity(synthetic.activity(2))
    assert fake.download_original(1) == b"zip"
    with pytest.raises(GarminNotFound):
        fake.download_original(2)
    fake.set_wellness(D, sleep=synthetic.sleep(D))
    assert fake.get_sleep_data(D)["dailySleepDTO"]["sleepTimeSeconds"] > 0
    assert fake.get_hrv_data(D) is None
    fake.fail("get_hrv_data", GarminRateLimited("429"), lambda: {"served": True})
    with pytest.raises(GarminRateLimited):
        fake.get_hrv_data(D)
    assert fake.get_hrv_data(D) == {"served": True}
    assert fake.get_hrv_data(D) is None
    assert fake.days_requested("get_hrv_data") == [D, D, D, D]
    assert fake.call_count("download_original") == 2
