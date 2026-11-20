# The coach workspace

The coding agent runs in a workspace directory: a brief, playbooks, memory files, and two
folders for files. `workspace/` in the repository is the generic template; a deployment's
workspace volume is seeded from it once (`coach workspace init`) and then belongs to the agent.
Nothing in the template names an athlete; what belongs to one athlete is seeded from `private/`
or learned in chat.

```
AGENTS.md            the brief, loaded into every session (CLAUDE.md contains only @AGENTS.md)
playbooks/
  chat.md            answering a message                       named by the bridge's chat prompt
  morning-briefing.md                                          named by the briefing prompt
  post-run.md        debrief after an upload                   named by the debrief prompt
  week-plan.md       planning a week or block                  read on demand
  race-prep.md       deciding, preparing, racing, recovering   read on demand
  onboarding.md      a new athlete with empty memory           read on demand
memory/              athlete.md, places.md, fitness.md, preferences.md, goals.md, journal.md
out/                 files the agent makes (plots); `coach send` accepts files only from here
inbox/<date>/        files the athlete sent (the bridge writes them)
```

`out/` and `inbox/` are created by the bridge at start, so the template does not ship them.

## Who reads what

- **`AGENTS.md`** is in the context of every run on every harness (Claude Code through the
  `CLAUDE.md` import, Codex and Cursor natively). Its length is paid on every run, so it holds
  only what every run needs: the situation, how the coach knows things, the judgment calls that
  go wrong, how it talks, the bridge mechanics, memory and limits (about 2,200 words now). It
  explains reasons rather than issuing bare rules, so the coach can apply them to cases they do
  not name.
- **Playbooks** are read when a prompt names one (`Follow playbooks/chat.md.`) or when the brief
  sends the coach there. Each says what a good result looks like, what to check on the way and
  the shape of the answer. Examples in them are marked illustrative and use invented numbers,
  because a single example tends to be copied.
- **Memory** is read in full once per session (`cat memory/*.md`) and written whenever the coach
  learns something lasting. Sessions end daily; memory and the database are all that survives.

## The contract with the bridge

The brief tells the coach, and the bridge relies on it, that

- its final reply is the chat message, and it sends nothing in between except content
  (`coach send photo|document|choice`), never status updates;
- the exact reply `NO_REPLY` sends nothing (proactive runs);
- the notes in proactive prompts (night not synced, night may be incomplete, HRV missing) are
  facts to pass on;
- earlier messages at the top of a fresh session's prompt are context, and numbers in them are
  checked before being repeated;
- "last night" is the `wellness_daily` row whose `wake_date` is today; numbers come from `coach`
  commands or SQL in the same run; anything to remember goes into `memory/` or the plan tables.

## Memory

| File | Holds |
|---|---|
| `athlete.md` | who the athlete is as it bears on training: circumstances, availability, constraints, health history, equipment |
| `places.md` | home area, routes, climbs (with segment keys), track, regular events, what weather does to them |
| `fitness.md` | bests (official vs watch), markers with date and evidence, load handled, sensor quirks |
| `preferences.md` | how they like to train and be coached; standing instructions |
| `goals.md` | goals, current phase, priorities between races, open decisions |
| `journal.md` | dated events that matter later, newest first |

Each skeleton starts with a short note on what belongs in it and how to keep it current. Rules
from the brief: write in the same run; correct an entry in place when the athlete corrects the
coach or new evidence arrives; date entries and say how sure they are; keep all files together
around 10 KB so they are read in full every session (a larger set gets cut off by the harness's
tool-output limit, which happened in a trial run); sessions, dates and results go into
`coach plan` and `coach races`, memory keeps the reasons.

Memory is plain files so it survives a harness switch and is part of the backup. Harness memory
features must stay unused; see "Findings for the deployment" below.

## The planning tools

`thresy.tools.planning_cli` adds three commands at the top level of `coach`. They work with the
agent's role (`coach_agent` may write `planned_workouts` and `races`), print text by default and
JSON with `--json`, and accept days as `YYYY-MM-DD`, `today`, `tomorrow`, `yesterday`, a weekday
(`fri`: the next one, today included) or `+N`, in the athlete's time zone (`COACH_TZ`).

- **`coach weather [PLACE]`**: Open-Meteo (free, no key). Daily rows for `--days` (default 7) and
  hourly rows from now to 22:00 today and 6:00–22:00 tomorrow; `--date` (repeatable) picks other
  days, up to 16 ahead and 92 back (the weather during a past run). A place is a name, a name with
  a qualifier (`"Springfield, Illinois"`) or `lat,lon`; the output names the place found and
  others with the same name. Default place: **`COACH_HOME_LOCATION`** (own settings class
  `thresy.tools.weather.WeatherSettings`).
