"""``coach web``: the dashboard server, its password, and the demo.

Imports of the web stack happen inside the commands, so ``coach`` itself stays quick.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from thresy.cli import fail

app = typer.Typer(help="The web dashboard (docs/web.md).", no_args_is_help=True)

DemoScenario = Annotated[
    str,
    typer.Option(
        "--scenario",
        help="full: two years of data; new: an athlete who just started; trouble: full with"
        " an expired Garmin login and failing backups.",
    ),
]


@app.command("run")
def run(
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Serve the synthetic demo athlete; no database, no login."),
    ] = False,
    scenario: DemoScenario = "full",
    host: Annotated[str | None, typer.Option(help="Address to listen on (COACH_WEB_HOST).")] = None,
    port: Annotated[int | None, typer.Option(help="Port (COACH_WEB_PORT).")] = None,
    now: Annotated[
        str | None,
        typer.Option(
            hidden=True,
            help="Development: pretend it is this UTC time (ISO), e.g. for the demo athlete"
            " loaded into a database.",
        ),
    ] = None,
) -> None:
    """Serve the dashboard (read-only database role, behind a login)."""
    from datetime import UTC, datetime

    import uvicorn

    from thresy.config import ConfigError, get_settings
    from thresy.web.app import Web, build_app, db_source_factory, demo_source_factory
    from thresy.web.i18n import Lang
    from thresy.web.settings import LOOPBACK, WebSettings, bind_host, require_secrets

    try:
        core = get_settings()
        ws = WebSettings()
        lang = Lang.get(core.language)
        if demo:
            web = Web(
                lang=lang,
                coach_name=core.name,
                source=demo_source_factory(scenario, lang.code),
                demo=True,
                secure_cookies=False,
            )
            bind = host or LOOPBACK
        else:
            secrets = require_secrets(ws)
            fixed = datetime.fromisoformat(now).replace(tzinfo=UTC) if now else None
            source, open_pool, close_pool = db_source_factory(
                core.require_database_url(read_only=True), core.zone, now=fixed
            )
            web = Web(
                lang=lang,
                coach_name=core.name,
                source=source,
                password_hash=secrets.password_hash,
                secret=secrets.secret,
                session_hours=ws.web_session_hours,
                secure_cookies=not ws.web_dev_http,
                client_ip_header=ws.web_client_ip_header,
                client_ip_hops=ws.web_client_ip_hops,
                on_startup=open_pool,
                on_shutdown=close_pool,
            )
            bind = bind_host(ws, host)
    except (ConfigError, ValueError) as exc:
        raise fail(str(exc)) from exc
    config = uvicorn.Config(
        build_app(web),
        host=bind,
        port=port or ws.web_port,
        log_config=None,  # logging is configured once by `coach` (thresy.logs)
        access_log=False,
        server_header=False,
        proxy_headers=False,  # the client address is chosen by the app (COACH_WEB_CLIENT_IP_*)
        timeout_keep_alive=5,
        limit_concurrency=100,
        h11_max_incomplete_event_size=16 * 1024,
    )
    what = "the demo" if demo else "the dashboard"
    typer.echo(f"serving {what} on http://{bind}:{config.port}", err=True)
    uvicorn.Server(config).run()


@app.command("password")
def password(
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Hash a password read from standard input instead.")
    ] = False,
) -> None:
    """Create a password for the dashboard and print the hash to configure.

    The password is printed once; keep it in a password manager. Only the hash goes into
    .env (COACH_WEB_PASSWORD_HASH)."""
    from thresy.web.security import generate_password, hash_password

    if stdin:
        value = sys.stdin.readline().rstrip("\r\n")
        if len(value) < 12:
            raise fail("the password is shorter than 12 characters; choose a longer one")
        typer.echo(f"COACH_WEB_PASSWORD_HASH={hash_password(value)}")
        return
    value = generate_password()
    typer.echo("Password (shown only now; keep it in your password manager):\n")
    typer.echo(f"    {value}\n")
    typer.echo("Put this line into .env, then restart the web service:\n")
    typer.echo(f"COACH_WEB_PASSWORD_HASH={hash_password(value)}")


@app.command("demo")
def demo(
    out: Annotated[
        Path,
        typer.Option("--out", help="Directory for the pages (or the file with --single-file)."),
    ] = Path("thresy-demo"),
    single_file: Annotated[
        bool,
        typer.Option(
            "--single-file", help="Write only the overview, as one self-contained HTML file."
        ),
    ] = False,
    scenario: DemoScenario = "full",
    language: Annotated[
        str | None,
        typer.Option(help="en or de (default: COACH_LANGUAGE; --single-file: en)."),
    ] = None,
) -> None:
    """Render the dashboard of a synthetic athlete as static files (no database needed)."""
    from thresy.config import get_settings
    from thresy.web.export import export_pages, export_single

    code = language or ("en" if single_file else get_settings().language)
    try:
        if single_file:
            target = out / "thresy-demo.html" if out.is_dir() else out
            export_single(target, lang=code, scenario=scenario)
            typer.echo(f"wrote {target}")
        else:
            files = export_pages(out, lang=code, scenario=scenario)
            typer.echo(f"wrote {len(files)} files to {out} (open {out / 'index.html'})")
    except ValueError as exc:
        raise fail(str(exc)) from exc
