"""The Garmin calls this project needs, as a small protocol with a real implementation.

:class:`GarminClient` lists exactly the requests the sync makes. :class:`ConnectClient` implements
it on top of a logged-in ``garminconnect.Garmin`` (see :mod:`thresy.garmin.auth` for logging in),
and :class:`thresy.garmin.fake.FakeGarminClient` serves canned payloads for tests and imports.

Payloads are returned exactly as Garmin sends them (they are stored as raw ``jsonb``), except
that "no data" answers become ``None``: Garmin answers some empty days with HTTP 204, which the
library turns into ``{}``, and an empty dict must not overwrite a stored payload.

Errors are translated into :mod:`thresy.garmin.errors`, so callers never import the library.
``garminconnect`` itself is imported lazily: it pulls in ``requests`` and ``curl_cffi``, and
``coach --help`` should stay fast.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any, Protocol, runtime_checkable

from thresy.garmin.errors import GarminAuthError, GarminError, GarminNotFound, GarminRateLimited
from thresy.logs import redact


@runtime_checkable
class GarminClient(Protocol):
    """The Garmin Connect requests used by the sync (and nothing else)."""

    def get_activities_by_date(self, start: date, end: date) -> list[dict[str, Any]]:
        """Activity summaries whose local start date is in ``[start, end]``, newest first."""
        ...

    def download_original(self, activity_id: int) -> bytes:
        """The activity's original upload: a zip archive holding the FIT file (usually)."""
        ...

    def get_sleep_data(self, day: date) -> dict[str, Any] | None:
        """Sleep of the night that ended on ``day`` (Garmin keys sleep by wake date)."""
        ...

    def get_hrv_data(self, day: date) -> dict[str, Any] | None:
        """HRV summary and 5-minute readings of the night that ended on ``day``."""
        ...

    def get_stats(self, day: date) -> dict[str, Any] | None:
        """Daily summary (steps, resting heart rate, stress, body battery) of ``day``."""
        ...

    def get_steps_data(self, day: date) -> list[dict[str, Any]] | None:
        """15-minute step intervals of ``day`` (only needed when the summary has no steps)."""
        ...

    def get_personal_records(self) -> list[dict[str, Any]]:
        """The athlete's personal records (``store.garmin_personal_records`` maps them)."""
        ...


def none_if_empty(payload: Any) -> Any:
    """``None`` for Garmin's empty answers (``None``, ``{}``, ``[]``), else ``payload``."""
    if payload is None or (isinstance(payload, Mapping | list) and not payload):
        return None
    return payload


NO_DATA_MESSAGE = "No data received from server"  # garminconnect's get_user_summary
_STATUS_RE = re.compile(r"(?:API Error|Error|HTTP)\s*\(?(\d{3})")


