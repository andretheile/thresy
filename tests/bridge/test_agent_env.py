"""End to end: the environment a real CLI child process receives from the bridge.

The bridge runs with its full service environment (Telegram token, read/write database URL,
other secrets). The agent's CLI is replaced by a small script that records its environment
and answers like the default runner's CLI (Claude Code stream-json), so the whole chain is
exercised: bridge -> RunRequest.env -> runner allow-list -> child process.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from thresy.runner import HarnessConfig, RunnerConfig, get_runner

from .conftest import TOKEN

pytestmark = pytest.mark.db

FAKE_CLI = """\
#!{python}
import json, os, sys
sys.stdin.read()
with open(os.environ["FAKE_ENV_DUMP"], "w") as fh:
    json.dump(dict(os.environ), fh)
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "env-session"}}))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "env recorded", "session_id": "env-session"}}))
"""

EXPECTED_COACH = {
    "COACH_DATABASE_URL",
    "COACH_DATABASE_URL_RO",
    "COACH_DATA_DIR",
    "COACH_WORKSPACE_DIR",
    "COACH_TZ",
    "COACH_LANGUAGE",
    "COACH_NAME",
    "COACH_RUN_ID",
}


def test_child_process_gets_only_the_agent_environment(make_harness, tmp_path, monkeypatch):
    script = tmp_path / "bin" / "agent-cli"
    script.parent.mkdir()
    script.write_text(FAKE_CLI.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    dump = tmp_path / "env.json"
    runner = get_runner(
        "claude",
        RunnerConfig(
            claude=HarnessConfig(
                binary=str(script), env={"FAKE_ENV_DUMP": str(dump)}, exit_wait_s=2
            )
        ),
    )

    async def scenario() -> None:
        async with make_harness(runner) as h:
            # The service environment, as compose.yml gives it to the bridge.
            service_env = {
                "COACH_TELEGRAM_TOKEN": TOKEN,
                "COACH_TELEGRAM_CHAT_IDS": "100",
                "COACH_DATABASE_URL": h.settings.database_url,
                "COACH_AGENT_DATABASE_URL": h.settings.agent_database_url,
                "COACH_DATABASE_URL_RO": h.settings.database_url_ro,
                "COACH_GARMIN_PASSWORD": "garmin-secret",
                "UNRELATED_SECRET": "s3cret",
            }
            for key, value in service_env.items():
                monkeypatch.setenv(key, value)
            await h.say("record your environment")
            await h.settle()
            assert h.transport.texts == ["env recorded"]
            env = json.loads(Path(dump).read_text())
            run_id = h.one("SELECT id FROM agent_runs")["id"]

            assert {k for k in env if k.startswith("COACH_")} == EXPECTED_COACH
            assert env["COACH_DATABASE_URL"] == h.settings.agent_database_url
            assert env["COACH_DATABASE_URL_RO"] == h.settings.database_url_ro
            assert env["COACH_RUN_ID"] == str(run_id)
            assert env["TZ"] == "America/Chicago"
            values = "\n".join(env.values())
            for secret in (TOKEN, h.settings.database_url, "garmin-secret", "s3cret"):
                assert secret not in values
            assert "UNRELATED_SECRET" not in env and "COACH_TELEGRAM_TOKEN" not in env
            assert h.one("SELECT native_session_id FROM agent_sessions")["native_session_id"] == (
                "env-session"
            )

    asyncio.run(scenario())
