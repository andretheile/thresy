"""Charts as inline SVG, drawn on the server.

No chart library and no script: every chart is plain SVG markup whose colours come from CSS
classes (``static/thresy.css``), so it follows the light and dark theme and passes the content
security policy (no inline styles). Text is escaped; names come from the database.

Conventions (see the dataviz notes in ``docs/web.md``): one baseline, hairline grid, thin
marks with 4 px rounded data ends, 2 px lines, a 2 px surface gap between touching fills,
selective direct labels, and a ``<title>`` on every hover target (the small script turns those
into tooltips; without it the browser shows them). Each chart is drawn at a fixed natural width
(``w``); pages render a phone and a desktop variant and CSS shows the right one.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from markupsafe import Markup, escape

from thresy.web.i18n import Lang

FONT_W = 0.56
"""Average glyph width relative to the font size, for fitting labels (system sans)."""


def text_width(text: str, size: float) -> float:
    return len(text) * size * FONT_W


def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    """Round tick values covering ``lo``..``hi`` with about ``n`` steps."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    v = start
    while v <= hi + step * 0.001:
        ticks.append(round(v, 10))
        v += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


TIME_STEPS = (5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600)


def time_ticks(lo: float, hi: float, n: int = 3) -> list[float]:
    """Like :func:`nice_ticks` for seconds (paces, durations): steps of 10 s, 15 s, 30 s,
    1 min, ... so the labels read 4:00, 4:30, 5:00."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(n, 1)
    step = next((s for s in TIME_STEPS if s >= raw), TIME_STEPS[-1])
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    return [float(v) for v in range(int(start), int(end) + 1, int(step))]


def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


class Svg:
    """A tiny SVG writer."""

    def __init__(self, w: float, h: float, *, label: str, cls: str = "") -> None:
        self.w, self.h = w, h
        self.parts: list[str] = []
        self.label = label
        self.cls = cls

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def rect(self, x: float, y: float, w: float, h: float, cls: str, *, rx: float = 0) -> None:
        if w <= 0 or h <= 0:
            return
        r = f' rx="{_f(rx)}"' if rx else ""
        self.add(
            f'<rect class="{cls}" x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" height="{_f(h)}"{r}/>'
        )

    def bar_up(
        self, x: float, y_top: float, w: float, y_base: float, cls: str, r: float = 4
    ) -> None:
        """A column from the baseline up to ``y_top``: rounded top, square at the baseline."""
        h = y_base - y_top
        if h <= 0.4 or w <= 0:
            return
        r = min(r, w / 2, h)
        d = (
            f"M{_f(x)},{_f(y_base)}V{_f(y_top + r)}Q{_f(x)},{_f(y_top)} {_f(x + r)},{_f(y_top)}"
            f"H{_f(x + w - r)}Q{_f(x + w)},{_f(y_top)} {_f(x + w)},{_f(y_top + r)}V{_f(y_base)}Z"
        )
        self.add(f'<path class="{cls}" d="{d}"/>')

    def line(self, x1: float, y1: float, x2: float, y2: float, cls: str) -> None:
        self.add(f'<line class="{cls}" x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}"/>')

    def text(
        self, x: float, y: float, value: str, cls: str = "tick", anchor: str = "start"
    ) -> None:
        a = "" if anchor == "start" else f' text-anchor="{anchor}"'
        self.add(f'<text class="{cls}" x="{_f(x)}" y="{_f(y)}"{a}>{escape(value)}</text>')

    def path(self, d: str, cls: str) -> None:
        if d:
            self.add(f'<path class="{cls}" d="{d}"/>')

    def circle(self, x: float, y: float, r: float, cls: str) -> None:
        self.add(f'<circle class="{cls}" cx="{_f(x)}" cy="{_f(y)}" r="{_f(r)}"/>')

    def hit(self, x: float, y: float, w: float, h: float, tip: str, **data: Any) -> None:
        """A transparent hover target with a tooltip text."""
        attrs = "".join(f' data-{k}="{escape(str(v))}"' for k, v in data.items())
        self.add(
            f'<rect class="hit" x="{_f(x)}" y="{_f(y)}" width="{_f(max(w, 1))}"'
            f' height="{_f(max(h, 1))}" data-tip="{escape(tip)}"{attrs}>'
            f"<title>{escape(tip)}</title></rect>"
        )

    def render(self) -> Markup:
        return Markup(
            f'<svg class="chart {self.cls}" width="{_f(self.w)}" height="{_f(self.h)}"'
            f' viewBox="0 0 {_f(self.w)} {_f(self.h)}" role="img"'
            f' aria-label="{escape(self.label)}">' + "".join(self.parts) + "</svg>"
        )


@dataclass(frozen=True)
class Pair:
    """A chart in its phone and its desktop size."""

    narrow: Markup
    wide: Markup

    def __html__(self) -> str:
        return (
            f'<div class="chart-box only-narrow">{self.narrow}</div>'
            f'<div class="chart-box only-wide">{self.wide}</div>'
        )


def pair(draw: Callable[..., Markup], narrow: float, wide: float, **kw: Any) -> Pair:
    return Pair(draw(w=narrow, **kw), draw(w=wide, **kw))


def _poly(points: Sequence[tuple[float, float] | None]) -> str:
    """A path through the points; ``None`` breaks the line."""
    out, pen = [], False
    for p in points:
        if p is None:
            pen = False
            continue
        out.append(f"{'L' if pen else 'M'}{_f(p[0])},{_f(p[1])}")
        pen = True
    return "".join(out)


# -- weekly volume ---------------------------------------------------------------------------------


def weekly_volume(
    weeks: list[dict[str, Any]], lang: Lang, *, w: float, h: float = 190, today: date
) -> Markup:
    """Stacked columns per week: the longest run (solid) under the other runs (lighter); the
    week in progress is drawn lighter. Labels: the latest week and the biggest one."""
    svg = Svg(w, h, label=lang.t("weekly_volume"), cls="c-volume")
    if not weeks:
        return svg.render()
    left, right, top, bottom = 30, 8, 18, 24
    pw, ph = w - left - right, h - top - bottom
    vmax = max((wk["distance_km"] or 0) for wk in weeks)
    ticks = nice_ticks(0, max(vmax, 10), 3)
    ymax = ticks[-1]

    def y(v: float) -> float:
        return top + ph - v / ymax * ph

    for t in ticks:
        svg.line(left, y(t), w - right, y(t), "gridline" if t else "base")
        svg.text(left - 6, y(t) + 4, lang.int(t), "tick", "end")
    n = len(weeks)
    band = pw / n
    bw = min(24.0, band * 0.68)
    biggest = max(range(n), key=lambda i: weeks[i]["distance_km"] or 0)
    every = max(1, math.ceil(46 / band))  # a date label needs about 46 px
    for i, wk in enumerate(weeks):
        x = left + i * band + (band - bw) / 2
        total = wk["distance_km"] or 0.0
        longest = min(wk.get("longest_km") or 0.0, total)
        current = i == n - 1 and wk["to"] >= today and (wk["to"] - wk["from"]).days < 6
        solid = "bar-accent" + (" is-partial" if current else "")
        soft = "bar-accent-soft" + (" is-partial" if current else "")
        base = y(0)
        if total > 0:
            y_long = y(longest)
            if longest >= total - 0.05:
                svg.bar_up(x, y_long, bw, base, solid)
            else:
                svg.rect(x, y_long, bw, base - y_long, solid)
                svg.bar_up(x, y(total), bw, y_long - 2, soft)
        show = i == n - 1 or i == biggest
        if show and total > 0:
            svg.text(x + bw / 2, y(total) - 6, lang.num(total, 0), "val", "middle")
        if (n - 1 - i) % every == 0:
            svg.text(x + bw / 2, h - 6, lang.day_month(wk["from"]), "tick", "middle")
        tip = (
            f"{lang.t('week_no', n=wk['week'])} · {lang.day_month(wk['from'])}: "
            f"{lang.num(total, 1)} km, {lang.tn('week_runs', wk['runs'])}"
        )
        if wk.get("longest_km"):
            tip += f", {lang.t('legend_long').lower()} {lang.num(wk['longest_km'], 1)} km"
        svg.hit(left + i * band, top, band, ph, tip)
    return svg.render()


# -- the year so far -------------------------------------------------------------------------------


def _value_at(points: list[tuple[int, float]], doy: float) -> float | None:
    """The cumulative value of a year's step line on a day of the year."""
    last = None
    for d, v in points:
        if d > doy:
            break
        last = v
    return last


