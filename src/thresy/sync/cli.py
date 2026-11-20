"""``coach sync once|poll|backfill|login|request|status``.

Exit codes: 0 success; 1 failure; 3 "not finished yet" (a login waiting for its verification
code, or ``request --wait`` timing out while the request is still pending or running).
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
from collections.abc import Iterator
from datetime import date
from typing import Annotated, Any

import typer

from thresy.cli import JsonOption, emit, fail

app = typer.Typer(
    help="Garmin sync: one pass, the poller, history import, login and sync requests.",
    no_args_is_help=True,
)

NOT_FINISHED = 3


# Hooks (tests replace them) -------------------------------------------------------------------


def make_config() -> Any:
    from thresy.sync.config import SyncConfig

    return SyncConfig.load()


def make_provider(config: Any) -> Any:
    from thresy.sync.auth import ClientProvider

    return ClientProvider(config)


def make_login(config: Any) -> Any:
    from thresy.garmin.auth import GarminLogin, TokenStore

    return GarminLogin(TokenStore(config.token_dir))


@contextlib.contextmanager
def _database(json_out: bool) -> Iterator[Any]:
    import psycopg

    from thresy import db
    from thresy.config import ConfigError

    try:
        conn = db.connect()
    except (ConfigError, psycopg.OperationalError) as exc:
        raise fail(f"database: {exc}", as_json=json_out) from exc
    with conn:
        try:
            yield conn
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
            # An unmigrated (or partly migrated) database: one line instead of a traceback.
            detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            raise fail(
                f"database is not migrated ({detail}); run `coach db migrate`",
                as_json=json_out,
            ) from exc


@contextlib.contextmanager
def _stop_on_sigterm() -> Iterator[threading.Event]:
    stop = threading.Event()
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        yield stop
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _parse_date(value: str | None, name: str, json_out: bool) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise fail(f"{name} must be a date like 2030-01-31", as_json=json_out, code=2) from exc


def _describe_result(result: dict[str, Any]) -> str:
    new = result.get("new_activities") or []
    if new:
        activities = (
            f"{len(new)} new activit{'y' if len(new) == 1 else 'ies'} ({', '.join(map(str, new))})"
        )
    else:
        activities = "no new activities"
    night = "synced" if result.get("sleep_synced") else "not synced yet"
    return f"{activities}; sleep of {result.get('wake_date')} {night}"


# Commands -------------------------------------------------------------------------------------


@app.command()
def once(json_out: JsonOption = False) -> None:
    """Run one sync pass now (answers pending sync requests too).

    Refused while the poller runs: then use `coach sync request --wait 120`.
    """
    from thresy.sync import agent_requests
    from thresy.sync.poller import holding_lock
    from thresy.sync.state import utc_now

    config = make_config()
    with _database(json_out) as conn, holding_lock(conn) as locked:
        if not locked:
            raise fail(
                "the sync poller is running; ask it with `coach sync request --wait 120`",
                as_json=json_out,
            )
        with _stop_on_sigterm() as stop:
            outcome, _ids = agent_requests.sync_and_answer(
                conn,
                config,
                make_provider(config),
                now=utc_now(),
                handled_by=agent_requests.handler_name(),
                should_stop=stop.is_set,
            )
    result = outcome.result
    data = {
        "ok": outcome.ok,
        "error": outcome.error,
        "error_kind": outcome.error_kind,
        "result": result.request_result() if result else None,
        "summary": result.summary() if result else None,
        "warnings": result.warnings if result else [],
    }
    emit(data, as_json=json_out, text=result.summary() if result else f"error: {outcome.error}")
    if not outcome.ok:
        raise typer.Exit(1)


@app.command()
def poll() -> None:
    """Run the sync service until SIGTERM or SIGINT."""
    from thresy import db
    from thresy.sync.poller import Poller

    config = make_config()
    poller = Poller(config, connect=db.connect, provider=make_provider(config))
    raise typer.Exit(poller.run())


@app.command()
def backfill(
    since: Annotated[str, typer.Option("--since", help="First day (YYYY-MM-DD).")],
    until: Annotated[
        str | None, typer.Option("--until", help="Last day (default and maximum: yesterday).")
    ] = None,
    wellness: Annotated[bool, typer.Option("--wellness/--no-wellness")] = True,
    fit: Annotated[bool, typer.Option("--fit/--no-fit", help="Download FIT files.")] = True,
    restart: Annotated[
        bool, typer.Option("--restart", help="Start over instead of resuming.")
    ] = False,
    json_out: JsonOption = False,
) -> None:
    """Import history slowly and resumably; raises no events."""
    from thresy.garmin.errors import GarminAuthRequired, GarminError
    from thresy.sync import backfill as bf
    from thresy.sync.poller import holding_lock
    from thresy.sync.state import utc_now

    first = _parse_date(since, "--since", json_out)
    last = _parse_date(until, "--until", json_out)
    assert first is not None
    config = make_config()
    with _database(json_out) as conn, holding_lock(conn, bf.BACKFILL_LOCK) as locked:
        if not locked:
            raise fail("another backfill is running", as_json=json_out)
        now = utc_now()
        try:
            client = make_provider(config).get(conn, now)
            with _stop_on_sigterm() as stop:
                result = bf.backfill(
                    conn,
                    client,
                    config,
                    since=first,
                    until=last,
                    wellness=wellness,
                    fit=fit,
                    restart=restart,
                    now=now,
                    should_stop=stop.is_set,
                )
        except GarminAuthRequired as exc:
            raise fail(exc.reason, as_json=json_out) from exc
        except (GarminError, bf.BackfillStopped) as exc:
            raise fail(f"{exc} (progress is saved; run the same command to continue)",
                       as_json=json_out) from exc  # fmt: skip
        except KeyboardInterrupt as exc:
            raise fail("interrupted; progress is saved, run the same command to continue",
                       as_json=json_out) from exc  # fmt: skip
    state = "finished" if result.finished else "stopped (run the same command to continue)"
    text = (
        f"backfill {result.since.isoformat()}..{result.until.isoformat()} {state}: "
        f"{result.activities} activities ({result.new_activities} new), "
        f"{result.fit_stored} FIT files stored, {result.fit_failed} failed, "
        f"{result.wellness_days} days of wellness fetched, {result.wellness_skipped} complete "
        f"days skipped, {result.requests} requests"
    )
    emit(result, as_json=json_out, text=text)


@app.command()
def login(
    code: Annotated[
        str | None, typer.Option("--code", help="Verification code Garmin sent you.")
    ] = None,
    restart: Annotated[
        bool, typer.Option("--restart", help="Ignore the saved session and a waiting login.")
    ] = False,
    json_out: JsonOption = False,
) -> None:
    """Log in to Garmin and save the session (first set-up, or when the sync asks for it).

    On a terminal you are asked for missing credentials and the verification code. Without
    a terminal (e.g. `docker compose exec -T sync coach sync login`) the login waits for
    `coach sync login --code <code>`.
    """
    import psycopg

    from thresy import db
    from thresy.config import ConfigError
    from thresy.garmin.errors import GarminError, GarminMfaRequired
    from thresy.sync.auth import human_login
    from thresy.sync.state import utc_now

    config = make_config()
    interactive = sys.stdin.isatty() and not json_out
    email = password = None
    if interactive and code is None and not config.has_credentials:
        email = config.garmin_email or typer.prompt("Garmin email")
        password = config.garmin_password or typer.prompt("Garmin password", hide_input=True)

    def prompt_code(method: str | None) -> str:
        via = f" (sent by {method})" if method else ""
        return typer.prompt(f"Garmin verification code{via}")

    conn = None
    try:
        conn = db.connect()
    except (ConfigError, psycopg.OperationalError) as exc:
        typer.echo(f"note: login state is not recorded in the database ({exc})", err=True)
    try:
        outcome = human_login(
            config,
            conn=conn,
            now=utc_now(),
            code=code,
            restart=restart,
            email=email,
            password=password,
            prompt_code=prompt_code if interactive else None,
            login=make_login(config),
        )
    except GarminMfaRequired as exc:
        emit({"status": "mfa_required", "message": exc.reason}, as_json=json_out, text=exc.reason)
        raise typer.Exit(NOT_FINISHED) from exc
    except GarminError as exc:
        raise fail(f"Garmin login failed: {exc}", as_json=json_out) from exc
    finally:
        if conn is not None:
            conn.close()
    emit({"status": outcome.status, "message": outcome.message}, as_json=json_out,
         text=outcome.message)  # fmt: skip
    if outcome.status == "mfa_required":
        raise typer.Exit(NOT_FINISHED)


@app.command()
def request(
    wait: Annotated[
        float, typer.Option("--wait", metavar="SECONDS", help="Wait this long for the result.")
    ] = 0,
    json_out: JsonOption = False,
) -> None:
    """Ask the sync service for a pass now (works with the agent's database role).

    Prints the result: new activity ids, and whether last night's sleep has arrived.
    """
    from thresy.sync import agent_requests

    with _database(json_out) as conn:
        request_id = agent_requests.create_request(conn)
        row = (
            agent_requests.wait_for_request(conn, request_id, wait)
            if wait > 0
            else agent_requests.get_request(conn, request_id)
        )
    assert row is not None
    data = {
        "id": request_id,
        "status": row["status"],
        "result": row["result"],
        "error": row["error"],
    }
    if row["status"] == "done":
        emit(data, as_json=json_out, text=f"sync done: {_describe_result(row['result'] or {})}")
        return
    if row["status"] == "failed":
        emit(data, as_json=json_out, text=f"sync failed: {row['error']}")
        raise typer.Exit(1)
    if wait > 0:
        text = (
            f"sync request {request_id} is still {row['status']} after {wait:g} s "
            "(is the sync service running? see `coach sync status`)"
        )
        emit(data, as_json=json_out, text=text)
        raise typer.Exit(NOT_FINISHED)
    emit(data, as_json=json_out, text=f"sync request {request_id} queued")


@app.command()
def status(json_out: JsonOption = False) -> None:
    """Last successful sync, last error, Garmin login and token state, pending requests."""
    from thresy.sync.status import status_report, status_text

    config = make_config()
    with _database(json_out) as conn:
        report = status_report(conn, config)
    emit(report, as_json=json_out, text=status_text(report))
