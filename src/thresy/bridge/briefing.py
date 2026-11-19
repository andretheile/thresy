"""The numbers-only morning briefing, built straight from the database.

Sent when the agent cannot write the briefing (usage limit reached, login expired, any other
failure), so the athlete still gets last night's data: sleep duration and score, HRV against
its balanced range, resting heart rate and yesterday's run(s).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .repo import Repo
from .texts import t


def _duration(seconds: float) -> str:
    total = round(seconds / 60)
    return f"{total // 60}:{total % 60:02d} h"


def _clock(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _num(value: Any) -> str:
    return f"{round(float(value))}"


def numbers_lines(
    language: str, day: date, wellness: dict[str, Any] | None, runs: list[dict[str, Any]]
) -> list[str]:
    lines = [f"**{t(language, 'numbers_title')}** · {day.isoformat()}"]
    synced = wellness is not None and wellness.get("sleep_synced_at") is not None
    if synced and wellness and wellness.get("sleep_s"):
        score = wellness.get("sleep_score")
        lines.append(
            t(
                language,
                "numbers_sleep",
                duration=_duration(wellness["sleep_s"]),
                score=t(language, "numbers_score", score=score) if score is not None else "",
            )
        )
    elif not synced:
        lines.append(t(language, "numbers_not_synced"))
    if synced and wellness and wellness.get("hrv_night_avg") is not None:
        value = float(wellness["hrv_night_avg"])
        low, high = wellness.get("hrv_balanced_low"), wellness.get("hrv_balanced_high")
        where = ""
        if low is not None and high is not None:
            key = (
                "numbers_hrv_below"
                if value < low
                else "numbers_hrv_above"
                if value > high
                else "numbers_hrv_within"
            )
            where = t(
                language,
                "numbers_hrv_range",
                low=_num(low),
                high=_num(high),
                where=t(language, key),
            )
        lines.append(t(language, "numbers_hrv", value=_num(value), range=where))
    if synced and wellness and wellness.get("resting_hr") is not None:
        lines.append(t(language, "numbers_rhr", value=_num(wellness["resting_hr"])))
    if runs:
        for run in runs:
            distance = float(run.get("distance_km") or 0)
            lines.append(
                t(
                    language,
                    "numbers_run",
                    distance=f"{distance:.1f}",
                    duration=_clock(run["time_s"]) if run.get("time_s") else "?",
                    pace=f" · {run['pace']}" if run.get("pace") else "",
                    hr=t(language, "numbers_hr", hr=_num(run["avg_hr"]))
                    if run.get("avg_hr")
                    else "",
                )
            )
    else:
        lines.append(t(language, "numbers_no_run"))
    return lines


async def numbers_briefing(repo: Repo, language: str, day: date) -> str:
    wellness = await repo.wellness(day)
    runs = await repo.runs_on(day - timedelta(days=1))
    return "\n".join(numbers_lines(language, day, wellness, runs))