def year_lines(
    years: list[dict[str, Any]], lang: Lang, *, w: float, h: float = 230, today: date
) -> Markup:
    """Cumulative distance per day of the year: this year in the accent colour, the previous
    years in grey. The window ends a few weeks after today (the whole year from October on),
    so the comparison that matters, this year so far against the same days before, is not
    squeezed into a corner; each line is labelled where it leaves the window."""
    svg = Svg(w, h, label=lang.t("year_so_far"), cls="c-year")
    if not years:
        return svg.render()
    today_doy = today.timetuple().tm_yday
    end = 366 if today_doy > 270 else min(366, max(today_doy + 56, 120))
    left, right, top, bottom = 40, 58, 16, 24
    pw, ph = w - left - right, h - top - bottom
    clipped = []
    for yr in sorted(years, key=lambda y: y["year"]):
        pts = [(d, v) for d, v in yr["points"] if d <= end]
        if not pts:
            continue
        if yr["year"] != today.year and pts[-1][0] < end:
            pts.append((end, pts[-1][1]))
        clipped.append({**yr, "points": pts})
    vmax = max(y["points"][-1][1] for y in clipped)
    ticks = nice_ticks(0, max(vmax, 50), 4)
    ymax = ticks[-1]

    def X(doy: float) -> float:
        return left + (doy - 1) / (end - 1) * pw

    def Y(v: float) -> float:
        return top + ph - v / ymax * ph

    for t in ticks:
        svg.line(left, Y(t), left + pw, Y(t), "gridline" if t else "base")
        svg.text(left - 6, Y(t) + 4, lang.int(t), "tick", "end")
    months = [m for m in range(1, 13) if date(2001, m, 1).timetuple().tm_yday < end]
    every = 1 if pw / max(len(months), 1) >= 34 else 2 if pw / max(len(months), 1) >= 17 else 3
    for m in months:
        doy = date(today.year, m, 1).timetuple().tm_yday
        if (m - 1) % every:
            continue
        svg.line(X(doy), top + ph, X(doy), top + ph + 4, "base")
        svg.text(X(doy) + 2, h - 6, lang.month(m, short=True), "tick")
    if end < 366:
        svg.line(X(today_doy), top, X(today_doy), top + ph, "today-line")
    labels = []
    for yr in clipped:
        pts = yr["points"]
        current = yr["year"] == today.year
        older = not current and yr["year"] < today.year - 1
        cls = "ln-accent" if current else ("ln-faint" if older else "ln-muted")
        svg.path(_poly([(X(d), Y(v)) for d, v in pts]), cls)
        end_doy, end_v = pts[-1]
        if current:
            svg.circle(X(end_doy), Y(end_v), 4.5, "dot-accent-ring")
        else:
            same_day = _value_at(pts, today_doy)
            if same_day is not None and end < 366:
                svg.circle(X(today_doy), Y(same_day), 3, "dot-muted-solid")
        labels.append((Y(end_v), X(end_doy), yr["year"], end_v, current))
    # labels: this year beside its end point, the others in the right margin, pushed apart
    # when they would overlap (with a leader line back to their line)
    margin = sorted((lb for lb in labels if not lb[4]), key=lambda lb: lb[0])
    placed: list[float] = []
    for y0, _x0, year, _value, _ in margin:
        ly = y0 + 4
        for p in placed:
            if ly - p < 14:
                ly = p + 14
        placed.append(ly)
        lx = left + pw + 8
        if abs(ly - 4 - y0) > 2:
            svg.line(left + pw + 1, y0, lx - 2, ly - 4, "leader")
        svg.text(lx, ly, str(year), "lbl")
    for y0, x0, year, value, current in labels:
        if not current:
            continue
        text = f"{lang.int(value)} km"
        lx = x0 + 9
        anchor = "start"
        if lx + text_width(text, 12) > left + pw:
            lx, anchor = x0 - 9, "end"
        svg.text(lx, y0 - 6, str(year), "val", anchor)
        svg.text(lx, y0 + 9, text, "lbl", anchor)
    # hover: one target per week with every year's value on that day
    by_year = {yr["year"]: yr["points"] for yr in clipped}
    step = 7
    for start in range(1, end, step):
        doy = min(start + step - 1, end - 1)
        parts = []
        for year in sorted(by_year, reverse=True):
            if year == today.year and doy > today_doy:
                continue
            value = _value_at(by_year[year], doy)
            if value is not None:
                parts.append(f"{year}: {lang.int(value)} km")
        if parts:
            day = date(today.year, 1, 1) + timedelta(days=doy - 1)
            tip = f"{lang.day_month(day)} · " + " · ".join(parts)
            svg.hit(X(start), top, X(min(doy + 1, end)) - X(start), ph, tip)
    return svg.render()


