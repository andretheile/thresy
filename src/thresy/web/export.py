"""The demo as static files: ``coach web demo --out DIR`` and ``--single-file``."""

from __future__ import annotations

import shutil
from pathlib import Path

from thresy.web import pages
from thresy.web.demo import DemoSource
from thresy.web.i18n import Lang
from thresy.web.render import STATIC, STATIC_FILES, Renderer

COACH_NAME = "Thresy"


def export_pages(out: Path, *, lang: str = "en", scenario: str = "full") -> list[Path]:
    """Every page of the demo athlete into ``out``; run pages for the runs with per-second
    data (and the races' runs). Returns the files written."""
    L = Lang.get(lang)
    source = DemoSource(scenario, L.code)
    years = [y["year"] for y in source.run_years()]
    run_ids = {a.id for a in source.runs() if a.has_records}
    run_ids |= {r["activity_id"] for r in source.races(source.today()) if r.get("activity_id")}
    renderer = Renderer(
        L, mode="export", demo=True, exported_runs=run_ids, exported_years=set(years)
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "static").mkdir(exist_ok=True)
    written: list[Path] = []

    def write(name: str, html: str) -> None:
        path = out / name
        path.write_text(html, encoding="utf-8")
        written.append(path)

    write(
        "index.html",
        renderer.render("overview.html", pages.overview_page(source, L, coach_name=COACH_NAME)),
    )
    write("runs.html", renderer.render("runs.html", pages.runs_page(source, L, None)))
    for year in years:
        write(f"runs-{year}.html", renderer.render("runs.html", pages.runs_page(source, L, year)))
    for activity_id in sorted(run_ids):
        context = pages.run_page(source, L, activity_id)
        if context is not None:
            write(f"run-{activity_id}.html", renderer.render("run.html", context))
    write("trends.html", renderer.render("trends.html", pages.trends_page(source, L)))
    write("races.html", renderer.render("races.html", pages.races_page(source, L)))
    write("segments.html", renderer.render("segments.html", pages.segments_page(source, L)))
    write("data.html", renderer.render("data.html", pages.data_page(source, L)))
    for name in STATIC_FILES:
        target = out / "static" / name
        shutil.copyfile(STATIC / name, target)
        written.append(target)
    return written


def single_page(*, lang: str = "en", scenario: str = "full") -> str:
    """The overview as one self-contained HTML document (styles, script and icon inline)."""
    L = Lang.get(lang)
    source = DemoSource(scenario, L.code)
    renderer = Renderer(L, mode="single", demo=True)
    return renderer.render("overview.html", pages.overview_page(source, L, coach_name=COACH_NAME))


def export_single(target: Path, *, lang: str = "en", scenario: str = "full") -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(single_page(lang=lang, scenario=scenario), encoding="utf-8")
    return target
