"""The web server: routes, the login, and the headers every response carries.

Default deny: a request needs a valid session unless its path is one of :data:`PUBLIC_PATHS`
(the login page, the health check, ``robots.txt``) or under ``/static/`` (the stylesheet,
scripts and icon, which hold no data). The gate sits in front of the router, so a route added
later is protected without doing anything; ``tests/web/test_security.py`` lists every route
and fails on one it does not know.

The service reads the database with the ``coach_ro`` role only, through a small pool.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from thresy.web import pages
from thresy.web.i18n import Lang
from thresy.web.render import STATIC, Renderer
from thresy.web.security import (
    Limiter,
    Session,
    Sessions,
    Signer,
    check_login_form_token,
    client_key,
    login_form_token,
    parse_password_hash,
    verify_password,
)
from thresy.web.source import DbSource, Source

log = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/login", "/healthz", "/robots.txt", "/favicon.ico"})
PUBLIC_PREFIXES = ("/static/",)
COOKIE_SECURE = "__Host-thresy_session"
COOKIE_DEV = "thresy_session"
MAX_FORM_BYTES = 4096
FAILED_LOGIN_FLOOR_S = 1.0
"""Every failed login takes at least this long, whatever failed."""

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; font-src 'self'; manifest-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = (
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-robots-tag", b"noindex, nofollow, noarchive"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (
        b"permissions-policy",
        b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()",
    ),
)
ROBOTS = "User-agent: *\nDisallow: /\n"


class SecurityHeaders:
    """Adds :data:`SECURITY_HEADERS` to every response (errors included) and
    ``Cache-Control: no-store`` unless the response set its own (only static assets do)."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {h for h, _ in SECURITY_HEADERS} and k.lower() != b"server"
                ]
                headers.extend(SECURITY_HEADERS)
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                if self.hsts:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class StaticAssets(StaticFiles):
    """Static files with a cache lifetime (they hold no data and carry a version tag)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["cache-control"] = "public, max-age=604800"
        return response


class Web:
    """The application state: settings, the renderer, auth, and where data comes from."""

    def __init__(
        self,
        *,
        lang: Lang,
        coach_name: str,
        source: Callable[[], Any],
        demo: bool = False,
        password_hash: str | None = None,
        secret: str | None = None,
        session_hours: float = 336,
        secure_cookies: bool = True,
        client_ip_header: str | None = None,
        client_ip_hops: int = 1,
        limiter: Limiter | None = None,
        clock: Callable[[], float] = time.time,
        on_startup: Callable[[], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
    ) -> None:
        self.lang = lang
        self.coach_name = coach_name
        self.source = source
        self.demo = demo
        self.renderer = Renderer(lang, demo=demo)
        self.clock = clock
        self.secure = secure_cookies
        self.cookie = COOKIE_SECURE if secure_cookies else COOKIE_DEV
        self.client_ip_header = client_ip_header
        self.client_ip_hops = client_ip_hops
        self.limiter = limiter or Limiter()
        self.on_startup = on_startup
        self.on_shutdown = on_shutdown
        self._verify_lock = threading.BoundedSemaphore(2)
        if demo:
            self.hash = None
            self.signer = Signer("demo-" + str(id(self)), "demo")
        else:
            if not password_hash or not secret:
                raise ValueError("a password hash and a secret are required outside the demo")
            self.hash = parse_password_hash(password_hash)
            self.signer = Signer(secret, password_hash)
        self.sessions = Sessions(self.signer, session_hours * 3600)

    # -- sessions -----------------------------------------------------------------------------

    def session_of(self, request: Request) -> Session | None:
        return self.sessions.read(request.cookies.get(self.cookie), self.clock())

    def set_cookie(self, response: Response, token: str) -> None:
        parts = [
            f"{self.cookie}={token}",
            "Path=/",
            f"Max-Age={int(self.sessions.lifetime_s)}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self.secure:
            parts.append("Secure")
        response.raw_headers.append((b"set-cookie", "; ".join(parts).encode("latin-1")))

    def clear_cookie(self, response: Response) -> None:
        parts = [f"{self.cookie}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
        if self.secure:
            parts.append("Secure")
        response.raw_headers.append((b"set-cookie", "; ".join(parts).encode("latin-1")))

    def client(self, request: Request) -> str:
        peer = request.client.host if request.client else None
        values = request.headers.getlist(self.client_ip_header) if self.client_ip_header else []
        return client_key(
            peer, values, trusted=bool(self.client_ip_header), hops=self.client_ip_hops
        )

    # -- rendering ----------------------------------------------------------------------------

    def html(
        self, template: str, context: dict[str, Any], *, status: int = 200, **extra: Any
    ) -> HTMLResponse:
        return HTMLResponse(self.renderer.render(template, context, **extra), status_code=status)

    def error(self, status: int, key: str) -> HTMLResponse:
        texts = {
            "not_found": ("not_found_title", "not_found_text"),
            "forbidden": ("forbidden_title", "forbidden_text"),
            "db": ("error_db_title", "error_db_text"),
            "error": ("error_title", "error_text"),
        }[key]
        heading, text = (self.lang.t(k) for k in texts)
        return self.html(
            "error.html",
            {"page": "error"},
            status=status,
            heading=heading,
            text=text,
            home=self.renderer.url("today"),
            title=heading,
        )

    @contextmanager
    def data(self) -> Iterator[Source]:
        with self.source() as source:
            yield source


def same_origin(request: Request) -> bool:
    """Whether a state-changing request comes from this site's own page.

    ``Sec-Fetch-Site`` (all current browsers) must say ``same-origin``; without it, an
    ``Origin`` header must name this host. A request with neither (an old browser, a script)
    is left to the form token, which a cross-site page cannot read."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site == "same-origin"
    origin = request.headers.get("origin")
    if origin is not None:
        host = urlsplit(origin).netloc.lower()
        return bool(host) and host == request.headers.get("host", "").lower()
    return True