# -- recovery: a daily value with its usual range ------------------------------------------------


def daily_range(
    days: list[date],
    values: list[float | None],
    lang: Lang,
    *,
    w: float,
    h: float = 150,
    band: list[tuple[float | None, float | None]] | None = None,
    avg: list[float | None] | None = None,
    unit: str,
    digits: int = 0,
    label: str,
    hue: str = "rest",
    fmt: Callable[[float], str] | None = None,
    bars: bool = False,
) -> Markup:
    """Daily values as dots (or thin columns with ``bars``), the usual range as a band and a
    7-day average as a line, over the same date axis as the other recovery charts."""
    svg = Svg(w, h, label=label, cls="c-daily")
    fmt = fmt or (lambda v: lang.num(v, digits))
    present = [v for v in values if v is not None]
    if not days or not present:
        return svg.render()
    left, right, top, bottom = 34, 10, 12, 22
    pw, ph = w - left - right, h - top - bottom
    pool = present + [b for pair in (band or []) for b in pair if b is not None]
    lo, hi = min(pool), max(pool)
    if bars:
        lo = 0
    pad = (hi - lo) * 0.08 or 1
    ticks = nice_ticks(lo - (0 if bars else pad), hi + pad, 3)
    y0, y1 = ticks[0], ticks[-1]
    n = len(days)
    step = pw / n

    def X(i: float) -> float:
        return left + (i + 0.5) * step

    def Y(v: float) -> float:
        return top + ph - (v - y0) / (y1 - y0) * ph

    for t in ticks:
        svg.line(left, Y(t), left + pw, Y(t), "gridline" if t != y0 else "base")
        svg.text(left - 6, Y(t) + 4, fmt(t) if not bars else lang.num(t, 0), "tick", "end")
    if band:
        upper = [
            (X(i), Y(b[1])) for i, b in enumerate(band) if b[0] is not None and b[1] is not None
        ]
        lower = [
            (X(i), Y(b[0])) for i, b in enumerate(band) if b[0] is not None and b[1] is not None
        ]
        if upper:
            d = _poly(upper) + "".join(f"L{_f(x)},{_f(y)}" for x, y in reversed(lower)) + "Z"
            svg.path(d, f"band-{hue}")
    if bars:
        bw = max(1.5, min(8.0, step * 0.62))
        for i, v in enumerate(values):
            if v is not None:
                svg.bar_up(X(i) - bw / 2, Y(v), bw, Y(y0), f"bar-{hue}-soft", r=min(2, bw / 2))
    else:
        for i, v in enumerate(values):
            if v is not None:
                out = band and band[i][0] is not None and (v < band[i][0] or v > band[i][1])  # type: ignore[operator]
                svg.circle(
                    X(i), Y(v), 2.6 if w < 500 else 3, f"dot-{hue}" + (" is-out" if out else "")
                )
    if avg:
        svg.path(
            _poly([(X(i), Y(v)) if v is not None else None for i, v in enumerate(avg)]), f"ln-{hue}"
        )
        last = next(((i, v) for i, v in reversed(list(enumerate(avg))) if v is not None), None)
        if last:
            svg.circle(X(last[0]), Y(last[1]), 3.5, f"dot-{hue}-ring")
    # month starts and Mondays as x ticks
    every = 14 if w < 500 else 7
    for i, d in enumerate(days):
        if (n - 1 - i) % every == 0:
            svg.text(X(i), h - 6, lang.day_month(d), "tick", "middle")
    for i, d in enumerate(days):
        v = values[i]
        tip = f"{lang.date(d, today=days[-1])}: " + (f"{fmt(v)} {unit}" if v is not None else "–")
        if band and band[i][0] is not None:
            tip += f" ({lang.t('usual_range')} {fmt(band[i][0])}–{fmt(band[i][1])})"  # type: ignore[arg-type]
        if avg and avg[i] is not None:
            tip += f" · {lang.t('avg7')} {fmt(avg[i])}"  # type: ignore[arg-type]
        svg.hit(left + i * step, top, step, ph, tip)
    return svg.render()


