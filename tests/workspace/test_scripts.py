"""``coach workspace scripts``: the index of kept scripts, built from their headers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from thresy.cli import get_app
from thresy.workspace import default_template, index_scripts
from thresy.workspace.scripts import MAX_SCRIPT_BYTES

CLIMB = '''#!/usr/bin/env python
"""How has the time on one climb developed over the seasons?

Usage: python scripts/climb_history.py SEGMENT_KEY
    [--since YYYY-MM-DD] [--plot]
Output: one row per effort.
Candidate: a segment history with any climb definition; specific to this athlete: none
"""

import argparse
'''


def make_scripts(root: Path) -> Path:
    scripts = root / "scripts"
    (scripts / "data").mkdir(parents=True)
    files = {
        "climb_history.py": CLIMB,
        "reps.py": "# Which reps of a session were faster than the target?\n"
        "# Usage: python scripts/reps.py ACTIVITY_ID --target PACE\n\nimport sys\n",
        "weekly.sql": "-- How many kilometres per week this year?\n"
        '-- Usage: coach sql "$(cat scripts/weekly.sql)"\nSELECT 1;\n',
        "broken.py": '"""Does this still work?\n\nUsage: x\n"""\ndef (:\n',
        "bare.py": "print('no header')\n",
        "no_usage.sh": "#!/bin/sh\n# What did the watch record?\necho\n",
        "_shared.py": '"""Helpers."""\n',
        ".hidden.py": '"""Hidden."""\n',
        "data/table.csv": "a,b\n",
    }
    for rel, text in files.items():
        (scripts / rel).write_text(text)
    (scripts / "linked.py").symlink_to(scripts / "climb_history.py")
    return root


def by_path(index):
    return {entry.path: entry for entry in index.scripts}


def test_index_reads_headers_and_reports_problems(tmp_path):
    index = index_scripts(make_scripts(tmp_path))
    entries = by_path(index)
    assert sorted(entries) == [
        "scripts/bare.py",
        "scripts/broken.py",
        "scripts/climb_history.py",
        "scripts/linked.py",
        "scripts/no_usage.sh",
        "scripts/reps.py",
        "scripts/weekly.sql",
    ]
    climb = entries["scripts/climb_history.py"]
    assert climb.question == "How has the time on one climb developed over the seasons?"
    assert climb.usage == (
        "python scripts/climb_history.py SEGMENT_KEY [--since YYYY-MM-DD] [--plot]"
    )
    assert climb.candidate.startswith("a segment history with any climb definition")
    assert climb.problems == []
    reps = entries["scripts/reps.py"]  # comment header instead of a docstring
    assert reps.question == "Which reps of a session were faster than the target?"
    assert reps.usage == "python scripts/reps.py ACTIVITY_ID --target PACE"
    assert reps.candidate is None
    assert entries["scripts/weekly.sql"].question == "How many kilometres per week this year?"
    assert any(
        "does not parse as Python (line 5)" in p for p in entries["scripts/broken.py"].problems
    )
    assert entries["scripts/bare.py"].problems == ["no header: add one (playbooks/scripts.md)"]
    assert entries["scripts/no_usage.sh"].question == "What did the watch record?"
    assert entries["scripts/no_usage.sh"].problems == ["no Usage: line in the header"]
    assert "a link" in entries["scripts/linked.py"].problems[0]
    assert entries["scripts/linked.py"].question is None  # never followed

    text = index.text()
    assert text.startswith("7 kept script(s) in scripts/:")
    assert "candidate for a built-in command: a segment history" in text
    assert "PROBLEM: no header" in text


def test_a_huge_file_is_not_read(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "huge.py").write_text("#" * (MAX_SCRIPT_BYTES + 1))
    entry = index_scripts(tmp_path).scripts[0]
    assert entry.question is None and "header not read" in entry.problems[0]


def test_missing_and_empty_directory(tmp_path):
    missing = index_scripts(tmp_path)
    assert not missing.exists and "no kept scripts yet" in missing.text()
    (tmp_path / "scripts").mkdir()
    empty = index_scripts(tmp_path)
    assert (
        empty.exists and empty.scripts == [] and empty.text() == "no kept scripts in scripts/ yet"
    )


def test_the_playbook_example_header_is_indexed_as_described(tmp_path):
    """The example in workspace/playbooks/scripts.md must read the way the playbook says."""
    playbook = (default_template() / "playbooks" / "scripts.md").read_text()
    example = re.search(r"```python\n(.*?)```", playbook, re.DOTALL)
    assert example is not None
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "climb_history.py").write_text(example.group(1))
    entry = index_scripts(tmp_path).scripts[0]
    assert entry.problems == []
    assert entry.question and entry.question.endswith("?")
    assert entry.usage and entry.usage.startswith("python scripts/climb_history.py")


def test_cli(tmp_path, monkeypatch):
    make_scripts(tmp_path)
    monkeypatch.setenv("COACH_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # no .env
    from thresy.config import get_settings

    get_settings.cache_clear()
    try:
        runner = CliRunner()
        text = runner.invoke(get_app(), ["workspace", "scripts"])
        assert text.exit_code == 0, text.output
        assert "- scripts/reps.py: Which reps of a session were faster" in text.output
        data = json.loads(runner.invoke(get_app(), ["workspace", "scripts", "--json"]).output)
        assert data["exists"] is True and len(data["scripts"]) == 7
        other = runner.invoke(
            get_app(), ["workspace", "scripts", "--workspace", str(tmp_path / "x")]
        )
        assert other.exit_code == 0 and "no kept scripts yet" in other.output
    finally:
        get_settings.cache_clear()