async def read_form(request: Request) -> dict[str, str] | None:
    """A small ``application/x-www-form-urlencoded`` body, or ``None`` when it is too large
    or of another type."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=10)
    except (UnicodeDecodeError, ValueError):
        return None
    return {k: v[0] for k, v in parsed.items()}


def safe_next(value: str | None) -> str:
    """A local path to go to after the login (never another site)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(c in value for c in "\r\n\t") or value.startswith("/login"):
        return "/"
    return value


# -- the gate ----------------------------------------------------------------------------------


class AuthGate:
    """Refuses every non-public request without a valid session: a page request is sent to
    the login page, anything else gets 403."""

    def __init__(self, app: ASGIApp, web: Web) -> None:
        self.app = app
        self.web = web

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.web.demo or is_public(scope["path"]):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        session = self.web.session_of(request)
        if session is None:
            if scope["method"] in ("GET", "HEAD"):
                target = scope["path"] + (
                    "?" + scope["query_string"].decode("latin-1")
                    if scope.get("query_string")
                    else ""
                )
                location = "/login"
                if target != "/":
                    location += "?next=" + quote(safe_next(target), safe="/")
                response: Response = RedirectResponse(location, status_code=303)
                if request.cookies.get(self.web.cookie):
                    self.web.clear_cookie(response)
            else:
                response = self.web.error(403, "forbidden")
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["session"] = session
        await self.app(scope, receive, send)


# -- views -------------------------------------------------------------------------------------


def _session(request: Request) -> Session | None:
    return request.scope.get("state", {}).get("session")


