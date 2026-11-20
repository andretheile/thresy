"""Logging set up exactly as a service sets it up, for tests of what reaches the log.

Under pytest the root logger already has pytest's capture handlers, so ``thresy.logs.configure``
would only add its filter to them. ``production_logging`` removes them for the duration, so
``configure`` installs the services' own handler (standard error, read with ``capfd`` or
``capsys``), and restores everything afterwards.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from collections.abc import Iterator, Mapping

from thresy import logs


@contextlib.contextmanager
def isolated_secrets() -> Iterator[None]:
    """Start from no registered secrets and restore the process's registry afterwards (it is
    global by design: other tests' commands register values from their environment)."""
    saved = set(logs._secrets), logs._known
    logs._secrets.clear()
    logs._known = None
    try:
        yield
    finally:
        logs._secrets.clear()
        logs._secrets.update(saved[0])
        logs._known = saved[1]


@contextlib.contextmanager
def production_logging(
    *, verbose: bool = False, environ: Mapping[str, str] | None = None
) -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in logs.QUIET_LOGGERS}
    last_resort = logging.lastResort
    hooks = sys.excepthook, threading.excepthook
    for handler in handlers:
        root.removeHandler(handler)
    try:
        logs.configure(verbose=verbose, environ=environ or {})
        yield
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
        for name, value in levels.items():
            logging.getLogger(name).setLevel(value)
        logging.lastResort = last_resort
        sys.excepthook, threading.excepthook = hooks
