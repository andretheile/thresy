"""Starting the agent CLI as its own Unix user (off by default).

The service that starts the agent (the bridge) holds secrets in its environment: the Telegram
token and the read/write database URL. On Linux a process can read the environment of every
other process of the same user through ``/proc/<pid>/environ``, so an allow-listed environment
alone does not keep those secrets from an agent that has a shell. With ``COACH_AGENT_USER`` set,
every CLI process the runner starts (agent runs and health checks) is started through a small
setuid helper (``docker/run-as-agent.c``) that switches to that user first:

    COACH_AGENT_USER          the agent's user name, e.g. ``agent`` (unset or empty: off)
    COACH_AGENT_USER_HELPER   the helper (default /usr/local/libexec/thresy/run-as-agent)

What changes when it is on:

* argv becomes ``[helper, "--", *argv]``. The environment is still the runner's allow-list plus
  the request's additions, except that ``HOME``, ``USER`` and ``LOGNAME`` describe the agent
  user (from the password database). It reaches the helper through ``execve``, never through
  arguments. The helper adds the variables of the agent's credential file
  (``<agent home>/.thresy/credentials.env``, see docs/deploy.md), and the C library removes
  ``TMPDIR``, ``LD_*`` and a few similar variables because the helper is setuid.
* The child gets umask 007, so files it creates in the shared workspace stay readable and
  writable for the group the bridge user shares with the agent, and for nobody else.
* Signals to the child's process group are sent through the helper (``--signal``): the
  bridge's user may not signal another user's processes, but the helper, running as the agent
  user, may. Whether the group still exists is checked directly (``killpg(pgid, 0)`` answers
  "no such process" or "not permitted", and the latter means it exists).
* Private files the CLI must read (the appended system prompt) get the agent user's group.

Standard library only, like the rest of the runner package.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pwd
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

DEFAULT_HELPER = "/usr/local/libexec/thresy/run-as-agent"
AGENT_UMASK = 0o007
CREDENTIALS_FILE = ".thresy/credentials.env"
"""The agent's credential file, relative to the agent user's home directory. The helper reads
it (as the agent user) and adds its ``NAME=value`` lines to the CLI's environment."""
SIGNAL_TIMEOUT_S = 5.0

# Exit status of `run-as-agent --signal` when the process group does not exist.
_SIGNAL_GONE = 3

# Variables the C library removes from the environment of a setuid program (glibc's
# UNSECURE_ENVVARS). Of the runner's allow-list only TMPDIR is affected.
SECURE_MODE_STRIPPED = frozenset(
    {
        "GCONV_PATH",
        "GETCONF_DIR",
        "HOSTALIASES",
        "LD_AUDIT",
        "LD_DEBUG",
        "LD_DEBUG_OUTPUT",
        "LD_DYNAMIC_WEAK",
        "LD_HWCAP_MASK",
        "LD_LIBRARY_PATH",
        "LD_ORIGIN_PATH",
        "LD_PRELOAD",
        "LD_PROFILE",
        "LD_SHOW_AUXV",
        "LD_USE_LOAD_BIAS",
        "LOCALDOMAIN",
        "LOCPATH",
        "MALLOC_TRACE",
        "NIS_PATH",
        "NLSPATH",
        "RESOLV_HOST_CONF",
        "RES_OPTIONS",
        "TMPDIR",
        "TZDIR",
    }
)

SignalOutcome = Literal["sent", "gone", "failed"]


@dataclass(frozen=True)
class UserSwitch:
    """Start CLI processes as another user through the setuid helper."""

    user: str
    uid: int
    gid: int
    home: str
    helper: str = DEFAULT_HELPER
    umask: int = AGENT_UMASK

    @classmethod
    def for_user(cls, user: str, helper: str | None = None) -> UserSwitch:
        """Look the user up in the password database; ValueError if it does not exist."""
        try:
            entry = pwd.getpwnam(user)
        except KeyError:
            raise ValueError(f"COACH_AGENT_USER={user!r}: no such user") from None
        if entry.pw_uid == os.getuid():
            raise ValueError(
                f"COACH_AGENT_USER={user!r} is the user this process runs as; the agent must "
                "run as another user (or unset COACH_AGENT_USER)"
            )
        return cls(
            user=entry.pw_name,
            uid=entry.pw_uid,
            gid=entry.pw_gid,
            home=entry.pw_dir,
            helper=helper or DEFAULT_HELPER,
        )

    @property
    def credentials_file(self) -> Path:
        return Path(self.home) / CREDENTIALS_FILE

    def wrap(self, argv: Sequence[str]) -> list[str]:
        """The command line that runs ``argv`` as the agent user."""
        return [self.helper, "--", *argv]

    def environment(self, env: Mapping[str, str]) -> dict[str, str]:
        """``env`` with the identity variables of the agent user."""
        out = dict(env)
        out.update(HOME=self.home, USER=self.user, LOGNAME=self.user)
        return out

    async def signal_group(self, pgid: int, sig: int) -> SignalOutcome:
        """Send ``sig`` to process group ``pgid`` as the agent user."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.helper,
                "--signal",
                str(int(sig)),
                str(int(pgid)),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={},
            )
        except OSError as exc:
            log.error("cannot run %s to signal process group %d: %s", self.helper, pgid, exc)
            return "failed"
        try:
            _, err = await asyncio.wait_for(proc.communicate(), SIGNAL_TIMEOUT_S)
        except TimeoutError:
            log.error("%s --signal did not finish within %gs", self.helper, SIGNAL_TIMEOUT_S)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.kill()
            return "failed"
        if proc.returncode == 0:
            return "sent"
        if proc.returncode == _SIGNAL_GONE:
            return "gone"
        detail = err.decode("utf-8", "replace").strip()
        log.error(
            "could not signal process group %d as %s (exit %s): %s",
            pgid,
            self.user,
            proc.returncode,
            detail,
        )
        return "failed"


def switch_from_env(env: Mapping[str, str]) -> UserSwitch | None:
    """``COACH_AGENT_USER`` / ``COACH_AGENT_USER_HELPER``; None when the switch is off."""
    user = (env.get("COACH_AGENT_USER") or "").strip()
    if not user:
        return None
    helper = (env.get("COACH_AGENT_USER_HELPER") or "").strip() or None
    return UserSwitch.for_user(user, helper)
