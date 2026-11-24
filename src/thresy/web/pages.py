"""What each page shows, computed from a :class:`thresy.web.source.Source`.

Each ``*_page`` function returns the template's context: plain values plus charts. Numbers
come from the source (the library functions of the ``coach`` commands); this module only
selects, combines and formats them.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from markupsafe import Markup

from thresy.analysis.data import Activity, pace_s_per_km
from thresy.logs import redact
from thresy.tools.plan import target_text
from thresy.web import charts, text
from thresy.web.i18n import Lang
from thresy.web.source import Source, monday_of, parse_utc

PHONE_W = 358
HALF_W = 520
FULL_W = 840

SYNC_STALE_H = 12
"""The sync counts as failing after this long without success (or 3 failures in a row)."""
BACKUP_OVERDUE_D = 8
"""A backup is overdue after this many days (a weekly schedule plus a day)."""


# -- shared pieces ---------------------------------------------------------------------------------


def run_time(a: Activity) -> float | None:
    """Moving time, else timer duration (as the ``runs`` view and ``coach analysis list``)."""
    return a.moving_s if a.moving_s else a.duration_s


def run_row(a: Activity, lang: Lang, today: date) -> dict[str, Any]:
    t = run_time(a)
    return {
        "id": a.id,
        "date": a.local_date,
        "date_text": lang.date(a.local_date, today=today),
        "weekday": lang.weekday(a.local_date, short=True),
        "day": a.local_date.day,
        "month": lang.month(a.local_date.month, short=True),
        "name": a.name or a.type,
        "km": lang.km(a.distance_m, 1 if (a.distance_m or 0) < 100_000 else 0),
        "time": lang.clock(t),
        "pace": lang.pace(pace_s_per_km(t, a.distance_m)),
        "hr": lang.int(a.avg_hr) if a.avg_hr else "–",
        "ascent": lang.int(a.elev_gain_m) if a.elev_gain_m is not None else "–",
        "start": a.start_local.strftime("%H:%M") if a.start_local else "",
    }


def sync_broken(state: dict[str, Any], now: datetime) -> bool:
    """The Garmin login needs renewing, or the sync keeps failing: no new data arrives."""
    sync, auth = state["sync_status"], state["garmin_auth"]
    if auth.get("status") not in (None, "ok"):
        return True
    last_ok = parse_utc(sync.get("last_success_at"))
    stale = last_ok is not None and now - last_ok > timedelta(hours=SYNC_STALE_H)
    return stale or int(sync.get("consecutive_failures") or 0) >= 3


def sync_line(source: Source, lang: Lang) -> dict[str, Any]:
    state = source.state()
    last = parse_utc(state["sync_status"].get("last_success_at"))
    return {
        "last": last,
        "ok": last is not None and not sync_broken(state, source.now()),
        "text": lang.t("synced_ago", ago=lang.ago(last, source.now()))
        if last
        else lang.t("never_synced"),
    }


def notices(source: Source, lang: Lang) -> list[dict[str, str]]:
    """What needs the owner's attention: Garmin login, a failing sync, the backup."""
    state = source.state()
    now = source.now()
    zone = source.zone
    out = []
    sync, auth, rate, backup = (
        state["sync_status"],
        state["garmin_auth"],
        state["garmin_rate_limit"],
        state["backup_status"],
    )

    def when(value: datetime | None) -> str:
        if value is None:
            return lang.t("never")
        local = value.astimezone(zone)
        return f"{lang.date(local.date(), today=now.astimezone(zone).date())} {local:%H:%M}"

    status = auth.get("status")
    if status and status != "ok":
        out.append(
            {
                "level": "attention",
                "title": lang.t("notice_auth_title"),
                "text": lang.t("notice_auth_text"),
            }
        )
    last_ok = parse_utc(sync.get("last_success_at"))
    failures = int(sync.get("consecutive_failures") or 0)
    stale = last_ok is not None and now - last_ok > timedelta(hours=SYNC_STALE_H)
    if (failures >= 3 or stale) and not (status and status != "ok"):
        out.append(
            {
                "level": "attention",
                "title": lang.t("notice_sync_title"),
                "text": lang.t("notice_sync_text", since=when(last_ok)),
            }
        )
    until = parse_utc(rate.get("until"))
    if until and until > now:
        local = until.astimezone(zone)
        out.append(
            {
                "level": "info",
                "title": lang.t("notice_rate_title"),
                "text": lang.t("notice_rate_text", time=f"{local:%H:%M}"),
            }
        )
    if backup:
        last_backup = parse_utc(backup.get("last_success_at"))
        if int(backup.get("failures_in_row") or 0) > 0:
            out.append(
                {
                    "level": "attention",
                    "title": lang.t("notice_backup_title"),
                    "text": lang.t("notice_backup_failing", since=when(last_backup)),
                }
            )
        elif last_backup is None or now - last_backup > timedelta(days=BACKUP_OVERDUE_D):
            out.append(
                {
                    "level": "attention",
                    "title": lang.t("notice_backup_title"),
                    "text": lang.t("notice_backup_overdue", since=when(last_backup)),
                }
            )
    return out


# -- overview --------------------------------------------------------------------------------------


MIN_WEEKS_SHOWN = 6
MIN_NIGHTS = 14
"""The recovery trends need at least this many nights in their 12 weeks."""
DESCRIPTION_CHARS = 180
"""A planned session's description is shown up to about this length (some three lines on a
phone); a longer one opens on request."""


