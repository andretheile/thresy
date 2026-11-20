# Writing and keeping analysis scripts

Some questions no `coach` command answers: a climb's history with a definition the segment commands do not have, the reps of one session with breathing rate, this year's run on a course against last year's. You answer them with a script against the analysis functions and the database. A script whose question is likely to come up again is worth keeping: the next session starts from nothing, and a script written anew tends to define things slightly differently and give slightly different numbers for the same question. Kept scripts live in `scripts/`, and the backup includes them.

## Before you write one

Run `coach workspace scripts`. It lists every kept script with the question it answers and how to call it, read from the scripts' headers. If one fits, read its header (`head -n 30 scripts/<name>.py`) and run it. If one nearly fits, extend it (a new option) instead of writing a second script for almost the same question. When a `coach analysis` command now answers what a kept script does, use the command and delete the script.

## When to keep one

Keep a script when it worked and its question will probably come again: a recurring session type, a course or climb the athlete runs often, a comparison they ask about every season. One-offs stay in `scratch/` and are deleted. One question, one script.

## What a kept script must be

A kept script will be trusted by later sessions that did not write it and cannot see this conversation, so its numbers must hold up without you:

- **It reads through the read-only database URL**: `from thresy import db` and `with db.connect_ro() as conn:` (`COACH_DATABASE_URL_RO`). A script that can only read cannot damage the plan, the races or the segments, whoever runs it later and with whatever arguments.
- **Its inputs are arguments** (`argparse`): activity ids, dates, segment keys, numbers of reps. Never hard-code the ids or dates of the day you wrote it; next month they are the wrong ones.
- **It says what it covered** in its output: which activities and dates, how many were skipped and why (no per-second data, heart rate suspect). Numbers without their basis cannot be judged.
- **It uses the functions behind `coach analysis`** (`thresy.analysis`; `python -m pydoc thresy.analysis.data` describes them) instead of re-deriving pauses, laps or the heart-rate check, so it counts the way the commands do. Plots go to `out/`.
- **You run it once more after saving**, from the workspace, with the arguments that answered the question. It must reproduce the numbers you just gave; one that does not is not kept.

Run scripts with the Python next to `coach`: `"$(dirname "$(command -v coach)")/python" scripts/<name>.py …`. Code shared by several scripts goes into a module whose name starts with `_` (`scripts/_climbs.py`); it is not listed.

## The header

The file starts with a docstring (Python) or a comment block (shell, SQL). An illustrative, invented example:

```python
"""How has the time on one climb developed over the seasons?

Usage: python scripts/climb_history.py SEGMENT_KEY [--since YYYY-MM-DD] [--plot]
Output: one row per effort: date, timer time, average and last-minute heart rate, pace.
  Efforts with suspect heart rate are listed but marked. Prints the date range searched
  and how many runs had per-second data.
"""
```

- First line: the question it answers, as the athlete would ask it.
- `Usage:` how to call it (indent continuation lines).
- `Output:` what the numbers mean: units, timer or elapsed time, which samples count.
- `Candidate:` only for a candidate for a built-in command (below).

`coach workspace scripts` shows the first line, `Usage:` and `Candidate:`, and flags a script without a header or one that no longer parses: fix those first.

## When a kept script turns out wrong

Fix it in place, or replace it under the same name; never leave an old and a new version side by side. If its definitions changed (what counts as a rep, which samples), say so in the header. Tell the athlete which earlier numbers were wrong, and correct `memory/` where they went.

## Candidates for a built-in command

When a kept script would help other athletes too, because it answers a general question rather than one about this athlete's routes, add one line to its header:

```
Candidate: <what it would offer as a general `coach analysis` command>; specific to this athlete: <what in it is still tied to them: a route, coordinates, ids, paces>
```

The first time you mark a script, tell the athlete in one sentence that it could become a built-in command and that they can propose it to the project if they want. Do not repeat that when you use it later. You never publish or send code anywhere yourself: a script written against this athlete's data can carry their places, ids or paces, and what reaches a public repository cannot be taken back. A person carries it into the project, removes what is personal and adds tests.
