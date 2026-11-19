"""Heart-rate plausibility: cadence lock, impossible jumps, spikes and dropouts.

A wrist sensor sometimes locks onto the arm swing and then reports the step cadence as heart
rate. Definitions (all thresholds are module constants):

* **Cadence lock.** A sample is *cadence-like* when cadence >= :data:`LOCK_MIN_CADENCE` and
  ``|hr - cadence| <= LOCK_TOL_BPM``. Over a centred window of :data:`LOCK_WINDOW_S` seconds the
  share of cadence-like samples is computed; stretches where it is at least
  :data:`LOCK_SHARE` and that last at least :data:`LOCK_MIN_S` are reported. A stretch is
  ``high`` confidence when heart rate follows cadence (Pearson r >= 0.6), matches it tightly
  (mean ``|hr - cadence|`` <= 1.5 bpm), or jumps by at least :data:`JUMP_BPM` within 15 s at
  either edge; otherwise ``possible`` (a hard effort can put heart rate and cadence close
  together by coincidence).
* **Jump.** Heart rate changes by at least :data:`JUMP_BPM` between two samples at most 3 s
  apart (a real heart cannot do that).
* **Spike.** Heart rate at least :data:`SPIKE_BPM` above its centred 61 s median for at most
  :data:`SPIKE_MAX_S` seconds (a short excursion such as a false maximum).
* **Dropout.** No heart rate for at least :data:`DROPOUT_S` seconds while moving.
* **Level shift.** The median heart rate over :data:`SHIFT_WINDOW_S` seconds after a moment
  differs by at least :data:`SHIFT_BPM` from the median over the same time before it (the
  10 s on either side left out, so a dropout in between does not hide it), while the effort
  (median power, else speed) changed by at most :data:`SHIFT_EFFORT_TOL`. A heart cannot
  settle that far from where it was at the same effort within a minute or two, so one side is
  wrong. For both sides the share of samples within 5 bpm of the cadence over 10 minutes is
  reported: a side near 1 is probably the sensor reading the step rate. The first 5 minutes
  are not checked (heart rate still rising).

The verdict is ``unreliable`` with a high-confidence lock of 5 minutes or more, a level
shift, or more than 15 % of the moving time flagged; ``suspect`` when anything else is
reported; ``none`` without heart rate; else ``ok``.
``avg_hr_clean`` and ``max_hr_clean`` leave flagged samples out (high-confidence locks and
spikes; ``possible`` locks are reported but not removed).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from thresy.analysis.data import MOVING_SPEED_MPS, Activity, max_or_none, mean_or_none

LOCK_MIN_CADENCE = 100.0
LOCK_TOL_BPM = 3.0
LOCK_WINDOW_S = 120
LOCK_SHARE = 0.75
LOCK_MIN_S = 120.0
JUMP_BPM = 20.0
SPIKE_BPM = 20.0
SPIKE_MAX_S = 30.0
DROPOUT_S = 30.0
UNRELIABLE_LOCK_S = 300.0
UNRELIABLE_SHARE = 0.15
SHIFT_BPM = 30.0
SHIFT_SAMPLES = 30
SHIFT_GAP_SAMPLES = 10
SHIFT_EFFORT_TOL = 0.10
SHIFT_STEADY_SAMPLES = 240
SHIFT_STEADY_CV = 0.20
SHIFT_MAX_DROPOUT_S = 300.0
SHIFT_SKIP_S = 300.0
NEAR_CADENCE_BPM = 5.0


def _near_cadence_share(df: pd.DataFrame, lo: float, hi: float) -> float | None:
    if "cad" not in df:
        return None
    part = df[(df["t"] >= lo) & (df["t"] < hi) & df["hr"].notna() & (df["cad"] >= LOCK_MIN_CADENCE)]
    if len(part) < 30:
        return None
    return round(float((np.abs(part["hr"] - part["cad"]) <= NEAR_CADENCE_BPM).mean()), 2)


def level_shifts(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "hr" not in df or df["hr"].notna().sum() < 120:
        return []
    effort_col = None
    for col in ("power", "speed"):
        if col in df and df[col].notna().mean() >= 0.8:
            effort_col = col
            break
    if effort_col is None:
        return []
    valid = df[df["hr"].notna() & df[effort_col].notna()].reset_index(drop=True)
    t = valid["t"].to_numpy(dtype="float64")
    hr = valid["hr"].astype("float64")
    effort = valid[effort_col].astype("float64")
    n, gap = SHIFT_SAMPLES, SHIFT_GAP_SAMPLES

    def before_after(x: pd.Series) -> tuple[np.ndarray, np.ndarray]:
        back = x.rolling(n, min_periods=n).median().shift(gap).to_numpy()
        fwd = x[::-1].rolling(n, min_periods=n).median()[::-1].shift(-gap).to_numpy()
        return back, fwd

    before, after = before_after(hr)
    e_before, e_after = before_after(effort)
    roll = effort.rolling(SHIFT_STEADY_SAMPLES, center=True, min_periods=SHIFT_STEADY_SAMPLES // 2)
    mean = roll.mean().to_numpy()
    steady = roll.std(ddof=0).to_numpy() / np.where(mean > 0, mean, np.nan)
    shift = after - before
    effort_change = np.abs(e_after - e_before) / np.where(e_before > 0, e_before, np.nan)
    span = np.full(len(t), np.inf)
    stopped = np.ones(len(t), dtype=bool)
    reach = n + gap
    if len(t) > 2 * reach:
        span[reach:-reach] = t[2 * reach :] - t[: -2 * reach]
        # a watch pause or standing still lets the heart rate really drop: not a sensor fault
        all_t = df["t"].to_numpy(dtype="float64")
        stops = np.r_[0, np.cumsum((df["paused"] | df["still"]).to_numpy())]
        lo = np.searchsorted(all_t, t[: -2 * reach], side="left")
        hi = np.searchsorted(all_t, t[2 * reach :], side="right")
        stopped[reach:-reach] = (stops[hi] - stops[lo]) > 0
    flagged = (
        (np.abs(shift) >= SHIFT_BPM)
        & (effort_change <= SHIFT_EFFORT_TOL)
        & (steady <= SHIFT_STEADY_CV)
        & (span <= 2 * reach + SHIFT_MAX_DROPOUT_S)
        & ~stopped
        & (t >= t[0] + SHIFT_SKIP_S)
    )
    out = []
    for i, j in _runs(np.nan_to_num(flagged).astype(bool)):
        k = i + int(np.nanargmax(np.abs(shift[i : j + 1])))
        at = float(t[k])
        out.append(
            {
                "t": round(at),
                "at_km": round(float(valid["dist"].iloc[k]) / 1000, 2) if "dist" in valid else None,
                "hr_before": round(float(before[k])),
                "hr_after": round(float(after[k])),
                "effort": effort_col,
                "effort_change": round(float(effort_change[k]), 2),
                "near_cadence_before": _near_cadence_share(df, at - 600, at),
                "near_cadence_after": _near_cadence_share(df, at, at + 600),
            }
        )
    return out


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    if mask.size == 0:
        return []
    edges = np.diff(np.r_[0, mask.astype(np.int8), 0])
    return list(
        zip(
            np.flatnonzero(edges == 1).tolist(),
            (np.flatnonzero(edges == -1) - 1).tolist(),
            strict=True,
        )
    )


def _timed(df: pd.DataFrame, values: pd.Series) -> pd.Series:
    s = pd.Series(values.to_numpy(dtype="float64"), index=pd.to_timedelta(df["t"], unit="s"))
    return s


def _edge_jump(hr: np.ndarray, t: np.ndarray, i: int) -> float:
    """Largest heart-rate change within 15 s around index ``i``."""
    sel = (t >= t[i] - 15) & (t <= t[i] + 15) & ~np.isnan(hr)
    return float(hr[sel].max() - hr[sel].min()) if sel.sum() >= 2 else 0.0


def cadence_lock(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "hr" not in df or "cad" not in df or df["hr"].notna().sum() < 60:
        return []
    hr = df["hr"].to_numpy(dtype="float64")
    cad = df["cad"].to_numpy(dtype="float64")
    t = df["t"].to_numpy(dtype="float64")
    like = (cad >= LOCK_MIN_CADENCE) & (np.abs(hr - cad) <= LOCK_TOL_BPM)
    share = (
        _timed(df, pd.Series(like.astype(float)))
        .rolling(f"{LOCK_WINDOW_S}s", center=True, min_periods=1)
        .mean()
        .to_numpy()
    )
    candidate = (share >= LOCK_SHARE) & like
    # bridge the gaps between cadence-like samples inside a locked stretch
    out = []
    runs = _runs(candidate)
    merged: list[list[int]] = []
    for i, j in runs:
        if merged and t[i] - t[merged[-1][1]] <= LOCK_WINDOW_S / 2:
            merged[-1][1] = j
        else:
            merged.append([i, j])
    n = len(like)
    for i, j in merged:
        # the window share lags the true edges: extend over adjacent cadence-like samples
        # (allowing gaps of up to 5 s)
        while i > 0 and like[max(i - 5, 0) : i].any():
            i = max(k for k in range(max(i - 5, 0), i) if like[k])
        while j < n - 1 and like[j + 1 : min(j + 6, n)].any():
            j = max(k for k in range(j + 1, min(j + 6, n)) if like[k])
        secs = t[j] - t[i]
        if secs < LOCK_MIN_S:
            continue
        h, c = hr[i : j + 1], cad[i : j + 1]
        ok = ~np.isnan(h) & ~np.isnan(c)
        r = (
            float(np.corrcoef(h[ok], c[ok])[0, 1])
            if ok.sum() > 10 and h[ok].std() > 0 and c[ok].std() > 0
            else None
        )
        mad = float(np.mean(np.abs(h[ok] - c[ok]))) if ok.any() else None
        jump = max(_edge_jump(hr, t, i), _edge_jump(hr, t, j))
        high = (r is not None and r >= 0.6) or (mad is not None and mad <= 1.5) or jump >= JUMP_BPM
        out.append(
            {
                "i": i,
                "j": j,
                "t_start": round(float(t[i])),
                "t_end": round(float(t[j])),
                "duration_s": round(float(secs)),
                "from_km": round(float(df["dist"].iloc[i]) / 1000, 2) if "dist" in df else None,
                "to_km": round(float(df["dist"].iloc[j]) / 1000, 2) if "dist" in df else None,
                "avg_hr": mean_or_none(h, 0),
                "avg_cadence": mean_or_none(c, 0),
                "mean_abs_diff": round(mad, 1) if mad is not None else None,
                "corr": round(r, 2) if r is not None else None,
                "edge_jump_bpm": round(jump),
                "confidence": "high" if high else "possible",
            }
        )
    return out


def jumps(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "hr" not in df:
        return []
    valid = df[df["hr"].notna()]
    if len(valid) < 2:
        return []
    hr = valid["hr"].to_numpy()
    t = valid["t"].to_numpy()
    dhr = np.diff(hr)
    dt = np.diff(t)
    idx = np.flatnonzero((np.abs(dhr) >= JUMP_BPM) & (dt <= 3))
    return [
        {
            "t": round(float(t[k + 1])),
            "from_hr": float(hr[k]),
            "to_hr": float(hr[k + 1]),
            "at_km": round(float(valid["dist"].iloc[k + 1]) / 1000, 2) if "dist" in valid else None,
        }
        for k in idx
    ]


def spikes(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "hr" not in df or df["hr"].notna().sum() < 60:
        return []
    hr = _timed(df, df["hr"])
    base = hr.rolling("61s", center=True, min_periods=10).median().to_numpy()
    h = df["hr"].to_numpy(dtype="float64")
    t = df["t"].to_numpy(dtype="float64")
    above = ~np.isnan(h) & ~np.isnan(base) & (h - base >= SPIKE_BPM)
    out = []
    for i, j in _runs(above):
        secs = t[j] - t[i] + 1
        if secs > SPIKE_MAX_S:
            continue
        out.append(
            {
                "i": i,
                "j": j,
                "t": round(float(t[i])),
                "duration_s": round(float(secs)),
                "max_hr": float(np.nanmax(h[i : j + 1])),
                "baseline_hr": round(float(np.nanmedian(base[i : j + 1]))),
                "at_km": round(float(df["dist"].iloc[i]) / 1000, 2) if "dist" in df else None,
            }
        )
    return out


def dropouts(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "hr" not in df:
        return []
    moving = (
        (~df["paused"]) & (df["speed"].fillna(0) >= MOVING_SPEED_MPS)
        if "speed" in df
        else ~df["paused"]
    )
    missing = (df["hr"].isna() & moving).to_numpy()
    t = df["t"].to_numpy()
    out = []
    for i, j in _runs(missing):
        secs = t[j] - t[i] + 1
        if secs >= DROPOUT_S:
            out.append({"t": round(float(t[i])), "duration_s": round(float(secs))})
    return out


def check(df: pd.DataFrame, activity: Activity | None = None) -> dict[str, Any]:
    """The full heart-rate check of one activity (see the module docstring)."""
    locks = cadence_lock(df)
    spk = spikes(df)
    jmp = jumps(df)
    drops = dropouts(df)
    shifts = level_shifts(df)
    flagged = np.zeros(len(df), dtype=bool)
    for item in [x for x in locks if x["confidence"] == "high"] + spk:
        flagged[item["i"] : item["j"] + 1] = True
    moving = (
        ((~df["paused"]) & (df["speed"].fillna(0) >= MOVING_SPEED_MPS)).to_numpy()
        if "speed" in df
        else np.ones(len(df), dtype=bool)
    )
    dt = df["dt"].to_numpy() if "dt" in df else np.ones(len(df))
    moving_s = float(dt[moving].sum())
    flagged_s = float(dt[flagged & moving].sum())
    share = flagged_s / moving_s if moving_s > 0 else 0.0
    high_lock_s = sum(x["duration_s"] for x in locks if x["confidence"] == "high")
    if "hr" not in df or df["hr"].notna().sum() == 0:
        verdict = "none"
    elif high_lock_s >= UNRELIABLE_LOCK_S or share > UNRELIABLE_SHARE or shifts:
        verdict = "unreliable"
    elif locks or spk or jmp or drops:
        verdict = "suspect"
    else:
        verdict = "ok"
    hr = df["hr"] if "hr" in df else pd.Series(dtype=float)
    clean = hr[~flagged] if len(hr) else hr
    strip = ("i", "j")
    return {
        "activity": activity.as_dict() if activity else None,
        "verdict": verdict,
        "has_hr": bool(len(hr) and hr.notna().any()),
        "flagged_s": round(flagged_s),
        "flagged_share": round(share, 3),
        "avg_hr": mean_or_none(hr, 1),
        "max_hr": max_or_none(hr),
        "avg_hr_clean": mean_or_none(clean, 1),
        "max_hr_clean": max_or_none(clean),
        "cadence_lock": [{k: v for k, v in x.items() if k not in strip} for x in locks],
        "spikes": [{k: v for k, v in x.items() if k not in strip} for x in spk],
        "jumps": jmp,
        "dropouts": drops,
        "level_shifts": shifts,
        "_locks": locks,
        "_spikes": spk,
    }
