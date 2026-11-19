"""Markdown to Telegram text + entities, and splitting under the size limit."""

from __future__ import annotations

import pytest

from thresy.transport.telegram import markdown as md
from thresy.transport.telegram.markdown import Entity, format_markdown, utf16_len


def one(markdown: str) -> md.Rich:
    chunks = format_markdown(markdown)
    assert len(chunks) == 1, chunks
    return chunks[0]


def covered(chunk: md.Rich, entity: Entity) -> str:
    """The text an entity covers (offsets are UTF-16 units)."""
    encoded = chunk.text.encode("utf-16-le")
    return encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")


def kinds(chunk: md.Rich) -> list[tuple[str, str]]:
    return [(e.type, covered(chunk, e)) for e in chunk.entities]


def test_inline_formatting() -> None:
    chunk = one("Some **bold _nested_**, *it*, `code`, ~~gone~~ and [a link](https://example.com).")
    assert chunk.text == "Some bold nested, it, code, gone and a link."
    assert kinds(chunk) == [
        ("bold", "bold nested"),
        ("italic", "nested"),
        ("italic", "it"),
        ("code", "code"),
        ("strikethrough", "gone"),
        ("text_link", "a link"),
    ]
    assert chunk.entities[-1].url == "https://example.com"


def test_unbalanced_and_odd_markup_stays_readable() -> None:
    chunk = one("**unbalanced bold and *half italic and snake_case_name and 3 * 4 < 5 & <b>x</b>")
    assert chunk.text == (
        "**unbalanced bold and *half italic and snake_case_name and 3 * 4 < 5 & <b>x</b>"
    )
    assert chunk.entities == ()


def test_headings_lists_and_quotes() -> None:
    chunk = one(
        "# Plan for **today**\n\n- easy 8 km\n- strides\n  1. four\n  2. six\n\n"
        "3. third\n4. fourth\n\n> keep it **easy**\n\n---\n\nDone"
    )
    assert chunk.text == (
        "Plan for today\n\n"
        "• easy 8 km\n• strides\n   1. four\n   2. six\n\n"
        "3. third\n4. fourth\n\n"
        "keep it easy\n\n———\n\nDone"
    )
    assert kinds(chunk) == [
        ("bold", "Plan for today"),  # no bold nested in bold
        ("blockquote", "keep it easy"),
        ("bold", "easy"),
    ]


def test_code_block_keeps_its_content_and_language() -> None:
    chunk = one("Run this:\n\n```python\nfor i in range(3):\n    print(i)  # **not bold**\n```")
    assert chunk.text == "Run this:\n\nfor i in range(3):\n    print(i)  # **not bold**"
    (pre,) = chunk.entities
    assert pre.type == "pre" and pre.language == "python"
    assert covered(chunk, pre) == "for i in range(3):\n    print(i)  # **not bold**"


def test_narrow_table_becomes_an_aligned_block() -> None:
    chunk = one(
        "Week:\n\n| Day | km | Pace |\n|---|--:|---|\n| Mon | 10.2 | 5:07 |\n| Tue | 8 | 5:30 |"
    )
    assert chunk.text.split("\n\n")[1] == (
        "Day    km  Pace\n───────────────\nMon  10.2  5:07\nTue     8  5:30"
    )
    assert [e.type for e in chunk.entities] == ["pre"]


def test_wide_table_becomes_a_list() -> None:
    chunk = one(
        "| Day | Distance | Pace | Avg HR | Notes |\n|---|---|---|---|---|\n"
        "| Monday | 10.2 km | 5:07/km | 142 | easy, **felt good** |\n"
        "| Tuesday | | 4:30/km | 165 | |"
    )
    assert chunk.text == (
        "• Monday: Distance: 10.2 km · Pace: 5:07/km · Avg HR: 142 · Notes: easy, felt good\n"
        "• Tuesday: Pace: 4:30/km · Avg HR: 165"
    )
    assert kinds(chunk) == [("bold", "Monday"), ("bold", "Tuesday")]


