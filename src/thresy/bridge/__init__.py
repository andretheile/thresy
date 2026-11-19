"""The chat bridge: chat loop, sessions, agent runs, outbox delivery and event dispatcher.

See docs/bridge.md. Entry point: ``coach bridge run``. This module imports nothing on purpose:
``coach`` imports ``thresy.bridge.cli`` on every invocation (including the agent's own
``coach send``), so the heavy parts load only when the bridge actually runs. Import from the
submodules: ``thresy.bridge.app.Bridge``, ``thresy.bridge.settings.BridgeSettings``,
``thresy.bridge.clock.FakeClock``.
"""
