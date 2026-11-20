"""Reading and writing files in directories that the agent controls.

The agent owns the workspace: at any moment it can replace a file or a directory under
``out/`` or ``inbox/`` with a symbolic link, also between a check and the use of the path.
The bridge, and the transport running inside it, can read things the agent must not see (its
own environment under ``/proc/self``, its home directory), so neither ever resolves a path
there by name:

* :func:`read_file_under` opens the root directory and then every path component relative to
  the one before, each with ``O_NOFOLLOW``, checks the file it opened (a regular file with one
  name, inside the root, within the size limit) and returns what it read from that same open
  file. A link anywhere on the way (the root itself, a parent directory, the file) is refused.
* :func:`create_file_under` creates directories and a new file the same way, with ``O_EXCL``,
  so it never writes through a link or into a file that is already there.

The root's parent path is trusted (it comes from the configuration); the root's own name is
not (the agent can replace ``out`` itself).
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK  # a FIFO must not block the bridge
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_CHUNK = 1 << 20


class FileRefused(Exception):
    """The file cannot be used; the message names it (relative to the root's parent)."""


def _shown(root: Path, parts: Sequence[str]) -> str:
    return "/".join((root.name, *parts))


def _explain(dir_fd: int, name: str, shown: str, exc: OSError) -> FileRefused:
    """A readable reason for a failed open (the lstat only words the message)."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except OSError:
        mode = None
    if mode is not None and stat.S_ISLNK(mode):
        return FileRefused(f"{shown} is a symbolic link; files are never read through links")
    if exc.errno == errno.ENOENT:
        return FileRefused(f"{shown} does not exist")
    if exc.errno == errno.ENOTDIR:
        return FileRefused(f"{shown} is not a directory")
    return FileRefused(f"cannot open {shown}: {exc.strerror or exc}")


def _check_name(name: str, shown: str) -> None:
    if name in ("", ".", "..") or "/" in name:
        raise FileRefused(f"{shown}: path components '.', '..' and empty names are not allowed")


def _open_at(dir_fd: int, name: str, flags: int, shown: str, mode: int = 0o777) -> int:
    _check_name(name, shown)
    try:
        return os.open(name, flags, mode, dir_fd=dir_fd)
    except FileExistsError:
        raise
    except OSError as exc:
        raise _explain(dir_fd, name, shown, exc) from exc


def _open_root(root: Path, *, create: bool = False) -> int:
    try:
        parent = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise FileRefused(f"cannot open {root.parent}: {exc.strerror or exc}") from exc
    try:
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(root.name, 0o777, dir_fd=parent)
        return _open_at(parent, root.name, _DIR_FLAGS, root.name)
    finally:
        os.close(parent)


def _walk(root: Path, dirs: Sequence[str], *, create: bool = False) -> int:
    """An open descriptor of ``root/dirs...``, no component followed if it is a link."""
    fd = _open_root(root, create=create)
    try:
        for i, name in enumerate(dirs):
            shown = _shown(root, dirs[: i + 1])
            if create:
                _check_name(name, shown)
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o777, dir_fd=fd)
            next_fd = _open_at(fd, name, _DIR_FLAGS, shown)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def fd_path(fd: int) -> str | None:
    """Where the kernel says an open file is, if the platform can tell (Linux, macOS)."""
    if sys.platform.startswith("linux"):
        try:
            return str(Path(f"/proc/self/fd/{fd}").readlink())
        except OSError:
            return None
    if sys.platform == "darwin":
        import fcntl

        try:
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        except (OSError, AttributeError):
            return None
        return os.fsdecode(raw.split(b"\0", 1)[0])
    return None


def _inside(path: str, root: Path) -> bool:
    # The root's parent may be spelled through links (it is trusted); its last part is not.
    candidates = {str(root), str(root.parent.resolve() / root.name)}
    return any(path.startswith(c.rstrip("/") + "/") for c in candidates)


def read_file_under(root: Path, parts: Sequence[str], *, max_bytes: int) -> bytes:
    """The content of the regular file ``root/parts...``, read from one open descriptor.

    Refuses (:class:`FileRefused`) links anywhere on the way, anything but a regular file, a
    file with more than one name (hard link), an empty file, and a file larger than
    ``max_bytes`` (also if it grows while being read).
    """
    if not parts:
        raise FileRefused(f"{root.name}/ is a directory, not a file")
    shown = _shown(root, parts)
    dir_fd = _walk(root, parts[:-1])
    try:
        fd = _open_at(dir_fd, parts[-1], _READ_FLAGS, shown)
    finally:
        os.close(dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FileRefused(f"{shown} is not a regular file")
        if info.st_nlink != 1:
            raise FileRefused(f"{shown} has {info.st_nlink} names (a hard link); send a copy")
        if info.st_size > max_bytes:
            raise FileRefused(
                f"{shown} is {info.st_size / 1e6:.1f} MB; the limit is "
                f"{max_bytes // (1024 * 1024)} MB"
            )
        where = fd_path(fd)
        if where is not None and not _inside(where, root):
            raise FileRefused(f"{shown} is no longer inside {root.name}/ (it was moved)")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, _CHUNK))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise FileRefused(f"{shown} grew beyond {max_bytes // (1024 * 1024)} MB while being read")
    if not data:
        raise FileRefused(f"{shown} is empty")
    return data


def create_file_under(root: Path, dirs: Sequence[str], name: str, data: bytes) -> Path:
    """Write ``data`` to the new file ``root/dirs.../name``; returns its path.

    Missing directories (``root`` included) are created. Raises :class:`FileExistsError` if the
    name is taken (a planted link included) and :class:`FileRefused` for a link on the way.
    """
    shown = _shown(root, (*dirs, name))
    dir_fd = _walk(root, dirs, create=True)
    try:
        fd = _open_at(dir_fd, name, _CREATE_FLAGS, shown, 0o666)
    finally:
        os.close(dir_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    return root.joinpath(*dirs, name)