def http_status(exc: BaseException) -> int | None:
    """The HTTP status carried by a ``garminconnect`` exception (attribute or message)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _is_network_error(exc: BaseException) -> bool:
    import requests

    if isinstance(exc, requests.ConnectionError | requests.Timeout | ConnectionError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    # curl_cffi (used by the library for some requests) has its own exception classes.
    return type(exc).__module__.startswith("curl_cffi")


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and all(cur is not s for s in seen):
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def translate_error(exc: BaseException) -> BaseException:
    """Map a ``garminconnect`` (or transport) exception to a :class:`GarminError`.

    The library reports *any* failure while loading the profile after a token login as
    ``GarminConnectAuthenticationError`` ("Failed to retrieve social profile"), network errors
    included. A password login must never follow a network blip, so the exception chain is
    inspected: a rate limit (429/427) becomes :class:`GarminRateLimited`, a network error or
    5xx a plain :class:`GarminError`, and only the rest :class:`GarminAuthError`.

    Exceptions that are not about talking to Garmin (``ValueError`` for a bad argument, bugs)
    are returned unchanged.

    Messages are redacted (:func:`thresy.logs.redact`): they are logged, stored in
    ``sync_state`` and events and shown to the athlete, and an exception of ``requests``
    quotes the request URL, which during a login carries the SSO service ticket.
    """
    import garminconnect as gc

    message = redact(str(exc) or type(exc).__name__)
    if isinstance(exc, gc.GarminConnectTooManyRequestsError):
        return GarminRateLimited(message)
    if isinstance(exc, gc.GarminConnectAuthenticationError):
        for link in _chain(exc)[1:]:
            status = http_status(link)
            if isinstance(link, gc.GarminConnectTooManyRequestsError) or status in (427, 429):
                return GarminRateLimited(redact(f"{message} ({link})"))
            if _is_network_error(link) or (status is not None and status >= 500):
                return GarminError(redact(f"{message} ({link})"))
        return GarminAuthError(message)
    if isinstance(exc, gc.GarminConnectNotFoundError):
        return GarminNotFound(message)
    if isinstance(exc, gc.GarminConnectConnectionError):
        status = http_status(exc)
        if status in (427, 429):
            return GarminRateLimited(message)
        if status == 404:
            return GarminNotFound(message)
        if status == 401:
            return GarminAuthError(message)
        return GarminError(message)
    if _is_network_error(exc):
        return GarminError(message)
    return exc


def _translated[T](func: Callable[..., T]) -> Callable[..., T]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return func(*args, **kwargs)
        except GarminError:
            raise
        except Exception as exc:
            translated = translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc

    return wrapper


class ConnectClient:
    """:class:`GarminClient` over a logged-in ``garminconnect.Garmin`` instance.

    Every method is one library call; the method names and signatures used are those of
    ``garminconnect`` 0.3.20 (``get_activities_by_date``, ``download_activity``,
    ``get_sleep_data``, ``get_hrv_data``, ``get_stats``, ``get_steps_data``,
    ``get_personal_record``). The library retries 5xx and network errors itself
    (``retry_attempts``); 401, 404 and 429 fail fast and are translated here.
    """

    def __init__(self, garmin: Any) -> None:
        self.garmin = garmin

    @_translated
    def get_activities_by_date(self, start: date, end: date) -> list[dict[str, Any]]:
        items = self.garmin.get_activities_by_date(start.isoformat(), end.isoformat())
        return [dict(item) for item in items or [] if isinstance(item, Mapping)]

    @_translated
    def download_original(self, activity_id: int) -> bytes:
        fmt = self.garmin.ActivityDownloadFormat.ORIGINAL
        return bytes(self.garmin.download_activity(str(int(activity_id)), dl_fmt=fmt))

    @_translated
    def get_sleep_data(self, day: date) -> dict[str, Any] | None:
        return none_if_empty(self.garmin.get_sleep_data(day.isoformat()))

    @_translated
    def get_hrv_data(self, day: date) -> dict[str, Any] | None:
        return none_if_empty(self.garmin.get_hrv_data(day.isoformat()))

    @_translated
    def get_stats(self, day: date) -> dict[str, Any] | None:
        import garminconnect as gc

        try:
            return none_if_empty(self.garmin.get_stats(day.isoformat()))
        except gc.GarminConnectConnectionError as exc:
            # The library raises this for an empty daily summary (e.g. a day that has not
            # started yet in the Garmin profile's time zone): that is "no data", not a failure.
            if type(exc) is gc.GarminConnectConnectionError and str(exc) == NO_DATA_MESSAGE:
                return None
            raise

    @_translated
    def get_steps_data(self, day: date) -> list[dict[str, Any]] | None:
        return none_if_empty(self.garmin.get_steps_data(day.isoformat()))

    @_translated
    def get_personal_records(self) -> list[dict[str, Any]]:
        payload = self.garmin.get_personal_record()
        if isinstance(payload, Mapping):  # documented as a dict, delivered as a list
            payload = payload.get("personalRecords") or []
        return [dict(item) for item in payload or [] if isinstance(item, Mapping)]
