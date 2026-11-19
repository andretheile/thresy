"""PNG figures for the analyses, in one style, with labels in ``COACH_LANGUAGE`` (en, de).

Style rules: one y-scale per panel (two measures go into stacked panels with a shared x-axis,
never a second y-axis); thin 2 px lines; solid hairline grids; categorical colours in a fixed
order (blue, orange, aqua; validated for colour-blind separation); an ordinal blue ramp for
ordered series such as repetitions; a legend whenever a panel has two or more series.

Every function returns the path of the written file, ``<COACH_WORKSPACE_DIR>/out/<name>.png``
by default. Names are deterministic, so plotting the same thing again overwrites the file.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Colours (light chart surface) ----------------------------------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BAND = "#f0efec"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")  # blue, orange, aqua: fixed order
RAMP = (
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
)
CRITICAL = "#d03b3b"

LABELS: dict[str, dict[str, str]] = {
    "en": {
        "distance": "Distance [km]",
        "time_min": "Time [min]",
        "pace": "Pace [min/km]",
        "hr": "Heart rate [bpm]",
        "altitude": "Altitude [m]",
        "cadence": "Cadence [spm]",
        "resp": "Breathing rate [/min]",
        "power": "Power [W]",
        "since_rep": "Time since rep start [s]",
        "rep": "Rep",
        "work": "work",
        "climb": "climb",
        "date": "Date",
        "duration_min": "Segment time [min]",
        "avg_hr": "Avg heart rate [bpm]",
        "hr_last30": "HR last 30 s",
        "best": "Best",
        "course_km": "Course distance [km]",
        "delta_min": "Behind (+) / ahead (−) [min]",
        "hrv": "HRV [ms]",
        "hrv_night": "Night average",
        "hrv_week": "7-day average",
        "balanced": "Balanced range",
        "rhr": "Resting HR [bpm]",
        "sleep_h": "Sleep [h]",
        "wake_date": "Wake date",
        "clock": "Local time",
        "stage": "Stage",
        "awake": "awake",
        "rem": "REM",
        "light": "light",
        "deep": "deep",
        "flagged": "flagged",
        "hr_vs_cad": "Heart rate and cadence [per min]",
    },
    "de": {
        "distance": "Strecke [km]",
        "time_min": "Zeit [min]",
        "pace": "Pace [min/km]",
        "hr": "Herzfrequenz [bpm]",
        "altitude": "Höhe [m]",
        "cadence": "Schrittfrequenz [spm]",
        "resp": "Atemfrequenz [/min]",
        "power": "Leistung [W]",
        "since_rep": "Zeit seit Intervallbeginn [s]",
        "rep": "Intervall",
        "work": "Belastung",
        "climb": "Anstieg",
        "date": "Datum",
        "duration_min": "Segmentzeit [min]",
        "avg_hr": "Ø Herzfrequenz [bpm]",
        "hr_last30": "HF letzte 30 s",
        "best": "Bestzeit",
        "course_km": "Streckenkilometer [km]",
        "delta_min": "Rückstand (+) / Vorsprung (−) [min]",
        "hrv": "HRV [ms]",
        "hrv_night": "Nachtschnitt",
        "hrv_week": "7-Tage-Schnitt",
        "balanced": "Normalbereich",
        "rhr": "Ruhepuls [bpm]",
        "sleep_h": "Schlaf [h]",
        "wake_date": "Aufwachtag",
        "clock": "Uhrzeit",
        "stage": "Phase",
        "awake": "wach",
        "rem": "REM",
        "light": "leicht",
        "deep": "tief",
        "flagged": "auffällig",
        "hr_vs_cad": "Herzfrequenz und Schrittfrequenz [pro min]",
    },
}


def labels(lang: str | None) -> dict[str, str]:
    return LABELS.get((lang or "en").lower()[:2], LABELS["en"])


def _plt() -> Any:
    import matplotlib

    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "axes.edgecolor": AXIS,
            "axes.linewidth": 0.8,
            "axes.labelcolor": INK_2,
            "axes.titlecolor": INK,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.labelsize": 10,
            "axes.grid": True,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "grid.linestyle": "-",
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelcolor": INK_2,
            "ytick.labelcolor": INK_2,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "lines.linewidth": 2.0,
            "font.family": "sans-serif",
            "text.color": INK,
        }
    )
    return plt


def default_dir() -> Path:
    from thresy.config import get_settings

    return get_settings().workspace_dir / "out"


def _save(fig: Any, name: str, out_dir: Path | None) -> Path:
    directory = Path(out_dir) if out_dir else default_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.png"
    fig.savefig(path, dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt

    plt.close(fig)
    return path


def _pace_axis(ax: Any, values: np.ndarray) -> None:
    """Pace in min/km on an inverted axis with M:SS tick labels."""
    from matplotlib.ticker import FuncFormatter

    finite = values[np.isfinite(values)]
    if finite.size:
        lo, hi = np.nanpercentile(finite, [1, 99])
        pad = max((hi - lo) * 0.1, 0.1)
        ax.set_ylim(hi + pad, max(lo - pad, 0))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}:{round(v % 1 * 60):02d}"))


def _pace_series(df: pd.DataFrame, window_s: int = 10) -> np.ndarray:
    speed = df["speed"].astype(float).rolling(window_s, center=True, min_periods=1).mean()
    pace = 1000.0 / speed / 60.0
    return pace.where(speed >= 1.0).to_numpy()


def _title(fig: Any, text: str) -> None:
    fig.suptitle(text, x=0.01, ha="left", fontsize=13, fontweight="bold", color=INK)


def _legend(ax: Any) -> None:
    handles, _ = ax.get_legend_handles_labels()
    if len(handles) >= 2:
        ax.legend(loc="best")


# Activity -------------------------------------------------------------------------------------


def plot_activity(
    title: str,
    df: pd.DataFrame,
    climbs: Sequence[dict],
    *,
    lang: str,
    name: str,
    out_dir: Path | None = None,
) -> Path:
    """Pace, heart rate, altitude and cadence over distance; climbs shaded."""
    plt = _plt()
    lab = labels(lang)
    panels = [
        ("pace", True),
        ("hr", "hr" in df and df["hr"].notna().any()),
        ("alt", "alt" in df and df["alt"].notna().any()),
        ("cad", "cad" in df and df["cad"].notna().any()),
    ]
    panels = [p for p, ok in panels if ok]
    fig, axes = plt.subplots(len(panels), 1, figsize=(11, 2.3 * len(panels) + 0.6), sharex=True)
    axes = np.atleast_1d(axes)
    km = df["dist"].to_numpy() / 1000
    for ax, panel in zip(axes, panels, strict=True):
        if panel == "pace":
            pace = _pace_series(df)
            ax.plot(km, pace, color=SERIES[0], linewidth=1.5)
            _pace_axis(ax, pace)
            ax.set_ylabel(lab["pace"])
        elif panel == "hr":
            ax.plot(km, df["hr"].rolling(5, center=True, min_periods=1).mean(), color=SERIES[0])
            ax.set_ylabel(lab["hr"])
        elif panel == "alt":
            alt = df["alt"].interpolate(limit_direction="both")
            ax.fill_between(km, alt, alt.min() - 5, color=RAMP[0], alpha=0.5, linewidth=0)
            ax.plot(km, alt, color=SERIES[0], linewidth=1.5)
            ax.set_ylabel(lab["altitude"])
        else:
            cad = df["cad"].where(df["cad"] > 0).rolling(10, center=True, min_periods=1).mean()
            ax.plot(km, cad, color=SERIES[0], linewidth=1.5)
            ax.set_ylabel(lab["cadence"])
        for c in climbs:
            if c.get("direction", "up") == "up":
                ax.axvspan(c["start_km"], c["end_km"], color=SERIES[1], alpha=0.10, linewidth=0)
    axes[-1].set_xlabel(lab["distance"])
    _title(fig, title)
    fig.tight_layout()
    return _save(fig, name, out_dir)


# Intervals ------------------------------------------------------------------------------------


def plot_intervals(
    title: str,
    df: pd.DataFrame,
    reps: Sequence[dict],
    *,
    lang: str,
    name: str,
    out_dir: Path | None = None,
) -> Path:
    """Top: pace and heart rate of the session with the reps shaded. Bottom: heart rate of each
    rep against time since the rep started (ordinal colours, light = first)."""
    plt = _plt()
    lab = labels(lang)
    fig = plt.figure(figsize=(11, 8.4))
    grid = fig.add_gridspec(3, 1, height_ratios=[1, 1, 1.3], hspace=0.45)
    ax_p = fig.add_subplot(grid[0])
    ax_h = fig.add_subplot(grid[1], sharex=ax_p)
    ax_o = fig.add_subplot(grid[2])
    if reps:
        lo = max(reps[0]["t_start"] - 300, 0)
        hi = reps[-1]["t_end"] + 300
    else:
        lo, hi = 0, float(df["t"].iloc[-1]) if len(df) else 1
    part = df[(df["t"] >= lo) & (df["t"] <= hi)]
    minutes = part["t"].to_numpy() / 60
    pace = _pace_series(part, 5)
    ax_p.plot(minutes, pace, color=SERIES[0], linewidth=1.5)
    _pace_axis(ax_p, pace)
    ax_p.set_ylabel(lab["pace"])
    if "hr" in part:
        ax_h.plot(minutes, part["hr"], color=SERIES[0], linewidth=1.5)
    ax_h.set_ylabel(lab["hr"])
    ax_h.set_xlabel(lab["time_min"])
    for r in reps:
        for ax in (ax_p, ax_h):
            ax.axvspan(r["t_start"] / 60, r["t_end"] / 60, color=SERIES[1], alpha=0.12, linewidth=0)
        ax_p.annotate(
            str(r["n"]),
            (r["t_start"] / 60, 1),
            xycoords=("data", "axes fraction"),
            xytext=(2, -12),
            textcoords="offset points",
            color=INK_2,
            fontsize=8,
        )
    n = len(reps)
    for k, r in enumerate(reps):
        w = df[(df["t"] >= r["t_start"]) & (df["t"] <= r["t_end"] + 60)]
        if "hr" not in w or w["hr"].isna().all():
            continue
        color = RAMP[round(k * (len(RAMP) - 1) / max(n - 1, 1))]
        ax_o.plot(
            w["t"] - r["t_start"],
            w["hr"],
            color=color,
            linewidth=1.6,
            label=f"{lab['rep']} {r['n']}",
        )
        ax_o.axvline(r["t_end"] - r["t_start"], color=color, linewidth=0.8, alpha=0.6)
    ax_o.set_xlabel(lab["since_rep"])
    ax_o.set_ylabel(lab["hr"])
    if 2 <= n <= 12:
        ax_o.legend(ncol=min(n, 6), loc="lower center")
    _title(fig, title)
    return _save(fig, name, out_dir)


def plot_sessions(
    title: str, sessions: Sequence[dict], *, lang: str, name: str, out_dir: Path | None = None
) -> Path:
    """Mean rep pace and heart rate (average and last 30 s) per session over time."""
    plt = _plt()
    lab = labels(lang)
    fig, (ax_p, ax_h) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True)
    dates = [pd.Timestamp(s["date"]) for s in sessions]
    pace = np.array([(s["pace_s_per_km"] or np.nan) / 60 for s in sessions], dtype=float)
    ax_p.plot(
        dates, pace, "o-", color=SERIES[0], markersize=7, markeredgecolor=SURFACE, markeredgewidth=2
    )
    _pace_axis(ax_p, pace)
    ax_p.set_ylabel(lab["pace"])
    for key, label, color in (
        ("avg_hr", lab["avg_hr"], SERIES[0]),
        ("hr_last30", lab["hr_last30"], SERIES[1]),
    ):
        values = [s.get(key) if s.get(key) is not None else np.nan for s in sessions]
        ax_h.plot(
            dates,
            values,
            "o-",
            color=color,
            label=label,
            markersize=7,
            markeredgecolor=SURFACE,
            markeredgewidth=2,
        )
    ax_h.set_ylabel(lab["hr"])
    ax_h.set_xlabel(lab["date"])
    _legend(ax_h)
    for d, s in zip(dates, sessions, strict=True):
        ax_p.annotate(
            f"{s['n_used']}×",
            (d, 0),
            xycoords=("data", "axes fraction"),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            color=MUTED,
            fontsize=8,
        )
    fig.autofmt_xdate()
    _title(fig, title)
    fig.tight_layout()
    return _save(fig, name, out_dir)


# Segments -------------------------------------------------------------------------------------


def plot_segment(
    title: str, efforts: Sequence[dict], *, lang: str, name: str, out_dir: Path | None = None
) -> Path:
    """Left: segment time over date, colour = average heart rate (sequential blue). Right:
    average heart rate against pace, colour = year (categorical, at most three recent years
    coloured; older ones grey)."""
    plt = _plt()
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.ticker import FuncFormatter

    lab = labels(lang)
    fig, (ax_t, ax_s) = plt.subplots(1, 2, figsize=(12, 5), gridspec_kw={"width_ratios": [1.4, 1]})
    dates = [pd.Timestamp(e["start_local"]) for e in efforts]
    minutes = np.array([e["duration_s"] / 60 for e in efforts])
    hr = np.array([e["avg_hr"] if e["avg_hr"] is not None else np.nan for e in efforts])
    cmap = LinearSegmentedColormap.from_list("seq", [RAMP[0], RAMP[-1]])
    sc = ax_t.scatter(dates, minutes, c=hr, cmap=cmap, s=60, edgecolors=SURFACE, linewidths=2)
    if np.isfinite(hr).any():
        bar = fig.colorbar(sc, ax=ax_t, pad=0.01)
        bar.set_label(lab["avg_hr"], color=INK_2)
        bar.outline.set_visible(False)
    if efforts:
        b = int(np.argmin(minutes))
        ax_t.annotate(
            f"{lab['best']} {int(minutes[b])}:{round(minutes[b] % 1 * 60):02d}",
            (dates[b], minutes[b]),
            xytext=(8, -12),
            textcoords="offset points",
            color=INK,
            fontsize=9,
        )
    ax_t.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}:{round(v % 1 * 60):02d}"))
    ax_t.set_ylabel(lab["duration_min"])
    ax_t.set_xlabel(lab["date"])
    fig.autofmt_xdate()
    years = sorted({d.year for d in dates})
    recent = years[-3:]
    for year in years:
        idx = [i for i, d in enumerate(dates) if d.year == year]
        color = SERIES[recent.index(year)] if year in recent else AXIS
        pace = [efforts[i]["pace_s_per_km"] / 60 for i in idx]
        ax_s.scatter(
            pace, hr[idx], s=60, color=color, edgecolors=SURFACE, linewidths=2, label=str(year)
        )
    if efforts:
        _pace_x(ax_s)
    ax_s.set_xlabel(lab["pace"])
    ax_s.set_ylabel(lab["avg_hr"])
    _legend(ax_s)
    _title(fig, title)
    fig.tight_layout()
    return _save(fig, name, out_dir)


def _pace_x(ax: Any) -> None:
    from matplotlib.ticker import FuncFormatter

    ax.invert_xaxis()
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}:{round(v % 1 * 60):02d}"))


# Route comparison -----------------------------------------------------------------------------


def plot_route(
    title: str,
    result: dict,
    label_a: str,
    label_b: str,
    *,
    lang: str,
    name: str,
    out_dir: Path | None = None,
) -> Path:
    """Course altitude, time difference along the course (B minus A), and heart rate of both."""
    plt = _plt()
    lab = labels(lang)
    fig, (ax_a, ax_d, ax_h) = plt.subplots(
        3, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [0.8, 1, 1]}
    )
    cum, alt = result["_profile"]
    if not np.isnan(alt).all():
        ax_a.fill_between(
            cum / 1000, alt, np.nanmin(alt) - 5, color=RAMP[0], alpha=0.5, linewidth=0
        )
        ax_a.plot(cum / 1000, alt, color=SERIES[0], linewidth=1.5)
    ax_a.set_ylabel(lab["altitude"])
    for c in result["climbs"]:
        for ax in (ax_a, ax_d, ax_h):
            ax.axvspan(c["from_km"], c["to_km"], color=SERIES[1], alpha=0.08, linewidth=0)
    curve = result["curve"]
    ax_d.plot([p["km"] for p in curve], [p["delta_s"] / 60 for p in curve], color=SERIES[0])
    ax_d.axhline(0, color=AXIS, linewidth=1)
    ax_d.set_ylabel(lab["delta_min"])
    (xa, ta), (xb, tb) = result["_tracks"]["a"], result["_tracks"]["b"]
    for key, (x, _), label, color in (
        ("a", (xa, ta), label_a, SERIES[0]),
        ("b", (xb, tb), label_b, SERIES[1]),
    ):
        hr = result.get(f"_hr_{key}")
        if hr is not None:
            ax_h.plot(x / 1000, hr, color=color, linewidth=1.5, label=label)
    ax_h.set_ylabel(lab["hr"])
    ax_h.set_xlabel(lab["course_km"])
    _legend(ax_h)
    lo, hi = result["common"]["from_km"], result["common"]["to_km"]
    ax_h.set_xlim(lo, hi)
    _title(fig, title)
    fig.tight_layout()
    return _save(fig, name, out_dir)


# Wellness -------------------------------------------------------------------------------------


def plot_wellness(
    title: str, daily: Sequence[dict], *, lang: str, name: str, out_dir: Path | None = None
) -> Path:
    """HRV (night and 7-day average) against the balanced range, resting HR, sleep hours."""
    plt = _plt()
    lab = labels(lang)
    fig, (ax_v, ax_r, ax_s) = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
    dates = [pd.Timestamp(d["wake_date"]) for d in daily]

    def col(key: str, scale: float = 1.0) -> np.ndarray:
        return np.array([d[key] / scale if d.get(key) is not None else np.nan for d in daily])

    low, high = col("hrv_balanced_low"), col("hrv_balanced_high")
    if np.isfinite(low).any():
        ax_v.fill_between(
            dates, low, high, color=BAND, step="mid", label=lab["balanced"], linewidth=0
        )
    ax_v.plot(
        dates, col("hrv_night_avg"), "o-", color=SERIES[0], label=lab["hrv_night"], markersize=5
    )
    ax_v.plot(dates, col("hrv_weekly_avg"), color=SERIES[1], label=lab["hrv_week"])
    ax_v.set_ylabel(lab["hrv"])
    _legend(ax_v)
    ax_r.plot(dates, col("resting_hr"), "o-", color=SERIES[0], markersize=5)
    ax_r.set_ylabel(lab["rhr"])
    ax_s.bar(dates, col("sleep_s", 3600), color=SERIES[0], width=0.7)
    ax_s.set_ylabel(lab["sleep_h"])
    ax_s.set_xlabel(lab["wake_date"])
    fig.autofmt_xdate()
    _title(fig, title)
    fig.tight_layout()
    return _save(fig, name, out_dir)


def plot_night(
    title: str, result: dict, *, lang: str, name: str, out_dir: Path | None = None
) -> Path:
    """Hypnogram (stages over the night) and the HRV readings, awake periods shaded."""
    plt = _plt()
    lab = labels(lang)
    fig, (ax_s, ax_v) = plt.subplots(
        2, 1, figsize=(11, 6), sharex=True, gridspec_kw={"height_ratios": [0.8, 1]}
    )
    order = {"deep": 0, "light": 1, "rem": 2, "awake": 3}
    xs: list[datetime] = []
    ys: list[int] = []
    for lv in result.get("levels", []):
        xs += [lv["start_local"], lv["end_local"]]
        ys += [order[lv["stage"]]] * 2
    if xs:
        ax_s.plot(xs, ys, color=SERIES[0], linewidth=1.5)
    ax_s.set_yticks(range(4), [lab["deep"], lab["light"], lab["rem"], lab["awake"]])
    ax_s.set_ylabel(lab["stage"])
    readings = result.get("readings", [])
    ax_v.plot(
        [r["local"] for r in readings],
        [r["value_ms"] for r in readings],
        "o-",
        color=SERIES[0],
        markersize=4,
        linewidth=1.5,
    )
    for a in result.get("awake_periods", []):
        for ax in (ax_s, ax_v):
            ax.axvspan(a["start_local"], a["end_local"], color=SERIES[1], alpha=0.15, linewidth=0)
    ax_v.set_ylabel(lab["hrv"])
    ax_v.set_xlabel(lab["clock"])
    import matplotlib.dates as mdates

    ax_v.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    _title(fig, title)
    fig.tight_layout()
    return _save(fig, name, out_dir)


# Sensor ---------------------------------------------------------------------------------------


def plot_sensor(
    title: str, df: pd.DataFrame, result: dict, *, lang: str, name: str, out_dir: Path | None = None
) -> Path:
    """Heart rate and cadence on one per-minute scale; flagged stretches shaded."""
    plt = _plt()
    lab = labels(lang)
    fig, ax = plt.subplots(figsize=(11, 4.5))
    minutes = df["t"].to_numpy() / 60
    if "cad" in df:
        cad = df["cad"].where(df["cad"] > 0)
        ax.plot(minutes, cad, color=SERIES[1], linewidth=1.0, label=lab["cadence"])
    ax.plot(minutes, df["hr"], color=SERIES[0], linewidth=1.5, label=lab["hr"])
    for item in result.get("_locks", []) + result.get("_spikes", []):
        ax.axvspan(
            item["t_start"] / 60 if "t_start" in item else item["t"] / 60,
            (item.get("t_end") or item["t"] + item["duration_s"]) / 60,
            color=CRITICAL,
            alpha=0.12,
            linewidth=0,
        )
    ax.set_xlabel(lab["time_min"])
    ax.set_ylabel(lab["hr_vs_cad"])
    _legend(ax)
    _title(fig, title)
    fig.tight_layout()
    return _save(fig, name, out_dir)
