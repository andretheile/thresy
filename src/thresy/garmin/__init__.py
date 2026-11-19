"""Garmin Connect access: a small client protocol, the real client, a fake, and logging in.

* :class:`GarminClient`: the calls the sync needs; :class:`ConnectClient` implements it over
  ``garminconnect``; :class:`~thresy.garmin.fake.FakeGarminClient` serves canned payloads.
* :mod:`thresy.garmin.auth`: token login, password login and the verification-code flow.
* :mod:`thresy.garmin.errors`: the exceptions callers handle.

``import thresy.garmin`` does not import ``garminconnect``; that happens on first use.
"""

from thresy.garmin.client import ConnectClient, GarminClient
from thresy.garmin.errors import (
    GarminAuthError,
    GarminAuthRequired,
    GarminError,
    GarminMfaRequired,
    GarminNotFound,
    GarminRateLimited,
)

__all__ = [
    "ConnectClient",
    "GarminAuthError",
    "GarminAuthRequired",
    "GarminClient",
    "GarminError",
    "GarminMfaRequired",
    "GarminNotFound",
    "GarminRateLimited",
]