- **`coach plan`**: `show [this|next|last|DATE] [--weeks N]` puts each day's planned workouts next
  to its activities, with totals and workouts whose day passed unmarked; `add`, `change`, `move`
  (in place while the day is in the future; afterwards the original stays as `moved` and a copy
  carries on), `done` (links the day's only unlinked run, or `--activity ID`), `skip`, `link`,
  `delete`. `target` holds `distance_km`, `duration_min`, `pace`, `hr`, `ascent_m`, plus `note`
  and the `moved_from`/`moved_to` bookkeeping; `--target-json` adds other keys.
- **`coach races`**: `list` (upcoming plus the last 60 days, or `--upcoming`, `--past`, `--all`),
  `show`, `add` (duplicates by date and name are refused), `change` (`--add-note` appends a dated
  line), `result` (`--time` with `--source official|reported`, otherwise the linked activity's
  timer time labelled `watch`; `--activity auto` links the longest run that day), `delete`.
  Status: `planned`, `maybe`, `done`, `dnf`, `dns`; priority A, B, C.

## Adapting the template

- Change `AGENTS.md` only for what every run needs, and keep it short; everything situational
  belongs in a playbook. When you add a rule, add its reason.
- A new proactive trigger needs a playbook and a line in the bridge's prompt; the bridge falls
  back to a one-line instruction while the file is missing.
- Keep examples illustrative and invented, and say so next to them.
- The template is English; the coach answers in `COACH_LANGUAGE` (the prompt names it). Its name
  is `COACH_NAME`. Neither appears in the template.
- Never put anything from a real athlete into the template: no names, places, paces, heart
  rates, races, dates or quotes. Lessons from a real athlete's history go in as general lessons,
  in your own words.

## Seeding an athlete

1. Create the workspace (`coach workspace init`), then copy the athlete's memory files over the
   skeletons. Write them in the athlete's language, one line per fact, each with its date and how
   sure it is; check facts against the database where you can.
2. Enter races and an agreed plan through the coach's own commands (`coach races add`,
   `coach races result`, `coach plan add`), with the agent's database role, in a small script
   kept in `private/` next to the memory files.
3. Without a seed, the coach follows `playbooks/onboarding.md`: it derives bests, volume and
   ranges from the data first and asks for the rest a few questions at a time.

## Trying a change with a real run

Real runs use the model subscription, so make each one count and read the transcript
critically: language and register, right night, real numbers, tools used rather than guessed,
phone length, padding or announcements, and whether memory and the plan were updated correctly.

1. **Scratch workspace outside the repository and not under `/tmp`**: copy `workspace/`, then
   the seed memory. Claude Code 2.1.327 does not load `CLAUDE.md` from a working directory under
   `/tmp` (verified), so a trial there runs without the brief.
2. **Seed the database** with the seed script and the agent's role; remove the rows afterwards
   with `coach plan delete` / `coach races delete`.
3. **Prompts as the bridge builds them**: call `thresy.bridge.prompts` (`chat_prompt`,
   `proactive_prompt` with `briefing_section` or `debrief_section`) from a small script.
4. **The bridge's environment** for the agent: start `coach runner run - --cwd <workspace> --json`
   from a clean environment (`env -i` with `HOME`, `USER`, `PATH` including the `coach` binary,
   `LANG`, `TMPDIR`), with `COACH_CLAUDE_BIN` set and the agent's variables in
   `COACH_CLAUDE_ENV`: `COACH_DATABASE_URL` (the `coach_agent` URL), `COACH_DATABASE_URL_RO`,
   `COACH_DATA_DIR`, `COACH_WORKSPACE_DIR`, `COACH_TZ` and `TZ`, `COACH_LANGUAGE`, `COACH_NAME`,
   `COACH_RUN_ID`, `COACH_HOME_LOCATION`. Do not run it from inside another Claude Code session
   without `env -i`: that session's `CLAUDE_*` variables would pass through.
5. **Read the transcript**: the runner's JSON lines give the final answer and cost; the harness's
   own transcript (for Claude Code `~/.claude/projects/<encoded cwd>/<session id>.jsonl`) shows
   every command and its output. To check what the harness loads without spending a model run,
   start it with an invalid `--model`: the request is refused before any inference, and the
   transcript still shows the attached instructions.
6. **Clean up**: database rows, the scratch workspace, and the harness's transcripts of the
   trial (they contain the athlete's data).

## Findings for the deployment

From six real runs with Claude Code 2.1.327 (Sonnet tier) against the dev database:

- **Workspace location:** `CLAUDE.md` is not loaded when the working directory is under `/tmp`.
  The workspace volume must be mounted elsewhere; `compose.yml` currently mounts it at
  `/workspace`, which is fine.
- **Harness memory:** in `-p` mode Claude Code's auto-memory was active and its system prompt
  told the model to keep a `MEMORY.md` under `~/.claude/projects/…`. The runner now switches it
  off on every run (docs/runners.md, "No memory of the harness's own").
- **The operator's own configuration leaks into local trials:** with the developer's `HOME`, the
  agent sees the developer's skills, MCP servers and account e-mail. The deployment's separate
  agent home avoids this.
- **The dev database lacked migration `0003_agent_requests`**, so `coach sync status` and
  `coach sync request` crashed with a traceback in the trials; the coach coped, but
  `coach sync status` could report a missing table more gracefully.