# -- last night ------------------------------------------------------------------------------------

STAGES = ("deep", "light", "rem", "awake")


def stage_bar(stages: dict[str, float], lang: Lang, *, w: float, h: float = 28) -> Markup:
    """One night split into its stages: a tall segmented bar, shares printed where they fit."""
    total = sum(stages.get(s, 0) or 0 for s in STAGES)
    svg = Svg(w, h, label=lang.t("last_night"), cls="c-stages")
    if total <= 0:
        return svg.render()
    x = 0.0
    parts = [(s, stages.get(s) or 0) for s in STAGES if (stages.get(s) or 0) > 0]
    gap = 2.0
    avail = w - gap * (len(parts) - 1)
    for k, (stage, secs) in enumerate(parts):
        bw = secs / total * avail
        svg.rect(x, 0, bw, h, f"st-{stage}", rx=4)
        share = f"{round(secs / total * 100)} %"
        if text_width(share, 12) + 12 < bw:
            svg.text(x + 7, h / 2 + 4.5, share, f"in-{stage}")
        svg.hit(x, 0, bw, h, f"{lang.t('stage_' + stage)}: {lang.hm(secs)} ({share})")
        x += bw + (gap if k < len(parts) - 1 else 0)
    return svg.render()


def hypnogram(levels: list[dict[str, Any]], lang: Lang, *, w: float, h: float = 96) -> Markup:
    """The night's stages over time, one row per stage (awake on top, deep at the bottom)."""
    svg = Svg(w, h, label=lang.t("last_night"), cls="c-hypno")
    if not levels:
        return svg.render()
    rows = ("awake", "rem", "light", "deep")
    left, right, top, bottom = 46, 4, 2, 20
    pw, ph = w - left - right, h - top - bottom
    rh = ph / len(rows)
    t0 = levels[0]["start_local"]
    t1 = levels[-1]["end_local"]
    span = max((t1 - t0).total_seconds(), 1)

    def X(t: datetime) -> float:
        return left + (t - t0).total_seconds() / span * pw

    for r, stage in enumerate(rows):
        svg.text(left - 8, top + r * rh + rh / 2 + 4, lang.t("stage_" + stage), "tick", "end")
        svg.line(left, top + (r + 1) * rh - 0.5, left + pw, top + (r + 1) * rh - 0.5, "gridline")
    for lv in levels:
        r = rows.index(lv["stage"]) if lv["stage"] in rows else 2
        x0, x1 = X(lv["start_local"]), X(lv["end_local"])
        svg.rect(x0, top + r * rh + 2, max(x1 - x0, 1.2), rh - 4, f"st-{lv['stage']}", rx=2)
    # hour ticks
    hour = t0.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    every = 2 if w < 500 else 1
    while hour < t1:
        if hour.hour % every == 0:
            svg.line(X(hour), top + ph, X(hour), top + ph + 3, "base")
            svg.text(X(hour), h - 5, f"{hour.hour:02d}:00", "tick", "middle")
        hour += timedelta(hours=1)
    for lv in levels:
        x0, x1 = X(lv["start_local"]), X(lv["end_local"])
        tip = f"{lang.t('stage_' + lv['stage'])}: {lv['start_local']:%H:%M}–{lv['end_local']:%H:%M}"
        svg.hit(x0, top, max(x1 - x0, 2), ph, tip)
    return svg.render()


