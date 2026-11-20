"""When the agent's model credential expires, and telling the athlete in good time.

The date comes from one of two places:

* the runner, when the harness reports it with a run (``RunResult.credential_expires_at``;
  Claude Code does for a stored claude.ai login). The bridge remembers the latest reported date
  in ``sync_state`` (``bridge:credential``); a successful run that reports none means the
  credential in use has no known date, and the remembered one is dropped.
* ``COACH_BRIDGE_CREDENTIAL_EXPIRES``, set by the operator for credentials whose harness does
  not report a date (a Claude Code token from ``claude setup-token`` is valid for a year).

A date reported by the runner wins over the setting. From ``COACH_BRIDGE_CREDENTIAL_WARN_DAYS``
before the end, the athlete gets a plain notice in the chat (no agent run): the first one at
once, then at most one every ``REPEAT`` and one more within ``FINAL`` of the end. Notices wait
for the end of quiet hours like the other notifications; when the last one went out is kept in
``bridge:notified:credential``, so a restart repeats nothing. After the end nothing more is
sent: runs then fail, and the failure message says that the login has expired.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from thresy.runner import RunResult

from .clock import Clock
from .repo import Repo
from .settings import BridgeSettings, parse_expiry
from .texts import t

log = logging.getLogger(__name__)

STATE = "credential"
NOTIFIED_STATE = "notified:credential"
REPEAT = timedelta(days=7)
"""At most one notice in this time while the end is near..."""
FINAL = timedelta(days=2)
"""...plus one more within this time of the end."""


def _from_state(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else None


class CredentialWatch:
    def __init__(
        self,
        *,
        repo: Repo,
        bridge_settings: BridgeSettings,
        clock: Clock,
        zone: ZoneInfo,
        language: str,
        default_chat: Callable[[], str | None],
        notify: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self.repo = repo
        self.bs = bridge_settings
        self.clock = clock
        self.zone = zone
        self.language = language
        self.default_chat = default_chat
        self.notify = notify

    # -- what is known -----------------------------------------------------------------------------

    async def observe(self, result: RunResult) -> None:
        """Remember what a finished run said about its credential's end."""
        expires = result.credential_expires_at
        state = await self.repo.get_state(STATE)
        known = _from_state(state.get("expires_at")) if isinstance(state, dict) else None
        if expires is not None:
            expires = expires.astimezone(UTC)
            if expires != known:
                log.info("%s reports that its credential ends at %s", result.runner, expires)
                await self._remember(expires, result.runner)
        elif result.ok and known is not None:
            log.info("%s reports no end for its credential any more", result.runner)
            await self._remember(None, result.runner)

    async def _remember(self, expires: datetime | None, runner: str) -> None:
        await self.repo.set_state(
            STATE,
            {
                "expires_at": expires.isoformat() if expires is not None else None,
                "runner": runner,
                "seen_at": self.clock.now().isoformat(),
            },
        )

    def configured(self) -> datetime | None:
        value = self.bs.bridge_credential_expires
        return parse_expiry(value, self.zone) if value else None

    async def expires_at(self) -> tuple[datetime | None, str | None]:
        """The known end and where it comes from (``runner`` or ``setting``)."""
        state = await self.repo.get_state(STATE)
        reported = _from_state(state.get("expires_at")) if isinstance(state, dict) else None
        if reported is not None:
            return reported, "runner"
        configured = self.configured()
        if configured is not None:
            return configured, "setting"
        return None, None

    # -- telling the athlete -----------------------------------------------------------------------

    def _left(self, expires: datetime, now: datetime) -> str:
        days = (expires.astimezone(self.zone).date() - now.astimezone(self.zone).date()).days
        if days <= 0:
            return t(self.language, "credential_today")
        if days == 1:
            return t(self.language, "credential_tomorrow")
        return t(self.language, "credential_in_days", days=days)

    def _date(self, expires: datetime) -> str:
        return f"{expires.astimezone(self.zone):%Y-%m-%d}"

    async def _last_notice(self, expires: datetime) -> datetime | None:
        """When the last notice about this end went out (a new end starts afresh)."""
        state = await self.repo.get_state(NOTIFIED_STATE)
        if not isinstance(state, dict) or _from_state(state.get("expires_at")) != expires:
            return None
        return _from_state(state.get("at"))

    async def check(self, now: datetime, *, quiet: bool) -> None:
        """Send a notice if one is due (called from the event dispatcher's tick)."""
        if self.bs.bridge_credential_warn_days <= 0:
            return
        expires, source = await self.expires_at()
        if expires is None or expires <= now:
            return
        left = expires - now
        if left > timedelta(days=self.bs.bridge_credential_warn_days):
            return
        last = await self._last_notice(expires)
        due = (
            last is None
            or now - last >= REPEAT
            or (left <= FINAL and expires - last > FINAL)  # nothing sent in the final stretch
        )
        if not due or quiet:
            return
        chat = self.default_chat()
        if chat is None:
            return
        text = t(
            self.language,
            "credential_expires",
            date=self._date(expires),
            left=self._left(expires, now),
        )
        if source == "setting":
            text += "\n" + t(self.language, "credential_setting")
        await self.notify(chat, text)
        await self.repo.set_state(
            NOTIFIED_STATE, {"at": now.isoformat(), "expires_at": expires.isoformat()}
        )
        log.info("told the athlete that the model credential ends at %s", expires)

    async def status_line(self, now: datetime) -> str:
        expires, _ = await self.expires_at()
        if expires is None:
            value = t(self.language, "unknown")
        elif expires <= now:
            value = t(self.language, "credential_expired", date=self._date(expires))
        else:
            value = f"{self._date(expires)} ({self._left(expires, now)})"
        return t(self.language, "status_credential", value=value)
