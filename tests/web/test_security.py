"""The dashboard's login and the protection of everything behind it."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from starlette.routing import Mount, Route
from typer.testing import CliRunner

from thresy import logs
from thresy.cli import get_app
from thresy.config import ConfigError
from thresy.web import app as webapp
from thresy.web.security import (
    Limiter,
    Sessions,
    Signer,
    client_key,
    generate_password,
    hash_password,
    parse_password_hash,
    verify_password,
)
from thresy.web.settings import WebSettings, bind_host, require_secrets

from .conftest import FAST_HASH, PASSWORD, SECRET, client, demo_source, form_token, login

# Every route of the app and whether it is public. A new route must be added here, which
# forces a decision; test_every_route_is_known fails otherwise.
ROUTES = {
    ("/", "GET"): False,
    ("/runs", "GET"): False,
    ("/runs/{activity_id:int}", "GET"): False,
    ("/trends", "GET"): False,
    ("/races", "GET"): False,
    ("/segments", "GET"): False,
    ("/data", "GET"): False,
    ("/logout", "POST"): False,
    ("/login", "GET"): True,
    ("/login", "POST"): True,
    ("/healthz", "GET"): True,
    ("/robots.txt", "GET"): True,
    ("/favicon.ico", "GET"): True,
    ("/static", "MOUNT"): True,
}
REQUIRED_HEADERS = {
    "content-security-policy": webapp.CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "x-robots-tag": "noindex, nofollow, noarchive",
}


def app_routes(app: Any) -> set[tuple[str, str]]:
    out = set()
    for route in app.starlette.routes:
        if isinstance(route, Mount):
            out.add((route.path, "MOUNT"))
        elif isinstance(route, Route):
            for method in route.methods or ():
                if method != "HEAD":
                    out.add((route.path, method))
    return out


def concrete(path: str) -> str:
    return path.replace("{activity_id:int}", "7000001")


def session_cookie(c: Any) -> str | None:
    return c.cookies.get(webapp.COOKIE_SECURE)


# -- passwords -------------------------------------------------------------------------------------


def test_password_hash_round_trip() -> None:
    stored = hash_password("correct horse battery staple", log_n=10)
    assert stored.startswith("scrypt:ln=10,r=8,p=1:")
    assert "$" not in stored  # survives Compose's interpolation of .env
    parsed = parse_password_hash(stored)
    assert verify_password("correct horse battery staple", parsed)
    assert not verify_password("correct horse battery stapl", parsed)
    assert not verify_password("", parsed)
    # salted: the same password hashes differently
    assert hash_password("x" * 12, log_n=10) != hash_password("x" * 12, log_n=10)


def test_default_hash_cost() -> None:
    assert parse_password_hash(hash_password("p" * 16)).log_n == 15


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "plaintext",
        "bcrypt:ln=15,r=8,p=1:AAAA:BBBB",
        "scrypt:ln=30,r=8,p=1:c2FsdHNhbHQ:aGFzaGhhc2hoYXNoaGFzaA",  # absurd cost
        "scrypt:ln=15,r=8:c2FsdHNhbHQ:aGFzaGhhc2hoYXNoaGFzaA",
        "scrypt:ln=15,r=8,p=1:c2E:aGFzaA",  # too short
    ],
)
def test_bad_hashes_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_password_hash(bad)


def test_generated_passwords_are_long_and_random() -> None:
    a, b = generate_password(), generate_password()
    assert a != b
    assert len(a.replace("-", "")) == 25


# -- signed values ---------------------------------------------------------------------------------


def test_signed_values_check_signature_purpose_and_expiry() -> None:
    signer = Signer(SECRET, FAST_HASH)
    token = signer.sign("session", {"sid": "abc", "exp": 200})
    assert signer.unsign("session", token, now=100) == {"sid": "abc", "exp": 200}
    assert signer.unsign("session", token, now=200) is None  # expired
    assert signer.unsign("logout", token, now=100) is None  # another purpose
    body, mac = token.split(".")
    forged_body = body[:-2] + ("AA" if body[-2:] != "AA" else "BB")
    assert signer.unsign("session", f"{forged_body}.{mac}", now=100) is None
    assert signer.unsign("session", f"{body}.{mac[:-1]}x", now=100) is None
    assert signer.unsign("session", "garbage", now=100) is None
    assert signer.unsign("session", None, now=100) is None
    # another secret, or another password hash, signs differently
    assert Signer("other-" + SECRET, FAST_HASH).unsign("session", token, now=100) is None
    assert (
        Signer(SECRET, hash_password("other", log_n=10)).unsign("session", token, now=100) is None
    )


def test_session_revocation() -> None:
    sessions = Sessions(Signer(SECRET, FAST_HASH), 3600)
    session, token = sessions.create(now=1000)
    assert sessions.read(token, 1000) == session
    assert sessions.read(token, 1000 + 3600) is None
    sessions.revoke(session, 1100)
    assert sessions.read(token, 1200) is None


# -- rate limit ------------------------------------------------------------------------------------


class Tick:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_limiter_grows_the_delay_per_client() -> None:
    tick = Tick()
    limiter = Limiter(client_free=3, base_s=10, client_cap_s=1000, clock=tick)
    for _ in range(3):
        assert limiter.check("a") == 0
        limiter.failure("a")
    assert limiter.check("a") == 10  # the 3rd failure was free; now 10 s
    tick.now += 10
    assert limiter.check("a") == 0
    limiter.failure("a")
    assert limiter.check("a") == 20
    tick.now += 20
    limiter.failure("a")
    assert limiter.check("a") == 40
    assert limiter.check("b") == 0  # other clients are not affected
    limiter.success("a")
    assert limiter.check("a") == 0


def test_limiter_caps_the_delay() -> None:
    tick = Tick()
    limiter = Limiter(client_free=0, base_s=10, client_cap_s=60, global_free=1000, clock=tick)
    for _ in range(20):
        limiter.failure("a")
    assert limiter.check("a") == 60


def test_rotating_clients_meet_the_global_limit() -> None:
    tick = Tick()
    limiter = Limiter(client_free=5, global_free=10, base_s=10, global_cap_s=300, clock=tick)
    for n in range(10):
        assert limiter.check(f"10.0.0.{n}") == 0
        limiter.failure(f"10.0.0.{n}")
    assert limiter.check("10.0.0.200") == 10  # a brand-new address waits as well
    tick.now += 10
    limiter.failure("10.0.0.201")
    assert limiter.check("10.0.0.202") == 20
    tick.now += 3601  # failures older than the window no longer count
    assert limiter.check("10.0.0.200") == 0


def test_limiter_memory_is_bounded() -> None:
    limiter = Limiter(max_clients=50, global_free=10**6, clock=Tick())
    for n in range(500):
        limiter.failure(f"c{n}")
    assert len(limiter._clients) <= 50


@pytest.mark.parametrize(
    ("peer", "values", "trusted", "hops", "expected"),
    [
        ("10.1.1.1", [], False, 1, "10.1.1.1"),
        ("10.1.1.1", ["203.0.113.9"], False, 1, "10.1.1.1"),  # untrusted header is ignored
        ("10.1.1.1", ["203.0.113.9"], True, 1, "203.0.113.9"),
        # a client-supplied X-Forwarded-For entry sits left of the one the proxy appended
        ("10.1.1.1", ["6.6.6.6, 203.0.113.9"], True, 1, "203.0.113.9"),
        ("10.1.1.1", ["6.6.6.6, 203.0.113.9, 172.16.0.2"], True, 2, "203.0.113.9"),
        ("10.1.1.1", ["203.0.113.9"], True, 2, "10.1.1.1"),  # chain too short: the peer
        ("10.1.1.1", ["not an address"], True, 1, "10.1.1.1"),
        ("10.1.1.1", ["203.0.113.9:4711"], True, 1, "203.0.113.9"),
        ("10.1.1.1", ["2001:db8:1:2:3:4:5:6"], True, 1, "2001:db8:1:2::/64"),
        ("10.1.1.1", ["[2001:db8:1:2::9]:443"], True, 1, "2001:db8:1:2::/64"),
        (None, [], False, 1, "unknown"),
    ],
)
def test_client_key(
    peer: str | None, values: list[str], trusted: bool, hops: int, expected: str
) -> None:
    assert client_key(peer, values, trusted=trusted, hops=hops) == expected


# -- settings --------------------------------------------------------------------------------------


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """No COACH_WEB_* from the environment or a .env file."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "COACH_WEB_PASSWORD_HASH",
        "COACH_WEB_SECRET",
        "COACH_WEB_DEV_HTTP",
        "COACH_WEB_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


