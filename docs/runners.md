# Runners

`thresy.runner` runs a coding-agent CLI (Claude Code, OpenAI Codex or the Cursor CLI) in a working directory and turns its output into a small, harness-independent event stream. It is the only package that knows about a specific harness. It uses the standard library only and runs on POSIX systems (Linux, macOS).

| Adapter | CLI | Status |
|---|---|---|
| `claude` (default) | Claude Code | Verified against Claude Code 2.1.327 with real runs |
| `codex` | OpenAI Codex CLI | Written from official docs; **not yet run against the real CLI** |
| `cursor` | Cursor CLI (`agent`) | Written from official docs; **not yet run against the real CLI** |
| `fake` | none | In-process runner for tests of other packages |

## Using it

```python
from pathlib import Path
from thresy.runner import Final, Progress, RunRequest, Started, get_runner

runner = get_runner()            # COACH_RUNNER (default "claude"), config from COACH_* variables
req = RunRequest(
    prompt="How did my run go?",
    cwd=Path("/srv/coach/workspace"),
    session_id=todays_session_id,  # None starts a new session
    model_tier="smart",            # or "fast"
    timeout_s=900,
)
async with runner.run(req) as run:   # leaving the block early stops the run
    async for event in run:
        match event:
            case Started(session_id=sid):
                remember(sid)                      # harness-native id for resuming
            case Progress(kind="tool", text=line):
                maybe_show(line)                   # e.g. "Bash: coach sql …"
            case Final(result=r) if r.ok:
                send(r.text)
            case Final(result=r):
                explain(r.error_kind, r.error_detail)
```

`runner.run(req)` returns a `RunHandle`: an async iterator with three extras:

- `cancel(reason)` stops the run cooperatively. The process group is stopped and the iterator still ends with a `Final` (`error_kind="other"`, `error_detail="cancelled: <reason>"`).
- `await handle.result()` consumes all events and returns the `RunResult`.
- `async with` / `aclose()` stop the run if the caller leaves early.

Event guarantees:

- `Started(session_id)` comes at most once, as soon as the harness reports its session id. A run that fails before that (missing binary, startup error) has no `Started`.
- `Progress("text", …)` is every assistant text block, including the one that becomes the final answer, so send `Final.result.text` as the answer and use text progress only for interim updates. `Progress("tool", …)` is one short line per tool call (tool name and a truncated target), never tool output.
- `Final(result)` is always the last event and comes exactly once, also on timeout, crash, missing binary and `cancel()`. `run` never raises for agent-side failures; a bug inside an adapter also ends as a `Final` with `error_kind="other"`.
- If the consuming *task* is cancelled (for example at service shutdown), the run's process group is stopped and `CancelledError` propagates; no `Final` is delivered because nobody is listening any more.

The timeout is enforced by a producer task, so it holds even while the consumer is busy.

### Results and error kinds

`RunResult` fields: `ok`, `text` (final answer; on failure whatever assistant text arrived before it), `session_id`, `error_kind`, `error_detail`, `cost_usd`, `usage`, `duration_s`, `runner`, `model`.

| `error_kind` | Meaning | What to tell the user |
|---|---|---|
| `rate_limit` | Subscription usage limit reached, API rate limiting (429), overload (529), spend/billing limit | "Usage limit reached, try again later" (the detail usually contains the reset time) |
| `auth` | Not logged in, invalid/expired/revoked token or login, account or organisation access problem | "The coach's login has expired; the owner must renew it" |
| `timeout` | No result within `timeout_s` | "That took too long" |
| `crash` | The CLI failed without a structured error: non-zero exit, killed by a signal, no result event | Generic failure; log the detail |
| `other` | Everything else: missing binary or working directory, cancellation, agent-reported errors such as max turns or prompt too long | Generic failure; log the detail |