def build_app(web: Web) -> ASGIApp:
    lang = web.lang

    async def page(
        request: Request, builder: Callable[[Source], dict[str, Any] | None], template: str
    ) -> Response:
        def work() -> str | None:
            with web.data() as source:
                context = builder(source)
            if context is None:
                return None
            session = _session(request)
            token = web.sessions.logout_token(session) if session else None
            return web.renderer.render(template, context, logout_token=token)

        try:
            html = await run_in_threadpool(work)
        except Exception as exc:
            if _is_db_error(exc):
                log.warning("database not reachable: %s", type(exc).__name__)
                return web.error(503, "db")
            raise
        if html is None:
            return web.error(404, "not_found")
        return HTMLResponse(html)

    async def overview(request: Request) -> Response:
        return await page(
            request,
            lambda s: pages.overview_page(s, lang, coach_name=web.coach_name),
            "overview.html",
        )

    async def runs(request: Request) -> Response:
        try:
            year = int(request.query_params["year"]) if "year" in request.query_params else None
        except ValueError:
            year = None
        return await page(request, lambda s: pages.runs_page(s, lang, year), "runs.html")

    async def run(request: Request) -> Response:
        activity_id = int(request.path_params["activity_id"])
        if activity_id <= 0 or activity_id > 2**63 - 1:
            return web.error(404, "not_found")
        return await page(request, lambda s: pages.run_page(s, lang, activity_id), "run.html")

    async def trends(request: Request) -> Response:
        return await page(request, lambda s: pages.trends_page(s, lang), "trends.html")

    async def races(request: Request) -> Response:
        return await page(request, lambda s: pages.races_page(s, lang), "races.html")

    async def segments(request: Request) -> Response:
        return await page(request, lambda s: pages.segments_page(s, lang), "segments.html")

    async def data(request: Request) -> Response:
        return await page(request, lambda s: pages.data_page(s, lang), "data.html")

    def login_page(
        request: Request, *, status: int = 200, message: str | None = None, error: bool = False
    ) -> Response:
        nxt = safe_next(request.query_params.get("next"))
        return web.html(
            "login.html",
            {"page": "login"},
            status=status,
            title=lang.t("login_title"),
            form_token=login_form_token(web.signer, web.clock()),
            next_path=nxt if nxt != "/" else None,
            message=message,
            message_error=error,
        )

    async def login_get(request: Request) -> Response:
        if not web.demo and web.session_of(request) is not None:
            return RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
        message = None
        if "bye" in request.query_params:
            message = lang.t("login_bye")
        elif "next" in request.query_params and request.cookies.get(web.cookie):
            message = lang.t("login_session_ended")
        return login_page(request, message=message)

    async def login_post(request: Request) -> Response:
        if web.demo:
            return RedirectResponse("/", status_code=303)
        started = time.monotonic()
        if not same_origin(request):
            return web.error(403, "forbidden")
        form = await read_form(request)
        if form is None:
            return web.error(400 if request.headers.get("content-length") else 403, "forbidden")
        if not check_login_form_token(web.signer, form.get("token"), web.clock()):
            return login_page(request, status=400, message=lang.t("login_expired"), error=True)
        client = web.client(request)
        wait = web.limiter.check(client)
        if wait > 0:
            log.warning(
                "login refused: too many failed attempts (client %s, %.0f s left)", client, wait
            )
            response = login_page(
                request,
                status=429,
                message=lang.t("login_locked", wait=lang.wait(wait)),
                error=True,
            )
            response.headers["retry-after"] = str(math.ceil(wait))
            return response
        password = form.get("password", "")[:1024]
        assert web.hash is not None

        def check() -> bool:
            with web._verify_lock:
                return verify_password(password, web.hash)  # type: ignore[arg-type]

        ok = await run_in_threadpool(check)
        if not ok:
            web.limiter.failure(client)
            log.warning("failed login (client %s)", client)
            elapsed = time.monotonic() - started
            if elapsed < FAILED_LOGIN_FLOOR_S:
                await asyncio.sleep(FAILED_LOGIN_FLOOR_S - elapsed)
            return login_page(request, status=401, message=lang.t("login_failed"), error=True)
        web.limiter.success(client)
        _, token = web.sessions.create(web.clock())
        response = RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
        web.set_cookie(response, token)
        log.info("login (client %s)", client)
        return response

    async def logout(request: Request) -> Response:
        session = _session(request)
        if web.demo or session is None:
            return RedirectResponse("/login", status_code=303)
        if not same_origin(request):
            return web.error(403, "forbidden")
        form = await read_form(request)
        if form is None or not web.sessions.check_logout_token(
            session, form.get("token"), web.clock()
        ):
            return web.error(403, "forbidden")
        web.sessions.revoke(session, web.clock())
        response = RedirectResponse("/login?bye=1", status_code=303)
        web.clear_cookie(response)
        return response

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def robots(request: Request) -> Response:
        return PlainTextResponse(ROBOTS)

    async def favicon(request: Request) -> Response:
        return Response((STATIC / "favicon.svg").read_bytes(), media_type="image/svg+xml")

    async def not_found(request: Request, exc: Exception) -> Response:
        return web.error(404, "not_found")

    async def http_error(request: Request, exc: Exception) -> Response:
        status = getattr(exc, "status_code", 500)
        if status == 404:
            return web.error(404, "not_found")
        if status in (400, 403, 405, 413):
            return web.error(status, "forbidden")
        return web.error(status, "error")

    async def server_error(request: Request, exc: Exception) -> Response:
        log.error("error while serving %s %s", request.method, request.url.path, exc_info=exc)
        return web.error(500, "error")

    routes = [
        Route("/", overview, methods=["GET"]),
        Route("/runs", runs, methods=["GET"]),
        Route("/runs/{activity_id:int}", run, methods=["GET"]),
        Route("/trends", trends, methods=["GET"]),
        Route("/races", races, methods=["GET"]),
        Route("/segments", segments, methods=["GET"]),
        Route("/data", data, methods=["GET"]),
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/robots.txt", robots, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Mount("/static", app=StaticAssets(directory=STATIC), name="static"),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        if web.on_startup:
            await run_in_threadpool(web.on_startup)
        try:
            yield
        finally:
            if web.on_shutdown:
                await run_in_threadpool(web.on_shutdown)

    app = Starlette(
        debug=False,
        routes=routes,
        exception_handlers={404: not_found, HTTPException: http_error, Exception: server_error},
        lifespan=lifespan,
    )
    app.state.web = web
    gated = AuthGate(app, web)
    outer = SecurityHeaders(gated, hsts=web.secure and not web.demo)
    outer.starlette = app  # type: ignore[attr-defined]  # the routes, for the tests
    return outer


def _is_db_error(exc: BaseException) -> bool:
    import psycopg

    try:
        from psycopg_pool import PoolTimeout
    except ImportError:  # pragma: no cover
        PoolTimeout = ()  # type: ignore[assignment]
    return isinstance(exc, psycopg.OperationalError | PoolTimeout)


# -- wiring ------------------------------------------------------------------------------------


def db_source_factory(
    url: str, zone: Any, *, now: Any = None
) -> tuple[Callable[[], Any], Callable[[], None], Callable[[], None]]:
    """A pool of read-only connections; returns (source context, open, close). ``now`` fixes
    the current time (development: the demo athlete loaded into a database)."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=4,
        open=False,
        timeout=10,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "connect_timeout": 10,
            "options": (
                "-c TimeZone=UTC -c default_transaction_read_only=on -c statement_timeout=20000"
            ),
        },
        name="thresy-web",
    )

    @contextmanager
    def source() -> Iterator[Source]:
        with pool.connection() as conn:
            yield DbSource(conn, zone, now=now)

    return source, lambda: pool.open(wait=False), pool.close


def demo_source_factory(scenario: str, lang: str) -> Callable[[], Any]:
    from thresy.web.demo import DemoSource

    demo = DemoSource(scenario, lang)

    @contextmanager
    def source() -> Iterator[Source]:
        yield demo

    return source
