"""Text the agent wrote (messages, plan descriptions) as an excerpt and as HTML.

Both come from one Markdown parse (CommonMark plus tables and strikethrough, no raw HTML, no
images):

* :func:`excerpt` keeps prose only: the paragraphs and headings before the first table,
  list, code block, quote or rule, as plain text without Markdown syntax, cut at a word.
* :func:`render_markdown` renders the whole text. Tables sit in a wrapper that scrolls
  sideways on a narrow screen, column alignment becomes a class (an inline ``style`` would be
  refused by the content security policy), links open without a referrer.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

STOP_BLOCKS = frozenset(
    {
        "table_open",
        "bullet_list_open",
        "ordered_list_open",
        "code_block",
        "fence",
        "blockquote_open",
        "hr",
        "html_block",
    }
)
"""Blocks an excerpt does not reach into."""

EXCERPT_CHARS = 260


@lru_cache(maxsize=1)
def _parser() -> MarkdownIt:
    md = (
        MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
        .enable(["table", "strikethrough"])
        .disable("image")
    )

    def link_open(renderer: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        tokens[idx].attrSet("rel", "noopener noreferrer nofollow")
        return renderer.renderToken(tokens, idx, options, env)

    def table_open(renderer: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return '<div class="md-table">' + renderer.renderToken(tokens, idx, options, env)

    def table_close(renderer: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return renderer.renderToken(tokens, idx, options, env) + "</div>"

    def cell_open(renderer: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        token = tokens[idx]
        style = token.attrGet("style")
        if style:
            del token.attrs["style"]
            match = re.search(r"text-align:\s*(left|center|right)", str(style))
            if match:
                token.attrSet("class", f"al-{match.group(1)}")
        return renderer.renderToken(tokens, idx, options, env)

    md.add_render_rule("link_open", link_open)
    md.add_render_rule("table_open", table_open)
    md.add_render_rule("table_close", table_close)
    md.add_render_rule("th_open", cell_open)
    md.add_render_rule("td_open", cell_open)
    return md


def render_markdown(text: str) -> str:
    return _parser().render(text or "")


def _inline_text(token: Token) -> str:
    out = []
    for child in token.children or ():
        if child.type in ("text", "code_inline"):
            out.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def prose(text: str) -> tuple[str, bool]:
    """The prose before the first table, list, code block, quote or rule, as plain text,
    and whether the text holds more than that. Without any prose before such a block, the
    prose after it is taken."""
    parts: list[str] = []
    skipped = False
    for token in _parser().parse(text or ""):
        if token.type in STOP_BLOCKS and token.level == 0:
            if parts:
                return " ".join(parts), True
            skipped = True
        # top-level paragraphs and headings: their inline content sits at level 1
        if token.type == "inline" and token.level == 1:
            line = _inline_text(token)
            if line:
                parts.append(line)
    return " ".join(parts), skipped


def _items(text: str) -> str:
    """Every line of text outside tables and code, joined: the fallback for a message with
    no prose at all (a bare list)."""
    tokens = _parser().parse(text or "")
    in_table = False
    lines = []
    for token in tokens:
        if token.type == "table_open":
            in_table = True
        elif token.type == "table_close":
            in_table = False
        elif token.type == "inline" and not in_table:
            line = _inline_text(token)
            if line:
                lines.append(line)
    return " · ".join(lines)


def _trail(text: str) -> str:
    """An ellipsis, unless the text ends a sentence."""
    if text.endswith((".", "!", "?", "…")):
        return text
    return text.rstrip(" ,;:–-") + "…"


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> tuple[str, bool]:
    """A short plain-text version and whether there is more to show."""
    plain, more = prose(text)
    fallback = not plain
    if fallback:
        plain = _items(text)  # no prose at all: the lines of the list, the full view shows it
    if len(plain) > limit:
        return plain[:limit].rsplit(" ", 1)[0].rstrip(" ,;:–-.") + "…", True
    if more and not fallback:
        return _trail(plain), True
    return plain, more