def test_refuses_to_start_unconfigured(no_env: None) -> None:
    with pytest.raises(ConfigError) as exc:
        require_secrets(WebSettings())
    assert "COACH_WEB_PASSWORD_HASH" in str(exc.value)
    assert "COACH_WEB_SECRET" in str(exc.value)
    with pytest.raises(ConfigError, match="too short"):
        require_secrets(WebSettings(web_password_hash=FAST_HASH, web_secret="short"))
    with pytest.raises(ConfigError, match="not usable"):
        require_secrets(WebSettings(web_password_hash="hunter2", web_secret=SECRET))
    ok = require_secrets(WebSettings(web_password_hash=FAST_HASH, web_secret=SECRET))
    assert ok.secret == SECRET


def test_secrets_are_redacted_from_logs(no_env: None) -> None:
    require_secrets(WebSettings(web_password_hash=FAST_HASH, web_secret=SECRET))
    assert SECRET not in logs.redact(f"oops {SECRET}")
    assert FAST_HASH not in logs.redact(f"hash {FAST_HASH}")


def test_cli_refuses_to_start_unconfigured(no_env: None) -> None:
    result = CliRunner().invoke(get_app(), ["web", "run"])
    assert result.exit_code != 0
    assert "COACH_WEB_PASSWORD_HASH" in result.output


