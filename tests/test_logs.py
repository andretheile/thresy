"""Keeping secrets out of the logs: the redaction, the filter, and the logging set-up."""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import threading

import pytest

from thresy import logs

from .logcapture import isolated_secrets, production_logging

# Obviously fake values, shaped like the real ones.
BOT_TOKEN = "987654321:FAKE-token-for-tests-only-000000000"
DB_PASSWORD = "fake-db-pw-0000"
DB_URL = f"postgresql://coach_app:{DB_PASSWORD}@db:5432/thresy"


@pytest.fixture(autouse=True)
def _no_registered_secrets():
    with isolated_secrets():
        yield


# -- redact: shapes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"POST https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", BOT_TOKEN),
        (f"token {BOT_TOKEN.replace(':', '%3A')}", "FAKE-token-for-tests-only"),
        (f"connect {DB_URL} failed", DB_PASSWORD),
        (f'missing "=" after "postgresql//coach_app:{DB_PASSWORD}@db/thresy"', DB_PASSWORD),
        ("postgresql://coach_app:p@ss-w0rd@db/thresy", "ss-w0rd"),
        ("git clone https://ghp-fake-0000000000@example.com/x", "ghp-fake"),
        ("Max retries exceeded with url: /app?ticket=ST-0000-fake-cas (Caused by", "ST-0000"),
        ("GET /oauth?client_id=x&access_token=fake-at-0000&b=1", "fake-at-0000"),
        ("Authorization: Bearer fake.bearer-0000000", "fake.bearer"),
        ("di token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.ZmFrZXNpZw", "eyJ"),
        ("auth failed for sk-ant-oat01-FAKE0000000000000000000", "FAKE0000"),
        ("host=db user=coach_app password=fake-kv-0000 dbname=thresy", "fake-kv-0000"),
        ('{"username": "a", "password": "fake-json-0000"}', "fake-json-0000"),
        ("COACH_TELEGRAM_TOKEN=abc COACH_GARMIN_PASSWORD=fake-env-0000", "fake-env-0000"),
        ("COACH_TELEGRAM_TOKEN=abc", "=abc"),
    ],
)
def test_redact_removes_credential_shapes(text: str, secret: str) -> None:
    clean = logs.redact(text)
    assert secret not in clean
    assert logs.MASK in clean


@pytest.mark.parametrize(
    "text",
    [
        'FATAL:  password authentication failed for user "coach_app"',
        "Check COACH_GARMIN_EMAIL and COACH_GARMIN_PASSWORD, then run `coach sync login`.",
        "InvalidToken: Unauthorized",
        "connection to server at http://127.0.0.1:8080/x refused; see https://example.com/a@b",
        "sync pass done: 3 activities, sleep of 2030-01-02 at 12:30",
        "run 12 (user_message) for chat 100: fresh session, tier smart, effort high",
    ],
)
def test_redact_leaves_ordinary_text_alone(text: str) -> None:
    assert logs.redact(text) == text


def test_redact_keeps_the_user_name_of_a_url() -> None:
    assert logs.redact(DB_URL) == "postgresql://coach_app:<redacted>@db:5432/thresy"


# -- known values -----------------------------------------------------------------------------


def test_registered_values_and_their_encoded_forms_are_redacted() -> None:
    secret = "fake/pw:with-chars"
    logs.register_secret(secret, None, "")
    encoded = secret.replace("/", "%2F").replace(":", "%3A")
    clean = logs.redact(f"a {secret} b {encoded} c")
    assert secret not in clean and encoded not in clean
    assert clean == "a <redacted> b <redacted> c"


def test_short_values_are_not_replaced_literally() -> None:
    logs.register_secret("abc")
    assert logs.redact("abcdef abc") == "abcdef abc"


def test_secrets_from_environ() -> None:
    environ = {
        "COACH_TELEGRAM_TOKEN": BOT_TOKEN,
        "COACH_GARMIN_PASSWORD": "fake-garmin-0000",
        "CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth-0000",
        "CODEX_API_KEY": "fake-codex-0000",
        "COACH_DATABASE_URL": DB_URL,
        # a development set-up: the password is the user name, nothing to hide
        "COACH_DATABASE_URL_RO": "postgresql://coach_ro:coach_ro@127.0.0.1:55432/thresy",
        "COACH_OTHER": "host=db password='fake kv 0000'",
        "COACH_GARMIN_TOKEN_DIR": "/garmin",
        "COACH_BACKUP_URL": "gs://bucket/prefix",
        "COACH_TZ": "UTC",
        "EMPTY_TOKEN": "",
    }
    assert logs.secrets_from_environ(environ) == {
        BOT_TOKEN,
        "fake-garmin-0000",
        "fake-oauth-0000",
        "fake-codex-0000",
        DB_PASSWORD,
        DB_URL,
        "fake kv 0000",
    }


# -- the filter -------------------------------------------------------------------------------


def handler_with_filter() -> tuple[logging.Handler, io.StringIO]:
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter(logs.FORMAT))
    handler.addFilter(logs.RedactingFilter())
    return handler, out


