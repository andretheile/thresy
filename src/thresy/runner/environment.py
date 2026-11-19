"""The child environment is built from an allow-list, never inherited wholesale.

The service that starts the agent holds secrets the agent must not see (the
Telegram token, the read/write database URL). The agent has a shell and reads
the web, so only these reach it from the parent environment:

* basic process variables (BASE_NAMES, BASE_PREFIXES),
* the variables the selected harness reads for its own auth and configuration
  (declared by each adapter),
* names or prefixes listed in the adapter's pass-through setting.

No COACH_* variable passes unless it is listed explicitly. The adapter's fixed
additions, COACH_<NAME>_ENV and RunRequest.env are added on top by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

BASE_NAMES: frozenset[str] = frozenset(
    {
        # process basics
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TERM",
        # CA certificates
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        # proxies
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "NODE_USE_ENV_PROXY",
    }
)
BASE_PREFIXES: tuple[str, ...] = ("LC_", "XDG_")


def parse_patterns(patterns: Iterable[str]) -> tuple[frozenset[str], tuple[str, ...]]:
    """Split pass-through entries into exact names and prefixes ("NAME_*")."""
    names: set[str] = set()
    prefixes: list[str] = []
    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        if pattern.endswith("*"):
            prefixes.append(pattern[:-1])
        else:
            names.add(pattern)
    return frozenset(names), tuple(prefixes)


def allowed_environment(
    parent: Mapping[str, str],
    *,
    harness_names: Iterable[str] = (),
    harness_prefixes: Iterable[str] = (),
    pass_through: Iterable[str] = (),
    blocked: Iterable[str] = (),
) -> dict[str, str]:
    """The subset of `parent` that a harness child may inherit."""
    extra_names, extra_prefixes = parse_patterns(pass_through)
    names = BASE_NAMES | frozenset(harness_names) | extra_names
    harness_prefixes = tuple(harness_prefixes)
    blocked_names = frozenset(blocked)
    env: dict[str, str] = {}
    for key, value in parent.items():
        if key in blocked_names:
            continue
        explicit = key in extra_names or (extra_prefixes and key.startswith(extra_prefixes))
        if key.startswith("COACH_") and not explicit:
            continue  # never by default, whatever the harness prefixes say
        if (
            key in names
            or key.startswith(BASE_PREFIXES)
            or (harness_prefixes and key.startswith(harness_prefixes))
            or explicit
        ):
            env[key] = value
    return env
