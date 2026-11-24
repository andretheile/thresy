"""Password hashing, signed sessions and form tokens, the login rate limit, the client address.

Standard library only.

**Password hash.** scrypt with a random 16-byte salt, stored self-describing as
``scrypt:ln=15,r=8,p=1:<salt>:<hash>`` (base64url without padding; ``ln`` is log2 of the cost
N). The format has no ``$``, so it survives Compose's variable interpolation in ``.env``.
Verification recomputes the hash with the stored parameters and compares in constant time.

**Signed values.** A token is ``<payload>.<mac>``: the payload is base64url JSON with a
``purpose`` and an expiry, the MAC is HMAC-SHA256 over purpose and payload. The key is derived
from ``COACH_WEB_SECRET`` *and* the configured password hash, so changing either ends every
session and invalidates every form token.

**Rate limit.** Failed logins are counted per client and overall, in memory. A client gets
:attr:`Limiter.client_free` failures; after that each failure locks it out for twice as long
as the one before (from :attr:`Limiter.base_s` up to :attr:`Limiter.client_cap_s`). The same
applies to all clients together once :attr:`Limiter.global_free` failures happened within an
hour, so rotating the client address (or a forwarded header) cannot get around the limit.
While locked out, the password is not checked at all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# -- passwords ------------------------------------------------------------------------------------

SCRYPT_LOG_N = 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32
MAX_LOG_N = 20
"""Hashes asking for more work than this are refused (a typo must not exhaust the memory)."""


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class PasswordHash:
    log_n: int
    r: int
    p: int
    salt: bytes
    digest: bytes

    def __str__(self) -> str:
        return f"scrypt:ln={self.log_n},r={self.r},p={self.p}:{b64e(self.salt)}:{b64e(self.digest)}"


def _scrypt(password: str, salt: bytes, log_n: int, r: int, p: int, dklen: int) -> bytes:
    n = 1 << log_n
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=2 * 128 * r * n * p + 1024 * 1024,
    )


def hash_password(password: str, *, log_n: int = SCRYPT_LOG_N, salt: bytes | None = None) -> str:
    """A new hash of ``password`` in the stored format."""
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, log_n, SCRYPT_R, SCRYPT_P, HASH_BYTES)
    return str(PasswordHash(log_n, SCRYPT_R, SCRYPT_P, salt, digest))


def parse_password_hash(text: str) -> PasswordHash:
    """Parse the stored format; raises ``ValueError`` saying what is wrong."""
    parts = text.strip().split(":")
    if len(parts) != 4 or parts[0] != "scrypt":
        raise ValueError("expected scrypt:ln=..,r=..,p=..:<salt>:<hash> (from coach web password)")
    try:
        params = dict(item.split("=", 1) for item in parts[1].split(","))
        log_n, r, p = int(params["ln"]), int(params["r"]), int(params["p"])
        salt, digest = b64d(parts[2]), b64d(parts[3])
    except (KeyError, ValueError) as exc:
        raise ValueError("the parameters or the base64 parts cannot be read") from exc
    if not (10 <= log_n <= MAX_LOG_N and 1 <= r <= 32 and 1 <= p <= 4):
        raise ValueError(f"unsupported scrypt parameters ln={log_n}, r={r}, p={p}")
    if len(salt) < 8 or len(digest) < 16:
        raise ValueError("salt or hash too short")
    return PasswordHash(log_n, r, p, salt, digest)


def verify_password(password: str, stored: PasswordHash) -> bool:
    """Whether ``password`` matches; always does the full work, compares in constant time."""
    candidate = _scrypt(password, stored.salt, stored.log_n, stored.r, stored.p, len(stored.digest))
    return hmac.compare_digest(candidate, stored.digest)


GENERATED_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
"""Lower-case letters and digits without the ones that are easy to confuse (i, l, o, 0, 1)."""


def generate_password(groups: int = 5, size: int = 5) -> str:
    """A random password such as ``k7wqm-3mfpx-x9tdn-u2vhr-r8cey`` (about 123 bits)."""
    return "-".join(
        "".join(secrets.choice(GENERATED_ALPHABET) for _ in range(size)) for _ in range(groups)
    )


# -- signed values ---------------------------------------------------------------------------------


class Signer:
    """Signs and checks small JSON payloads for one purpose each (``session``, ``login``,
    ``logout``)."""

    def __init__(self, secret: str, password_hash: str) -> None:
        base = hmac.new(secret.encode("utf-8"), b"thresy-web-v1", hashlib.sha256).digest()
        self._key = hmac.new(base, password_hash.encode("utf-8"), hashlib.sha256).digest()

    def _mac(self, purpose: str, body: str) -> str:
        msg = purpose.encode("ascii") + b"." + body.encode("ascii")
        return b64e(hmac.new(self._key, msg, hashlib.sha256).digest())

    def sign(self, purpose: str, payload: dict[str, Any]) -> str:
        body = b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return f"{body}.{self._mac(purpose, body)}"

    def unsign(self, purpose: str, token: str | None, *, now: float) -> dict[str, Any] | None:
        """The payload when ``token`` was signed for ``purpose`` and has not expired."""
        if not token or len(token) > 4096 or token.count(".") != 1:
            return None
        body, mac = token.split(".")
        if not hmac.compare_digest(
            mac.encode("ascii", "replace"), self._mac(purpose, body).encode()
        ):
            return None
        try:
            payload = json.loads(b64d(body))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if not isinstance(exp, int | float) or exp <= now:
            return None
        return payload


@dataclass(frozen=True)
class Session:
    sid: str
    issued_at: float
    expires_at: float


class Sessions:
    """Session cookies: create, read, revoke (logout)."""

    def __init__(self, signer: Signer, lifetime_s: float) -> None:
        self.signer = signer
        self.lifetime_s = lifetime_s
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self, now: float) -> tuple[Session, str]:
        session = Session(secrets.token_urlsafe(16), now, now + self.lifetime_s)
        token = self.signer.sign(
            "session", {"sid": session.sid, "iat": int(now), "exp": int(session.expires_at)}
        )
        return session, token

    def read(self, token: str | None, now: float) -> Session | None:
        payload = self.signer.unsign("session", token, now=now)
        if payload is None or not isinstance(payload.get("sid"), str):
            return None
        with self._lock:
            if payload["sid"] in self._revoked:
                return None
        return Session(payload["sid"], float(payload.get("iat", 0)), float(payload["exp"]))

    def revoke(self, session: Session, now: float) -> None:
        """Forget a session on logout (in memory: a restart forgets the revocation, the
        cookie expires anyway; rotate ``COACH_WEB_SECRET`` to end every session for good)."""
        with self._lock:
            self._revoked = {s: exp for s, exp in self._revoked.items() if exp > now}
            self._revoked[session.sid] = session.expires_at

    def logout_token(self, session: Session) -> str:
        return self.signer.sign("logout", {"sid": session.sid, "exp": int(session.expires_at)})

    def check_logout_token(self, session: Session, token: str | None, now: float) -> bool:
        payload = self.signer.unsign("logout", token, now=now)
        return bool(payload and payload.get("sid") == session.sid)


LOGIN_FORM_TTL_S = 2 * 3600


def login_form_token(signer: Signer, now: float) -> str:
    return signer.sign("login", {"n": secrets.token_urlsafe(8), "exp": int(now + LOGIN_FORM_TTL_S)})


def check_login_form_token(signer: Signer, token: str | None, now: float) -> bool:
    return signer.unsign("login", token, now=now) is not None


# -- rate limit ------------------------------------------------------------------------------------


@dataclass
class _Client:
    failures: int
    last: float


class Limiter:
    """Failed logins per client and overall, with growing lockouts (see the module docstring).

    The login calls :meth:`reserve` before it checks a password: it returns the seconds to
    wait (0.0: go ahead), and a granted check counts as an attempt in flight until
    :meth:`finish` records its outcome. Attempts in flight count against the free failures
    like failures, so a burst of parallel requests gets no more password checks than the free
    count, per client and overall, however many requests it sends. ``check``, ``failure``
    and ``success`` are the parts without the reservation. ``clock`` is injectable for tests.
    """

    def __init__(
        self,
        *,
        client_free: int = 5,
        global_free: int = 30,
        base_s: float = 30.0,
        client_cap_s: float = 3600.0,
        global_cap_s: float = 900.0,
        forget_s: float = 24 * 3600.0,
        global_window_s: float = 3600.0,
        max_clients: int = 10_000,
        clock: Any = time.monotonic,
    ) -> None:
        self.client_free = client_free
        self.global_free = global_free
        self.base_s = base_s
        self.client_cap_s = client_cap_s
        self.global_cap_s = global_cap_s
        self.forget_s = forget_s
        self.global_window_s = global_window_s
        self.max_clients = max_clients
        self.clock = clock
        self._clients: OrderedDict[str, _Client] = OrderedDict()
        self._global: deque[float] = deque()
        self._in_flight: dict[str, int] = {}
        self._in_flight_total = 0
        self._lock = threading.Lock()

    def _delay(self, failures: int, free: int, cap: float) -> float:
        over = failures - free
        if over < 0:
            return 0.0
        return min(cap, self.base_s * 2 ** min(over, 30))

    def _prune(self, now: float) -> None:
        while self._global and self._global[0] <= now - self.global_window_s:
            self._global.popleft()
        while self._clients:
            key, entry = next(iter(self._clients.items()))
            if entry.last > now - self.forget_s and len(self._clients) <= self.max_clients:
                break
            del self._clients[key]

    def _wait(self, client: str, now: float) -> float:
        """Seconds to wait; call with the lock held."""
        self._prune(now)
        wait = 0.0
        entry = self._clients.get(client)
        failures = entry.failures if entry is not None else 0
        if entry is not None:
            until = entry.last + self._delay(failures, self.client_free, self.client_cap_s)
            wait = max(wait, until - now)
        if self._global:
            delay = self._delay(len(self._global), self.global_free, self.global_cap_s)
            wait = max(wait, self._global[-1] + delay - now)
        # the free attempts are taken by checks still running: wait for their outcome
        in_flight = self._in_flight.get(client, 0)
        if in_flight and failures + in_flight >= self.client_free:
            wait = max(wait, self.base_s)
        if self._in_flight_total and len(self._global) + self._in_flight_total >= self.global_free:
            wait = max(wait, self.base_s)
        return max(0.0, wait)

    def check(self, client: str) -> float:
        """Seconds until ``client`` may try again (0.0: now)."""
        with self._lock:
            return self._wait(client, self.clock())

    def reserve(self, client: str) -> float:
        """Like :meth:`check`, and when it is 0.0, count one check of ``client`` as in flight
        (end it with :meth:`finish`)."""
        with self._lock:
            wait = self._wait(client, self.clock())
            if wait == 0.0:
                self._in_flight[client] = self._in_flight.get(client, 0) + 1
                self._in_flight_total += 1
            return wait

    def finish(self, client: str, ok: bool) -> None:
        """End a reserved check: record its outcome."""
        with self._lock:
            left = self._in_flight.get(client, 0) - 1
            if left > 0:
                self._in_flight[client] = left
            else:
                self._in_flight.pop(client, None)
            self._in_flight_total = max(0, self._in_flight_total - 1)
        if ok:
            self.success(client)
        else:
            self.failure(client)

    def failure(self, client: str) -> None:
        now = self.clock()
        with self._lock:
            entry = self._clients.pop(client, None) or _Client(0, now)
            entry.failures += 1
            entry.last = now
            self._clients[client] = entry  # most recent last
            self._global.append(now)
            self._prune(now)

    def success(self, client: str) -> None:
        with self._lock:
            self._clients.pop(client, None)


# -- the client's address --------------------------------------------------------------------------


def _parse_address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = text.strip().strip('"')
    if value.startswith("["):  # [v6]:port
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:  # v4:port
        value = value.split(":", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def client_key(
    peer: str | None, header_values: Iterable[str], *, trusted: bool, hops: int = 1
) -> str:
    """The key failed logins are counted under: the client's address, an IPv6 address
    reduced to its /64 network (one subscriber usually has a whole /64 to rotate through).

    With ``trusted`` the address comes from the forwarded header (``hops`` entries from the
    right of a comma-separated list); when the header is missing, malformed or shorter than
    ``hops``, the connection's address is used instead. ``peer`` is the connection's address.
    """
    address = None
    if trusted:
        entries = [p.strip() for v in header_values for p in v.split(",") if p.strip()]
        if len(entries) >= hops:
            address = _parse_address(entries[-hops])
    if address is None and peer:
        address = _parse_address(peer)
    if address is None:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{address}/64", strict=False))
    return str(address)
