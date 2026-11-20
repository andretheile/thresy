"""``coach workspace scripts``: the index of the analysis scripts the coach keeps in ``scripts/``.

The index is built from each script's header every time it is asked for, so it cannot drift
from the files, as a list the coach kept by hand would. A header is a Python script's module
docstring, or the comment block at the top of any other script (``#`` or ``--``, after an
optional ``#!`` line). Its first line is the question the script answers; the lines starting
with ``Usage:`` and ``Candidate:`` (and their indented continuation lines) are picked out. The
convention is described for the coach in ``workspace/playbooks/scripts.md``.

Files directly in ``scripts/`` are indexed; names starting with ``.`` or ``_`` (shared helper
modules) and subdirectories are not. Nothing is executed: Python files are parsed, which also
reports a kept script that no longer parses. Links are reported, never followed, because the
backup skips them too.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

MAX_SCRIPT_BYTES = 256 * 1024


@dataclass
class ScriptEntry:
    path: str
    """Relative to the workspace (``scripts/<name>``)."""
    question: str | None = None
    usage: str | None = None
    candidate: str | None = None
    """Set when the script is marked as a candidate for a built-in command."""
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "question": self.question,
            "usage": self.usage,
            "candidate": self.candidate,
            "problems": self.problems,
        }


@dataclass
class ScriptIndex:
    directory: Path
    exists: bool
    scripts: list[ScriptEntry] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "directory": str(self.directory),
            "exists": self.exists,
            "scripts": [s.as_dict() for s in self.scripts],
        }

    def text(self) -> str:
        if not self.exists:
            return f"no scripts/ directory in {self.directory.parent}: no kept scripts yet"
        if not self.scripts:
            return "no kept scripts in scripts/ yet"
        lines = [f"{len(self.scripts)} kept script(s) in scripts/:"]
        for entry in self.scripts:
            lines.append(f"- {entry.path}: {entry.question or '(no header)'}")
            if entry.usage:
                lines.append(f"    usage: {entry.usage}")
            if entry.candidate:
                lines.append(f"    candidate for a built-in command: {entry.candidate}")
            lines += [f"    PROBLEM: {p}" for p in entry.problems]
        return "\n".join(lines)


def _comment_header(text: str) -> str | None:
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    header: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped and not header:
            continue
        marker = next((m for m in ("--", "#") if stripped.startswith(m)), None)
        if marker is None:
            break
        body = stripped[len(marker) :]
        header.append(body[1:] if body.startswith(" ") else body)
    return "\n".join(header).strip() or None


def _field(lines: list[str], name: str) -> str | None:
    """The value of ``Name: value`` plus its indented continuation lines."""
    for index, line in enumerate(lines):
        key, sep, value = line.strip().partition(":")
        if not sep or key.strip().lower() != name:
            continue
        parts = [value.strip()] if value.strip() else []
        for following in lines[index + 1 :]:
            if not following.strip() or not following[:1].isspace():
                break
            parts.append(following.strip())
        return " ".join(parts) or None
    return None


def read_entry(path: Path, rel: str) -> ScriptEntry:
    entry = ScriptEntry(path=rel)
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_SCRIPT_BYTES + 1)
    except OSError as exc:
        entry.problems.append(f"cannot be read: {exc.strerror or exc}")
        return entry
    if len(data) > MAX_SCRIPT_BYTES:
        entry.problems.append(f"larger than {MAX_SCRIPT_BYTES // 1024} KB; header not read")
        return entry
    text = data.decode("utf-8", errors="replace")
    header = None
    if path.suffix == ".py":
        try:
            header = ast.get_docstring(ast.parse(text))
        except (SyntaxError, ValueError) as exc:
            line = getattr(exc, "lineno", None)
            entry.problems.append(
                f"does not parse as Python{f' (line {line})' if line else ''}: fix or delete it"
            )
    header = header or _comment_header(text)
    if not header:
        if not entry.problems:  # a file that does not parse has that problem first
            entry.problems.append("no header: add one (playbooks/scripts.md)")
        return entry
    lines = header.splitlines()
    entry.question = next((line.strip() for line in lines if line.strip()), None)
    entry.usage = _field(lines, "usage")
    entry.candidate = _field(lines, "candidate")
    if entry.usage is None:
        entry.problems.append("no Usage: line in the header")
    return entry


def index_scripts(workspace: Path) -> ScriptIndex:
    """Index ``<workspace>/scripts/``."""
    directory = workspace.expanduser() / "scripts"
    if directory.is_symlink() or not directory.is_dir():
        return ScriptIndex(directory=directory, exists=False)
    index = ScriptIndex(directory=directory, exists=True)
    for path in sorted(directory.iterdir()):
        if path.name.startswith((".", "_")):
            continue
        rel = f"scripts/{path.name}"
        if path.is_symlink():
            index.scripts.append(
                ScriptEntry(rel, problems=["a link: not indexed and not backed up; keep a file"])
            )
        elif path.is_file():
            index.scripts.append(read_entry(path, rel))
    return index
