"""Reading and writing in directories the agent controls: no link is ever followed."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from thresy.transport import files
from thresy.transport.files import FileRefused, create_file_under, read_file_under

SECRET = b"TELEGRAM_TOKEN=not-for-the-agent"


@pytest.fixture
def out(tmp_path: Path) -> Path:
    (tmp_path / "workspace" / "out").mkdir(parents=True)
    (tmp_path / "bridge-only").mkdir()
    (tmp_path / "bridge-only" / "secret.txt").write_bytes(SECRET)
    return tmp_path / "workspace" / "out"


def test_a_regular_file_is_read(out: Path) -> None:
    (out / "sub").mkdir()
    (out / "sub" / "plot.png").write_bytes(b"png")
    assert read_file_under(out, ("sub", "plot.png"), max_bytes=10) == b"png"


def test_a_link_as_the_file_is_refused(out: Path) -> None:
    (out / "plot.png").symlink_to(out.parent.parent / "bridge-only" / "secret.txt")
    with pytest.raises(FileRefused, match=r"out/plot.png is a symbolic link"):
        read_file_under(out, ("plot.png",), max_bytes=1000)


def test_a_link_as_a_parent_directory_is_refused(out: Path) -> None:
    (out / "sub").symlink_to(out.parent.parent / "bridge-only")
    with pytest.raises(FileRefused, match=r"out/sub is a symbolic link"):
        read_file_under(out, ("sub", "secret.txt"), max_bytes=1000)


def test_a_link_as_the_root_is_refused(out: Path) -> None:
    out.rmdir()
    out.symlink_to(out.parent.parent / "bridge-only")
    with pytest.raises(FileRefused, match=r"^out is a symbolic link"):
        read_file_under(out, ("secret.txt",), max_bytes=1000)


def test_dot_dot_is_refused(out: Path) -> None:
    with pytest.raises(FileRefused, match="not allowed"):
        read_file_under(out, ("..", "..", "bridge-only", "secret.txt"), max_bytes=1000)


def test_only_regular_files_of_one_name_within_the_limit(out: Path) -> None:
    (out / "dir").mkdir()
    with pytest.raises(FileRefused, match="not a regular file"):
        read_file_under(out, ("dir",), max_bytes=1000)
    (out / "empty.png").write_bytes(b"")
    with pytest.raises(FileRefused, match="empty"):
        read_file_under(out, ("empty.png",), max_bytes=1000)
    (out / "big.png").write_bytes(b"x" * 2000)
    with pytest.raises(FileRefused, match="MB; the limit"):
        read_file_under(out, ("big.png",), max_bytes=1000)
    (out / "twice.png").write_bytes(b"x")
    os.link(out / "twice.png", out / "again.png")
    with pytest.raises(FileRefused, match="hard link"):
        read_file_under(out, ("again.png",), max_bytes=1000)
    with pytest.raises(FileRefused, match="does not exist"):
        read_file_under(out, ("missing.png",), max_bytes=1000)


def test_a_fifo_is_refused_without_blocking(out: Path) -> None:
    os.mkfifo(out / "pipe.png")
    done: list[str] = []

    def read() -> None:
        try:
            read_file_under(out, ("pipe.png",), max_bytes=1000)
        except FileRefused as exc:
            done.append(str(exc))

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(5)
    assert done and "not a regular file" in done[0]


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="the kernel tells an open file's path only on Linux and macOS",
)
def test_the_open_file_is_located_inside_the_root(out: Path, monkeypatch) -> None:
    (out / "plot.png").write_bytes(b"png")
    fd = os.open(out / "plot.png", os.O_RDONLY)
    try:
        where = files.fd_path(fd)
    finally:
        os.close(fd)
    assert where is not None and files._inside(where, out)
    # A directory moved out of out/ while the file was being opened: refused.
    monkeypatch.setattr(files, "fd_path", lambda fd: str(out.parent / "elsewhere" / "plot.png"))
    with pytest.raises(FileRefused, match="no longer inside out/"):
        read_file_under(out, ("plot.png",), max_bytes=1000)


def test_create_makes_directories_and_never_overwrites(tmp_path: Path) -> None:
    inbox = tmp_path / "workspace" / "inbox"
    inbox.parent.mkdir()
    path = create_file_under(inbox, ("2030-01-02",), "10-photo.jpg", b"jpeg")
    assert path == inbox / "2030-01-02" / "10-photo.jpg" and path.read_bytes() == b"jpeg"
    with pytest.raises(FileExistsError):
        create_file_under(inbox, ("2030-01-02",), "10-photo.jpg", b"other")
    assert path.read_bytes() == b"jpeg"


def test_create_refuses_links(tmp_path: Path) -> None:
    inbox = tmp_path / "workspace" / "inbox"
    (inbox / "2030-01-02").mkdir(parents=True)
    victim = tmp_path / "bridge-only.txt"
    victim.write_bytes(SECRET)
    # A planted link with the name the next file will get is not written through.
    (inbox / "2030-01-02" / "11-photo.jpg").symlink_to(victim)
    with pytest.raises(FileExistsError):
        create_file_under(inbox, ("2030-01-02",), "11-photo.jpg", b"overwritten")
    assert victim.read_bytes() == SECRET
    # A day directory that is a link is refused.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (inbox / "2030-01-03").symlink_to(elsewhere)
    with pytest.raises(FileRefused, match="inbox/2030-01-03 is a symbolic link"):
        create_file_under(inbox, ("2030-01-03",), "12-photo.jpg", b"jpeg")
    assert list(elsewhere.iterdir()) == []