def range_strip(
    value: float | None,
    low: float | None,
    high: float | None,
    lang: Lang,
    *,
    w: float,
    h: float = 20,
    unit: str,
    hue: str = "rest",
    label: str,
) -> Markup:
    """Where one value sits against its usual range: a band, the value as a dot."""
    text = f"{label}: {lang.int(value)} {unit}" if value is not None else label
    if low is not None and high is not None:
        text += f" ({lang.int(low)}–{lang.int(high)})"
    svg = Svg(w, h, label=text, cls="c-strip")
    if value is None and (low is None or high is None):
        return svg.render()
    pool = [v for v in (value, low, high) if v is not None]
    lo, hi = min(pool), max(pool)
    pad = max((hi - lo) * 0.35, 3)
    lo, hi = lo - pad, hi + pad
    mid = h / 2

    def X(v: float) -> float:
        return 8 + (v - lo) / (hi - lo) * (w - 16)

    svg.rect(8, mid - 1, w - 16, 2, "track", rx=1)
    if low is not None and high is not None:
        svg.rect(X(low), mid - 5, X(high) - X(low), 10, f"band-{hue}-strong", rx=5)
    if value is not None:
        out = low is not None and high is not None and not low <= value <= high
        svg.circle(X(value), mid, 6.5, "dot-ring")
        svg.circle(X(value), mid, 5, f"dot-{hue}" + (" is-out" if out else ""))
    return svg.render()


