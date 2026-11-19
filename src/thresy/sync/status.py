"""``coach sync status``: when the last sync ran, what failed, and the Garmin login state.

Reads only (``sync_state``, ``wellness_daily``, ``agent_requests``, ``pg_locks``), so it also
works with the agent's ``coach_agent`` role. The token directory exists only in the sync
service; elsewhere the token fields say ``null``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from thresy.garmin.auth import TokenStore
from thresy.store import Conn
from thresy.sync import agent_requests, state
from thresy.sync.config import SyncConfig
from thresy.sync.passes import local_today
from thresy.sync.poller import POLLER_LOCK, lock_held


def _token_info(config: SyncConfig, now: datetime) -> dict[str, Any]:
    store = TokenStore(config.token_dir)
    if not store.directory.is_dir():
        return {"present": None, "modified_at": None, "waiting_for_code": None}
    pending = store.load_pending_mfa()
    return {
        "present": store.has_token(),
        "modified_at": state.iso(store.token_modified_at()),
        "waiting_for_code": bool(pending and not pending.expired(now)),
    }


def status_report(conn: Conn, config: SyncConfig, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or state.utc_now()
    status = state.get(conn, state.STATUS)
    auth = state.get(conn, state.AUTH)
    today = local_today(now, config)
    night = conn.execute(
        "SELECT sleep_synced_at FROM wellness_daily WHERE wake_date = %s", (today,)
    ).fetchone()
    until = state.rate_limited_until(conn)
    return {
        "poller_running": lock_held(conn, POLLER_LOCK),
        "last_success_at": status.get("last_success_at"),
        "last_attempt_at": status.get("last_attempt_at"),
        "last_error": status.get("last_error"),
        "last_error_kind": status.get("last_error_kind"),
        "last_error_at": status.get("last_error_at"),
        "consecutive_failures": int(status.get("consecutive_failures") or 0),
        "last_result": status.get("last_result"),
        "rate_limited_until": state.iso(until) if until and until > now else None,
        "garmin_auth": {
            "status": auth.get("status"),
            "reason": auth.get("reason"),
            "updated_at": auth.get("updated_at"),
            "last_ok_at": auth.get("last_ok_at"),
            "password_login_paused_until": auth.get("password_login_paused_until"),
        },
        "token": _token_info(config, now),
        "requests": agent_requests.counts(conn),
        "today": {
            "wake_date": today.isoformat(),
            "sleep_synced_at": state.iso(night["sleep_synced_at"]) if night else None,
        },
    }


def status_text(report: dict[str, Any]) -> str:
    auth = report["garmin_auth"]
    token = report["token"]
    lines = [
        f"poller running:     {'yes' if report['poller_running'] else 'no'}",
        f"last success:       {report['last_success_at'] or 'never'}",
        f"last attempt:       {report['last_attempt_at'] or 'never'}",
    ]
    if report["consecutive_failures"]:
        lines.append(
            f"last error:         {report['last_error']} ({report['last_error_kind']}, "
            f"{report['last_error_at']}; {report['consecutive_failures']} in a row)"
        )
    if report["rate_limited_until"]:
        lines.append(f"rate limited until: {report['rate_limited_until']}")
    lines.append(f"Garmin login:       {auth['status'] or 'unknown'}")
    if auth["reason"]:
        lines.append(f"                    {auth['reason']}")
    if token["present"] is None:
        lines.append("token:              not visible from here (it lives in the sync service)")
    else:
        saved = f"saved {token['modified_at']}" if token["present"] else "missing"
        waiting = "; a login waits for a verification code" if token["waiting_for_code"] else ""
        lines.append(f"token:              {saved}{waiting}")
    requests = report["requests"]
    lines.append(
        f"requests:           {requests['pending']} pending, {requests['running']} running"
    )
    today = report["today"]
    synced = today["sleep_synced_at"] or "not yet"
    lines.append(f"sleep of {today['wake_date']}: {synced}")
    return "\n".join(lines)
