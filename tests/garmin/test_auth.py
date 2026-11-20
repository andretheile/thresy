"""Token login, password login and the two-step code flow, with the real library offline."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta

import garminconnect
import pytest
import requests

from tests.garmin.offline import GOOD_CODE, OfflineGarmin, api_error
from thresy.garmin import ConnectClient, GarminAuthError, GarminError, GarminRateLimited
from thresy.garmin.auth import (
    MFA_PENDING_TTL,
    GarminLogin,
    PendingMfa,
    TokenMissing,
    TokenStore,
    write_private,
)

NOW = datetime(2030, 1, 2, 6, 0, tzinfo=UTC)


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def offline(monkeypatch) -> OfflineGarmin:
    return OfflineGarmin(monkeypatch)


@pytest.fixture
def login(tmp_path) -> GarminLogin:
    return GarminLogin(TokenStore(tmp_path / "garmin"))


def test_token_login_without_token_makes_no_request(offline, login) -> None:
    with pytest.raises(TokenMissing):
        login.token_login()
    assert offline.calls == []


def test_password_login_saves_tokens_privately_and_token_login_reuses_them(offline, login) -> None:
    started = login.start_password_login("athlete@example.invalid", "secret", now=NOW)
    assert started.pending is None and isinstance(started.client, ConnectClient)
    store = login.store
    assert mode(store.directory) == 0o700 and mode(store.token_file) == 0o600
    assert json.loads(store.token_file.read_text())["di_token"] == "header.payload.signature"
    assert offline.count("password_login") == 1
    # The library loaded the profile through the saved token.
    assert started.client.garmin.display_name == "synthetic-user"

    client = GarminLogin(TokenStore(store.directory)).token_login()
    assert client.garmin.display_name == "synthetic-user"
    assert offline.count("password_login") == 1  # no second password login


def test_rejected_token_is_an_auth_error_and_never_sends_the_password(offline, login) -> None:
    login.start_password_login("athlete@example.invalid", "secret", now=NOW)
    offline.profile_error = api_error(401)
    with pytest.raises(GarminAuthError) as info:
        login.token_login()
    assert type(info.value) is GarminAuthError
    assert offline.count("password_login") == 1  # only the one above


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (requests.ConnectionError("network down"), GarminError),
        (api_error(503), GarminError),
        (api_error(429), GarminRateLimited),
    ],
)
def test_token_login_problems_that_are_not_rejections(offline, login, error, expected) -> None:
    login.start_password_login("athlete@example.invalid", "secret", now=NOW)
    offline.profile_error = error
    with pytest.raises(GarminError) as info:
        login.token_login()
    assert type(info.value) is expected  # not GarminAuthError: no password login follows


def test_unreadable_token_file_is_an_auth_error(offline, login) -> None:
    write_private(login.store.token_file, "{not json")
    with pytest.raises(GarminAuthError):
        login.token_login()


def test_wrong_password(offline, login) -> None:
    offline.password_result = garminconnect.GarminConnectAuthenticationError(
        "401 Unauthorized (Invalid Username or Password)"
    )
    with pytest.raises(GarminAuthError):
        login.start_password_login("athlete@example.invalid", "wrong", now=NOW)
    assert not login.store.has_token()


def test_two_step_code_flow_across_processes(offline, tmp_path) -> None:
    directory = tmp_path / "garmin"
    offline.password_result = "mfa"
    started = GarminLogin(TokenStore(directory)).start_password_login(
        "athlete@example.invalid", "secret", now=NOW
    )
    assert started.client is None and started.pending is not None
    assert started.pending.method == "email"
    store = TokenStore(directory)
    assert mode(store.mfa_file) == 0o600 and not store.has_token()
    assert store.load_pending_mfa() == started.pending

    # Later, another process: a wrong code keeps the waiting login.
    later = GarminLogin(TokenStore(directory))
    with pytest.raises(GarminAuthError, match="MFA verification failed"):
        later.complete_mfa("000000", now=NOW + timedelta(minutes=2))
    assert store.load_pending_mfa() is not None

    client = later.complete_mfa(f" {GOOD_CODE} ", now=NOW + timedelta(minutes=3))
    assert client.garmin.display_name == "synthetic-user"
    assert store.has_token() and mode(store.token_file) == 0o600
    assert not store.mfa_file.exists()
    # The saved state, not the instance, carried the login to the second process.
    assert offline.resumed_states[-1]["flow"] == "ios"
    assert GarminLogin(TokenStore(directory)).token_login().garmin.display_name


def test_interactive_code_on_the_same_instance(offline, login) -> None:
    offline.password_result = "mfa"
    started = login.start_password_login("athlete@example.invalid", "secret", now=NOW)
    client = login.complete_mfa(GOOD_CODE, now=NOW, garmin=started.garmin)
    assert isinstance(client, ConnectClient) and login.store.has_token()


def test_complete_mfa_without_or_with_an_expired_waiting_login(offline, login) -> None:
    with pytest.raises(GarminAuthError, match="no Garmin login is waiting"):
        login.complete_mfa(GOOD_CODE, now=NOW)
    offline.password_result = "mfa"
    login.start_password_login("athlete@example.invalid", "secret", now=NOW)
    with pytest.raises(GarminAuthError, match="expired"):
        login.complete_mfa(GOOD_CODE, now=NOW + MFA_PENDING_TTL + timedelta(seconds=1))
    assert login.store.load_pending_mfa() is None
    with pytest.raises(GarminAuthError, match="empty"):
        login.complete_mfa("  ", now=NOW)


def test_errors_never_carry_the_sso_ticket_or_the_password(login) -> None:
    """``requests`` quotes the request URL in its errors; while a login exchanges its SSO
    service ticket that URL holds the ticket, and the error text is logged, stored and shown
    to the athlete."""
    ticket = "ST-0000-FAKE-ticket-cas"

    class TicketExchangeFails:
        def resume_login(self, state, code):
            raise requests.ConnectionError(
                "HTTPSConnectionPool(host='connect.example.invalid', port=443): Max retries "
                f"exceeded with url: /app?ticket={ticket} (Caused by NewConnectionError('x'))"
            )

    with pytest.raises(GarminError) as info:
        login.complete_mfa(GOOD_CODE, now=NOW, garmin=TicketExchangeFails())
    assert ticket not in str(info.value) and "ticket=<redacted>" in str(info.value)

    # A password typed in (not in the environment) is redacted from then on as well.
    password = "fake-typed-password-0000"

    class EchoesThePassword:
        def login(self):
            raise ValueError(f"login form rejected: {password}")

    echo = GarminLogin(login.store, factory=lambda **_kw: EchoesThePassword())
    with pytest.raises(GarminError) as info:
        echo.start_password_login("athlete@example.invalid", password, now=NOW)
    assert password not in str(info.value) and "<redacted>" in str(info.value)


def test_pending_file_round_trip_and_garbage(tmp_path) -> None:
    store = TokenStore(tmp_path / "garmin")
    assert store.load_pending_mfa() is None
    pending = PendingMfa(created_at=NOW, method=None, client_state={"flow": "widget"})
    store.save_pending_mfa(pending)
    assert store.load_pending_mfa() == pending
    write_private(store.mfa_file, "[]")
    assert store.load_pending_mfa() is None
    store.clear_pending_mfa()
    store.clear_pending_mfa()  # idempotent
    assert not store.mfa_file.exists()


def test_ensure_private_tightens_modes(tmp_path) -> None:
    store = TokenStore(tmp_path / "garmin")
    store.directory.mkdir(mode=0o755)
    store.token_file.write_text("{}")
    store.token_file.chmod(0o644)
    store.ensure_private()
    assert mode(store.directory) == 0o700 and mode(store.token_file) == 0o600