# -- this week -------------------------------------------------------------------------------------


def week_strip(
    days: list[dict[str, Any]], lang: Lang, *, w: float, h: float = 112, today: date
) -> Markup:
    """Seven days: the planned distance as a light column, what was run as a solid one."""
    svg = Svg(w, h, label=lang.t("this_week"), cls="c-week")
    top, bottom = 18, 26
    ph = h - top - bottom
    vmax = max([d["planned_km"] or 0 for d in days] + [d["run_km"] or 0 for d in days] + [8.0])
    band = w / 7
    bw = min(26.0, band * 0.56)

    def Y(v: float) -> float:
        return top + ph - v / vmax * ph

    svg.line(0, Y(0), w, Y(0), "base")
    for i, d in enumerate(days):
        cx = i * band + band / 2
        is_today = d["date"] == today
        if is_today:
            svg.rect(i * band + 2, 0, band - 4, h - 2, "today-wash", rx=8)
        if d["planned_km"]:
            svg.bar_up(cx - bw / 2, Y(d["planned_km"]), bw, Y(0), "bar-plan")
        if d["run_km"]:
            inner = bw * 0.54
            svg.bar_up(cx - inner / 2, Y(d["run_km"]), inner, Y(0), "bar-accent")
        value = d["run_km"] or d["planned_km"]
        if value:
            svg.text(
                cx,
                Y(value) - 5,
                lang.num(value, 0 if value >= 10 else 1),
                "val" if d["run_km"] else "lbl",
                "middle",
            )
        svg.text(
            cx,
            h - 9,
            lang.weekday(d["date"], short=True),
            "day" + (" is-today" if is_today else ""),
            "middle",
        )
        tip = lang.weekday(d["date"]) + ": "
        bits = []
        if d["planned_km"]:
            bits.append(f"{lang.t('legend_planned')} {lang.num(d['planned_km'], 1)} km")
        if d["run_km"]:
            bits.append(f"{lang.t('legend_ran')} {lang.num(d['run_km'], 1)} km")
        svg.hit(i * band, 0, band, h, tip + (" · ".join(bits) or "–"))
    return svg.render()


# -- one run ---------------------------------------------------------------------------------------


def track(
    lat: Sequence[float],
    lon: Sequence[float],
    lang: Lang,
    *,
    w: float,
    h: float,
    km_marks: Sequence[tuple[float, float, int]] = (),
) -> Markup:
    """The route's shape without a map: start (ring) and finish (dot), kilometre marks."""
    svg = Svg(w, h, label=lang.t("track"), cls="c-track")
    pts = [
        (a, b)
        for a, b in zip(lat, lon, strict=True)
        if a is not None and b is not None and not (math.isnan(a) or math.isnan(b))
    ]
    if len(pts) < 2:
        return svg.render()
    lat0 = sum(p[0] for p in pts) / len(pts)
    k = math.cos(math.radians(lat0))
    xs = [p[1] * k for p in pts]
    ys = [-p[0] for p in pts]
    pad = 16
    sx = (w - 2 * pad) / max(max(xs) - min(xs), 1e-9)
    sy = (h - 2 * pad) / max(max(ys) - min(ys), 1e-9)
    s = min(sx, sy)
    ox = (w - (max(xs) - min(xs)) * s) / 2
    oy = (h - (max(ys) - min(ys)) * s) / 2
    mx, my = min(xs), min(ys)

    def P(la: float, lo: float) -> tuple[float, float]:
        return ox + (lo * k - mx) * s, oy + (-la - my) * s

    xy = [P(a, b) for a, b in pts]
    svg.path(_poly(xy), "route-halo")
    svg.path(_poly(xy), "route")
    for la, lo, n in km_marks:
        x, y = P(la, lo)
        svg.circle(x, y, 7.5, "km-mark")
        svg.text(x, y + 3.5, str(n), "km-num", "middle")
    sx0, sy0 = xy[0]
    ex, ey = xy[-1]
    svg.circle(ex, ey, 6, "finish")
    svg.circle(sx0, sy0, 6, "start")
    return svg.render()