def test_emoji_offsets_are_utf16() -> None:
    chunk = one("🏃‍♂️ done 💪 **strong** finish")
    (bold,) = chunk.entities
    assert covered(chunk, bold) == "strong"
    assert bold.offset == utf16_len("🏃‍♂️ done 💪 ")


def test_links_with_other_schemes_and_autolinks() -> None:
    chunk = one("<https://example.org>, [file](ftp://files.example/x) and https://plain.example")
    # Bare and auto links are linked by Telegram itself; other schemes stay visible.
    assert (
        chunk.text == "https://example.org, file (ftp://files.example/x) and https://plain.example"
    )
    assert chunk.entities == ()
    unsafe = one("[click](javascript:alert(1))")
    assert unsafe.text == "[click](javascript:alert(1))" and unsafe.entities == ()


def test_long_message_is_split_at_paragraphs() -> None:
    paragraphs = [f"Paragraph {i}: " + "word " * 150 for i in range(12)]
    chunks = format_markdown("\n\n".join(paragraphs))
    assert len(chunks) > 1
    assert all(utf16_len(c.text) <= md.DEFAULT_CHUNK for c in chunks)
    assert all(c.text.startswith("Paragraph") for c in chunks)
    assert "".join(c.text.replace("\n\n", "") for c in chunks).count("Paragraph") == 12


def test_very_long_paragraph_is_split_at_sentences_then_words() -> None:
    sentences = " ".join(
        f"This is sentence number {i} of a very long paragraph." for i in range(400)
    )
    chunks = format_markdown(sentences)
    assert len(chunks) > 1
    for chunk in chunks:
        assert utf16_len(chunk.text) <= md.DEFAULT_CHUNK
        assert chunk.text.endswith(".")
    no_spaces = "x" * 9000
    chunks = format_markdown(no_spaces)
    assert [len(c.text) for c in chunks] == [4000, 4000, 1000]


def test_entities_crossing_a_cut_are_split() -> None:
    text = "**" + ("bold words " * 800).strip() + "**"
    chunks = format_markdown(text)
    assert len(chunks) == 3
    for chunk in chunks:
        (bold,) = chunk.entities
        assert bold.type == "bold" and bold.offset == 0
        assert bold.length == utf16_len(chunk.text)


def test_long_code_block_is_split_into_code_blocks() -> None:
    code = "\n".join(f"line {i:04d} = compute({i})" for i in range(400))
    chunks = format_markdown(f"```\n{code}\n```")
    assert len(chunks) == 3
    assert all(c.entities[0].type == "pre" for c in chunks)
    assert chunks[0].text.startswith("line 0000") and chunks[-1].text.endswith("compute(399)")


def test_split_limit_counts_utf16_units() -> None:
    chunks = md.split("😀" * 3000, [], limit=4000)
    assert [utf16_len(c.text) for c in chunks] == [4000, 2000]


def test_renderer_failure_falls_back_to_plain_text(monkeypatch) -> None:
    def broken(_):
        raise RuntimeError("bug")

    monkeypatch.setattr(md, "render", broken)
    chunks = format_markdown("**still** delivered")
    assert chunks == [md.Rich("**still** delivered")]


def test_empty_input() -> None:
    assert format_markdown("") == []
    assert format_markdown("   \n\n ") == []


def test_plain_drops_entities() -> None:
    chunk = one("**x**")
    assert md.plain(chunk) == md.Rich("x")


@pytest.mark.parametrize(
    "markdown",
    [
        "| a |\n|---|\n",
        "```\n```",
        "- \n-",
        "> > nested quote with ```code```",
        "*" * 50,
        "[](http://x)",
        "![alt](http://img.example/x.png)",
        "1) one\n2) two",
        "<div>block html</div>\n\ntext",
        "\x00 control \x1b chars",
    ],
)
def test_awkward_inputs_never_raise(markdown: str) -> None:
    for chunk in format_markdown(markdown):
        assert chunk.text
        for entity in chunk.entities:
            assert entity.length > 0
            assert entity.offset + entity.length <= utf16_len(chunk.text)
