"""Turning page contexts into HTML, for the server and for the static export of the demo.

Three modes differ only in their links and assets:

* ``server``: absolute paths (``/runs/123``) and ``/static/...`` assets with a version tag;
* ``export``: one directory of files (``index.html``, ``run-123.html``) and ``static/``; a page
  that was not exported is not linked;
* ``single``: one self-contained file, everything inline, no links between pages.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Container
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from thresy.web.i18n import Lang

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
STATIC_FILES = ("thresy.css", "thresy.js", "theme.js", "favicon.svg")

NAV = (
    ("today", "today"),
    ("runs", "runs"),
    ("trends", "trends"),
    ("races", "races"),
    ("segments", "segments"),
)
TITLES = {
    "today": "nav_today",
    "runs": "nav_runs",
    "trends": "nav_trends",
    "races": "nav_races",
    "segments": "nav_segments",
    "data": "data_title",
}


def static_version() -> str:
    digest = hashlib.sha256()
    for name in STATIC_FILES:
        digest.update((STATIC / name).read_bytes())
    return digest.hexdigest()[:10]


@dataclass(frozen=True)
class Inline:
    css: Markup
    js: Markup
    theme_js: Markup
    favicon: str


def _inline() -> Inline:
    def read(name: str) -> Markup:
        text = (STATIC / name).read_text(encoding="utf-8")
        return Markup(
            text.replace("</", "<\\/") if name.endswith(".js") else text.replace("</", "")
        )

    fav = base64.b64encode((STATIC / "favicon.svg").read_bytes()).decode("ascii")
    return Inline(
        css=read("thresy.css"),
        js=read("thresy.js"),
        theme_js=read("theme.js"),
        favicon=f"data:image/svg+xml;base64,{fav}",
    )


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    return env


class Renderer:
    """Renders pages in one language and one link mode."""

    def __init__(
        self,
        lang: Lang,
        *,
        mode: str = "server",
        demo: bool = False,
        exported_runs: Container[int] | None = None,
        exported_years: Container[int] | None = None,
    ) -> None:
        if mode not in ("server", "export", "single"):
            raise ValueError(mode)
        self.lang = lang
        self.mode = mode
        self.demo = demo
        self.exported_runs = exported_runs
        self.exported_years = exported_years
        self.env = _env()
        self.version = static_version()
        self.inline = _inline() if mode == "single" else None

    def url(self, name: str, **params: Any) -> str | None:
        if self.mode == "single":
            return None
        if self.mode == "export":
            if name == "run":
                ok = self.exported_runs is None or params["id"] in self.exported_runs
                return f"run-{params['id']}.html" if ok else None
            if name == "runs" and params.get("year") is not None:
                ok = self.exported_years is None or params["year"] in self.exported_years
                return f"runs-{params['year']}.html" if ok else None
            if name in ("login", "logout"):
                return None
            return {"today": "index.html"}.get(name, f"{name}.html")
        if name == "today":
            return "/"
        if name == "run":
            return f"/runs/{int(params['id'])}"
        if name == "runs" and params.get("year") is not None:
            return f"/runs?year={int(params['year'])}"
        return f"/{name}"

    def static(self, name: str) -> str:
        if self.mode == "export":
            return f"static/{name}"
        return f"/static/{name}?v={self.version}"

    def render(self, template: str, context: dict[str, Any], **extra: Any) -> str:
        page = context.get("page", "")
        title = extra.pop("title", None) or self.lang.t(TITLES.get(page, "nav_today"))
        values = {
            "L": self.lang,
            "url": self.url,
            "static": self.static,
            "nav": NAV,
            "demo": self.demo,
            "inline": self.inline,
            "title": title,
            "logout_token": None,
            **context,
            **extra,
        }
        return self.env.get_template(template).render(values)


TemplateFor = Callable[[str], str]