def test_filter_redacts_message_arguments_and_traceback() -> None:
    secret = "fake-known-secret-0000"
    logs.register_secret(secret)
    handler, out = handler_with_filter()
    log = logging.getLogger("tests.logs.filter")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    try:
        log.warning("request to %s failed", f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
        try:
            try:
                raise ConnectionError(f"cause: {DB_URL}")
            except ConnectionError as exc:
                raise RuntimeError(f"wrapped {secret}") from exc
        except RuntimeError:
            log.exception("it failed")
        log.info("stack %s", secret, stack_info=True)
        log.info("bad %s %s", "arguments")  # logging would print the arguments as they are
    finally:
        log.removeHandler(handler)
        log.setLevel(logging.NOTSET)
        log.propagate = True
    text = out.getvalue()
    for leaked in (BOT_TOKEN, DB_PASSWORD, secret):
        assert leaked not in text
    assert "RuntimeError: wrapped <redacted>" in text
    assert "ConnectionError: cause: postgresql://coach_app:<redacted>@db" in text
    assert "Traceback (most recent call last)" in text and "Stack (most recent call last)" in text
    assert "could not be formatted" in text


def test_filter_leaves_the_record_itself_unchanged() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s", (BOT_TOKEN,), None)
    clean = logs.RedactingFilter().filter(record)
    assert clean is not record
    assert clean.getMessage() == "token <redacted>"
    assert record.getMessage() == f"token {BOT_TOKEN}"


# -- configure --------------------------------------------------------------------------------


def test_configure_installs_a_redacting_handler_and_quiets_http_clients(capsys) -> None:
    with production_logging(verbose=True, environ={"COACH_TELEGRAM_TOKEN": BOT_TOKEN}):
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        logging.getLogger("thresy.test").info("token is %s", BOT_TOKEN)
        logging.getLogger("httpx").info("HTTP Request: POST https://example.invalid/x")
        logging.getLogger("httpcore.http11").debug("send_request_headers.started")
        logging.getLogger("urllib3.connectionpool").debug('"GET /app?ticket=x HTTP/1.1" 302')
        logging.getLogger("telegram.ext.ExtBot").debug("Set Bot API URL: …")
        logging.getLogger("httpx").warning("a warning from httpx")
        logs.configure(verbose=True, environ={})  # idempotent: no second handler
        assert len(root.handlers) == 1
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "INFO thresy.test: token is <redacted>",
        "WARNING httpx: a warning from httpx",
    ]


def test_loggers_that_do_not_propagate_go_through_the_last_resort_redacted(capsys) -> None:
    with production_logging():
        lonely = logging.getLogger("tests.logs.lonely")
        lonely.propagate = False  # like google-auth's "google" logger
        try:
            lonely.warning("lonely %s", DB_URL)
        finally:
            lonely.propagate = True
    err = capsys.readouterr().err
    assert DB_PASSWORD not in err and "coach_app:<redacted>@db" in err


def test_uncaught_exceptions_are_printed_redacted(capsys) -> None:
    try:
        raise ValueError(f"cannot connect to {DB_URL}")
    except ValueError:
        logs.excepthook(*sys.exc_info())
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert DB_PASSWORD not in err and "ValueError: cannot connect to" in err

    def boom() -> None:
        raise RuntimeError(f"thread saw {BOT_TOKEN}")

    try:
        boom()
    except RuntimeError as exc:
        args = threading.ExceptHookArgs(
            (type(exc), exc, exc.__traceback__, threading.current_thread())
        )
    logs.thread_excepthook(args)
    err = capsys.readouterr().err
    assert err.startswith("Exception in thread ") and "RuntimeError: thread saw" in err
    assert BOT_TOKEN not in err


def test_cli_errors_are_redacted(capsys) -> None:
    from thresy.cli import fail

    fail(f"cannot connect: {DB_URL}")
    assert capsys.readouterr().err == (
        "error: cannot connect: postgresql://coach_app:<redacted>@db:5432/thresy\n"
    )


# -- services as whole processes ---------------------------------------------------------------
#
# libpq quotes a connection string it cannot parse, password included, in its error. No
# connection is made: the URL fails to parse before that.

MALFORMED_URLS = [
    f"postgresql//coach_app:{DB_PASSWORD}@127.0.0.1:1/thresy",  # "://" mistyped
    f"postgresql://coach_app:{DB_PASSWORD}%zz@127.0.0.1:1/thresy",  # a bad "%" escape
]


def run_coach(*args: str, environ: dict[str, str], cwd) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COACH_")}
    env.update(environ)
    return subprocess.run(
        [sys.executable, "-c", "from thresy.cli import main; main()", *args],
        env=env,
        cwd=cwd,  # no .env file here
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("url", MALFORMED_URLS)
def test_migrate_never_prints_the_database_password(url, tmp_path) -> None:
    proc = run_coach("db", "migrate", environ={"COACH_DATABASE_URL": url}, cwd=tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "Traceback (most recent call last)" in output  # the uncaught error, as Docker logs it
    assert "ProgrammingError" in output and "<redacted>" in output
    assert DB_PASSWORD not in output


@pytest.mark.parametrize("url", MALFORMED_URLS)
def test_backup_never_prints_the_database_password(url, tmp_path) -> None:
    environ = {
        "COACH_DATABASE_URL": url,
        "COACH_BACKUP_URL": (tmp_path / "target").as_uri(),
        "COACH_DATA_DIR": str(tmp_path / "data"),
        "COACH_WORKSPACE_DIR": str(tmp_path / "workspace"),
    }
    proc = run_coach("backup", "run", environ=environ, cwd=tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1
    assert "could not record the backup failure" in output  # logged with its traceback
    assert "error: backup failed: ProgrammingError" in output and "<redacted>" in output
    assert DB_PASSWORD not in output


def test_configure_replaces_only_default_exception_hooks(monkeypatch) -> None:
    def custom(*args) -> None:
        pass

    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.setattr(threading, "excepthook", custom)
    with production_logging():
        assert sys.excepthook is logs.excepthook
        assert threading.excepthook is custom  # pytest's own hook stays
