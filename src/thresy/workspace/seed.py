"""``coach workspace init``: copy the template into the workspace, keep template files current.

Rules:

* **Empty workspace** (nothing in it but possibly ``out/``, ``inbox/`` and ``lost+found``):
  every template file is copied, the memory skeleton included.
* **Later runs:** only template-owned paths (:data:`TEMPLATE_OWNED`) are added or updated to the
  template's content. Files under ``playbooks/`` that the template no longer has are reported
  and left alone. Nothing else is written; template files outside the owned paths that are
  missing in the workspace (a new memory skeleton, say) are reported as not copied.
* ``out/`` and ``inbox/`` are created when missing (empty directories; the bridge needs them).
* Writes are atomic (temporary file in the same directory, then rename), so a reader never sees
  half a file, and an agent's symlink in place of a template file is replaced, not followed.
* **Shared workspace:** when the workspace directory has the setgid bit (the container image
  sets ``/workspace`` to 2770, group ``agent``), new directories get mode 2770 and new files
  0660, so the agent can edit what this command (run as the bridge's user) creates. Otherwise
  the process umask decides.

:func:`import_memory` copies an athlete's own memory files (a directory, or a tar archive on
stdin) into ``memory/``, replacing files of the same name. It runs only when asked.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO

TEMPLATE_OWNED: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "playbooks")
"""Paths the template keeps up to date on every run."""
AGENT_OWNED: tuple[str, ...] = ("memory", "out", "inbox")
"""Paths seeded once (``out/``, ``inbox/`` also created when missing) and never touched again."""
_IGNORED_FOR_EMPTY = frozenset({"out", "inbox", "lost+found"})
_SKIP_NAMES = frozenset({".DS_Store", "__pycache__"})
SHARED_DIR_MODE = 0o2770
SHARED_FILE_MODE = 0o660
MAX_MEMORY_FILE = 10 * 1024 * 1024


class WorkspaceError(RuntimeError):
    """The template or the workspace cannot be used."""


@dataclass
class InitResult:
    workspace: Path
    template: Path
    seeded: bool
    """True when the workspace was empty and the whole template was copied."""
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    not_copied: list[str] = field(default_factory=list)
    """Template files outside the template-owned paths that the workspace lacks."""
    stale: list[str] = field(default_factory=list)
    """Files under template-owned directories that the template no longer has (kept)."""
    conflicts: list[str] = field(default_factory=list)
    """Template-owned paths that are a directory on one side and a file on the other."""
    created_dirs: list[str] = field(default_factory=list)
    memory_imported: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "template": str(self.template),
            "seeded": self.seeded,
            "added": self.added,
            "updated": self.updated,
            "unchanged": len(self.unchanged),
            "not_copied": self.not_copied,
            "stale": self.stale,
            "conflicts": self.conflicts,
            "created_dirs": self.created_dirs,
            "memory_imported": self.memory_imported,
        }

    def text(self) -> str:
        head = f"workspace {self.workspace} (template {self.template})"
        lines = [head]
        if self.seeded:
            lines.append(f"seeded from the template: {len(self.added)} file(s)")
        elif not (self.added or self.updated):
            lines.append(f"template files are up to date ({len(self.unchanged)} checked)")
        lines += [f"  added    {p}" for p in self.added if not self.seeded]
        lines += [f"  updated  {p}" for p in self.updated]
        lines += [f"  created  {p}/" for p in self.created_dirs]
        lines += [
            f"  kept     {p} (not in the template any more; delete it if unwanted)"
            for p in self.stale
        ]
        lines += [
            f"  skipped  {p} (in the template but not in the workspace; not copied, this part "
            "belongs to the agent once seeded)"
            for p in self.not_copied
        ]
        lines += [
            f"  CONFLICT {p} (file on one side, directory on the other)" for p in self.conflicts
        ]
        lines += [f"  memory   {p}" for p in self.memory_imported]
        return "\n".join(lines)


def default_template() -> Path:
    """``COACH_WORKSPACE_TEMPLATE`` or the repository's ``workspace/`` (``/app/workspace`` in
    the image: the package is installed in editable mode next to it)."""
    value = os.environ.get("COACH_WORKSPACE_TEMPLATE", "").strip()
    if value:
        return Path(value).expanduser()
    return Path(__file__).resolve().parents[3] / "workspace"


def _template_files(template: Path) -> Iterator[tuple[PurePosixPath, Path]]:
    for path in sorted(template.rglob("*")):
        rel = path.relative_to(template)
        if any(part in _SKIP_NAMES for part in rel.parts):
            continue
        if path.is_file() and not path.is_symlink():
            yield PurePosixPath(rel.as_posix()), path


def _template_dirs(template: Path) -> list[PurePosixPath]:
    return [
        PurePosixPath(p.relative_to(template).as_posix())
        for p in sorted(template.rglob("*"))
        if p.is_dir() and not p.is_symlink() and p.name not in _SKIP_NAMES
    ]


def _is_empty(workspace: Path) -> bool:
    return all(entry.name in _IGNORED_FOR_EMPTY for entry in workspace.iterdir())


def _shared(workspace: Path) -> bool:
    return bool(workspace.stat().st_mode & stat.S_ISGID)


class _Writer:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.shared = _shared(workspace)

    def mkdir(self, path: Path) -> bool:
        """Create ``path`` and missing parents inside the workspace; True if it was created."""
        if path.is_dir():
            return False
        created = False
        for parent in reversed([path, *path.parents]):
            if parent == self.workspace or self.workspace not in parent.parents:
                continue
            if not parent.is_dir():
                parent.mkdir()
                if self.shared:
                    parent.chmod(SHARED_DIR_MODE)
                created = True
        return created

    def write(self, path: Path, data: bytes) -> None:
        self.mkdir(path.parent)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            mode = SHARED_FILE_MODE if self.shared else 0o666 & ~_umask()
            Path(tmp).chmod(mode)
            Path(tmp).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise


def _umask() -> int:
    current = os.umask(0o022)
    os.umask(current)
    return current


def _owned(rel: PurePosixPath) -> bool:
    return rel.parts[0] in TEMPLATE_OWNED


def init_workspace(workspace: Path, template: Path | None = None) -> InitResult:
    """Seed an empty workspace from the template, or refresh the template-owned files."""
    template = (template or default_template()).expanduser().resolve()
    if not (template / "AGENTS.md").is_file():
        raise WorkspaceError(
            f"no workspace template at {template} (expected AGENTS.md there; set "
            "COACH_WORKSPACE_TEMPLATE or pass --template)"
        )
    workspace = workspace.expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    workspace = workspace.resolve()
    if workspace == template or template in workspace.parents:
        raise WorkspaceError(f"the workspace {workspace} must not be inside the template")
    fresh = _is_empty(workspace)
    writer = _Writer(workspace)
    result = InitResult(workspace=workspace, template=template, seeded=fresh)

    dirs = _template_dirs(template)
    for rel in dirs:
        if fresh or _owned(rel) or rel.parts[0] in ("out", "inbox"):
            target = workspace / rel
            if target.exists() and not target.is_dir():
                result.conflicts.append(str(rel))
            elif writer.mkdir(target) and not fresh:
                result.created_dirs.append(str(rel))
    for name in ("out", "inbox"):
        if not (workspace / name).exists() and writer.mkdir(workspace / name) and not fresh:
            result.created_dirs.append(name)

    seen: set[PurePosixPath] = set()
    for rel, source in _template_files(template):
        seen.add(rel)
        target = workspace / rel
        if not fresh and not _owned(rel):
            if not target.exists() and not target.is_symlink():
                result.not_copied.append(str(rel))
            continue
        if target.is_dir() and not target.is_symlink():
            result.conflicts.append(str(rel))
            continue
        data = source.read_bytes()
        if target.is_file() and not target.is_symlink():
            try:
                # Size first: the agent owns the workspace and could leave a huge file here.
                if target.stat().st_size == len(data) and target.read_bytes() == data:
                    result.unchanged.append(str(rel))
                    continue
            except PermissionError:
                pass  # replace it all the same
            writer.write(target, data)
            result.updated.append(str(rel))
        else:
            existed = target.is_symlink()
            writer.write(target, data)
            (result.updated if existed else result.added).append(str(rel))

    if not fresh:
        for name in TEMPLATE_OWNED:
            root = workspace / name
            if not root.is_dir() or root.is_symlink():
                continue
            for path in sorted(root.rglob("*")):
                rel = PurePosixPath(path.relative_to(workspace).as_posix())
                if path.is_file() and rel not in seen and not path.name.startswith("."):
                    result.stale.append(str(rel))
    return result


def import_memory(workspace: Path, source: Path | IO[bytes]) -> list[str]:
    """Copy memory files into ``<workspace>/memory/``, replacing files of the same name.

    ``source`` is a directory (every regular file below it, symlinks skipped) or a binary
    stream with a tar archive (only regular files and directories, no absolute paths, nothing
    leading out of ``memory/``). Returns the paths written, relative to the workspace.
    """
    workspace = workspace.expanduser().resolve()
    memory = workspace / "memory"
    writer = _Writer(workspace)
    writer.mkdir(memory)
    written: list[str] = []

    def put(rel: PurePosixPath, data: bytes) -> None:
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise WorkspaceError(f"unsafe path in the memory files: {rel}")
        target = memory / rel
        if target.is_dir():
            raise WorkspaceError(f"memory/{rel} is a directory in the workspace")
        writer.write(target, data)
        written.append(f"memory/{rel}")

    if isinstance(source, Path):
        source = source.expanduser()
        if not source.is_dir():
            raise WorkspaceError(f"{source} is not a directory")
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = PurePosixPath(path.relative_to(source).as_posix())
            if any(part in _SKIP_NAMES for part in rel.parts):
                continue
            put(rel, path.read_bytes())
        return written

    with tarfile.open(fileobj=source, mode="r|*") as archive:
        for member in archive:
            raw = member.name
            while raw.startswith("./"):
                raw = raw[2:]
            name = PurePosixPath(raw or ".")
            if str(name) == "." or member.isdir():
                continue
            if not member.isreg():
                raise WorkspaceError(f"only regular files may be imported, not {member.name}")
            if member.size > MAX_MEMORY_FILE:
                raise WorkspaceError(f"{member.name} is larger than {MAX_MEMORY_FILE} bytes")
            fh = archive.extractfile(member)
            if fh is None:
                continue
            put(name, fh.read())
    return written
