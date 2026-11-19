"""The prompts the bridge builds, pinned verbatim, and the numbers-only briefing lines."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from thresy.bridge import prompts
from thresy.bridge.briefing import numbers_lines
from thresy.bridge.texts import TEXTS, lang_code, t

ZONE = ZoneInfo("Europe/Berlin")
NOW = datetime(2030, 1, 2, 8, 15, tzinfo=ZONE)
FOOTER = (
    "Reply in German. Your final reply is sent to the athlete as one chat message. Send nothing"
    " in between unless it carries content (`coach send`, for plots, files or questions with"
    " buttons)."
)


def env(tmp_path: Path, *, playbooks: bool) -> prompts.PromptEnv:
    if playbooks:
        (tmp_path / "playbooks").mkdir()
        for name in ("chat.md", "morning-briefing.md", "post-run.md"):
            (tmp_path / "playbooks" / name).write_text("x")
    return prompts.PromptEnv(workspace=tmp_path, zone=ZONE, language="de", transport="telegram")


def test_chat_prompt(tmp_path: Path) -> None:
    e = env(tmp_path, playbooks=True)
    prompt = prompts.chat_prompt(e, [prompts.PromptMessage(ts=NOW, kind="text", text="Wie war's?")])
    assert prompt == (
        "The athlete wrote in the chat (telegram, Wed 2030-01-02 08:15, Europe/Berlin):\n\n"
        "Wie war's?\n\n"
        "Follow playbooks/chat.md.\n" + FOOTER
    )


def test_briefing_and_debrief_prompts(tmp_path: Path) -> None:
    e = env(tmp_path, playbooks=True)
    briefing = prompts.briefing_section(e, NOW.date(), synced=True, fallback_time="10:00")
    assert prompts.proactive_prompt(e, [briefing], NOW) == (
        "Nobody wrote: the bridge started this run on its own (Wed 2030-01-02 08:15,"
        " Europe/Berlin).\n\n"
        "Trigger: morning briefing for Wed 2030-01-02. Last night's sleep (wake date 2030-01-02)"
        " has arrived in the database.\n\n"
        "Follow playbooks/morning-briefing.md.\n" + FOOTER + "\n"
        "If there is nothing worth sending, reply with exactly NO_REPLY."
    )
    activity = {
        "id": 77,
        "type": "trail_running",
        "start_local": datetime(2030, 1, 2, 7, 0),
        "distance_m": 12345,
    }
    debrief = prompts.debrief_section(e, [activity])
    text = prompts.proactive_prompt(e, [debrief], NOW)
    assert (
        "Trigger: post-run debrief. New run uploaded: activity 77 (trail_running, started"
        " 2030-01-02 07:00 local, 12.3 km).\n\nFollow playbooks/post-run.md." in text
    )


def test_language_names() -> None:
    assert prompts.language_name("de") == "German"
    assert prompts.language_name("de-AT") == "German"
    assert prompts.language_name("tlh") == "tlh"
    assert lang_code("de_CH") == "de" and lang_code("fr") == "en"


def test_every_text_exists_in_both_languages() -> None:
    assert set(TEXTS["en"]) == set(TEXTS["de"])
    assert t("xx", "new") == TEXTS["en"]["new"]


def test_numbers_lines_without_optional_values() -> None:
    wellness = {
        "sleep_synced_at": NOW,
        "sleep_s": 25200,
        "sleep_score": None,
        "hrv_night_avg": 40.4,
        "hrv_balanced_low": 45,
        "hrv_balanced_high": 60,
        "resting_hr": None,
    }
    runs = [{"distance_km": 5.0, "time_s": 1500, "pace": None, "avg_hr": None}]
    lines = numbers_lines("en", date(2030, 1, 2), wellness, runs)
    assert lines[1:] == [
        "Sleep: 7:00 h",
        "HRV: 40 ms (balanced range 45–60: below)",
        "Yesterday: 5.0 km in 25:00",
    ]