def trim_weeks(weeks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the empty weeks before the first run (a new athlete's chart starts with their
    first week), keeping at least :data:`MIN_WEEKS_SHOWN` weeks."""
    first = next((i for i, w in enumerate(weeks) if w["runs"]), len(weeks))
    return weeks[min(first, max(len(weeks) - MIN_WEEKS_SHOWN, 0)) :]


def four_week_average(weeks: list[dict[str, Any]]) -> float | None:
    """Kilometres a week over the four complete weeks before this one, once there are four
    weeks since the first run (an average over weeks before it would mean nothing)."""
    full = weeks[-5:-1]
    first = next((i for i, w in enumerate(weeks) if w["runs"]), None)
    if len(full) < 4 or first is None or first > len(weeks) - 5:
        return None
    return sum(w["distance_km"] for w in full) / 4


def _night(source: Source, lang: Lang, today: date) -> dict[str, Any]:
    n = source.night(today)
    if not n.get("synced"):
        latest = n.get("latest_synced")
        out: dict[str, Any] = {"state": "pending" if latest else "none"}
        if latest:
            prev = source.night(latest)
            out["latest"] = lang.t(
                "night_latest",
                date=lang.date(latest, today=today),
                dur=lang.hm((prev.get("sleep") or {}).get("sleep_s")),
            )
        return out
    sleep = n["sleep"]
    stages = {
        "deep": sleep.get("deep_s") or 0,
        "light": sleep.get("light_s") or 0,
        "rem": sleep.get("rem_s") or 0,
        "awake": sleep.get("awake_s") or 0,
    }
    hrv = n["garmin_hrv"]
    value, low, high = hrv.get("night_avg"), hrv.get("balanced_low"), hrv.get("balanced_high")
    if value is None:
        hrv_state = "none"
    elif low is not None and value < low:
        hrv_state = "below"
    elif high is not None and value > high:
        hrv_state = "above"
    else:
        hrv_state = "in"
    # resting heart rate against the previous four weeks (10th to 90th percentile)
    trend = source.wellness(today - timedelta(days=1), 28)
    rhr_hist = [d["resting_hr"] for d in trend["daily"] if d["resting_hr"] is not None]
    rhr = n.get("resting_hr")
    rhr_low = rhr_high = rhr_delta = None
    if len(rhr_hist) >= 7:
        rhr_low, rhr_high = (float(np.percentile(rhr_hist, q)) for q in (10, 90))
        rhr_low, rhr_high = math.floor(rhr_low), math.ceil(rhr_high)
        if rhr is not None:
            rhr_delta = rhr - float(np.median(rhr_hist))
    synced_at = n.get("sleep_synced_at")
    return {
        "state": "synced",
        "sleep_s": sleep.get("sleep_s"),
        "duration": lang.hm(sleep.get("sleep_s")),
        "hours": int((sleep.get("sleep_s") or 0) // 3600),
        "minutes": round(((sleep.get("sleep_s") or 0) % 3600) / 60),
        "score": sleep.get("score"),
        "window": lang.t(
            "slept_from_to",
            start=lang.time(sleep.get("start_local")),
            end=lang.time(sleep.get("end_local")),
        ),
        "stages": [
            {"key": k, "name": lang.t("stage_" + k), "dur": lang.hm_short(v)}
            for k, v in stages.items()
            if v
        ],
        "stage_bar": charts.pair(charts.stage_bar, PHONE_W, HALF_W, stages=stages, lang=lang),
        "hypnogram": charts.pair(
            charts.hypnogram, PHONE_W, HALF_W, levels=n.get("levels") or [], lang=lang
        )
        if n.get("levels")
        else None,
        "hrv": {
            "value": lang.int(value) if value is not None else "–",
            "state": hrv_state,
            "text": lang.t("hrv_" + hrv_state),
            "usual": lang.t("hrv_usual", low=lang.int(low), high=lang.int(high))
            if low is not None and high is not None
            else "",
            "strip": charts.range_strip(
                value, low, high, lang, w=150, unit="ms", label=lang.t("hrv")
            ),
        },
        "rhr": {
            "value": lang.int(rhr) if rhr is not None else "–",
            "delta": (
                lang.t("vs_usual", delta=f"{lang.signed(rhr_delta, 0)} {lang.t('unit_bpm')}")
                if rhr_delta is not None and round(rhr_delta) != 0
                else lang.t("as_usual")
                if rhr_delta is not None
                else ""
            ),
            "usual": (
                f"{lang.int(rhr_low)}–{lang.int(rhr_high)} {lang.t('unit_bpm')}"
                if rhr_low is not None
                else ""
            ),
            "strip": charts.range_strip(
                rhr,
                rhr_low,
                rhr_high,
                lang,
                w=150,
                unit=lang.t("unit_bpm"),
                hue="accent",
                label=lang.t("resting_hr"),
            ),
        },
        "synced_at": lang.t("night_synced_at", time=lang.time(synced_at.astimezone(source.zone)))
        if synced_at
        else "",
    }


def _workout(w: dict[str, Any], lang: Lang, today: date) -> dict[str, Any]:
    status = w["status"]
    if status == "planned":
        status = "missed" if w["date"] < today else "upcoming" if w["date"] > today else "planned"
    target = w.get("target") or {}
    bits = []
    if target.get("distance_km") is not None:
        km = float(target["distance_km"])
        bits.append(f"{lang.num(km, 1 if km % 1 else 0)} km")
    if target.get("duration_min") is not None:
        bits.append(f"{lang.num(float(target['duration_min']), 0)} min")
    if target.get("pace"):
        pace = str(target["pace"]).replace("-", "–")
        bits.append(pace + (" /km" if all(c.isdigit() or c in ":–- ." for c in pace) else ""))
    if target.get("hr"):
        bits.append(f"{lang.t('col_hr')} {target['hr']}")
    if target.get("ascent_m") is not None:
        bits.append(f"+{lang.int(float(target['ascent_m']))} m")
    description = w.get("description") or ""
    # a description can run to a thousand characters (conditions, pacing, shoes): a few
    # lines of it, the rest behind a control
    desc_short, desc_more = text.excerpt(description, DESCRIPTION_CHARS)
    return {
        "id": w["id"],
        "title": w["title"],
        "kind": w.get("kind"),
        "is_rest": w.get("kind") == "rest",
        "description": description,
        "desc_short": desc_short,
        "desc_more": desc_more,
        "desc_full": Markup(text.render_markdown(description)) if description else "",
        "target": bits,
        "target_text": target_text(target),
        "status": status,
        "status_text": lang.t("status_" + status),
        "note": target.get("note"),
        "activity_id": w.get("activity_id"),
    }


def _week(source: Source, lang: Lang, today: date) -> dict[str, Any]:
    view = source.plan_week(monday_of(today), 1, today)
    days = []
    for d in view["days"]:
        workouts = [_workout(w, lang, today) for w in d["workouts"]]
        runs = [a for a in d["activities"] if a["is_run"]]
        planned_km = sum(
            float(w["target"]["distance_km"])
            for w in d["workouts"]
            if w["status"] != "moved"
            and isinstance((w.get("target") or {}).get("distance_km"), int | float)
        )
        run_km = sum((a["distance_m"] or 0) for a in runs) / 1000
        days.append(
            {
                "date": d["date"],
                "weekday": lang.weekday(d["date"], short=True),
                "weekday_long": lang.weekday(d["date"]),
                "day": d["date"].day,
                "is_today": d["date"] == today,
                "is_past": d["date"] < today,
                "workouts": workouts,
                "runs": [
                    {
                        "id": a["id"],
                        "name": a["name"],
                        "km": lang.km(a["distance_m"]),
                        "pace": lang.pace(a.get("pace_s_per_km")),
                    }
                    for a in runs
                ],
                "planned_km": planned_km,
                "run_km": run_km,
            }
        )
    t = view["totals"]
    has_plan = any(d["workouts"] for d in days)
    has_any = has_plan or any(d["runs"] for d in days)
    return {
        "days": days,
        "has_plan": has_plan,
        "has_any": has_any,
        "run_km": lang.num(t["run_km"], 1),
        "planned_km": lang.num(t["planned_km"], 0),
        "progress": min(t["run_km"] / t["planned_km"], 1) if t["planned_km"] else None,
        "headline": lang.t(
            "week_km", done=lang.num(t["run_km"], 1), planned=lang.num(t["planned_km"], 0)
        )
        if t["planned_km"]
        else lang.t("week_km_noplan", done=lang.num(t["run_km"], 1)),
        "runs": lang.tn("week_runs", t["runs"]),
        "strip": charts.pair(charts.week_strip, PHONE_W, HALF_W, days=days, lang=lang, today=today),
        "meter": Markup(_meter(t["run_km"], t["planned_km"])) if t["planned_km"] else None,
    }


def _meter(done: float, planned: float, w: float = 200) -> str:
    frac = min(done / planned, 1) if planned else 0
    svg = charts.Svg(w, 8, label="", cls="c-meter")
    svg.rect(0, 0, w, 8, "meter-track", rx=4)
    if frac > 0:
        svg.rect(0, 0, max(8, frac * w), 8, "meter-fill", rx=4)
    return svg.render()


def _coach(source: Source, lang: Lang, coach_name: str, today: date) -> dict[str, Any] | None:
    msg = source.coach_message()
    if not msg or not msg.get("text"):
        return None
    trigger = msg.get("trigger") or ""
    kind = (
        "morning_briefing"
        if "morning_briefing" in trigger
        else "post_run"
        if "post_run" in trigger
        else "other"
    )
    ts = parse_utc(msg["ts"])
    local = ts.astimezone(source.zone) if ts else None
    short, more = text.excerpt(msg["text"])
    when = ""
    if local is not None:
        when = (
            f"{lang.rel_day(local.date(), today)}, {local:%H:%M}"
            if abs((local.date() - today).days) <= 1
            else f"{lang.date(local.date(), today=today)}, {local:%H:%M}"
        )
    return {
        "kind": lang.t("msg_" + kind),
        "who": lang.t("coach_said", coach=coach_name),
        "when": when,
        "short": short,
        "more": more,
        "full": Markup(text.render_markdown(msg["text"])),
    }


def _next_race(races: list[dict[str, Any]], lang: Lang, today: date) -> dict[str, Any] | None:
    upcoming = [r for r in races if r["date"] >= today and r["status"] in ("planned", "maybe")]
    if not upcoming:
        return None
    r = min(upcoming, key=lambda r: (r["date"], r["id"]))
    return _race(r, lang, today)


def _race(r: dict[str, Any], lang: Lang, today: date) -> dict[str, Any]:
    days = (r["date"] - today).days
    result = r.get("result") or {}
    time_s = result.get("time_s")
    pace = None
    if time_s and r.get("distance_km"):
        pace = lang.pace(time_s / r["distance_km"])
    return {
        "id": r["id"],
        "name": r["name"],
        "date": r["date"],
        "date_text": lang.date(r["date"], today=today),
        "date_long": lang.date_long(r["date"])
        + ("" if r["date"].year == today.year else f" {r['date'].year}"),
        "days": days,
        "days_text": lang.tn("days_to_go", days),
        "weeks": days // 7,
        "distance": f"{lang.num(r['distance_km'], 1 if r['distance_km'] % 1 else 0)} km"
        if r.get("distance_km")
        else "",
        "elev": f"+{lang.int(r['elev_m'])} m" if r.get("elev_m") else "",
        "start": r["start_time_local"].strftime("%H:%M") if r.get("start_time_local") else "",
        "priority": lang.t("priority", p=r["priority"]) if r.get("priority") else "",
        "priority_key": r.get("priority") or "",
        "goal": r.get("goal") or "",
        "status": r["status"],
        "status_text": lang.t("race_status_" + r["status"])
        if r["status"] in ("maybe", "dnf", "dns", "planned")
        else "",
        "location": r.get("location") or "",
        "time": result.get("time") or (lang.clock(time_s) if time_s else ""),
        "time_source": result.get("source") or "",
        "pace": pace,
        "place": result.get("place") or "",
        "note": result.get("note") or "",
        "activity_id": r.get("activity_id"),
        "ring": charts.countdown_ring(days, 112) if days >= 0 else None,
    }


def overview_page(source: Source, lang: Lang, *, coach_name: str) -> dict[str, Any]:
    today = source.today()
    weeks = source.weeks(today, 12)
    recent = source.runs(limit=5)
    races = source.races(today)
    wk = trim_weeks(weeks["weeks"])
    avg = four_week_average(wk)
    week = _week(source, lang, today)
    today_day = next(d for d in week["days"] if d["is_today"])
    return {
        "page": "today",
        "today": today,
        "date_long": lang.date_long(today),
        "notices": notices(source, lang),
        "night": {
            **_night(source, lang, today),
            "blocked": sync_broken(source.state(), source.now()),
        },
        "today_plan": today_day["workouts"],
        "today_runs": today_day["runs"],
        "has_plan": week["has_plan"],
        "coach": _coach(source, lang, coach_name, today),
        "week": week,
        "recent": [run_row(a, lang, today) for a in recent],
        "race": _next_race(races, lang, today),
        "volume": {
            "chart": charts.pair(
                charts.weekly_volume, PHONE_W, HALF_W, weeks=wk, lang=lang, today=today
            ),
            "avg4": lang.t("avg_4w", km=lang.num(avg, 0)) if avg is not None else "",
            "has_data": any(w["distance_km"] for w in wk),
        },
        "sync": sync_line(source, lang),
    }


# -- runs ------------------------------------------------------------------------------------------


def runs_page(source: Source, lang: Lang, year: int | None) -> dict[str, Any]:
    today = source.today()
    years = source.run_years()
    available = [y["year"] for y in years]
    if year not in available:
        year = available[0] if available else today.year
    runs = source.runs(since=date(year, 1, 1), until=date(year, 12, 31))
    months: list[dict[str, Any]] = []
    for a in runs:
        key = (a.local_date.year, a.local_date.month)
        if not months or months[-1]["key"] != key:
            months.append({"key": key, "name": lang.month(key[1]), "runs": [], "m": 0.0})
        months[-1]["runs"].append(run_row(a, lang, today))
        months[-1]["m"] += a.distance_m or 0
    for m in months:
        m["total"] = f"{lang.km(m['m'], 0)} km"
    this = next((y for y in years if y["year"] == year), None)
    return {
        "page": "runs",
        "year": year,
        "years": [{"year": y["year"], "current": y["year"] == year} for y in years],
        "title": lang.t("runs_in_year", year=year),
        "total": lang.tn("runs_year_total", this["runs"], km=lang.km(this["distance_m"], 0))
        if this
        else "",
        "months": months,
        "sync": sync_line(source, lang),
    }


# -- one run ---------------------------------------------------------------------------------------


SMOOTH_S = 30
"""Speed is averaged over this many seconds before binning, so the pace line shows the
effort rather than GPS noise."""


EVEN_S_PER_KM = 2.0
"""Halves whose paces differ by less than this count as even."""


def halves_sentence(lang: Lang, halves: dict[str, Any]) -> str:
    """The first half against the second, from ``activity.halves``: faster, slower or even,
    with the difference and its unit."""
    diff = halves["pace_diff_s_per_km"]
    if diff <= -EVEN_S_PER_KM:
        return lang.t("negative_split", diff=lang.pace_delta(diff))
    if diff >= EVEN_S_PER_KM:
        return lang.t("positive_split", diff=lang.pace_delta(diff))
    return lang.t("even_split")


def _downsample(df: pd.DataFrame, n: int = 240) -> pd.DataFrame:
    """Mean of each of ``n`` equal distance bins (moving samples only for the pace)."""
    if df.empty or df["dist"].iloc[-1] <= 0:
        return pd.DataFrame()
    total = float(df["dist"].iloc[-1])
    bins = np.minimum((df["dist"].to_numpy() / total * n).astype(int), n - 1)
    has_speed = "speed" in df
    moving = (
        (~df["paused"]) & (df["speed"].fillna(0) >= 1.0)
        if has_speed
        else pd.Series(True, index=df.index)
    )
    speed = (
        df["speed"].where(moving).rolling(SMOOTH_S, center=True, min_periods=3).mean()
        if has_speed
        else np.nan
    )
    frame = pd.DataFrame(
        {
            "bin": bins,
            "dist": df["dist"],
            "alt": df.get("alt", np.nan),
            "hr": df.get("hr", np.nan),
            "speed": speed,
            "lat": df.get("lat", np.nan),
            "lon": df.get("lon", np.nan),
        }
    )
    frame.loc[~moving, "speed"] = np.nan
    out = frame.groupby("bin").agg(
        dist=("dist", "max"),
        alt=("alt", "mean"),
        hr=("hr", "mean"),
        speed=("speed", "mean"),
        lat=("lat", "mean"),
        lon=("lon", "mean"),
    )
    out["pace"] = 1000 / out["speed"].where(out["speed"] > 0.8)
    return out.reset_index(drop=True)


def run_page(source: Source, lang: Lang, activity_id: int) -> dict[str, Any] | None:
    from thresy.analysis import activity as act
    from thresy.analysis import intervals

    a = source.activity(activity_id)
    if a is None:
        return None
    today = source.today()
    extra = source.activity_extra(activity_id)
    t = run_time(a)
    pace = pace_s_per_km(t, a.distance_m)
    out: dict[str, Any] = {
        "page": "runs",
        "id": a.id,
        "name": a.name or a.type,
        "is_run": a.is_run,
        "date_long": lang.date_long(a.local_date)
        + ("" if a.local_date.year == today.year else f" {a.local_date.year}"),
        "start": a.start_local.strftime("%H:%M") if a.start_local else "",
        "year": a.local_date.year,
        "stats": [],
        "sync": sync_line(source, lang),
    }
    hero = [
        ("distance", lang.km(a.distance_m, 2), "km"),
        ("moving_time", lang.clock(t), ""),
        ("pace", lang.pace(pace), "/km"),
    ]
    out["hero"] = [{"label": lang.t(k), "value": v, "unit": u} for k, v, u in hero]
    stats = [
        ("avg_hr", lang.int(a.avg_hr) if a.avg_hr else None, lang.t("unit_bpm")),
        ("max_hr", lang.int(a.max_hr) if a.max_hr else None, lang.t("unit_bpm")),
        ("ascent", lang.int(a.elev_gain_m) if a.elev_gain_m is not None else None, "m"),
        ("cadence", lang.int(a.avg_cadence_spm) if a.avg_cadence_spm else None, "spm"),
        ("power", lang.int(a.avg_power_w) if a.avg_power_w else None, "W"),
        (
            "training_effect",
            lang.num(extra.get("aerobic_te"), 1) if extra.get("aerobic_te") else None,
            "",
        ),
    ]
    out["stats"] = [{"label": lang.t(k), "value": v, "unit": u} for k, v, u in stats if v]
    out["plan"] = [_workout(w, lang, today) for w in source.planned_for(activity_id)]
    out["efforts"] = [
        {
            "name": e["segment"],
            "time": lang.clock(e["duration_s"]),
            "pace": lang.pace(e.get("pace_s_per_km")),
            "hr": lang.int(e["avg_hr"]) if e.get("avg_hr") else "–",
            "gain": f"+{lang.int(e['gain_m'])} m" if e.get("gain_m") is not None else "",
        }
        for e in source.activity_efforts(activity_id)
    ]
    df = source.records(activity_id) if a.has_records else pd.DataFrame()
    out["has_records"] = not df.empty
    if df.empty:
        return out
    summary = act.summarize(a, df)
    ds = _downsample(df)
    x = ds["dist"].div(1000).tolist()
    profile_id = f"run-{a.id}"
    pace_vals = ds["pace"].tolist()
    hr_vals = ds["hr"].tolist()

    def domain(values: list[float]) -> tuple[float, float] | None:
        """The middle 98 % of the values: a stop or a sensor glitch must not flatten the
        rest of the line (the line is clipped at the edges instead)."""
        finite = [v for v in values if v is not None and not math.isnan(v)]
        if not finite:
            return None
        return float(np.percentile(finite, 1)), float(np.percentile(finite, 99))

    pace_domain = domain(pace_vals)
    out["profiles"] = [
        {
            "title": lang.t("profile_pace"),
            "unit": "/km",
            "chart": charts.pair(
                charts.profile,
                PHONE_W,
                FULL_W,
                x_km=x,
                values=pace_vals,
                lang=lang,
                kind="pace",
                label=lang.t("profile_pace"),
                fmt=lang.pace,
                invert=True,
                domain=pace_domain,
                chart_id=profile_id,
                h=124,
            ),
        },
        {
            "title": lang.t("profile_hr"),
            "unit": lang.t("unit_bpm"),
            "chart": charts.pair(
                charts.profile,
                PHONE_W,
                FULL_W,
                x_km=x,
                values=hr_vals,
                lang=lang,
                kind="hr",
                label=lang.t("profile_hr"),
                fmt=lambda v: lang.int(v),
                domain=domain(hr_vals),
                chart_id=profile_id,
                h=112,
            ),
        },
        {
            "title": lang.t("profile_alt"),
            "unit": "m",
            "chart": charts.pair(
                charts.profile,
                PHONE_W,
                FULL_W,
                x_km=x,
                values=ds["alt"].tolist(),
                lang=lang,
                kind="alt",
                label=lang.t("profile_alt"),
                fmt=lambda v: lang.int(v),
                area=True,
                chart_id=profile_id,
                h=104,
            ),
        },
    ]
    out["profile_id"] = profile_id
    out["profile_data"] = {
        "x": [round(v, 3) for v in x],
        "pace": [lang.pace(p) if p == p else "–" for p in pace_vals],
        "hr": [lang.int(v) if v == v else "–" for v in ds["hr"].tolist()],
        "alt": [lang.int(v) if v == v else "–" for v in ds["alt"].tolist()],
        "labels": [lang.t("profile_pace"), lang.t("profile_hr"), lang.t("profile_alt")],
        "hint": lang.t("profile_hint"),
        "units": ["/km", lang.t("unit_bpm"), "m"],
    }
    # track with kilometre marks
    if "lat" in df and df["lat"].notna().sum() > 10:
        step = 1 if (a.distance_m or 0) <= 12_000 else 2 if (a.distance_m or 0) <= 26_000 else 5
        marks = []
        dist = df["dist"].to_numpy()
        for km in range(step, int((a.distance_m or 0) / 1000) + 1, step):
            if (a.distance_m or 0) - km * 1000 < 400:
                continue  # it would sit on the finish
            i = int(np.searchsorted(dist, km * 1000))
            if i < len(df) and not math.isnan(df["lat"].iloc[i]):
                marks.append((float(df["lat"].iloc[i]), float(df["lon"].iloc[i]), km))
        sub = df[["lat", "lon"]].dropna().iloc[:: max(1, len(df) // 900)]
        out["track"] = charts.pair(
            charts.track,
            320,
            380,
            lat=sub["lat"].tolist(),
            lon=sub["lon"].tolist(),
            lang=lang,
            h=300,
            km_marks=marks,
        )
    # splits against the run's average pace
    splits = summary["splits"]
    avg = (
        pace_s_per_km(sum(s["time_s"] for s in splits), sum(s["dist_m"] for s in splits))
        if splits
        else None
    )
    deltas = [s["pace_s_per_km"] - avg if s["pace_s_per_km"] and avg else None for s in splits]
    full = [d for s, d in zip(splits, deltas, strict=True) if d is not None and s["dist_m"] >= 900]
    scale = max([abs(d) for d in full] + [10.0])
    out["splits"] = [
        {
            "n": lang.num(s["end_m"] / 1000, 1) if s["dist_m"] < 990 else s["n"],
            "pace": lang.pace(s["pace_s_per_km"]),
            "delta": lang.signed_seconds(d) if d is not None else "",
            "faster": d is not None and d < 0,
            "bar": charts.split_bar(d, scale),
            "hr": lang.int(s["avg_hr"]) if s.get("avg_hr") else "–",
            "ascent": f"+{lang.int(s['ascent_m'])}" if s.get("ascent_m") is not None else "–",
            "partial": s["dist_m"] < 990,
        }
        for s, d in zip(splits, deltas, strict=True)
    ]
    out["avg_pace"] = lang.pace(avg)
    halves = summary.get("halves")
    if halves and halves.get("pace_diff_s_per_km") is not None:
        out["halves_text"] = halves_sentence(lang, halves)
        out["halves"] = [
            {
                "pace": lang.pace(halves[h]["pace_s_per_km"]),
                "hr": lang.int(halves[h]["avg_hr"]) if halves[h]["avg_hr"] else "–",
            }
            for h in ("first", "second")
        ]
    drift = summary.get("drift") or {}
    if drift.get("decoupling_pct") is not None:
        pct = drift["decoupling_pct"]
        # below about 5 % the pace was held without the heart rate creeping up
        # (docs/analysis.md); a clearly faster second half makes the number meaningless
        verdict = "drift_faster" if pct <= -5 else "drift_good" if pct < 5 else "drift_high"
        out["drift"] = {
            "text": lang.t(
                "drift_text", bpm=lang.signed(drift["drift_bpm"], 0), pct=lang.num(pct, 1)
            ),
            "verdict": lang.t(verdict),
            "good": verdict == "drift_good",
        }
    out["climbs"] = [
        {
            "km": f"{lang.num(c['start_km'], 1)}–{lang.num(c['end_km'], 1)}",
            "gain": f"+{lang.int(c['gain_m'])} m",
            "grade": f"{lang.num(c['grade_pct'], 1)} %",
            "time": lang.clock(c["time_s"]),
            "pace": lang.pace(c["pace_s_per_km"]),
            "hr": lang.int(c["avg_hr"]) if c.get("avg_hr") else "–",
        }
        for c in summary.get("climbs") or []
    ]
    # reps of a structured session
    try:
        laps = source.laps(activity_id, df["ts"].iloc[0] if "ts" in df else None)
        iv = intervals.analyze(a, df, laps)
    except Exception:  # detection is best effort; the page works without it
        iv = {"reps": []}
    reps = iv.get("reps") or []
    if len(reps) >= 2:
        s = iv.get("summary") or {}
        out["reps"] = {
            "summary": lang.t("reps_summary", n=len(reps), pace=lang.pace(s.get("pace_s_per_km"))),
            "rows": [
                {
                    "n": r["n"],
                    "dist": f"{lang.int(r['dist_m'])} m" if r.get("dist_m") else "–",
                    "time": lang.clock(r.get("time_s")),
                    "pace": lang.pace(r.get("pace_s_per_km")),
                    "hr": lang.int(r.get("avg_hr")) if r.get("avg_hr") else "–",
                }
                for r in reps
            ],
        }
    return out


# -- trends ----------------------------------------------------------------------------------------


def _years(source: Source, today: date, n_years: int = 4) -> list[dict[str, Any]]:
    since = date(today.year - n_years + 1, 1, 1)
    by_day = source.daily_distance(since, today)
    out = []
    for year in range(today.year - n_years + 1, today.year + 1):
        days = [(d, m) for d, m in by_day if d.year == year]
        if not days:
            continue
        cum = 0.0
        pts = [(1, 0.0)]
        for d, m in days:
            doy = d.timetuple().tm_yday
            if doy - 1 > pts[-1][0]:
                pts.append((doy - 1, cum))  # flat until the day of the run
            cum += m / 1000
            pts.append((doy, cum))
        end = (
            today.timetuple().tm_yday
            if year == today.year
            else (date(year, 12, 31).timetuple().tm_yday)
        )
        if pts[-1][0] < end:
            pts.append((end, cum))
        same_day = charts._value_at(pts, today.timetuple().tm_yday) or 0.0
        out.append({"year": year, "points": pts, "total": cum, "same_day": same_day})
    return out


def trends_page(source: Source, lang: Lang) -> dict[str, Any]:
    today = source.today()
    weeks = source.weeks(today, 26)
    wk = trim_weeks(weeks["weeks"])
    ytd, prev = weeks["ytd"], weeks["ytd_last_year"]
    years = _years(source, today)
    n_days = 84
    trend = source.wellness(today, n_days)
    daily = trend["daily"]
    days = [d["wake_date"] for d in daily]

    def avg7(key: str, scale: float = 1.0) -> list[float | None]:
        out = []
        for i in range(len(daily)):
            window = [
                daily[j][key] for j in range(max(0, i - 6), i + 1) if daily[j][key] is not None
            ]
            out.append(sum(window) / len(window) / scale if len(window) >= 4 else None)
        return out

    hrv = [d["hrv_night_avg"] for d in daily]
    rhr = [d["resting_hr"] for d in daily]
    sleep_h = [d["sleep_s"] / 3600 if d["sleep_s"] else None for d in daily]
    rhr_vals = [v for v in rhr if v is not None]
    rhr_band = None
    if len(rhr_vals) >= 7:
        lo, hi = float(np.percentile(rhr_vals, 10)), float(np.percentile(rhr_vals, 90))
        rhr_band = [(lo, hi)] * len(daily)
    # a band and a 7-day average say nothing over a handful of nights
    has_wellness = sum(1 for v in sleep_h if v is not None) >= MIN_NIGHTS
    avg = four_week_average(wk)
    summary = trend["summary"]
    weeks_table = [
        {
            "week": lang.t("week_no", n=w["week"]),
            "from": lang.date(w["from"], today=today, weekday=False),
            "runs": w["runs"],
            "km": lang.num(w["distance_km"], 1),
            "long": lang.num(w["longest_km"], 1) if w["longest_km"] else "–",
        }
        for w in reversed(wk)
    ]
    return {
        "page": "trends",
        "volume": {
            "chart": charts.pair(
                charts.weekly_volume, PHONE_W, FULL_W, weeks=wk, lang=lang, today=today, h=210
            ),
            "this_week": lang.num(wk[-1]["distance_km"], 1) if wk else "0",
            "avg4": lang.num(avg, 0) if avg is not None else None,
            "runs": lang.tn("week_runs", sum(w["runs"] for w in wk)),
            "weeks_shown": len(wk),
            "table": weeks_table,
            "has_data": any(w["distance_km"] for w in wk),
        },
        "ytd": {
            "km": lang.num(ytd["distance_km"], 0),
            "runs": lang.tn("week_runs", ytd["runs"]),
            "delta": lang.t(
                "ytd_vs", delta=lang.signed(ytd["distance_km"] - prev["distance_km"], 0) + " km"
            )
            if prev["runs"]
            else lang.t("ytd_first"),
            "up": ytd["distance_km"] >= prev["distance_km"],
            "chart": charts.pair(
                charts.year_lines, PHONE_W, FULL_W, years=years, lang=lang, today=today
            )
            if years
            else None,
            "table": [
                {
                    "year": y["year"],
                    "same_day": lang.num(y["same_day"], 0),
                    "km": lang.num(y["total"], 0) if y["year"] != today.year else "–",
                }
                for y in sorted(years, key=lambda y: -y["year"])
            ],
            "same_day_head": lang.t("col_same_day", date=lang.day_month(today)),
            "year": today.year,
        },
        "recovery": {
            "has_data": has_wellness,
            "sub": lang.t("recovery_sub", n=n_days // 7),
            "hrv": charts.pair(
                charts.daily_range,
                PHONE_W,
                FULL_W,
                days=days,
                values=hrv,
                lang=lang,
                band=[(d["hrv_balanced_low"], d["hrv_balanced_high"]) for d in daily],
                avg=avg7("hrv_night_avg"),
                unit="ms",
                label=lang.t("hrv"),
            ),
            "hrv_now": lang.int(summary["last7"]["hrv"])
            if summary["last7"]["hrv"] is not None
            else "–",
            "hrv_below": lang.tn("nights_below", summary["nights_below_range"]),
            "rhr": charts.pair(
                charts.daily_range,
                PHONE_W,
                FULL_W,
                days=days,
                values=rhr,
                lang=lang,
                band=rhr_band,
                avg=avg7("resting_hr"),
                unit=lang.t("unit_bpm"),
                label=lang.t("resting_hr_long"),
                hue="accent",
            ),
            "rhr_now": lang.int(summary["last7"]["resting_hr"])
            if summary["last7"]["resting_hr"] is not None
            else "–",
            "sleep": charts.pair(
                charts.daily_range,
                PHONE_W,
                FULL_W,
                days=days,
                values=sleep_h,
                lang=lang,
                avg=avg7("sleep_s", 3600),
                unit="h",
                label=lang.t("sleep"),
                bars=True,
                fmt=lambda v: lang.hm_short(v * 3600),
            ),
            "sleep_now": lang.hm(summary["last7"]["sleep_s"])
            if summary["last7"]["sleep_s"]
            else "–",
            "table": [
                {
                    "date": lang.date(d["wake_date"], today=today),
                    "hrv": lang.int(d["hrv_night_avg"]) if d["hrv_night_avg"] is not None else "–",
                    "range": f"{lang.int(d['hrv_balanced_low'])}–{lang.int(d['hrv_balanced_high'])}"
                    if d["hrv_balanced_low"] is not None
                    else "–",
                    "rhr": lang.int(d["resting_hr"]) if d["resting_hr"] is not None else "–",
                    "sleep": lang.hm_short(d["sleep_s"]) if d["sleep_s"] else "–",
                }
                for d in reversed(daily)
            ],
        },
        "sync": sync_line(source, lang),
    }


# -- races -----------------------------------------------------------------------------------------


def races_page(source: Source, lang: Lang) -> dict[str, Any]:
    today = source.today()
    races = source.races(today)
    nxt = _next_race(races, lang, today)
    upcoming = [
        _race(r, lang, today)
        for r in races
        if r["date"] >= today and (nxt is None or r["id"] != nxt["id"])
    ]
    past = [
        _race(r, lang, today)
        for r in sorted(
            (r for r in races if r["date"] < today), key=lambda r: r["date"], reverse=True
        )
    ]
    return {
        "page": "races",
        "next": nxt,
        "upcoming": upcoming,
        "past": past,
        "sync": sync_line(source, lang),
    }


# -- segments --------------------------------------------------------------------------------------


def segments_page(source: Source, lang: Lang) -> dict[str, Any]:
    today = source.today()
    out = []
    for seg in source.segments():
        hist = source.segment_history(seg)
        efforts = hist["efforts"]
        best = hist.get("best")
        latest = efforts[-1] if efforts else None

        def effort(e: dict[str, Any] | None) -> dict[str, Any] | None:
            if e is None:
                return None
            return {
                "time": lang.clock(e["duration_s"]),
                "date": lang.date(e["start_local"].date(), today=today)
                if e.get("start_local")
                else "",
                "pace": lang.pace(e.get("pace_s_per_km")),
                "hr": lang.int(e["avg_hr"]) if e.get("avg_hr") else "–",
                "activity_id": e["activity_id"],
            }

        dists = [e["dist_m"] for e in efforts if e.get("dist_m")]
        gains = [e["gain_m"] for e in efforts if e.get("gain_m") is not None]
        out.append(
            {
                "key": seg.key,
                "name": seg.name,
                "count": lang.tn("efforts", hist["count"]),
                "distance": f"{lang.km(float(np.median(dists)), 2)} km" if dists else "",
                "gain": f"+{lang.int(float(np.median(gains)))} m" if gains else "",
                "best": effort(best),
                "latest": effort(latest),
                "latest_is_best": best is not None and latest is not None and best is latest,
                "delta": lang.clock(latest["duration_s"] - best["duration_s"])
                if best and latest and latest is not best
                else "",
                "chart": charts.pair(
                    charts.segment_dots,
                    PHONE_W,
                    FULL_W,
                    efforts=efforts,
                    lang=lang,
                    best_id=(best["activity_id"], best["start_utc"]) if best else None,
                    latest_id=(latest["activity_id"], latest["start_utc"]) if latest else None,
                )
                if len(efforts) >= 2
                else None,
            }
        )
    return {"page": "segments", "segments": out, "sync": sync_line(source, lang)}


# -- the state of the data -------------------------------------------------------------------------


def data_page(source: Source, lang: Lang) -> dict[str, Any]:
    state = source.state()
    now = source.now()
    zone = source.zone
    today = source.today()

    def when(value: Any) -> tuple[str, str]:
        """(local date and time, how long ago)."""
        ts = parse_utc(value)
        if ts is None:
            return lang.t("never"), ""
        local = ts.astimezone(zone)
        return f"{lang.date(local.date(), today=today)}, {local:%H:%M}", lang.ago(ts, now)

    def row(label: str, value: str, note: str = "", *, bad: bool = False) -> dict[str, Any]:
        return {"label": label, "value": value, "note": note, "bad": bad}

    sync, auth, backup = state["sync_status"], state["garmin_auth"], state["backup_status"]
    failures = int(sync.get("consecutive_failures") or 0)
    login_ok = (auth.get("status") or "ok") == "ok"
    rows_sync = [
        row(lang.t("last_sync"), *when(sync.get("last_success_at"))),
        row(lang.t("last_attempt"), *when(sync.get("last_attempt_at"))),
        row(
            lang.t("garmin_login"),
            lang.t("login_ok") if login_ok else lang.t("login_needed"),
            bad=not login_ok,
        ),
    ]
    if failures:
        rows_sync.append(
            row(
                lang.t("last_error"),
                redact(str(sync.get("last_error") or "")),
                lang.tn("failures_in_row", failures),
                bad=True,
            )
        )
    rows_backup = []
    if backup:
        rows_backup.append(row(lang.t("last_backup"), *when(backup.get("last_success_at"))))
        check = backup.get("restore_check")
        if isinstance(check, dict):
            at, ago = when(check.get("at"))
            key = "restore_ok" if check.get("ok") else "restore_failed"
            rows_backup.append(
                row(lang.t("restore_check"), lang.t(key, when=at), ago, bad=not check.get("ok"))
            )
        if int(backup.get("failures_in_row") or 0):
            rows_backup.append(
                row(lang.t("last_error"), redact(str(backup.get("last_error") or "")), bad=True)
            )
    counts = source.counts()
    rows_counts = [
        row(lang.t("count_runs"), lang.int(counts["runs"])),
        row(lang.t("count_records"), lang.int(counts["with_records"])),
        row(lang.t("count_nights"), lang.int(counts["nights"])),
    ]
    if counts.get("first_run"):
        rows_counts.append(
            row(
                lang.t("count_span_label"),
                f"{lang.date_full(counts['first_run'])} – {lang.date_full(counts['last_run'])}",
            )
        )
    found = notices(source, lang)
    return {
        "page": "data",
        "notices": found,
        "all_good": not found,
        "sync_rows": rows_sync,
        "backup_rows": rows_backup,
        "count_rows": rows_counts,
        "sync": sync_line(source, lang),
        "now": now.astimezone(UTC),
    }
