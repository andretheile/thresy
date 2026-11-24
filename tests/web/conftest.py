"""Fixtures for the web dashboard: an app over the synthetic demo athlete, behind the login."""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

with warnings.catch_warnings():
    # Starlette 1.x prefers the httpx2 package for its test client; httpx works the same.
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    from starlette.testclient import TestClient

from thresy.web import app as webapp
from thresy.web.demo import DemoSource
from thresy.web.i18n import Lang
from thresy.web.security import Limiter, hash_password

PASSWORD = "a long and synthetic test password"
SECRET = "test-secret-" + "x" * 40
FAST_HASH = hash_password(PASSWORD, log_n=10)  # the minimum cost the parser accepts: quick tests


class Clock:
    """A settable clock for session expiry and the rate limit."""

    def __init__(self, now: float = 1_900_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def demo_source(scenario: str = "full") -> Callable[[], Any]:
    source = DemoSource(scenario, "en")

    @contextmanager
    def factory() -> Iterator[Any]:
        yield source

    return factory


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_app(clock: Clock) -> Callable[..., tuple[webapp.Web, Any]]:
    """``make_app(**web_kwargs) -> (web, asgi_app)``; defaults: demo data, the test password,
    secure cookies, the shared test clock (also for the rate limit)."""

    def make(**kw: Any) -> tuple[webapp.Web, Any]:
        options: dict[str, Any] = {
            "lang": Lang.get("en"),
            "coach_name": "Thresy",
            "source": demo_source(),
            "password_hash": FAST_HASH,
            "secret": SECRET,
            "clock": clock,
            "limiter": Limiter(clock=clock),
        }
        options.update(kw)
        web = webapp.Web(**options)
        return web, webapp.build_app(web)

    return make


@pytest.fixture(autouse=True)
def quick_failed_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed logins wait at least FAILED_LOGIN_FLOOR_S; shorter in tests."""
    monkeypatch.setattr(webapp, "FAILED_LOGIN_FLOOR_S", 0.05)


def client(app: Any, *, raise_errors: bool = True) -> TestClient:
    return TestClient(
        app,
        base_url="https://testserver",
        follow_redirects=False,
        raise_server_exceptions=raise_errors,
    )


def form_token(response: Any) -> str:
    match = re.search(r'name="token" value="([^"]+)"', response.text)
    assert match, "no form token on the page"
    return match.group(1)


def login(c: TestClient, password: str = PASSWORD, **headers: str) -> Any:
    token = form_token(c.get("/login"))
    return c.post(
        "/login",
        data={"token": token, "password": password},
        headers={"origin": "https://testserver", **headers},
    )
