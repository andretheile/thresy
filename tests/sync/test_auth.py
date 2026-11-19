"""When the sync logs in: token first, one password attempt, the code flow, events.

The real garminconnect.Garmin runs with its network methods replaced (tests.garmin.offline).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import garminconnect
import pytest
import requests

from tests.garmin.offline import GOOD_CODE, OfflineGarmin, api_error
from tests.sync.helpers import make_config
from thresy.garmin import GarminAuthRequired, GarminError, GarminMfaRequired
from thresy.garmin.auth import GarminLogin, TokenStore
from thresy.sync import state
from thresy.sync.auth import ClientProvider, human_login
from thresy.sync.passes import sync_once

pytestmark = pytest.mark.db

NOW = datetime(2030, 1, 2, 6, 0, tzinfo=UTC)
EMAIL, PASSWORD = "athlete@example.invalid", "secret"


@pytest.fixture
def offline(monkeypatch) -> OfflineGarmin:
    return OfflineGarmin(monkeypatch)


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path, garmin_email=EMAIL, garmin_password=PASSWORD)


def auth_events(conn) -> list[dict]:
    return conn.execute(
        "SELECT dedupe_key, payload FROM events WHERE kind = 'garmin_auth_required' ORDER BY id"
    ).fetchall()


def provider_for(config) -> ClientProvider:
    return ClientProvider(config, login=GarminLogin(TokenStore(config.token_dir)))


def test_no_token_and_no_credentials(conn, offline, tmp_path) -> None:
    config = make_config(tmp_path)
    provider = provider_for(config)
    for minutes in (0, 20, 40):
        with pytest.raises(GarminAuthRequired) as info:
            provider.get(conn, NOW + timedelta(minutes=minutes))
    assert info.value.status == "no_credentials"
    assert "coach sync login" in info.value.reason
    assert offline.calls == []  # nothing was sent to Garmin
    found = auth_events(conn)
    assert len(found) == 1 and found[0]["dedupe_key"] == "garmin_auth:2030-01-02"
    assert found[0]["payload"] == {"reason": info.value.reason}
    assert state.get(conn, state.AUTH)["status"] == "no_credentials"


def test_password_login_once_then_the_token(conn, offline, config) -> None:
    client = provider_for(config).get(conn, NOW)
    assert client.garmin.display_name == "synthetic-user"
    assert offline.count("password_login") == 1
    auth = state.get(conn, state.AUTH)
    assert auth["status"] == "ok" and auth["password_login_paused_until"] is None
    # A restarted service uses the saved token.
    provider_for(config).get(conn, NOW + timedelta(hours=1))
    assert offline.count("password_login") == 1
    assert auth_events(conn) == []


def test_cached_client_until_invalidated(conn, offline, config) -> None:
    provider = provider_for(config)
    first = provider.get(conn, NOW)
    assert provider.get(conn, NOW) is first
    provider.invalidate()
    assert provider.get(conn, NOW) is not first


def test_failed_password_login_is_not_retried(conn, offline, config) -> None:
    offline.password_result = garminconnect.GarminConnectAuthenticationError(
        "401 Unauthorized (Invalid Username or Password)"
    )
    provider = provider_for(config)
    with pytest.raises(GarminAuthRequired) as info:
        provider.get(conn, NOW)
    assert info.value.status == "login_failed"
    assert offline.count("password_login") == 1

    for hours in (1, 6, 17):  # the poller keeps trying the token only
        with pytest.raises(GarminAuthRequired) as info:
            provider_for(config).get(conn, NOW + timedelta(hours=hours))
        assert info.value.status == "login_paused"
    assert offline.count("password_login") == 1
    assert len(auth_events(conn)) == 1  # one per UTC day
    with pytest.raises(GarminAuthRequired):
        provider_for(config).get(conn, NOW + timedelta(hours=23))  # the next UTC day
    assert [e["dedupe_key"] for e in auth_events(conn)] == [
        "garmin_auth:2030-01-02",
        "garmin_auth:2030-01-03",
    ]
    assert offline.count("password_login") == 1

    offline.password_result = "ok"
    provider_for(config).get(conn, NOW + timedelta(hours=24, minutes=1))
    assert offline.count("password_login") == 2
    assert state.get(conn, state.AUTH)["status"] == "ok"


def test_rate_limited_password_login_pauses_too(conn, offline, config) -> None:
    offline.password_result = garminconnect.GarminConnectTooManyRequestsError("429")
    with pytest.raises(GarminAuthRequired, match="rate-limiting"):
        provider_for(config).get(conn, NOW)
    with pytest.raises(GarminAuthRequired):
        provider_for(config).get(conn, NOW + timedelta(minutes=20))
    assert offline.count("password_login") == 1


def test_attempt_is_counted_before_it_is_made(conn, offline, config) -> None:
    offline.password_result = KeyboardInterrupt()  # the process dies mid-login
    with pytest.raises(KeyboardInterrupt):
        provider_for(config).get(conn, NOW)
    paused = state.get(conn, state.AUTH)["password_login_paused_until"]
    assert paused == (NOW + timedelta(hours=config.login_retry_h)).isoformat()
    with pytest.raises(GarminAuthRequired, match="paused"):
        provider_for(config).get(conn, NOW + timedelta(minutes=5))
    assert offline.count("password_login") == 1


@pytest.mark.parametrize(
    "error", [requests.ConnectionError("network down"), api_error(503), api_error(429)]
)
def test_token_problems_that_are_not_rejections_never_cause_a_password_login(
    conn, offline, config, error
) -> None:
    provider_for(config).get(conn, NOW)  # saves a token
    offline.profile_error = error
    with pytest.raises(GarminError) as info:
        provider_for(config).get(conn, NOW + timedelta(hours=1))
    assert not isinstance(info.value, GarminAuthRequired)
    assert offline.count("password_login") == 1


def test_rejected_token_falls_back_to_one_password_login(conn, offline, config) -> None:
    provider_for(config).get(conn, NOW)
    offline.profile_error = api_error(401)
    offline.password_result = garminconnect.GarminConnectAuthenticationError("account locked")
    with pytest.raises(GarminAuthRequired) as info:
        provider_for(config).get(conn, NOW + timedelta(hours=1))
    assert info.value.status == "login_failed"
    assert offline.count("password_login") == 2


def test_code_flow_on_a_server(conn, offline, config) -> None:
    offline.password_result = "mfa"
    with pytest.raises(GarminAuthRequired) as info:
        provider_for(config).get(conn, NOW)
    assert info.value.status == "mfa_required"
    assert "coach sync login --code <code>" in info.value.reason and "email" in info.value.reason
    assert TokenStore(config.token_dir).load_pending_mfa() is not None
    assert auth_events(conn)[0]["payload"]["reason"] == info.value.reason

    # Passes while waiting: no second password login (it would send a new code).
    with pytest.raises(GarminAuthRequired):
        provider_for(config).get(conn, NOW + timedelta(minutes=20))
    assert offline.count("password_login") == 1

    # The athlete runs `coach sync login --code ...` in the sync container.
    with pytest.raises(GarminError):
        human_login(config, conn=conn, now=NOW + timedelta(minutes=25), code="999999")
    outcome = human_login(config, conn=conn, now=NOW + timedelta(minutes=26), code=GOOD_CODE)
    assert outcome.status == "ok"
    assert state.get(conn, state.AUTH)["status"] == "ok"
    assert TokenStore(config.token_dir).load_pending_mfa() is None
    provider_for(config).get(conn, NOW + timedelta(minutes=40))
    assert offline.count("password_login") == 1


def test_sync_once_reports_auth_problems(conn, offline, tmp_path) -> None:
    outcome = sync_once(conn, make_config(tmp_path), provider_for(make_config(tmp_path)), now=NOW)
    assert outcome.error_kind == "auth" and "No Garmin login" in outcome.error
    assert state.get(conn, state.STATUS)["last_error_kind"] == "auth"


# coach sync login ------------------------------------------------------------------------------


def test_human_login_checks_the_token_first(conn, offline, config) -> None:
    assert human_login(config, conn=conn, now=NOW).status == "ok"
    assert human_login(config, conn=conn, now=NOW).status == "token_ok"
    assert offline.count("password_login") == 1
    assert human_login(config, conn=conn, now=NOW, restart=True).status == "ok"
    assert offline.count("password_login") == 2


def test_human_login_ignores_the_pause_and_works_without_database(offline, config) -> None:
    offline.password_result = "mfa"
    outcome = human_login(config, conn=None, now=NOW)
    assert outcome.status == "mfa_required" and "--code" in outcome.message
    with pytest.raises(GarminMfaRequired, match="--restart"):
        human_login(config, conn=None, now=NOW)
    assert offline.count("password_login") == 1
    assert human_login(config, conn=None, now=NOW, restart=True).status == "mfa_required"
    assert human_login(config, conn=None, now=NOW, code=GOOD_CODE).status == "ok"


def test_human_login_interactive_code(conn, offline, config) -> None:
    offline.password_result = "mfa"
    methods = []

    def prompt(method):
        methods.append(method)
        return GOOD_CODE

    outcome = human_login(config, conn=conn, now=NOW, prompt_code=prompt)
    assert outcome.status == "ok" and methods == ["email"]
    assert TokenStore(config.token_dir).has_token()
    assert TokenStore(config.token_dir).load_pending_mfa() is None


def test_human_login_credentials_from_arguments_or_none(conn, offline, tmp_path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(GarminAuthRequired, match="COACH_GARMIN_EMAIL"):
        human_login(config, conn=conn, now=NOW)
    outcome = human_login(config, conn=conn, now=NOW, email=EMAIL, password=PASSWORD)
    assert outcome.status == "ok"
    assert offline.calls[0] == ("password_login", EMAIL)


def test_human_login_failure_is_recorded(conn, offline, config) -> None:
    offline.password_result = garminconnect.GarminConnectAuthenticationError("bad password")
    with pytest.raises(GarminError):
        human_login(config, conn=conn, now=NOW)
    auth = state.get(conn, state.AUTH)
    assert auth["status"] == "login_failed" and auth["password_login_paused_until"]
