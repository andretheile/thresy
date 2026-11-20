"""``coach workspace init`` with a synthetic template."""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from thresy.cli import get_app
from thresy.workspace import WorkspaceError, import_memory, import_scripts, init_workspace


def make_template(root: Path) -> Path:
    template = root / "template"
    files = {
        "AGENTS.md": "# Coach\nGeneric instructions.\n",
        "CLAUDE.md": "@AGENTS.md\n",
        "playbooks/chat.md": "chat v1\n",
        "playbooks/morning-briefing.md": "briefing v1\n",
        "memory/profile.md": "# Profile\n(skeleton)\n",
        "memory/goals.md": "# Goals\n(skeleton)\n",
        "out/.gitkeep": "",
        "inbox/.gitkeep": "",
    }
    for rel, text in files.items():
        path = template / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return template


def tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_seeds_an_empty_workspace(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    result = init_workspace(workspace, template)
    assert result.seeded
    assert tree(workspace) == tree(template)
    assert sorted(result.added) == sorted(tree(template))
    assert "seeded from the template: 8 file(s)" in result.text()
    assert (workspace / "scripts").is_dir() and not any((workspace / "scripts").iterdir())


def test_out_and_inbox_do_not_make_a_workspace_non_empty(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    (workspace / "inbox" / "2030-01-02").mkdir(parents=True)
    (workspace / "inbox" / "2030-01-02" / "1-photo.jpg").write_bytes(b"jpg")
    (workspace / "out").mkdir()
    result = init_workspace(workspace, template)
    assert result.seeded
    assert (workspace / "memory" / "profile.md").exists()
    assert (workspace / "inbox" / "2030-01-02" / "1-photo.jpg").read_bytes() == b"jpg"


def test_later_runs_update_only_template_owned_files(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    # The agent and the athlete work in the workspace.
    (workspace / "memory" / "profile.md").write_text("# Profile\nreal athlete notes\n")
    (workspace / "memory" / "goals.md").unlink()
    (workspace / "out" / "plot.png").write_bytes(b"png")
    (workspace / "notes.md").write_text("agent's own notes\n")
    (workspace / "playbooks" / "agent-written.md").write_text("mine\n")
    # The template moves on.
    (template / "AGENTS.md").write_text("# Coach\nNew instructions.\n")
    (template / "playbooks" / "chat.md").write_text("chat v2\n")
    (template / "playbooks" / "race-week.md").write_text("race week\n")
    (template / "playbooks" / "morning-briefing.md").unlink()
    (template / "memory" / "races.md").write_text("# Races\n(skeleton)\n")
    (template / "memory" / "profile.md").write_text("# Profile\n(new skeleton)\n")

    result = init_workspace(workspace, template)
    assert not result.seeded
    assert sorted(result.updated) == ["AGENTS.md", "playbooks/chat.md"]
    assert result.added == ["playbooks/race-week.md"]
    assert result.unchanged == ["CLAUDE.md"]
    assert result.not_copied == ["memory/goals.md", "memory/races.md"]
    assert sorted(result.stale) == ["playbooks/agent-written.md", "playbooks/morning-briefing.md"]
    assert (workspace / "AGENTS.md").read_text() == "# Coach\nNew instructions.\n"
    assert (workspace / "playbooks" / "chat.md").read_text() == "chat v2\n"
    # Never touched:
    assert (workspace / "memory" / "profile.md").read_text() == "# Profile\nreal athlete notes\n"
    assert not (workspace / "memory" / "goals.md").exists()
    assert not (workspace / "memory" / "races.md").exists()
    assert (workspace / "out" / "plot.png").read_bytes() == b"png"
    assert (workspace / "notes.md").read_text() == "agent's own notes\n"
    assert (workspace / "playbooks" / "agent-written.md").read_text() == "mine\n"
    assert (workspace / "playbooks" / "morning-briefing.md").read_text() == "briefing v1\n"
    text = result.text()
    assert "updated  AGENTS.md" in text and "kept     playbooks/agent-written.md" in text
    assert "skipped  memory/races.md" in text


def test_second_run_without_changes(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    result = init_workspace(workspace, template)
    assert not result.seeded and not result.added and not result.updated
    assert "template files are up to date (4 checked)" in result.text()


def test_recreates_missing_out_inbox_and_scripts(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    for name in ("out", "inbox", "scripts"):
        for path in (workspace / name).iterdir():
            path.unlink()
        (workspace / name).rmdir()
    result = init_workspace(workspace, template)
    assert sorted(result.created_dirs) == ["inbox", "out", "scripts"]
    assert all((workspace / name).is_dir() for name in ("out", "inbox", "scripts"))


def test_kept_scripts_are_never_touched(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    kept = {
        "scripts/climb_history.py": '"""Agent\'s own."""\n',
        "scripts/_shared.py": "X = 1\n",
        "scripts/data/notes.txt": "kept\n",
    }
    for rel, text in kept.items():
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text(text)
    # A later template that ships files under scripts/, one with the agent's file name.
    (template / "scripts").mkdir()
    (template / "scripts" / "climb_history.py").write_text('"""Template\'s."""\n')
    (template / "scripts" / "example.py").write_text('"""Example."""\n')

    result = init_workspace(workspace, template)
    assert not result.seeded
    assert not any(p.startswith("scripts") for p in result.added + result.updated + result.stale)
    assert result.not_copied == ["scripts/example.py"]  # reported, never written
    assert {rel: (workspace / rel).read_text() for rel in kept} == kept
    assert not (workspace / "scripts" / "example.py").exists()


def test_a_workspace_holding_only_kept_scripts_is_not_seeded_over(tmp_path):
    """A restore that brings scripts/ back before the first start must not count as empty."""
    template = make_template(tmp_path)
    (template / "scripts").mkdir()
    (template / "scripts" / "climb_history.py").write_text('"""Template\'s."""\n')
    workspace = tmp_path / "ws"
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts" / "climb_history.py").write_text('"""Restored."""\n')
    result = init_workspace(workspace, template)
    assert not result.seeded
    assert (workspace / "scripts" / "climb_history.py").read_text() == '"""Restored."""\n'
    assert (workspace / "AGENTS.md").exists()  # template-owned files still arrive


def test_a_link_in_place_of_a_created_directory_is_left_alone(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    (workspace / "scripts").rmdir()
    (workspace / "scripts").symlink_to(tmp_path / "nowhere")  # dangling
    result = init_workspace(workspace, template)
    assert "scripts" not in result.created_dirs
    assert (workspace / "scripts").is_symlink() and not (tmp_path / "nowhere").exists()


def test_a_symlink_in_place_of_a_template_file_is_replaced_not_followed(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch\n")
    (workspace / "AGENTS.md").unlink()
    (workspace / "AGENTS.md").symlink_to(outside)
    result = init_workspace(workspace, template)
    assert result.updated == ["AGENTS.md"]
    assert not (workspace / "AGENTS.md").is_symlink()
    assert outside.read_text() == "do not touch\n"


def test_shared_workspace_gets_group_writable_modes(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    workspace.chmod(0o2770)
    init_workspace(workspace, template)
    assert stat.S_IMODE((workspace / "memory").stat().st_mode) == 0o2770
    assert stat.S_IMODE((workspace / "playbooks").stat().st_mode) == 0o2770
    assert stat.S_IMODE((workspace / "memory" / "profile.md").stat().st_mode) == 0o660
    assert stat.S_IMODE((workspace / "AGENTS.md").stat().st_mode) == 0o660


def test_conflict_is_reported(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    (workspace / "CLAUDE.md").unlink()
    (workspace / "CLAUDE.md").mkdir()
    result = init_workspace(workspace, template)
    assert result.conflicts == ["CLAUDE.md"]


def test_missing_template(tmp_path):
    with pytest.raises(WorkspaceError, match="no workspace template"):
        init_workspace(tmp_path / "ws", tmp_path / "nothing")


def test_import_memory_from_a_directory(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    private = tmp_path / "private-memory"
    (private / "sub").mkdir(parents=True)
    (private / "profile.md").write_text("real profile\n")
    (private / "sub" / "history.md").write_text("history\n")
    (private / "link.md").symlink_to(private / "profile.md")
    written = import_memory(workspace, private)
    assert written == ["memory/profile.md", "memory/sub/history.md"]
    assert (workspace / "memory" / "profile.md").read_text() == "real profile\n"
    assert (workspace / "memory" / "goals.md").read_text() == "# Goals\n(skeleton)\n"
    assert not (workspace / "memory" / "link.md").exists()


def _tar(members: dict[str, bytes], *, extra: tarfile.TarInfo | None = None) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if extra is not None:
            archive.addfile(extra)
    buf.seek(0)
    return buf


def test_import_memory_from_a_tar_stream(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    written = import_memory(workspace, _tar({"./profile.md": b"p\n", "./.hidden.md": b"h\n"}))
    assert written == ["memory/profile.md", "memory/.hidden.md"]
    assert (workspace / "memory" / ".hidden.md").read_bytes() == b"h\n"


@pytest.mark.parametrize("name", ["../escape.md", "/etc/passwd", "a/../../escape.md"])
def test_import_memory_refuses_unsafe_paths(tmp_path, name):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(WorkspaceError, match="unsafe path"):
        import_memory(workspace, _tar({name: b"x"}))
    assert not (tmp_path / "escape.md").exists()


def test_import_memory_refuses_links(tmp_path):
    link = tarfile.TarInfo("evil.md")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(WorkspaceError, match="only regular files"):
        import_memory(tmp_path, _tar({}, extra=link))


def test_import_scripts_restores_kept_scripts(tmp_path):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    init_workspace(workspace, template)
    (workspace / "scripts" / "old.py").write_text("old\n")
    written = import_scripts(workspace, _tar({"./climb.py": b"new\n", "_shared.py": b"s\n"}))
    assert written == ["scripts/climb.py", "scripts/_shared.py"]
    assert (workspace / "scripts" / "climb.py").read_bytes() == b"new\n"
    assert (workspace / "scripts" / "old.py").read_text() == "old\n"  # others stay
    with pytest.raises(WorkspaceError, match=r"unsafe path in the files for scripts/"):
        import_scripts(workspace, _tar({"../AGENTS.md": b"x"}))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for path in (workspace / "scripts").iterdir():
        path.unlink()
    (workspace / "scripts").rmdir()
    (workspace / "scripts").symlink_to(elsewhere)
    with pytest.raises(WorkspaceError, match="is a link"):
        import_scripts(workspace, _tar({"climb.py": b"x"}))
    assert not any(elsewhere.iterdir())


def test_cli_init_and_memory(tmp_path, monkeypatch):
    template = make_template(tmp_path)
    workspace = tmp_path / "ws"
    monkeypatch.setenv("COACH_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("COACH_WORKSPACE_TEMPLATE", str(template))
    monkeypatch.chdir(tmp_path)  # no .env
    from thresy.config import get_settings

    get_settings.cache_clear()
    runner = CliRunner()
    first = runner.invoke(get_app(), ["workspace", "init"])
    assert first.exit_code == 0, first.output
    assert "seeded from the template" in first.output
    stream = _tar({"profile.md": b"from stdin\n"}).getvalue()
    second = runner.invoke(
        get_app(), ["workspace", "init", "--memory-from", "-", "--json"], input=stream
    )
    assert second.exit_code == 0, second.output
    data = json.loads(second.output)
    assert data["seeded"] is False and data["memory_imported"] == ["memory/profile.md"]
    assert (workspace / "memory" / "profile.md").read_text() == "from stdin\n"
    restored = tmp_path / "restored-scripts"
    restored.mkdir()
    (restored / "climb.py").write_text('"""Q?"""\n')
    third = runner.invoke(
        get_app(),
        ["workspace", "init", "--scripts-from", str(restored), "--memory-from", "-"],
        input=_tar({"goals.md": b"g\n"}).getvalue(),
    )
    assert third.exit_code == 0, third.output
    assert (
        "script   scripts/climb.py" in third.output and "memory   memory/goals.md" in third.output
    )
    both = runner.invoke(
        get_app(), ["workspace", "init", "--scripts-from", "-", "--memory-from", "-"]
    )
    assert both.exit_code == 1 and "only one of" in both.output
    bad = runner.invoke(get_app(), ["workspace", "init", "--template", str(tmp_path / "none")])
    assert bad.exit_code == 1 and "no workspace template" in bad.output
    get_settings.cache_clear()


def test_default_template_is_the_repository_workspace(monkeypatch):
    from thresy.workspace import default_template

    monkeypatch.delenv("COACH_WORKSPACE_TEMPLATE", raising=False)
    assert default_template() == Path(__file__).resolve().parents[2] / "workspace"
    monkeypatch.setenv("COACH_WORKSPACE_TEMPLATE", "/somewhere/else")
    assert default_template() == Path("/somewhere/else")
    assert os.sep == "/"
