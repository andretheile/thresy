"""Building the bridge from the settings and running it until a stop signal."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from thresy.config import ConfigError, Settings, get_settings
from thresy.runner import AgentRunner, RunnerConfig, get_runner
from thresy.transport.base import Transport

from .app import Bridge
from .repo import Repo
from .settings import BridgeSettings

log = logging.getLogger(__name__)


def runner_from_env() -> AgentRunner:
    """The configured runner; like ``coach runner``, a ``.env`` file fills gaps."""
    environ: dict[str, str] = {}
    if Path(".env").is_file():
        from dotenv import dotenv_values

        environ.update({k: v for k, v in dotenv_values(".env").items() if v is not None})
    environ.update(os.environ)
    try:
        return get_runner(None, RunnerConfig.from_env(environ))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def build_transport(settings: Settings, bridge_settings: BridgeSettings) -> Transport:
    name = bridge_settings.bridge_transport.strip().lower()
    if name != "telegram":
        raise ConfigError(f"COACH_BRIDGE_TRANSPORT={name!r}: only 'telegram' is available")
    if settings.telegram_token is None:
        raise ConfigError("COACH_TELEGRAM_TOKEN is not set")
    if not settings.telegram_chat_ids:
        raise ConfigError("COACH_TELEGRAM_CHAT_IDS is not set (comma-separated chat ids)")
    from thresy.transport.telegram import TelegramTransport

    workspace = Path(settings.workspace_dir).expanduser().resolve()
    return TelegramTransport(
        settings.telegram_token.get_secret_value(),
        settings.telegram_chat_ids,
        inbox_dir=workspace / "inbox",
    )


def check_settings(settings: Settings) -> None:
    app_url = settings.require_database_url()
    agent_url = settings.require_agent_database_url()
    if agent_url == app_url:
        raise ConfigError(
            "COACH_AGENT_DATABASE_URL must be the coach_agent URL, not the read/write URL"
        )
    if not settings.database_url_ro:
        log.warning("COACH_DATABASE_URL_RO is not set: the agent's `coach sql` will not work")


async def main() -> None:
    settings = get_settings()
    bridge_settings = BridgeSettings()
    check_settings(settings)
    runner = runner_from_env()
    transport = build_transport(settings, bridge_settings)
    health = await runner.healthcheck()
    if health.ok:
        log.info("runner %s: %s", runner.name, health.detail)
    else:
        log.error("runner %s does not look usable: %s", runner.name, health.detail)
    for warning in health.warnings:
        log.warning("runner %s: %s", runner.name, warning)

    repo = Repo(settings.require_database_url())
    await repo.open()
    bridge = Bridge(
        settings=settings,
        bridge_settings=bridge_settings,
        repo=repo,
        runner=runner,
        transport=transport,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await bridge.start()
        await stop.wait()
    finally:
        await bridge.shutdown()
        await repo.close()