Adapters classify from structured fields first (Claude Code's error codes and HTTP statuses) and fall back to recognising documented error texts.

`usage` has normalised keys where known (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`) plus the harness's own figures under `raw`. `cost_usd` is the harness's own estimate for this run (Claude Code only; with a subscription it is a list-price estimate, not a bill).

Recommended bridge policy for resumed sessions: if a run with `session_id` fails with `other` or `crash`, retry once without `session_id` and supply recent context from the message log (the session may have been lost or the harness switched).

## Configuration

`RunnerConfig.from_env()` reads these variables (`get_runner()` calls it when no config is passed). `from_env(mapping)` accepts any mapping, for example values merged from a `.env` file.

| Variable | Meaning |
|---|---|
| `COACH_RUNNER` | `claude` (default), `codex`, `cursor` or `fake` |
| `COACH_<NAME>_BIN` | Path or name of the CLI (`NAME` = `CLAUDE`, `CODEX`, `CURSOR`) |
| `COACH_<NAME>_ARGS` | Extra arguments for every run, shell-quoted, e.g. `--max-turns 40` |
| `COACH_<NAME>_PERMISSION_ARGS` | Replaces the adapter's unattended-permission arguments; empty string = none |
| `COACH_<NAME>_MODEL_FAST`, `COACH_<NAME>_MODEL_SMART` | Model per tier. Unset: adapter default. Empty: pass no model flag, the CLI decides |
| `COACH_<NAME>_ENV` | Extra environment for the CLI, shell-quoted `KEY=VALUE` pairs |

The child process gets the parent environment, then the adapter's fixed additions, then `COACH_<NAME>_ENV`, then `RunRequest.env`.

Defaults:

| | claude | codex | cursor |
|---|---|---|---|
| Binary | `claude` | `codex` | `agent`, then `cursor-agent` |
| fast / smart model | `haiku` / `sonnet` | CLI default / CLI default | CLI default / CLI default |
| Permission args | `--permission-mode bypassPermissions --permission-prompts none` | `--dangerously-bypass-approvals-and-sandbox` | `--force --trust` |

The permission defaults turn off every approval prompt because nobody can answer one and **the container is the sandbox**. Do not use these defaults outside an isolated container.

## Claude Code

### Command line

```
claude -p --output-format stream-json --verbose
       [--model <model>] [--resume <session_id>] [--append-system-prompt <text>]
       --permission-mode bypassPermissions --permission-prompts none
       [COACH_CLAUDE_ARGS]
```

The prompt goes to stdin (no argument-length limit, not visible in `ps`). The working directory is the process's working directory; Claude Code has no `--cwd` flag. The adapter adds `CLAUDE_CODE_STARTUP_FAILURE_RESULTS=1` to the environment so startup failures also end with a structured result message.

| Flag | Purpose | Confirmed in |
|---|---|---|
| `-p` | Non-interactive (print) mode | docs, `--help`, real run |
| `--output-format stream-json` | One JSON object per line | docs, `--help`, real run |
| `--verbose` | Required by `stream-json` in print mode | docs, real run |
| `--model haiku` / `sonnet` | Model alias per tier; also passed on resume because a resumed session otherwise keeps its saved model | docs, `--help`, real run (haiku) |
| `--resume <id>` | Continue a session; the session id stays the same | docs, `--help`, real run |
| `--append-system-prompt <text>` | `RunRequest.system_append` | docs, `--help` (not exercised in a real run) |
| `--permission-mode bypassPermissions` | No approval prompts (`-p` starts in Manual mode since 2.1.302 and would deny tool calls) | docs, `--help`, real run |
| `--permission-prompts none` | Anything that would still prompt is denied instead of waited on; removes `AskUserQuestion`. Needs Claude Code ≥ 2.1.259 | docs, `--help`, real run |
| prompt on stdin | | docs, real run |

What the three real runs showed (Claude Code 2.1.327, `haiku`, empty directory): a new session, a resume of it with one Bash call, and a run with a deliberately invalid `CLAUDE_CODE_OAUTH_TOKEN`. Their anonymised output is in `tests/runner/fixtures/claude/`. Details that are not obvious from the docs:

- On resume, `result.total_cost_usd` and `modelUsage` include the earlier runs of the session. The result also carries `model_usage_since_last_result`, which covers only this run; the adapter computes `cost_usd` from it (falling back to `total_cost_usd` on versions without it). `result.usage` is already per run.
- An API failure ends with `subtype: "success"` but `is_error: true`, plus `api_error_status`, `api_error_type` and `terminal_reason: "api_error"`; the synthetic assistant message carries `error: "authentication_failed"`. The process exits with code 1.
- The `system/init` event reports `apiKeySource: "none"` for a claude.ai login.
- The result carries `credential_expires_at`; the adapter logs a warning when it is less than three days away.
- A `rate_limit_event` appears on every run (status `allowed` with utilisation); only `status: "rejected"` is treated as a failure signal.

### Authentication on a server

1. On a machine with a browser, run `claude setup-token` (needs a Pro, Max, Ultra, Team or Enterprise subscription). It prints a one-year OAuth token.
2. Give the container `CLAUDE_CODE_OAUTH_TOKEN=<token>` (a secret in the Compose file or `.env`, never in git).
3. Make sure neither `ANTHROPIC_API_KEY` nor `ANTHROPIC_AUTH_TOKEN` is set: both take precedence over the OAuth token, and usage would be billed to the API key.
4. Check with `python -m thresy.runner check` (later `coach runner check`). It runs `claude auth status --json`, which spends no model usage. With a token it reports `oauth_token`; whether the token is still valid only shows on the first model request (an invalid token fails within seconds with `error_kind="auth"`).

**Never pass `--bare`** and never set `CLAUDE_CODE_SIMPLE=1`: bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN` or the stored login. The adapter refuses both in its configuration, and the healthcheck warns if `CLAUDE_CODE_SIMPLE` is set in the environment.

**Risk: `--bare` becoming the default.** The headless docs say bare mode "will become the default for `-p` in a future release". As of 2.1.327 there is no documented flag or variable to opt out explicitly (`CLAUDE_CODE_SIMPLE=1` turns bare mode on; nothing documents `0` as an opt-out). If that release ships, runs will fail with `error_kind="auth"` right away and the healthcheck will still look fine. Before upgrading Claude Code in the image, read the changelog for the opt-out and add it to `COACH_CLAUDE_ARGS`; pin the Claude Code version in the Dockerfile.

### Other things to know

- `bypassPermissions` refuses to start as root (outside a recognised sandbox), so run the container as a non-root user (the architecture already requires this).
- The working directory's `CLAUDE.md`, `.claude/settings.json` hooks and `.mcp.json` servers load in `-p` mode without a trust prompt. The coach workspace is ours, so that is intended.
- **System prompt on resume:** Claude Code records the system prompt (including `--append-system-prompt`) at a session's first request and reuses it on every resume until the conversation is compacted. A changed `system_append` only takes effect in a new session. Put per-run context into the prompt, not into `system_append`, or add `--system-prompt-snapshot off` to `COACH_CLAUDE_ARGS` (at the price of prompt-cache reuse).
- After the final result, `claude -p` can stay alive for up to 10 minutes waiting for background work (background shell commands, sub-agents). The adapter keeps the answer if the timeout hits after a result arrived, and stops the process group.
- On SIGINT Claude Code ends the turn cleanly; on SIGTERM it exits with code 143 and records no result. That is why the stop sequence starts with SIGINT.

## Codex (not yet verified)

Built from developers.openai.com/codex (non-interactive mode, CLI reference, configuration reference, authentication) and the JSONL event definitions in the openai/codex repository (`codex-rs/exec/src/exec_events.rs`, `codex-rs/exec/src/cli.rs`). The CLI was not installed; the parser is tested only with the documented sample stream and synthetic fixtures.

```
codex [-c developer_instructions=<TOML string>]
      exec --json --skip-git-repo-check --cd <cwd>
      [--model <model>]
      --dangerously-bypass-approvals-and-sandbox
      [COACH_CODEX_ARGS]
      [resume <session_id>] -
```

- The prompt goes to stdin (`-`). Codex appends piped stdin to an argument prompt, so the child's stdin must never be inherited; the runner always gives it either the prompt or `/dev/null`.
- All options come before `resume`: in `cli.rs`, `--json`, `--model`, `--skip-git-repo-check` and the bypass flag are global, but `--cd` and `--sandbox` are not.
- `-c` is placed before `exec` (a documented global flag of `codex`). `system_append` goes into the documented `developer_instructions` setting, encoded as a TOML basic string. Whether Codex applies it on `exec resume` is unverified.
- `--skip-git-repo-check` because the coach workspace need not be a Git repository.
- Events: `thread.started` → `Started(thread_id)`; `item.*` with `agent_message` → text progress (the last one is the answer); `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, `collab_tool_call` → one tool line; `turn.completed` → success and usage; `turn.failed` / `error` → failure, classified from the message text. Codex reports no cost.
- Models: no default; set `COACH_CODEX_MODEL_FAST/SMART` (model names change often; see the Codex models page).
- Authentication: `codex login --device-auth` on the server (or copy `~/.codex/auth.json`, which is a password-equivalent file), or `CODEX_API_KEY` for API billing. The healthcheck runs `codex login status` (exit code 0 when logged in).

## Cursor CLI (not yet verified)

Built from cursor.com/docs/cli (headless, output format, parameters, authentication). The CLI was not installed; tested only with the documented example stream and synthetic lines.

```
agent -p --output-format stream-json --workspace <cwd>
      [--model <model>] [--resume=<session_id>]
      --force --trust
      [COACH_CURSOR_ARGS]
      -- <prompt>
```

- The prompt is an argument after `--` (so a prompt starting with `-` is not read as an option). The `--resume=<id>` form is used because `--resume` takes an optional value.
- `--trust` skips the workspace-trust prompt in headless mode; `--force` allows all commands.
- No system-prompt option exists (`supports_system_append=False`), so `system_append` is prepended to the prompt, separated by a `---` line. Cursor also reads `AGENTS.md` from the workspace.
- The terminal `result` concatenates all assistant segments without separators, so the adapter uses the last assistant message (the text after the last tool call) as the answer.
- On failure Cursor exits non-zero with a message on stderr and may emit no `result`; the adapter classifies that message.
- No usage or cost is reported.
- Authentication: `agent login` (set `NO_OPEN_BROWSER=1` on a server) or `CURSOR_API_KEY`. The healthcheck runs `agent status --format json`; its JSON shape is undocumented, so the result may be "auth unknown".

## Process handling

- Each CLI runs in its own session and process group (`start_new_session=True`). stdout is read in chunks and split into lines (lines over 16 MiB are dropped and counted); stderr is drained concurrently into a 16 KiB tail, so a chatty child never blocks.
- On timeout, cancellation or consumer cancellation the whole group gets SIGINT, then SIGTERM, then SIGKILL, with 3 s between steps. Processes left in the group after a normal exit (for example a server the agent started in the background) are stopped the same way.
- **Run the container with an init process** (`init: true` in Compose, or `tini`). Otherwise orphaned grandchildren become zombies of PID 1, are never reaped, and keep their process group "alive", which makes every stop wait for the full grace periods.

## Command line

```
python -m thresy.runner check [--runner NAME | --all] [--json]
python -m thresy.runner run --runner NAME --cwd DIR [--resume ID] [--tier fast|smart]
                            [--timeout S] [--system-append TEXT] [--json] "prompt"   # "-" reads stdin
```

`check` exits 0 when the runner looks usable. `run` prints events as they arrive and exits 0 on success; the first Ctrl+C cancels the run cleanly. The project CLI (`coach runner check`) can wrap `thresy.runner.cli`.

## Tests and fixtures

`tests/runner/fixtures/claude/claude_new_session.jsonl`, `claude_resume_tool_call.jsonl` and `claude_auth_invalid_token.jsonl` are real Claude Code output with ids, paths, tool lists and usage figures replaced by synthetic values. All other fixtures are synthetic, built from the documentation of each CLI. The subprocess tests generate a small fake CLI in Python and check streaming, garbage lines, non-zero exits, timeouts, SIGKILL escalation, cancellation and that the process group is gone afterwards.

To capture a new fixture from a real CLI: run it in an empty temporary directory with the cheapest model and a trivial prompt, then replace everything personal (ids, paths, e-mail addresses, organisation ids, host names, tool and plugin lists) before committing.

## Adding another harness

1. Read the CLI's current documentation for non-interactive mode, JSON event output, session resume, model selection, unattended approvals and an auth-status command. Confirm every flag with `--help`.
2. Add `src/thresy/runner/<name>.py` with a `StreamParser` subclass (`feed` maps one JSON object to `Started`/`Progress` events, `finish` builds the `RunResult` from what it saw and the `ProcessExit`) and a `CliRunner` subclass (`name`, `capabilities`, `default_binaries`, `default_models`, `default_permission_args`, `verified`, `build_invocation`, `new_parser`, `check_auth`). Use `classify_text`, `summarize_tool` and `failure_from_exit` from the base modules.
3. Register it in `registry.py`, add its name to `RUNNER_NAMES` and a field to `RunnerConfig`.
4. Add fixtures and tests under `tests/runner/`, and a section here. Mark it unverified until it has been run against the real CLI.
