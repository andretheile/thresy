"""Markdown (as the agent writes it) to Telegram text with message entities.

Telegram's own Markdown and HTML parse modes fail the whole message on one unbalanced
character, so this module never uses them. It parses the Markdown with markdown-it
(CommonMark plus tables and strikethrough) and produces plain text plus a list of entities
(bold, italic, code, pre, links, ...). Sending text with explicit entities cannot fail to
parse; if Telegram rejects the entities anyway, the same text can be sent without them and
still reads well, because all Markdown syntax has already been removed.

* Headings become bold lines; lists get "•" or "1." prefixes; quotes become blockquotes;
  fenced code becomes a ``pre`` block.
* Tables (Telegram has none) become an aligned monospace block when they are narrow enough
  for a phone, and a list with one line per row otherwise.
* :func:`split` cuts long messages under Telegram's limit at paragraph, line, sentence or word
  boundaries (in that order of preference); entities that cross a cut are split as well.

Offsets in the entities are UTF-16 code units, as Telegram counts them.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
DEFAULT_CHUNK = 4000  # a little below TEXT_LIMIT
TABLE_MAX_WIDTH = 36  # characters of a monospace block that fit a phone screen

_LINK_SCHEMES = ("http://", "https://", "tg://", "mailto:")
_NO_NESTING = frozenset({"code", "pre"})


@dataclass(frozen=True)
class Entity:
    """A Telegram message entity; ``offset`` and ``length`` in UTF-16 code units."""

    type: str
    offset: int
    length: int
    url: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class Rich:
    """Text plus entities, ready for one Telegram message."""

    text: str
    entities: tuple[Entity, ...] = ()


@dataclass
class _Span:
    type: str
    start: int
    end: int = -1
    url: str | None = None
    language: str | None = None


def utf16_len(text: str) -> int:
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


# Rendering ------------------------------------------------------------------------------------


@dataclass
class _Builder:
    parts: list[str] = field(default_factory=list)
    length: int = 0
    spans: list[_Span] = field(default_factory=list)
    stack: list[_Span] = field(default_factory=list)

    def write(self, text: str) -> None:
        if text:
            self.parts.append(text)
            self.length += len(text)

    def ends_with_newline(self) -> bool:
        return not self.parts or self.parts[-1].endswith("\n")

    def open(self, kind: str, **kw: Any) -> _Span | None:
        open_types = {s.type for s in self.stack}
        if kind in open_types or open_types & _NO_NESTING:
            return None  # no same-type nesting, nothing inside code/pre
        span = _Span(kind, self.length, **kw)
        self.stack.append(span)
        return span

    def close(self, span: _Span | None) -> None:
        if span is None:
            return
        self.stack.remove(span)
        span.end = self.length
        if span.end > span.start:
            self.spans.append(span)

    def in_quote(self) -> bool:
        return any(s.type == "blockquote" for s in self.stack)

    def text(self) -> str:
        return "".join(self.parts)


def _parser():
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def _tree(markdown: str):
    from markdown_it.tree import SyntaxTreeNode

    return SyntaxTreeNode(_parser().parse(markdown))


def _render_blocks(nodes: list[Any], b: _Builder, indent: str, sep: str) -> None:
    first = True
    for node in nodes:
        if not first:
            b.write(sep)
        first = False
        _render_block(node, b, indent)


def _render_block(node: Any, b: _Builder, indent: str) -> None:
    kind = node.type
    if kind == "paragraph":
        _render_inline_container(node, b)
    elif kind == "heading":
        span = b.open("bold")
        _render_inline_container(node, b)
        b.close(span)
    elif kind in ("bullet_list", "ordered_list"):
        _render_list(node, b, indent)
    elif kind == "blockquote":
        span = None if b.in_quote() else b.open("blockquote")
        _render_blocks(node.children, b, indent, "\n")
        b.close(span)
    elif kind in ("fence", "code_block"):
        code = node.content.rstrip("\n") or " "
        if b.in_quote():
            b.write(code)  # pre inside a blockquote is not reliably accepted
            return
        language = (node.info or "").strip().split(" ")[0] if kind == "fence" else ""
        span = b.open("pre", language=language or None)
        b.write(code)
        b.close(span)
    elif kind == "hr":
        b.write("———")
    elif kind == "table":
        _render_table(node, b)
    elif kind == "html_block":
        b.write(node.content.strip("\n"))
    elif kind == "inline":
        _render_inline(node.children, b)
    else:  # anything new: render its children as blocks
        _render_blocks(node.children, b, indent, "\n")


def _render_list(node: Any, b: _Builder, indent: str) -> None:
    ordered = node.type == "ordered_list"
    number = 1
    if ordered and node.children:
        try:
            number = int(node.children[0].info or 1)
        except ValueError:
            number = 1
    bullet = "•" if not indent else "◦"
    first = True
    for item in node.children:
        if not first:
            b.write("\n")
        first = False
        marker = f"{number}." if ordered else bullet
        number += 1
        b.write(f"{indent}{marker} ")
        _render_blocks(item.children, b, indent + "   ", "\n")


def _render_inline_container(node: Any, b: _Builder) -> None:
    for child in node.children:
        if child.type == "inline":
            _render_inline(child.children, b)
        else:
            _render_block(child, b, "")


def _render_inline(nodes: list[Any], b: _Builder) -> None:
    for node in nodes:
        kind = node.type
        if kind == "text":
            b.write(node.content)
        elif kind in ("softbreak", "hardbreak"):
            b.write("\n")
        elif kind in ("strong", "em", "s"):
            span = b.open({"strong": "bold", "em": "italic", "s": "strikethrough"}[kind])
            _render_inline(node.children, b)
            b.close(span)
        elif kind == "code_inline":
            span = b.open("code")
            b.write(node.content)
            b.close(span)
        elif kind == "link":
            _render_link(node, b)
        elif kind == "image":
            alt = _plain_inline(node.children).strip()
            src = str(node.attrs.get("src", ""))
            b.write(f"{alt} ({src})" if alt else src)
        elif kind == "html_inline":
            b.write(node.content)
        else:
            _render_inline(node.children, b)


def _render_link(node: Any, b: _Builder) -> None:
    href = str(node.attrs.get("href", ""))
    label = _plain_inline(node.children)
    if not label.strip():
        b.write(href)
        return
    if label == href or node.markup == "autolink" or not href.startswith(_LINK_SCHEMES):
        # A bare URL is linked by Telegram itself; an unsupported scheme stays visible.
        _render_inline(node.children, b)
        if label != href and href and not href.startswith(_LINK_SCHEMES):
            b.write(f" ({href})")
        return
    span = b.open("text_link", url=href)
    _render_inline(node.children, b)
    b.close(span)


def _plain_inline(nodes: list[Any]) -> str:
    out: list[str] = []
    for node in nodes:
        if node.type in ("text", "code_inline", "html_inline"):
            out.append(node.content)
        elif node.type in ("softbreak", "hardbreak"):
            out.append(" ")
        elif node.type == "image":
            out.append(_plain_inline(node.children))
        else:
            out.append(_plain_inline(node.children))
    return "".join(out)


# Tables -------------------------------------------------------------------------------------


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int, right: bool) -> str:
    gap = " " * max(0, width - _display_width(text))
    return gap + text if right else text + gap


def _table_rows(node: Any) -> tuple[list[str], list[list[str]], list[bool]]:
    header: list[str] = []
    rows: list[list[str]] = []
    align_right: list[bool] = []
    for section in node.children:
        for tr in section.children:
            cells = []
            for cell in tr.children:
                inline = cell.children[0].children if cell.children else []
                cells.append(" ".join(_plain_inline(inline).split()))
                if section.type == "thead":
                    style = str(cell.attrs.get("style", "")) if cell.attrs else ""
                    align_right.append("right" in style)
            if section.type == "thead":
                header = cells
            else:
                rows.append(cells)
    columns = max([len(header)] + [len(r) for r in rows] or [0])
    header += [""] * (columns - len(header))
    rows = [r + [""] * (columns - len(r)) for r in rows]
    align_right += [False] * (columns - len(align_right))
    return header, rows, align_right


def _render_table(node: Any, b: _Builder) -> None:
    header, rows, right = _table_rows(node)
    if not header and not rows:
        return
    widths = [max(_display_width(r[i]) for r in [header, *rows]) for i in range(len(header))]
    total = sum(widths) + 2 * (len(widths) - 1)
    if total <= TABLE_MAX_WIDTH and not b.in_quote():
        lines = ["  ".join(_pad(c, w, r) for c, w, r in zip(header, widths, right, strict=True))]
        lines.append("─" * total)
        lines += [
            "  ".join(_pad(c, w, r) for c, w, r in zip(row, widths, right, strict=True))
            for row in rows
        ]
        span = b.open("pre")
        b.write("\n".join(line.rstrip() for line in lines))
        b.close(span)
        return
    # Too wide for a phone: one list line per row, "first cell: Header value · Header value".
    if not rows:
        b.write(" · ".join(c for c in header if c))
        return
    first = True
    for row in rows:
        if not first:
            b.write("\n")
        first = False
        b.write("• ")
        span = b.open("bold")
        b.write(row[0] or "—")
        b.close(span)
        pairs = []
        for head, cell in zip(header[1:], row[1:], strict=True):
            if not cell:
                continue
            pairs.append(f"{head}: {cell}" if head else cell)
        if pairs:
            b.write(": " + " · ".join(pairs))


# Public API -----------------------------------------------------------------------------------


def render(markdown: str) -> tuple[str, list[_Span]]:
    """Markdown to plain text plus spans (offsets in code points)."""
    b = _Builder()
    _render_blocks(_tree(markdown).children, b, "", "\n\n")
    text = b.text()
    stripped = text.rstrip()
    spans = []
    for span in sorted(b.spans, key=lambda s: (s.start, -s.end)):
        span.end = min(span.end, len(stripped))
        if span.end > span.start:
            spans.append(span)
    return stripped, spans


def _to_entities(text: str, spans: list[_Span], start: int, end: int, u16: list[int]):
    entities = []
    for span in spans:
        s, e = max(span.start, start), min(span.end, end)
        if e <= s:
            continue
        if span.type not in _NO_NESTING:
            # Entities on pure whitespace are pointless and sometimes rejected.
            while s < e and text[s].isspace():
                s += 1
            while e > s and text[e - 1].isspace():
                e -= 1
            if e <= s:
                continue
        entities.append(
            Entity(
                type=span.type,
                offset=u16[s] - u16[start],
                length=u16[e] - u16[s],
                url=span.url,
                language=span.language,
            )
        )
    return tuple(entities)


_SENTENCE_END = re.compile(r"[.!?…](?=\s)")


def _cut_point(text: str, start: int, hard: int) -> int:
    """Best index in (start, hard] to end a chunk."""
    window = text[start:hard]
    floor = len(window) // 3  # do not produce tiny chunks
    for sep in ("\n\n", "\n"):
        pos = window.rfind(sep)
        if pos > floor:
            return start + pos + len(sep)
    ends = [m.end() for m in _SENTENCE_END.finditer(window) if m.end() > floor]
    if ends:
        return start + ends[-1]
    pos = window.rfind(" ")
    if pos > floor:
        return start + pos + 1
    return hard


def split(text: str, spans: list[_Span] | None = None, limit: int = DEFAULT_CHUNK) -> list[Rich]:
    """Cut text (with code-point spans) into chunks of at most ``limit`` UTF-16 units."""
    if limit < 16:
        raise ValueError("limit too small")
    spans = spans or []
    u16 = [0]
    for ch in text:
        u16.append(u16[-1] + (2 if ord(ch) > 0xFFFF else 1))
    chunks: list[Rich] = []
    start = 0
    n = len(text)
    while start < n:
        if u16[n] - u16[start] <= limit:
            end = n
        else:
            hard = start
            while hard < n and u16[hard + 1] - u16[start] <= limit:
                hard += 1
            end = _cut_point(text, start, hard)
        s, e = start, end
        while s < e and text[s] in " \n":
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            chunks.append(Rich(text[s:e], _to_entities(text, spans, s, e, u16)))
        start = end
    return chunks


def format_markdown(markdown: str, limit: int = DEFAULT_CHUNK) -> list[Rich]:
    """Markdown to one or more Telegram-ready chunks.

    Never raises for odd input: if rendering fails, the raw text is split without entities.
    """
    try:
        text, spans = render(markdown)
    except Exception:  # the renderer must never lose a message
        log.exception("markdown rendering failed; sending plain text")
        return split(markdown.strip(), [], limit)
    return split(text, spans, limit)


def plain(chunk: Rich) -> Rich:
    """The same chunk without entities (the fallback when Telegram rejects them)."""
    return Rich(chunk.text)
