"""``coach send …`` and ``coach sql``: commands the agent runs (merged into ``coach`` itself).

The help texts are written for the agent: it learns these tools from ``--help``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from thresy.cli import JsonOption, emit, fail

app = typer.Typer()


def _help(*paragraphs: str) -> str:
    """Help text: one line per paragraph, so the terminal wraps it cleanly. No square
    brackets: the help renderer would read them as markup."""
    return "\n\n".join(" ".join(p.split()) for p in paragraphs)


SEND_HELP = _help(
    "Send something to the athlete's chat IN ADDITION to your final answer.",
    """Your final answer is delivered to the athlete automatically when your run ends. Do not
    use `coach send` to repeat it, and never to announce that you are working on something
    ("I'll look into it"): every message should carry content.""",
    """Use it for what cannot be part of the final answer: an image (a plot you made), a file,
    or a question with buttons. Items are queued and delivered in the order you send them,
    before your final answer. If everything you have to say went out with `coach send`, end
    your run with the single word NO_REPLY and no separate final message is sent.""",
    """Text is Markdown: **bold**, _italic_, `code`, lists, links, code blocks; tables are
    converted for the chat. Files must be saved under $COACH_WORKSPACE_DIR/out/ first; other
    paths are refused.""",
)

send_app = typer.Typer(help=SEND_HELP, no_args_is_help=True)
app.add_typer(send_app, name="send")


def _read_text(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _queue(
    kind: str,
    *,
    json_out: bool,
    text: str | None = None,
    file: Path | None = None,
    options: list[str] | None = None,
) -> None:
    import psycopg

    from thresy import db
    from thresy.config import ConfigError, get_settings
    from thresy.tools import send

    try:
        settings = get_settings()
        run_id = send.run_id_from_env()
        checked_text = send.check_text(
            text, required=kind in ("text", "choice"), what="caption" if file else "text"
        )
        checked_file = (
            send.check_file(file, settings.workspace_dir.expanduser(), kind=kind)
            if file is not None
            else None
        )
        checked_options = send.check_options(options) if kind == "choice" else None
        with db.connect() as conn:
            queued = send.queue(
                conn,
                kind=kind,
                text=checked_text,
                file_path=checked_file,
                options=checked_options,
                run_id=run_id,
            )
    except send.SendError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    except ConfigError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    except psycopg.errors.InsufficientPrivilege as exc:
        raise fail(
            "this database role may not queue messages (permission denied on the outbox table)",
            as_json=json_out,
        ) from exc
    except psycopg.Error as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise fail(f"database error: {detail}", as_json=json_out) from exc
    emit(
        {"id": queued.id, "kind": queued.kind, "run_id": queued.run_id, "file": queued.file_path},
        as_json=json_out,
        text=f"queued {kind} #{queued.id}; it is delivered in order, before your final answer",
    )


@send_app.command(
    "text",
    help=_help(
        "Send an extra text message (Markdown).",
        """Only for a message that must arrive separately from your final answer, for example
        before a plot. Long texts are split automatically.""",
    ),
)
def send_text(
    message: Annotated[str, typer.Argument(help="Markdown text, or - to read it from stdin.")],
    json_out: JsonOption = False,
) -> None:
    _queue("text", text=_read_text(message), json_out=json_out)


@send_app.command(
    "photo",
    help=_help(
        "Send an image (PNG or JPEG, at most 10 MB), e.g. a plot.",
        """Save it under $COACH_WORKSPACE_DIR/out/ first (for example out/hr-zones.png). A short
        caption goes with the image; longer text follows as a separate message.""",
    ),
)
def send_photo(
    path: Annotated[Path, typer.Argument(help="Image under $COACH_WORKSPACE_DIR/out/.")],
    caption: Annotated[
        str | None, typer.Option("--caption", "-c", help="Markdown caption (- reads stdin).")
    ] = None,
    json_out: JsonOption = False,
) -> None:
    _queue(
        "photo",
        file=path,
        text=_read_text(caption) if caption is not None else None,
        json_out=json_out,
    )


@send_app.command(
    "document",
    help=_help(
        "Send a file (GPX, CSV, PDF, ...; at most 50 MB) from $COACH_WORKSPACE_DIR/out/.",
    ),
)
def send_document(
    path: Annotated[Path, typer.Argument(help="File under $COACH_WORKSPACE_DIR/out/.")],
    caption: Annotated[
        str | None, typer.Option("--caption", "-c", help="Markdown caption (- reads stdin).")
    ] = None,
    json_out: JsonOption = False,
) -> None:
    _queue(
        "document",
        file=path,
        text=_read_text(caption) if caption is not None else None,
        json_out=json_out,
    )


@send_app.command(
    "choice",
    help=_help(
        "Ask a question with buttons.",
        'Example: coach send choice "Tomorrow?" -o "Easy 8 km" -o "Intervals" -o "Rest"',
        """The athlete's press comes back to you in a later run as a message saying which button
        was pressed for which question. Asking is a fine way to end a run: if the question is
        all you have to say, finish with NO_REPLY.""",
    ),
)
def send_choice(
    question: Annotated[str, typer.Argument(help="The question (Markdown), or - for stdin.")],
    option: Annotated[
        list[str],
        typer.Option(
            "--option", "-o", help="A button label; repeat for each option (1 to 10, short)."
        ),
    ],
    json_out: JsonOption = False,
) -> None:
    _queue("choice", text=_read_text(question), options=option, json_out=json_out)


SQL_HELP = _help(
    "Run one read-only SQL query against the coach database and print the result.",
    """Uses the read-only role (COACH_DATABASE_URL_RO): SELECT only, read-only session,
    statement timeout.""",
    """Main tables: activities (and the view runs, which adds distance_km, pace_s_per_km and
    pace), activity_laps, activity_records (per-second samples: many rows, aggregate them),
    wellness_daily (one row per wake_date: sleep, HRV, resting HR, steps), hrv_readings,
    sleep_levels, personal_records, planned_workouts, races, segments, segment_efforts,
    messages (the chat log), agent_runs.""",
    """Timestamps are UTC (timestamptz); columns ending in _local are local time. Last night is
    the wellness_daily row whose wake_date is today's local date.""",
    """Only the first --limit rows are printed, with a note when there were more; aggregate in
    SQL instead of fetching raw samples. --json prints columns, rows and a truncated flag.""",
)


@app.command("sql", help=SQL_HELP)
def sql_command(
    query: Annotated[str, typer.Argument(help="The SQL query, or - to read it from stdin.")],
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, max=10_000, help="Maximum rows to print.")
    ] = 200,
    timeout: Annotated[
        float, typer.Option("--timeout", min=1, max=600, help="Statement timeout in seconds.")
    ] = 30,
    json_out: JsonOption = False,
) -> None:
    import psycopg

    from thresy import db
    from thresy.config import ConfigError, get_settings
    from thresy.tools import sql

    text = _read_text(query)
    try:
        url = get_settings().require_database_url(read_only=True)
        with db.connect_ro(url, statement_timeout_s=timeout) as conn:
            result = sql.run_query(conn, text, limit=limit)
    except ConfigError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    except ValueError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    except psycopg.errors.QueryCanceled as exc:
        raise fail(
            f"query cancelled after {timeout:g} s (statement timeout); make it cheaper",
            as_json=json_out,
        ) from exc
    except psycopg.Error as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise fail(detail, as_json=json_out) from exc
    emit(sql.as_json(result), as_json=json_out, text=sql.as_text(result))
