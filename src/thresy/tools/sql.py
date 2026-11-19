"""``coach sql``: run one read-only query and format the result.

The query runs on the read-only connection (``COACH_DATABASE_URL_RO``, role ``coach_ro``, a
read-only session with a statement timeout). Rows are fetched through a server-side cursor
where possible, so a query over millions of samples does not load them all into memory; only
``limit + 1`` rows are fetched to detect truncation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import psycopg

MAX_CELL = 80


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    limit: int = 0
    status: str | None = None
    """Command status for statements without a result set (e.g. ``SET``)."""


def _clean(query: str) -> str:
    query = query.strip()
    while query.endswith(";"):
        query = query[:-1].rstrip()
    return query


def run_query(conn: psycopg.Connection[Any], query: str, *, limit: int) -> QueryResult:
    """Run ``query`` and return at most ``limit`` rows (plus whether there were more)."""
    query = _clean(query)
    if not query:
        raise ValueError("empty query")
    try:
        with conn.transaction(), conn.cursor(name="coach_sql") as cur:
            cur.execute(query)  # DECLARE ... CURSOR FOR <query>
            rows = cur.fetchmany(limit + 1)
            columns = [d.name for d in cur.description or []]
        return _result(columns, rows, limit)
    except (psycopg.errors.SyntaxError, psycopg.errors.FeatureNotSupported):
        pass  # not a cursor-able statement (EXPLAIN, SHOW, ...): run it plainly
    with conn.cursor() as cur:
        cur.execute(query)
        if cur.description is None:
            return QueryResult(status=cur.statusmessage, limit=limit)
        rows = cur.fetchmany(limit + 1)
        columns = [d.name for d in cur.description]
    return _result(columns, rows, limit)


def _result(columns: list[str], rows: list[Any], limit: int) -> QueryResult:
    truncated = len(rows) > limit
    rows = rows[:limit]
    dict_rows = [
        dict(r) if isinstance(r, dict) else dict(zip(columns, r, strict=False)) for r in rows
    ]
    return QueryResult(columns=columns, rows=dict_rows, truncated=truncated, limit=limit)


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, bytes | memoryview):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value
    return str(value)


def as_json(result: QueryResult) -> dict[str, Any]:
    if result.status is not None:
        return {"status": result.status}
    return {
        "columns": result.columns,
        "rows": [{k: json_value(v) for k, v in row.items()} for row in result.rows],
        "row_count": len(result.rows),
        "truncated": result.truncated,
    }


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = f"{value:.6g}" if abs(value) < 1e15 else str(value)
    elif isinstance(value, dict | list):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    elif isinstance(value, datetime):
        text = value.isoformat(sep=" ", timespec="seconds")
    else:
        text = str(json_value(value))
    text = " ".join(text.split())
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def as_text(result: QueryResult) -> str:
    if result.status is not None:
        return result.status
    if not result.columns:
        return "(no columns)"
    cells = [[_cell(row.get(c)) for c in result.columns] for row in result.rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(result.columns)]
    lines = [" | ".join(c.ljust(w) for c, w in zip(result.columns, widths, strict=True))]
    lines.append("-+-".join("-" * w for w in widths))
    lines += [" | ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in cells]
    lines = [line.rstrip() for line in lines]
    n = len(result.rows)
    if result.truncated:
        lines.append(
            f"(showing the first {n} rows; the query returned more. Aggregate in SQL, add a"
            f" WHERE or LIMIT, or pass --limit)"
        )
    else:
        lines.append(f"({n} row{'' if n == 1 else 's'})")
    return "\n".join(lines)