def profile(
    x_km: Sequence[float],
    values: Sequence[float | None],
    lang: Lang,
    *,
    w: float,
    h: float = 120,
    kind: str,
    label: str,
    fmt: Callable[[float], str],
    invert: bool = False,
    area: bool = False,
    domain: tuple[float, float] | None = None,
    chart_id: str,
) -> Markup:
    """One measure over the distance of a run (pace, heart rate or altitude); the three share
    the distance axis and a crosshair (``data-profile``)."""
    svg = Svg(w, h, label=label, cls=f"c-profile c-{kind}")
    pts = [(x, v) for x, v in zip(x_km, values, strict=True) if v is not None and not math.isnan(v)]
    if len(pts) < 2:
        return svg.render()
    left, right, top, bottom = 40, 10, 10, 20
    pw, ph = w - left - right, h - top - bottom
    xmax = max(x_km[-1], 0.1)
    lo, hi = domain or (min(v for _, v in pts), max(v for _, v in pts))
    if kind == "pace":
        ticks = time_ticks(lo, hi, 4)
    else:
        pad = (hi - lo) * 0.08 or 1
        ticks = nice_ticks(lo - pad, hi + pad, 3)
    y0, y1 = ticks[0], ticks[-1]

    def X(x: float) -> float:
        return left + x / xmax * pw

    def Y(v: float) -> float:
        v = min(max(v, y0), y1)
        frac = (v - y0) / (y1 - y0)
        return top + (frac if invert else 1 - frac) * ph

    for t in ticks:
        svg.line(left, Y(t), left + pw, Y(t), "gridline")
        svg.text(left - 6, Y(t) + 4, fmt(t), "tick", "end")
    svg.line(left, top + ph, left + pw, top + ph, "base")
    xt = nice_ticks(0, xmax, 4 if w < 500 else 8)
    for t in xt:
        if t <= xmax + 1e-9:
            svg.text(X(t), h - 5, lang.num(t, 0 if t == int(t) else 1), "tick", "middle")
    line: list[tuple[float, float] | None] = []
    prev = None
    for x, v in zip(x_km, values, strict=True):
        if v is None or math.isnan(v):
            line.append(None)
            continue
        if prev is not None and x - prev > xmax / 40:
            line.append(None)
        line.append((X(x), Y(v)))
        prev = x
    if area:
        seq = [p for p in line if p is not None]
        d = _poly(seq) + f"L{_f(seq[-1][0])},{_f(top + ph)}L{_f(seq[0][0])},{_f(top + ph)}Z"
        svg.path(d, "area-muted")
        svg.path(_poly(line), "ln-muted")
    else:
        svg.path(_poly(line), "ln-accent")
    svg.add(
        f'<g class="xhair" data-profile="{escape(chart_id)}" data-left="{left}"'
        f' data-width="{_f(pw)}"'
        f' data-top="{top}" data-height="{_f(ph)}"></g>'
    )
    svg.add(
        f'<rect class="hit-area" x="{left}" y="{top}" width="{_f(pw)}" height="{_f(ph)}"'
        f' data-profile="{escape(chart_id)}"/>'
    )
    return svg.render()


def split_bar(delta_s: float | None, scale_s: float, *, w: float = 96, h: float = 12) -> Markup:
    """A split's pace against the run's average: right (accent) = faster, left = slower."""
    svg = Svg(w, h, label="", cls="c-split")
    mid = w / 2
    svg.line(mid, 0, mid, h, "base")
    if delta_s is None or scale_s <= 0:
        return svg.render()
    length = min(abs(delta_s) / scale_s, 1) * (mid - 2)
    if delta_s < 0:  # faster than average
        svg.rect(mid + 1, 2, length, h - 4, "bar-accent", rx=2)
    else:
        svg.rect(mid - 1 - length, 2, length, h - 4, "bar-muted", rx=2)
    return svg.render()


