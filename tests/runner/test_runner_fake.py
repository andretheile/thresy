"""FakeRunner: the runner other packages use in their tests."""

from __future__ import annotations

import asyncio

from runner_testkit import run_collect

from thresy.runner import (
    AgentRunner,
    Delay,
    Fail,
    FakeRunner,
    Final,
    Progress,
    RunRequest,
    RunResult,
    Started,
    get_runner,
)


def req(tmp_path, prompt="Wie geht's?", **kw):
    return RunRequest(prompt=prompt, cwd=tmp_path, **kw)


def test_echo_mode(tmp_path):
    runner = FakeRunner()
    events, result = run_collect(runner.run(req(tmp_path)))
    assert isinstance(events[0], Started) and events[0].session_id.startswith("fake-")
    assert events[1] == Progress("text", "Wie geht's?")
    assert result.ok and result.text == "Wie geht's?" and result.runner == "fake"
    assert result.session_id == events[0].session_id
    assert runner.requests == [req(tmp_path)]
    assert isinstance(runner, AgentRunner)


def test_resume_keeps_session_id(tmp_path):
    _, result = run_collect(FakeRunner().run(req(tmp_path, session_id="abc")))
    assert result.session_id == "abc"


def test_reply_and_fixed_session(tmp_path):
    runner = FakeRunner(reply="Lauf locker.", session_id="s-1", cost_usd=0.01, usage={"x": 1})
    events, result = run_collect(runner.run(req(tmp_path)))
    assert events[0] == Started("s-1")
    assert result.text == "Lauf locker." and result.cost_usd == 0.01 and result.usage == {"x": 1}


def test_scripted_events(tmp_path):
    script = [
        Started("s-2"),
        Progress("tool", "Bash: coach sql"),
        Delay(0.01),
        Progress("text", "Fertig."),
    ]
    events, result = run_collect(FakeRunner(script).run(req(tmp_path)))
    assert events[:-1] == script[:2] + script[3:]
    assert result.ok and result.text == "Fertig." and result.session_id == "s-2"


def test_scripted_final(tmp_path):
    final = RunResult(
        ok=True,
        text="given",
        session_id=None,
        error_kind=None,
        error_detail=None,
        cost_usd=None,
        usage=None,
        duration_s=0,
        runner="fake",
        model=None,
    )
    events, result = run_collect(
        FakeRunner([Final(final), Progress("text", "ignored")]).run(req(tmp_path))
    )
    assert result.text == "given" and result.session_id  # session filled in
    assert len(events) == 2


def test_callable_script(tmp_path):
    runner = FakeRunner(lambda r: [Progress("text", r.prompt.upper())])
    _, result = run_collect(runner.run(req(tmp_path, prompt="hi")))
    assert result.text == "HI"


def test_fail_kinds(tmp_path):
    for kind in ("rate_limit", "auth", "crash", "other"):
        events, result = run_collect(FakeRunner(fail=kind).run(req(tmp_path)))
        assert not result.ok and result.error_kind == kind
        assert [type(e) for e in events] == [Started, Final]
    _, result = run_collect(
        FakeRunner([Progress("text", "a"), Fail("auth", "expired")]).run(req(tmp_path))
    )
    assert result.error_kind == "auth" and result.error_detail == "expired" and result.text == "a"


def test_timeout(tmp_path):
    _, result = run_collect(FakeRunner([Delay(60)]).run(req(tmp_path, timeout_s=0.1)))
    assert result.error_kind == "timeout"
    _, result = run_collect(FakeRunner(delay_s=60).run(req(tmp_path, timeout_s=0.1)))
    assert result.error_kind == "timeout"


def test_cancel(tmp_path):
    async def scenario():
        handle = FakeRunner([Delay(60)]).run(req(tmp_path))
        events = []
        async for event in handle:
            events.append(event)
            if isinstance(event, Started):
                handle.cancel()
        return events

    events = asyncio.run(scenario())
    assert events[-1].result.error_kind == "other"
    assert events[-1].result.error_detail == "cancelled"


def test_healthcheck_and_registry(tmp_path):
    runner = get_runner("fake")
    assert isinstance(runner, FakeRunner)
    health = asyncio.run(runner.healthcheck())
    assert health.ok and health.runner == "fake"
