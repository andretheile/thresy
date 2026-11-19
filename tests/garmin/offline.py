"""Run the real ``garminconnect.Garmin`` without network access.

Only the methods of ``garminconnect.client.Client`` that would talk to Garmin are replaced
(``connectapi``, ``download``, ``login``, ``resume_login``); everything above them, including
``Garmin.login``, the retry/translation decorator, the token file handling and
``resume_login``, is the installed library's own code. All responses are synthetic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import garminconnect
import pytest
from garminconnect.client import Client

PROFILE = {"displayName": "synthetic-user", "fullName": "Synthetic User", "profileId": 7}
SETTINGS = {"userData": {"measurementSystem": "metric"}}
TOKEN = "header.payload.signature"  # not decodable as a JWT: never "about to expire"
MFA_STATE = {
    "domain": "garmin.com",
    "flow": "ios",
    "method": "email",
    "login_params": {},
    "post_headers": {},
    "service_url": None,
    "csrf_token": None,
    "impersonate": None,
    "cookies": [],
}
GOOD_CODE = "123456"


def api_error(status: int) -> garminconnect.GarminConnectConnectionError:
    """What the library's transport raises for an HTTP error status."""
    return garminconnect.GarminConnectConnectionError(f"API Error {status}")


class OfflineGarmin:
    """Install with ``OfflineGarmin(monkeypatch)``; then configure the attributes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.routes: dict[str, Any] = {}
        """API path prefix -> response, exception, or callable(path, kwargs) -> response."""
        self.downloads: dict[str, Any] = {}
        self.calls: list[tuple[str, Any]] = []
        self.password_result: Any = "ok"  # "ok", "mfa" or an exception
        self.profile_error: BaseException | None = None
        self.resumed_states: list[Any] = []
        test = self

        def connectapi(client: Client, path: str, **kwargs: Any) -> Any:
            test.calls.append(("connectapi", path))
            if path.startswith("/userprofile-service/socialProfile"):
                if test.profile_error is not None:
                    raise test.profile_error
                return dict(PROFILE)
            if path.startswith("/userprofile-service/userprofile/user-settings"):
                return dict(SETTINGS)
            for prefix, response in test.routes.items():
                if path.startswith(prefix):
                    return test._respond(response, path, kwargs)
            raise api_error(404)

        def download(client: Client, path: str, **kwargs: Any) -> bytes:
            test.calls.append(("download", path))
            if path not in test.downloads:
                raise api_error(404)
            return test._respond(test.downloads[path], path, kwargs)

        def password_login(
            client: Client, email: str, password: str, prompt_mfa: Any = None, **kwargs: Any
        ) -> tuple[Any, Any]:
            test.calls.append(("password_login", email))
            result = test.password_result
            if isinstance(result, BaseException):
                raise result
            if result == "mfa":
                client._mfa_pending = True
                return "needs_mfa", dict(MFA_STATE)
            client.di_token, client.di_refresh_token, client.di_client_id = TOKEN, "r", "c"
            return None, None

        def resume_login(client: Client, client_state: Any, mfa_code: str) -> tuple[Any, Any]:
            test.calls.append(("resume_login", mfa_code))
            test.resumed_states.append(client_state)
            if mfa_code != GOOD_CODE:
                raise garminconnect.GarminConnectAuthenticationError(
                    "MFA verification failed: [INVALID_MFA_CODE]"
                )
            client.di_token, client.di_refresh_token, client.di_client_id = TOKEN, "r", "c"
            client._mfa_pending = False
            return None, None

        monkeypatch.setattr(Client, "connectapi", connectapi)
        monkeypatch.setattr(Client, "download", download)
        monkeypatch.setattr(Client, "login", password_login)
        monkeypatch.setattr(Client, "resume_login", resume_login)
        # The library sleeps between profile retries and 5xx retries.
        monkeypatch.setattr(garminconnect.api.base.time, "sleep", lambda _s: None)
        monkeypatch.delenv("GARMINTOKENS", raising=False)

    @staticmethod
    def _respond(response: Any, path: str, kwargs: dict[str, Any]) -> Any:
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(path, kwargs)
        return response

    def count(self, kind: str) -> int:
        return sum(1 for name, _ in self.calls if name == kind)

    def paths(self) -> list[str]:
        return [arg for name, arg in self.calls if name in ("connectapi", "download")]


def sequence(*responses: Any) -> Callable[[str, dict[str, Any]], Any]:
    """A route that answers successive calls with ``responses`` (the last one repeats)."""
    items = list(responses)

    def respond(_path: str, _kwargs: dict[str, Any]) -> Any:
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, BaseException):
            raise item
        return item

    return respond