def segment_dots(
    efforts: list[dict[str, Any]],
    lang: Lang,
    *,
    w: float,
    h: float = 150,
    best_id: Any,
    latest_id: Any,
) -> Markup:
    """Every effort on a segment over time; faster is higher. Best and latest are marked."""
    svg = Svg(w, h, label=lang.t("segment_chart_sub"), cls="c-seg")
    pts = [e for e in efforts if e.get("start_local") and e.get("duration_s")]
    if not pts:
        return svg.render()
    left, right, top, bottom = 44, 12, 22, 22
    pw, ph = w - left - right, h - top - bottom
    t0 = pts[0]["start_local"]
    t1 = pts[-1]["start_local"]
    span = max((t1 - t0).total_seconds(), 86400.0)
    durs = [e["duration_s"] for e in pts]
    lo, hi = min(durs), max(durs)
    pad = (hi - lo) * 0.12 or 10
    ticks = time_ticks(lo - pad, hi + pad, 3)
    y0, y1 = ticks[0], ticks[-1]

    def X(t: datetime) -> float:
        return left + (t - t0).total_seconds() / span * pw

    def Y(v: float) -> float:
        return top + (v - y0) / (y1 - y0) * ph  # faster (smaller) is higher

    for t in ticks:
        svg.line(left, Y(t), left + pw, Y(t), "gridline")
        svg.text(left - 6, Y(t) + 4, lang.clock(t), "tick", "end")
    years = sorted({e["start_local"].year for e in pts})
    for year in years:
        jan = datetime(year, 1, 1)
        if t0 <= jan <= t1:
            svg.line(X(jan), top, X(jan), top + ph, "gridline")
        x = max(X(max(jan, t0)), left)
        svg.text(x + 3, h - 6, str(year), "tick")
    special = []
    for e in pts:
        key = (e["activity_id"], e["start_utc"])
        if key in (best_id, latest_id):
            special.append(e)
            continue
        svg.circle(X(e["start_local"]), Y(e["duration_s"]), 3.2, "dot-muted")
    for e in special:
        key = (e["activity_id"], e["start_utc"])
        x, y = X(e["start_local"]), Y(e["duration_s"])
        if key == best_id:
            svg.circle(x, y, 6, "dot-ring")
            svg.circle(x, y, 4.5, "dot-accent")
            label = f"{lang.t('best')} {lang.clock(e['duration_s'])}"
            lx = min(
                max(x, left + text_width(label, 12) / 2), w - right - text_width(label, 12) / 2
            )
            svg.text(lx, y - 10, label, "val", "middle")
        if key == latest_id and key != best_id:
            svg.circle(x, y, 5, "dot-latest")
            label = f"{lang.t('latest')} {lang.clock(e['duration_s'])}"
            lx = min(x, w - right - text_width(label, 12) / 2)
            svg.text(lx, y + 18, label, "lbl", "middle")
    for e in pts:
        tip = f"{lang.date(e['start_local'].date())}: {lang.clock(e['duration_s'])}" + (
            f" · {lang.int(e['avg_hr'])} {lang.t('unit_bpm')}" if e.get("avg_hr") else ""
        )
        svg.hit(X(e["start_local"]) - 6, Y(e["duration_s"]) - 6, 12, 12, tip)
    return svg.render()


def countdown_ring(days_left: int, total_days: int, *, w: float = 120) -> Markup:
    """Weeks to go as a thin ring that fills up towards race day."""
    svg = Svg(w, w, label="", cls="c-ring")
    r = w / 2 - 6
    c = w / 2
    frac = 1 - min(max(days_left / max(total_days, 1), 0), 1)
    svg.circle(c, c, r, "ring-track")
    if frac > 0:
        a = 2 * math.pi * frac
        x, y = c + r * math.sin(a), c - r * math.cos(a)
        large = 1 if frac > 0.5 else 0
        if frac >= 0.999:
            svg.circle(c, c, r, "ring-fill")
        else:
            svg.path(
                f"M{_f(c)},{_f(c - r)}A{_f(r)},{_f(r)} 0 {large} 1 {_f(x)},{_f(y)}", "ring-fill"
            )
    return svg.render()