def test_dev_http_listens_on_loopback_only(no_env: None) -> None:
    assert bind_host(WebSettings()) == "0.0.0.0"
    assert bind_host(WebSettings(web_dev_http=True)) == "127.0.0.1"
    assert bind_host(WebSettings(web_dev_http=True), "::1") == "::1"
    with pytest.raises(ConfigError, match="loopback"):
        bind_host(WebSettings(web_dev_http=True, web_host="0.0.0.0"))
    with pytest.raises(ConfigError, match="loopback"):
        bind_host(WebSettings(web_dev_http=True), "192.168.1.5")


def test_the_app_needs_secrets_outside_the_demo(make_app: Any) -> None:
    with pytest.raises(ValueError):
        make_app(password_hash=None)
    with pytest.raises(ValueError):
        make_app(secret=None)


# -- the gate --------------------------------------------------------------------------------------


def test_every_route_is_known(make_app: Any) -> None:
    _, app = make_app()
    assert app_routes(app) == set(ROUTES), "classify new routes in ROUTES (public or not)"
    for path in webapp.PUBLIC_PATHS:
        assert ROUTES.get((path, "GET"))


def test_unauthenticated_requests_are_refused(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    for (path, method), public in ROUTES.items():
        if public:
            continue
        url = concrete(path)
        response = c.request(method, url, headers={"origin": "https://testserver"})
        if method == "GET":
            assert response.status_code == 303, url
            assert response.headers["location"].startswith("/login"), url
            assert c.head(url).status_code == 303
        else:
            assert response.status_code == 403, url
        assert "Riverside" not in response.text  # no data from the demo athlete


def test_unknown_paths_do_not_reveal_anything(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    for url in ("/admin", "/runs/abc", "/static", "/.env", "/api/runs"):
        response = c.get(url)
        assert response.status_code == 303, url  # same as a real page: to the login


def test_public_routes_need_no_session(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    assert c.get("/login").status_code == 200
    assert c.get("/healthz").text == "ok"
    robots = c.get("/robots.txt")
    assert robots.text == "User-agent: *\nDisallow: /\n"
    assert c.get("/static/thresy.css").status_code == 200
    assert c.get("/favicon.ico").status_code == 200


def test_the_login_redirects_back_to_the_page_asked_for(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    first = c.get("/trends")
    assert first.headers["location"] == "/login?next=/trends"
    page = c.get(first.headers["location"])
    token = form_token(page)
    action = re.search(r'<form method="post" action="([^"]+)"', page.text).group(1)
    response = c.post(
        action.replace("&amp;", "&"),
        data={"token": token, "password": PASSWORD},
        headers={"origin": "https://testserver"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/trends"


@pytest.mark.parametrize(
    "target", ["//evil.example/x", "https://evil.example/", "/\\evil", "/login"]
)
def test_no_open_redirect(make_app: Any, target: str) -> None:
    _, app = make_app()
    c = client(app)
    token = form_token(c.get("/login"))
    response = c.post(
        "/login",
        params={"next": target},
        data={"token": token, "password": PASSWORD},
        headers={"origin": "https://testserver"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# -- logging in ------------------------------------------------------------------------------------


def test_wrong_and_right_password(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    wrong = login(c, "not the password")
    assert wrong.status_code == 401
    assert "That password did not work." in wrong.text
    assert session_cookie(c) is None
    right = login(c)
    assert right.status_code == 303
    assert right.headers["location"] == "/"
    assert session_cookie(c)
    page = c.get("/")
    assert page.status_code == 200
    assert "Riverside Half Marathon" in page.text


def test_every_failed_login_looks_and_takes_the_same(make_app: Any) -> None:
    _, app = make_app()
    responses, durations = [], []
    for password in ("wrong", "", "x" * 2000, PASSWORD[:-1]):
        c = client(app)
        started = time.monotonic()
        response = login(c, password)
        durations.append(time.monotonic() - started)
        responses.append(response)
    assert {r.status_code for r in responses} == {401}
    bodies = {re.sub(r'name="token" value="[^"]+"', "", r.text) for r in responses}
    assert len(bodies) == 1
    assert min(durations) >= webapp.FAILED_LOGIN_FLOOR_S


def test_session_cookie_attributes(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    response = login(c)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{webapp.COOKIE_SECURE}=")
    for attribute in ("Path=/", "HttpOnly", "Secure", "SameSite=Lax", "Max-Age="):
        assert attribute in cookie
    assert "Domain" not in cookie


def test_dev_cookies_are_not_secure(make_app: Any) -> None:
    _, app = make_app(secure_cookies=False)
    c = client(app)
    cookie = login(c).headers["set-cookie"]
    assert cookie.startswith(f"{webapp.COOKIE_DEV}=")
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie


def test_sessions_expire(make_app: Any, clock: Any) -> None:
    _, app = make_app(session_hours=2)
    c = client(app)
    login(c)
    assert c.get("/").status_code == 200
    clock.advance(2 * 3600 - 5)
    assert c.get("/").status_code == 200
    clock.advance(10)
    expired = c.get("/races")
    assert expired.status_code == 303
    assert expired.headers["location"].startswith("/login")


def test_tampered_cookies_are_refused(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    login(c)
    good = session_cookie(c)
    body, mac = good.split(".")
    for bad in (f"{body}.{mac[:-2]}xx", f"{body[:-3]}AAA.{mac}", body, f"{good}.extra", ""):
        c.cookies.clear()
        c.cookies.set(webapp.COOKIE_SECURE, bad, domain="testserver")
        assert c.get("/").status_code == 303, bad


def test_changing_the_secret_ends_every_session(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    login(c)
    cookie = session_cookie(c)
    _, other = make_app(secret="another-" + SECRET)
    c2 = client(other)
    c2.cookies.set(webapp.COOKIE_SECURE, cookie, domain="testserver")
    assert c2.get("/").status_code == 303
    # and so does changing the password
    _, third = make_app(password_hash=hash_password("new password here", log_n=10))
    c3 = client(third)
    c3.cookies.set(webapp.COOKIE_SECURE, cookie, domain="testserver")
    assert c3.get("/").status_code == 303


def test_logout(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    login(c)
    cookie = session_cookie(c)
    page = c.get("/")
    token = re.search(r'name="token" value="([^"]+)"', page.text).group(1)
    # without the token, or from another site: refused
    assert c.post("/logout", headers={"origin": "https://testserver"}).status_code == 403
    assert (
        c.post(
            "/logout", data={"token": token}, headers={"sec-fetch-site": "cross-site"}
        ).status_code
        == 403
    )
    response = c.post("/logout", data={"token": token}, headers={"origin": "https://testserver"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?bye=1"
    assert "Max-Age=0" in response.headers["set-cookie"]
    # the old cookie no longer works, even if someone kept it
    c.cookies.set(webapp.COOKIE_SECURE, cookie, domain="testserver")
    assert c.get("/").status_code == 303


def test_login_needs_a_form_token_and_the_same_origin(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    token = form_token(c.get("/login"))
    no_token = c.post(
        "/login", data={"password": PASSWORD}, headers={"origin": "https://testserver"}
    )
    assert no_token.status_code == 400
    assert session_cookie(c) is None
    for headers in (
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
    ):
        response = c.post("/login", data={"token": token, "password": PASSWORD}, headers=headers)
        assert response.status_code == 403, headers
        assert session_cookie(c) is None
    ok = c.post(
        "/login",
        data={"token": token, "password": PASSWORD},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert ok.status_code == 303


def test_login_form_tokens_expire(make_app: Any, clock: Any) -> None:
    _, app = make_app()
    c = client(app)
    token = form_token(c.get("/login"))
    clock.advance(3 * 3600)
    response = c.post(
        "/login",
        data={"token": token, "password": PASSWORD},
        headers={"origin": "https://testserver"},
    )
    assert response.status_code == 400
    assert "expired" in response.text


def test_login_bodies_are_small_forms(make_app: Any) -> None:
    _, app = make_app()
    c = client(app)
    token = form_token(c.get("/login"))
    big = c.post(
        "/login",
        data={"token": token, "password": "x" * 10_000},
        headers={"origin": "https://testserver"},
    )
    assert big.status_code in (400, 403, 413)
    as_json = c.post(
        "/login",
        json={"token": token, "password": PASSWORD},
        headers={"origin": "https://testserver"},
    )
    assert as_json.status_code in (400, 403)
    assert session_cookie(c) is None


# -- rate limit through the app --------------------------------------------------------------------


def test_failed_logins_are_rate_limited(make_app: Any, clock: Any) -> None:
    _, app = make_app()
    c = client(app)
    for _ in range(5):
        assert login(c, "wrong").status_code == 401
    locked = login(c, "wrong")
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) >= 1
    assert "Too many attempts" in locked.text
    # while locked out, even the right password is not checked
    assert login(c).status_code == 429
    assert session_cookie(c) is None
    clock.advance(31)
    assert login(c).status_code == 303


def test_rotating_a_forwarded_header_does_not_help(make_app: Any) -> None:
    """The deployment trusts X-Forwarded-For; an attacker adds their own entries in front of
    the one the proxy appends, so every attempt still counts against the same client."""
    _, app = make_app(client_ip_header="x-forwarded-for")
    c = client(app)
    for n in range(6):
        response = login(c, "wrong", **{"x-forwarded-for": f"198.51.100.{n}, 203.0.113.7"})
    assert response.status_code == 429


def test_many_clients_meet_the_overall_limit(make_app: Any) -> None:
    """Even with a fresh address for every attempt (a header the proxy does not control, or
    a botnet), the overall limit stops the guessing."""
    _, app = make_app(client_ip_header="x-real-ip")
    c = client(app)
    codes = [
        login(c, "wrong", **{"x-real-ip": f"198.51.{n // 250}.{n % 250}"}).status_code
        for n in range(32)
    ]
    assert codes[:30] == [401] * 30
    assert 429 in codes[30:]


# -- headers and errors ----------------------------------------------------------------------------


def check_headers(response: Any, *, data: bool) -> None:
    for name, value in REQUIRED_HEADERS.items():
        assert response.headers.get(name) == value, (response.url, name)
    assert "server" not in response.headers
    if data:
        assert response.headers.get("cache-control") == "no-store", response.url


def test_headers_on_every_response(make_app: Any) -> None:
    _, app = make_app()
    anonymous = client(app)
    for url in ("/", "/runs/1", "/login", "/healthz", "/robots.txt", "/nothing", "/favicon.ico"):
        check_headers(anonymous.get(url), data=True)
    check_headers(anonymous.post("/logout"), data=True)
    check_headers(anonymous.put("/login"), data=True)
    static = anonymous.get("/static/thresy.css")
    check_headers(static, data=False)
    assert "public" in static.headers["cache-control"]
    c = client(app)
    check_headers(login(c, "wrong"), data=True)
    check_headers(login(c), data=True)
    for url in (
        "/",
        "/runs",
        "/runs/7000577",
        "/runs/1",
        "/trends",
        "/races",
        "/segments",
        "/data",
    ):
        response = c.get(url)
        assert response.status_code in (200, 404), url
        check_headers(response, data=True)
        assert response.headers["strict-transport-security"].startswith("max-age=")


def broken_source(exc: Exception) -> Any:
    @contextmanager
    def factory() -> Any:
        raise exc
        yield  # pragma: no cover

    return factory


def test_errors_reveal_nothing(make_app: Any) -> None:
    secret_text = "SELECT * FROM activities -- postgresql://coach_ro:hunter2@db/x"
    _, app = make_app(source=broken_source(RuntimeError(secret_text)))
    c = client(app, raise_errors=False)
    login(c)
    response = c.get("/")
    assert response.status_code == 500
    check_headers(response, data=True)
    for leak in ("SELECT", "hunter2", "Traceback", "RuntimeError", "coach_ro"):
        assert leak not in response.text


def test_database_down_is_a_calm_page(make_app: Any) -> None:
    _, app = make_app(
        source=broken_source(psycopg.OperationalError("connection refused to db:5432"))
    )
    c = client(app)
    login(c)
    response = c.get("/trends")
    assert response.status_code == 503
    assert "database cannot be reached" in response.text
    assert "5432" not in response.text


def test_pages_obey_the_content_security_policy(make_app: Any) -> None:
    """No inline script (data blocks only), no inline styles, no other origins."""
    _, app = make_app()
    c = client(app)
    login(c)
    for url in ("/", "/runs", "/runs/7000577", "/trends", "/races", "/segments", "/data", "/login"):
        html = c.get(url).text
        assert not re.search(r"\sstyle=", html), url
        assert not re.search(r"\son[a-z]+=", html), url
        for script in re.findall(r"<script\b[^>]*>", html):
            assert 'src="/static/' in script or 'type="application/json"' in script, (url, script)
        for ref in re.findall(r'(?:src|href|action)="([^"]+)"', html):
            assert ref.startswith(("/", "#")), (url, ref)


def test_demo_mode_needs_no_login(make_app: Any) -> None:
    _, app = make_app(demo=True, password_hash=None, secret=None, source=demo_source())
    c = client(app)
    page = c.get("/")
    assert page.status_code == 200
    assert "An invented athlete" in page.text
    check_headers(page, data=True)
