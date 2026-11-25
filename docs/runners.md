# Runners

`thresy.runner` runs a coding-agent CLI (Claude Code, OpenAI Codex or the Cursor CLI) in a working directory and turns its output into a small, harness-independent event stream. It is the only package that knows about a specific harness. It uses the standard library only (except the `coach runner` typer shim in `thresy/runner/cli.py`, which `import thresy.runner` never loads) and runs on POSIX systems (Linux, macOS).

| Adapter | CLI | Status |
|---|---|---|
| `claude` (default) | Claude Code | Verified against Claude Code 2.1.327 with real runs, except the effort flag (see [Reasoning effort](#reasoning-effort)) |
| `codex` | OpenAI Codex CLI | Written from official docs; **not yet run against the real CLI** |
| `cursor` | Cursor CLI (`agent`) | Written from official docs; **not yet run against the real CLI**; **puts the prompt on the command line** (see [Cursor](#cursor-cli-not-yet-verified)) |
| `fake` | none | In-process runner for tests of other packages |

## Using it

```python
from pathlib import Path

from thresy.runner import Final, Progress, RunRequest, Started, get_runner

runner = get_runner()  # COACH_RUNNER (default "claude"), config from COACH_* variables
req = RunRequest(
    prompt="How did my run go?",
    cwd=Path("/srv/coach/workspace"),
    session_id=todays_session_id,  # None starts a new session
    model_tier="smart",  # or "fast"
    effort="high",  # low | medium | high | xhigh | max; None: the harness's default
    timeout_s=900,
    env={"COACH_DATABASE_URL": agent_database_url},  # what the agent may see
)
async with runner.run(req) as run:  # leaving the block early stops the run
    async for event in run:
        match event:
            case Started(session_id=sid):
                remember(sid)  # harness-native id for resuming
            case Progress(kind="tool", text=line):
                maybe_show(line)  # e.g. "Bash: coach sql …"
            case Final(result=r) if r.ok:
                send(r.text)
            case Final(result=r):
                explain(r.error_kind, r.error_detail)
```

`runner.run(req)` returns a `RunHandle`: an async iterator with three extras:

- `cancel(reason)` stops the run cooperatively. The process group is stopped and the iterator still ends with a `Final` (`error_kind="cancelled"`, `error_detail=reason`).
- `await handle.result()` consumes all events and returns the `RunResult`.
- `async with` / `aclose()` stop the run if the caller leaves early.

Event guarantees:

- `Started(session_id)` comes at most once, as soon as the harness reports its session id. A run that fails before that (missing binary, startup error) has no `Started`.
- `Progress("text", …)` is every assistant text block, including the one that becomes the final answer, so send `Final.result.text` as the answer and use text progress only for interim updates. `Progress("tool", …)` is one short line per tool call (tool name and a truncated target), never tool output.
- `Final(result)` is always the last event and comes exactly once, also on timeout, crash, missing binary and `cancel()`. `run` never raises for agent-side failures; a bug inside an adapter also ends as a `Final` with `error_kind="other"`.
- If the consuming *task* is cancelled (for example at service shutdown), the run's process group is stopped and `CancelledError` propagates; no `Final` is delivered because nobody is listening any more.

The timeout is enforced by a producer task, so it holds even while the consumer is busy. Once the harness has reported its final result, the CLI gets `exit_wait_s` (default 10 s) to exit on its own; then its process group is stopped and the result is returned, so a lingering process cannot hold back an answer that is already there.

### Results and error kinds

`RunResult` fields: `ok`, `text` (final answer; on failure whatever assistant text arrived before it), `session_id`, `error_kind`, `error_detail`, `cost_usd`, `usage`, `duration_s`, `runner`, `model`, `credential_expires_at`.

`credential_expires_at` is when the credential the harness used ends for good (an aware UTC `datetime`), if the harness says so with its result; `None` when it does not know or does not tell, which is the common case. Only the Claude Code adapter fills it, and only for some logins (see [Credential expiry](#credential-expiry)). A parser sets `StreamParser.credential_expires_at`, and every result it builds afterwards carries it. The bridge uses it to tell the athlete before the agent stops working (docs/bridge.md, "Before the model credential expires").

| `error_kind` | Meaning | What to tell the user |
|---|---|---|
| `rate_limit` | Subscription usage limit reached, API rate limiting (429), overload (529), spend/billing limit | "Usage limit reached, try again later" (the detail usually contains the reset time) |
| `auth` | Not logged in, invalid/expired/revoked token or login, account or organisation access problem | "The coach's login has expired; the owner must renew it" |
| `timeout` | No result within `timeout_s` | "That took too long" |
| `crash` | The CLI failed without a structured error: non-zero exit, killed by a signal, no result event | Generic failure; log the detail |
| `cancelled` | `handle.cancel(reason)` stopped the run; `error_detail` is the reason | Usually nothing (the caller cancelled it) |
| `other` | Everything else: missing binary or working directory, agent-reported errors such as max turns or prompt too long, internal runner errors | Generic failure; log the detail |

Adapters classify from structured fields first (Claude Code's error codes and HTTP statuses) and fall back to recognising documented error texts.

`usage` has normalised keys where known (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`) plus the harness's own figures under `raw`. `cost_usd` is the harness's own estimate for this run, not for the whole resumed session (Claude Code only; with a subscription it is a list-price estimate, not a bill).

Recommended bridge policy for resumed sessions: if a run with `session_id` fails with `other` or `crash`, retry once without `session_id` and supply recent context from the message log (the session may have been lost or the harness switched).

## Configuration

`RunnerConfig.from_env()` reads these variables (`get_runner()` calls it when no config is passed). `from_env(mapping)` accepts any mapping; `coach runner …` passes the environment with a `.env` file in the current directory filling gaps, like the project's other settings.

| Variable | Meaning |
|---|---|
| `COACH_RUNNER` | `claude` (default), `codex`, `cursor` or `fake` |
| `COACH_<NAME>_BIN` | Path or name of the CLI (`NAME` = `CLAUDE`, `CODEX`, `CURSOR`) |
| `COACH_<NAME>_ARGS` | Extra arguments for every run, shell-quoted, e.g. `--max-turns 40` |
| `COACH_<NAME>_PERMISSION_ARGS` | Replaces the adapter's unattended-permission arguments; empty string = none |
| `COACH_<NAME>_MODEL_FAST`, `COACH_<NAME>_MODEL_SMART` | Model per tier. Unset: adapter default. Empty: pass no model flag, the CLI decides |
| `COACH_<NAME>_ENV` | Extra environment for the CLI, shell-quoted `KEY=VALUE` pairs |
| `COACH_<NAME>_PASS_ENV` | Parent variables the CLI may inherit beyond the allow-list: names, or prefixes ending in `*`, separated by spaces or commas (e.g. `AWS_REGION GOOGLE_*`) |
| `COACH_<NAME>_EXIT_WAIT_S` | Seconds the CLI gets to exit after its final result before it is stopped (default `10`) |
| `COACH_AGENT_USER` | Start every CLI process as this Unix user, for all harnesses (unset or empty: off). See [The agent's own Unix user](#the-agents-own-unix-user) |
| `COACH_AGENT_USER_HELPER` | The setuid helper that switches users (default `/usr/local/libexec/thresy/run-as-agent`) |

Defaults:

| | claude | codex | cursor |
|---|---|---|---|
| Binary | `claude` | `codex` | `agent`, then `cursor-agent` |
| fast / smart model | `haiku` / `sonnet` | CLI default / CLI default | CLI default / CLI default |
| Permission args | `--permission-mode bypassPermissions --permission-prompts none` | `--dangerously-bypass-approvals-and-sandbox` | `--force --trust` |

The permission defaults turn off every approval prompt because nobody can answer one and **the container is the sandbox**. Do not use these defaults outside an isolated container.

### The agent's environment

The agent has a shell and reads the web, so the CLI does **not** inherit the service's environment (which holds the Telegram token and the read/write database URL). Its environment is built in this order:

1. From the parent environment, only:
   - basic process variables: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LANGUAGE`, `LC_*`, `TZ`, `TMPDIR`, `TERM`, `XDG_*`;
   - CA certificates and proxies: `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY` (and their lowercase forms), `NODE_USE_ENV_PROXY`;
   - the harness's own auth and configuration variables: Claude Code `ANTHROPIC_*` and `CLAUDE_*` (`CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, …; `CLAUDE_CODE_SIMPLE` is always dropped because it turns on bare mode); Codex `CODEX_*` (`CODEX_HOME`, `CODEX_API_KEY`, `CODEX_CA_CERTIFICATE`) and `OPENAI_API_KEY`; Cursor `CURSOR_*` (`CURSOR_API_KEY`, `CURSOR_CONFIG_DIR`);
   - names and prefixes listed in `COACH_<NAME>_PASS_ENV`.

   No `COACH_*` variable passes unless it is listed in `COACH_<NAME>_PASS_ENV`.
2. The adapter's fixed additions (Claude: `CLAUDE_CODE_STARTUP_FAILURE_RESULTS=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`).
3. `COACH_<NAME>_ENV`.
4. `RunRequest.env`: this is how the bridge hands the agent its own settings, such as `COACH_DATABASE_URL` set to the `coach_agent` URL.

With `COACH_AGENT_USER` set, `HOME`, `USER` and `LOGNAME` describe the agent user instead, the variables of the agent's credential file are added, and the C library removes `TMPDIR` (see below).

The healthcheck uses the same environment. Bedrock, Vertex or Foundry credentials (`AWS_*`, `GOOGLE_*`) do not pass by default; list them in `COACH_CLAUDE_PASS_ENV` if you use them. When you run `coach runner run` from inside another Claude Code session, that session's `CLAUDE_*` variables pass through as well; use a clean shell for manual tests.

### No memory of the harness's own

The coach's memory is the workspace's `memory/` files (docs/ARCHITECTURE.md, principle 1), because the next session may run on another harness. Every adapter keeps its harness's own memory feature off on every run, whatever the harness's settings and configuration files say:

| Harness | Memory feature | What the adapter does |
|---|---|---|
| Claude Code | Auto memory (`~/.claude/projects/<project>/memory/`), on by default | Sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. Env-vars reference: "Set to `1` to disable auto memory. [...] When disabled, Claude does not create or load auto memory files". Settings reference (`autoMemoryEnabled`): "`CLAUDE_CODE_DISABLE_AUTO_MEMORY` takes precedence over this key for one session, in either direction" |
| Codex | Memories (`$CODEX_HOME/memories*/`), off by default (`[features] memories`) | Passes `-c features.memories=false`, because the agent's `config.toml` could turn them on and a `-c` override outranks every configuration file |
| Cursor CLI | None in the CLI documentation (overview, parameters, configuration, changelog; checked 2026-11) | Nothing; check again when upgrading |

`COACH_<NAME>_ENV` and `COACH_<NAME>_ARGS` come later on the command line and in the environment, so they could switch a memory back on; do not. The same goes for the agent's own credential file (applied last, see [The agent's own Unix user](#the-agents-own-unix-user)): this keeps the coach from using a harness memory by accident; it does not stop an agent that sets out to. Notes Claude Code wrote before this setting existed stay in the agent's home but are no longer loaded; delete them there if you want them gone.

### Reasoning effort

`RunRequest.effort` is a harness-neutral hint: `low`, `medium`, `high`, `xhigh` or `max` (`thresy.runner.EFFORTS`, lowest first), or `None` for the harness's (or model's) default. The bridge sets it per kind of run (`COACH_BRIDGE_CHAT_EFFORT`, `COACH_BRIDGE_PROACTIVE_EFFORT`, docs/bridge.md); `coach runner run --effort` tries a level by hand. `RunnerCapabilities.supports_effort` says whether it reaches the harness; an adapter without it ignores the hint.

| Harness | Mechanism | A level the model lacks | Documentation relied on (checked 2026-11) |
|---|---|---|---|
| Claude Code | `--effort <level>`, same names | Claude Code runs it at the highest level the model has below it | CLI reference, `--effort`: "Set the effort level for the current session. Options: `low`, `medium`, `high`, `xhigh`, `max`, or `ultracode`. Available levels depend on the model. [...] On the main conversation's model, overrides the `modelSettings` and `effortLevel` settings for this session and doesn't persist". Model configuration: "If you set a level the active model does not support, Claude Code falls back to the highest supported level at or below the one you set. For example, `xhigh` runs as `high` on Opus 4.6." |
| Codex | `-c model_reasoning_effort="<level>"`, same names | Not documented; the runner's retry (below) covers a rejection | Configuration reference, `model_reasoning_effort`: "Reasoning effort advertised by the selected model, such as `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`. Available levels depend on the model and client." CLI reference, `--config, -c`: "Override configuration values. Values parse as TOML if possible; otherwise the literal string is used." |
| Cursor CLI | None: the hint is ignored (`supports_effort=False`) | | Parameters reference: no option for effort or reasoning (`--model <model>`: "Model to use"); configuration reference: no such key or variable. Reasoning variants are part of some model names (`/model sonnet-4-thinking`), so choose one through `COACH_CURSOR_MODEL_*` |

**Claude Code: the flag, not the variable.** Claude Code resolves the level from, in this order: "1. The `CLAUDE_CODE_EFFORT_LEVEL` environment variable 2. A level you pick in the session, such as with `/effort` [...] 3. Launching with `--effort` 4. Your settings [...] 5. The model's default effort" (model configuration). The adapter passes `--effort` deliberately:

- It is the documented per-session mechanism and "doesn't persist", which matches a per-run hint; each run (also a resumed one) starts the CLI anew with its own level.
- It outranks the settings files in the agent's home (`effortLevel`, `modelSettings`), which the agent could edit.
- It leaves `CLAUDE_CODE_EFFORT_LEVEL` as the operator's override: set in `COACH_CLAUDE_ENV` or the agent's credential file, it wins over every per-run level ("Takes precedence over `--effort`, `/effort`, and the `modelSettings` and `effortLevel` settings", env-vars reference), and `auto` there makes Claude Code send no level at all. Had the adapter used the variable, an operator could not override it without a code change. A `--effort` in `COACH_CLAUDE_ARGS` also wins, because it comes later on the command line.

`--effort xhigh` needs Claude Code 2.1.111 or later (changelog: "Added `xhigh` effort level [...] Available via `/effort`, `--effort`, and the model picker"); the image pins a later version. Without a level, Opus 5.5, Sonnet 5.5 and Haiku 5.5 run at `medium` (model configuration: they "default to `medium`"), which is what the `smart` tier (`sonnet`) gets today. Claude Code's changelog suggests that changing the level within a session invalidates the prompt cache on most models; chat and proactive runs share the day's session, so different levels for the two cost one cache re-read when one follows the other within the cache's lifetime.

**Never failing a run over effort.** A level the harness or model does not accept must not cost the answer. Claude Code documents its fallback; Codex documents none, and a CLI older than a level may reject the argument. So when a run that was given a level ends with `other` or `crash` *before any output* (no text, no tool call; so nothing was done), the shared CLI runner starts it once more without a level, within the time left of `timeout_s`, and logs a warning. Auth, rate-limit, timeout and cancelled failures are not retried, nor is a run that had already produced output. The caller sees one run: at most one `Started` (the first; the final result carries the session id of the attempt that answered) and one `Final`.

**Not verified with a real run.** The Claude Code adapter was verified with real runs before this was added; the effort flag was not run here. One real run on the server settles it (see [Checking the effort level](#checking-the-effort-level)). The Codex mapping is as unverified as the rest of that adapter.

#### Checking the effort level

In the bridge container, with the agent user as in production, on the fast tier (cheap) and in an empty directory (no brief):

```sh
docker compose exec bridge coach runner run --cwd /tmp --tier fast --effort high --json "Reply with exactly OK"
docker compose run --rm --no-deps --user agent bridge sh -c 'grep -ho "\"effort[^,}]*" /home/agent/.claude/projects/-tmp/*.jsonl | sort | uniq -c'
docker compose run --rm --no-deps --user agent bridge sh -c 'rm -f /home/agent/.claude/projects/-tmp/*.jsonl'
```

The last two use `run`, not `exec --user agent`: `exec` would start a process as the agent user inside the running bridge container with the bridge's environment, which the agent's other processes could then read (docs/deploy.md, "The model credential"). A `run` container shares the agent's home volume and nothing else.

Check that (1) the run ends with `"ok": true` and the text `OK`, and no line says "retrying once without an effort setting" (that would mean the pinned CLI rejected `--effort high`); (2) the session transcript names the level `high` (Claude Code's changelog says transcripts record the effort in force; if the field is named differently, look for the level next to the request); (3) neither `COACH_CLAUDE_ENV`, `COACH_CLAUDE_ARGS` nor the agent's credential file sets `CLAUDE_CODE_EFFORT_LEVEL` or `--effort`, which would override the bridge's levels. To check a resumed session as well, repeat the first command with `--resume <session id from the first run> --effort low` and look for `low` in the same transcript.

## Token use and cost

Thresy is built to run on a subscription login, where a run costs nothing extra. On pay-per-token credits (an API key, or a router such as OpenRouter) every run is billed by its tokens, and an agent run uses far more of them than the length of its answer suggests: the harness sends the whole context again with every tool call, so a run of 30 steps over a context of 60,000 tokens reads about 2 million input tokens.

**One measurement.** The first six days of one deployment, in November 2026: 26 runs (4.3 a day) with Claude Code and Claude Opus 5.5, effort `xhigh` for every kind of run, one session per day. The tokens are the run records' `usage`, the cost is their `cost_usd`.

| Kind of run | Runs | Input tokens per run | Of which new | Output tokens per run | Cost per run | Range |
|---|---|---|---|---|---|---|
| Chat message answered in text | 13 | 555,000 | 27,000 | 7,400 | $0.47 | $0.04 to $0.89 |
| Chat message answered with a plot | 3 | 2,290,000 | 46,000 | 21,500 | $1.25 | $0.88 to $1.66 |
| Morning briefing | 5 | 332,000 | 32,000 | 4,900 | $0.41 | $0.35 to $0.56 |
| After-run debrief | 5 | 673,000 | 58,000 | 7,800 | $0.74 | $0.42 to $1.01 |
| All | 26 | 735,000 | 36,000 | 8,600 | $0.60 | $0.04 to $1.66 |

Together that is 19.1 million input tokens and 224,000 output tokens. Of the input, 18.2 million tokens were read from the prompt cache and 0.94 million were new and written to it. The cost was $15.65: $2.61 a day, about $78 in 30 days. The list prices of that month (per million tokens: input $4, output $20, cache read $0.20, one-hour cache write $8) give $15.63 for the same tokens, so `cost_usd` is the list-price cost. A router's own fee comes on top (OpenRouter: 5.5 % when buying credits, no markup per token).

**Where the money goes.** 48 % was cache writes, 29 % output and 23 % cache reads; input outside the cache cost nothing worth counting. Claude Code wrote every cache entry with the one-hour lifetime, which costs twice the input price. A run that starts more than an hour after the session's previous one therefore writes the whole context again, 30,000 to 70,000 tokens here, or $0.25 to $0.55 before the first word of the answer. That is most of what a short run such as the morning briefing costs.

**What changes it.**

- The model tier, more than anything else. At the same token counts a model at half the price halves the bill (`COACH_CLAUDE_MODEL_SMART`; Claude Sonnet 5.5 was at half of Opus 5.5 on each of the four prices).
- The effort (`COACH_BRIDGE_CHAT_EFFORT`, `COACH_BRIDGE_PROACTIVE_EFFORT`), which moves the output tokens and the number of steps. Not measured here.
- Another model family or another harness. Pricing these token counts with another model's list prices gives a first guess and no more: a different tokenizer counts the same text differently, a different model takes a different number of steps, and the result depends on caching above all. 95 % of the input here was cache reads; without a single cache hit the same tokens would have cost $81 at the same prices. No harness other than Claude Code has been measured.

**Your own numbers.** Every run record holds them, on a subscription too (there `cost_usd` is the list-price estimate, not a bill). Codex reports tokens but no cost, the Cursor CLI neither.

```sql
SELECT trigger, count(*) AS runs, round(avg(cost_usd)::numeric, 2) AS usd_per_run,
       round(avg((usage->>'input_tokens')::numeric + (usage->>'cache_read_tokens')::numeric
                 + (usage->>'cache_write_tokens')::numeric)) AS input_tokens_per_run,
       round(avg((usage->>'output_tokens')::numeric)) AS output_tokens_per_run
FROM agent_runs WHERE status = 'ok' GROUP BY trigger ORDER BY runs DESC;
```

## Claude Code

### Command line

```
claude -p --output-format stream-json --verbose
       [--model <model>] [--resume <session_id>] [--effort <level>]
       [--append-system-prompt-file <private temporary file>]
       --permission-mode bypassPermissions --permission-prompts none
       [COACH_CLAUDE_ARGS]
```

The prompt goes to stdin and `RunRequest.system_append` to a file in a new directory under `TMPDIR` that only the service user can read (directory mode 0700, file 0600), removed when the run ends. No personal text is on the command line, which every user on the host can read via `ps`. The working directory is the process's working directory; Claude Code has no `--cwd` flag.

| Flag | Purpose | Confirmed in |
|---|---|---|
| `-p` | Non-interactive (print) mode | docs, `--help`, real run |
| `--output-format stream-json` | One JSON object per line | docs, `--help`, real run |
| `--verbose` | Required by `stream-json` in print mode | docs, real run |
| `--model haiku` / `sonnet` | Model alias per tier; also passed on resume because a resumed session otherwise keeps its saved model | docs, `--help`, real run (haiku) |
| `--resume <id>` | Continue a session; the session id stays the same | docs, `--help`, real run |
| `--effort <level>` | `RunRequest.effort` (see [Reasoning effort](#reasoning-effort)) | docs only; **not yet run** |
| `--append-system-prompt-file <path>` | `RunRequest.system_append` | docs (CLI reference), `--help` (mentioned in the `--bare` text only), real run |
| `--permission-mode bypassPermissions` | No approval prompts (`-p` starts in Manual mode since 2.1.302 and would deny tool calls) | docs, `--help`, real run |
| `--permission-prompts none` | Anything that would still prompt is denied instead of waited on; removes `AskUserQuestion`. Needs Claude Code ≥ 2.1.259 | docs, `--help`, real run |
| prompt on stdin | | docs, real run |
| `CLAUDE_CODE_STARTUP_FAILURE_RESULTS=1` | Startup failures also end with a structured result message | docs only |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | No auto memory (see [No memory of the harness's own](#no-memory-of-the-harnesss-own)) | docs only |

What the four real runs showed (Claude Code 2.1.327, `haiku`, empty directory). The first three were a new session, a resume of it with one Bash call, and a run with a deliberately invalid `CLAUDE_CODE_OAUTH_TOKEN`; their anonymised output is in `tests/runner/fixtures/claude/`. The fourth went through `coach runner run` with an allow-listed environment and a `system_append` that told the model to answer "BANANA", which it did. Details that are not obvious from the docs:

- On resume, `result.total_cost_usd` and `modelUsage` include the earlier runs of the session. The result also carries `model_usage_since_last_result`, which covers only this run; the adapter computes `cost_usd` from it (falling back to `total_cost_usd` on versions without it). `result.usage` is already per run.
- An API failure ends with `subtype: "success"` but `is_error: true`, plus `api_error_status`, `api_error_type` and `terminal_reason: "api_error"`; the synthetic assistant message carries `error: "authentication_failed"`. The process exits with code 1.
- The `system/init` event reports `apiKeySource: "none"` for a claude.ai login.
- The result carried `credential_expires_at` (these runs used a stored claude.ai login; see [Credential expiry](#credential-expiry)).
- A `rate_limit_event` appears on every run (status `allowed` with utilisation); only `status: "rejected"` is treated as a failure signal.
- The fourth run took 9.9 s of wall time for a one-word answer (the API part of earlier runs was about 2.5 s). The CLI exited on its own within the 10 s exit wait; how the time splits between startup and shutdown on the development machine (which has user-level plugins and connectors) was not measured.

### Authentication on a server

1. On a machine with a browser, run `claude setup-token` (needs a Pro, Max, Ultra, Team or Enterprise subscription). It prints a one-year OAuth token.
2. With the agent user (the Compose default), write `CLAUDE_CODE_OAUTH_TOKEN=<token>` into the agent's credential file (see [The agent's own Unix user](#the-agents-own-unix-user) and docs/deploy.md); the bridge then never holds it. Without it, give the bridge service `CLAUDE_CODE_OAUTH_TOKEN=<token>` (a secret in `.env`, never in git); it passes the allow-list because it starts with `CLAUDE_`.
3. Make sure neither `ANTHROPIC_API_KEY` nor `ANTHROPIC_AUTH_TOKEN` is set: both take precedence over the OAuth token, and usage would be billed to the API key. The healthcheck warns if `ANTHROPIC_API_KEY` is set.
4. Check with `coach runner check` (or `python -m thresy.runner check`). It runs `claude auth status --json`, which spends no model usage. With a token it reports `oauth_token`; whether the token is still valid only shows on the first model request (an invalid token fails within seconds with `error_kind="auth"`).

**Never pass `--bare`** and never set `CLAUDE_CODE_SIMPLE=1`: bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN` or the stored login. The adapter refuses both in its configuration, drops `CLAUDE_CODE_SIMPLE` from the inherited environment, and the healthcheck warns if it is set.

**Risk: `--bare` becoming the default.** The headless docs say bare mode "will become the default for `-p` in a future release". As of 2.1.327 there is no documented flag or variable to opt out explicitly (`CLAUDE_CODE_SIMPLE=1` turns bare mode on; nothing documents `0` as an opt-out). If that release ships, runs will fail with `error_kind="auth"` right away and the healthcheck will still look fine. Before upgrading Claude Code in the image, read the changelog for the opt-out and add it to `COACH_CLAUDE_ARGS`; the image pins the Claude Code version.

### Credential expiry

The result message of `claude -p` (stream-json and json) can carry `credential_expires_at`, an ISO 8601 instant such as `2026-12-03T20:14:46.229Z`. The Claude Code 2.1.318 release notes introduced it: "Added the date a stored claude.ai sign-in ends to `claude auth status` (`loginExpiresAt`) and to `claude -p` JSON results (`credential_expires_at`)". Older versions never report it. Claude Code's documentation pages do not describe it; the Agent SDK's type definition does (`@anthropic-ai/claude-agent-sdk`, `sdk.d.ts`, `SDKResultMessage`, checked in version 0.3.329):

> When the claude.ai credential this process's requests send ends for good, as an ISO 8601 instant, read as the result is written. For a stored claude.ai login it is the end of the login's refresh chain as stored at sign-in, which renewing the access token does not move. That date is the server's when it sent one and otherwise a 30-day estimate, and the real end can be up to 3 days earlier. The server is not asked, so a login revoked early still shows its date, and a date in the past means the login has probably ended, not that it has. Absent when no date is known, for every other credential (an API key, a token from the environment, another provider), on results this process did not number, and from older producers.

So, by login kind (what `claude auth status` shows for a token or an API key is from a published third-party test of 2.1.318, not from Anthropic):

| Login | `credential_expires_at` | `claude auth status --json` |
|---|---|---|
| Stored claude.ai login (`claude auth login`) | the end of the login, possibly a 30-day estimate; can be up to 3 days late | `loginExpiresAt` with the same date |
| `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, valid one year) | absent | `authMethod: "oauth_token"`, no date |
| `ANTHROPIC_API_KEY`, other providers | absent | no date |

The adapter passes the date on as `RunResult.credential_expires_at` and logs a warning when it is less than three days away. With the token that docs/deploy.md sets up, no date arrives: the bridge then relies on `COACH_BRIDGE_CREDENTIAL_EXPIRES` (docs/bridge.md). The field in the captured fixtures means those runs used a stored login; no run with a setup token was captured, so its absence there rests on the SDK's description. To see what a deployment's CLI reports, run `coach runner check` (it shows `login valid until …` when `claude auth status` names a date) or look for `credential_expires_at` in the final event of `coach runner run --json` (one small model request).

### Other things to know

- `bypassPermissions` refuses to start as root (outside a recognised sandbox), so run the container as a non-root user.
- With the agent user (above), Claude Code keeps its state (`~/.claude.json`, `~/.claude/`, session transcripts) in the agent's home, the `agent-home` volume. Settings and hooks there are the agent's own; a manipulated agent can change them and so influence its later runs.
- The working directory's `CLAUDE.md`, `.claude/settings.json` hooks and `.mcp.json` servers load in `-p` mode without a trust prompt. The coach workspace is ours, so that is intended.
- **System prompt on resume:** Claude Code records the system prompt (including the appended file) at a session's first request and reuses it on every resume until the conversation is compacted. A changed `system_append` only takes effect in a new session. Put per-run context into the prompt, not into `system_append`, or add `--system-prompt-snapshot off` to `COACH_CLAUDE_ARGS` (at the price of prompt-cache reuse).
- After the final result, `claude -p` can stay alive waiting for background work (background shell commands, sub-agents; by its docs up to 10 minutes) and may even start another turn with its result. The runner returns the first result after `exit_wait_s` and stops that background work.
- On SIGINT Claude Code ends the turn cleanly; on SIGTERM it exits with code 143 and records no result. That is why the stop sequence starts with SIGINT.

## Codex (not yet verified)

Built from developers.openai.com/codex (non-interactive mode, CLI reference, configuration reference, authentication) and the JSONL event definitions in the openai/codex repository (`codex-rs/exec/src/exec_events.rs`, `codex-rs/exec/src/cli.rs`). The CLI was not installed; the parser is tested only with the documented sample stream and synthetic fixtures.

```
codex exec --json --skip-git-repo-check --cd <cwd> -c features.memories=false
      [-c model_reasoning_effort="<level>"] [--model <model>]
      --dangerously-bypass-approvals-and-sandbox
      [COACH_CODEX_ARGS]
      [resume <session_id>] -
```

- The prompt goes to stdin (`-`). Codex appends piped stdin to an argument prompt, so the child's stdin is never inherited; it gets the prompt.
- `system_append` is **prepended to the stdin prompt** (separated by a `---` line), so `supports_system_append` is `False`. Codex's `developer_instructions` setting would add real developer instructions, but it can only be given as a `-c` argument, which is visible via `ps`; its file setting `model_instructions_file` replaces the built-in instructions and `AGENTS.md` instead of adding to them.
- All options come before `resume`: in `cli.rs`, `--json`, `--model`, `--skip-git-repo-check` and the bypass flag are global, but `--cd` and `--sandbox` are not.
- `--skip-git-repo-check` because the coach workspace need not be a Git repository.
- `-c features.memories=false` keeps Codex's memories off (see [No memory of the harness's own](#no-memory-of-the-harnesss-own)); the CLI reference lists `-c, --config` among the `codex exec` options.
- `-c model_reasoning_effort="<level>"` carries `RunRequest.effort` (the level names are Codex's own; see [Reasoning effort](#reasoning-effort)).
- Events: `thread.started` → `Started(thread_id)`; `item.*` with `agent_message` → text progress (the last one is the answer); `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, `collab_tool_call` → one tool line; `turn.completed` → success and usage; `turn.failed` / `error` → failure, classified from the message text. `turn.completed` and `turn.failed` start the exit wait. Codex reports no cost.
- Models: no default; set `COACH_CODEX_MODEL_FAST/SMART` (model names change often; see the Codex models page).
- Authentication: `codex login --device-auth` on the server (or copy `~/.codex/auth.json`, which is a password-equivalent file; `CODEX_HOME` moves it), or `CODEX_API_KEY` for API billing. The healthcheck runs `codex login status` (exit code 0 when logged in).

## Cursor CLI (not yet verified)

> **Warning: the prompt is visible to other users on the host.** Cursor's documentation offers no stdin or file route for the prompt, so the prompt (including any `system_append`) is a command-line argument, which every user on the host can read via `ps`. The healthcheck always reports this as a warning. Use this adapter only where that is acceptable, for example on a host with no other users.

Built from cursor.com/docs/cli (headless, output format, parameters, authentication, configuration). The CLI was not installed; tested only with the documented example stream and synthetic lines.

```
agent -p --output-format stream-json --workspace <cwd>
      [--model <model>] [--resume=<session_id>]
      --force --trust
      [COACH_CURSOR_ARGS]
      -- <system_append, a "---" line, then the prompt>
```

- The prompt is an argument after `--` (so a prompt starting with `-` is not read as an option). The `--resume=<id>` form is used because `--resume` takes an optional value.
- `--trust` skips the workspace-trust prompt in headless mode; `--force` allows all commands.
- No system-prompt option exists (`supports_system_append=False`), so `system_append` is prepended to the prompt. Cursor also reads `AGENTS.md` from the workspace.
- No effort option exists either (`supports_effort=False`): `RunRequest.effort` is ignored (see [Reasoning effort](#reasoning-effort)).
- The terminal `result` concatenates all assistant segments without separators, so the adapter uses the last assistant message (the text after the last tool call) as the answer. The `result` event starts the exit wait.
- On failure Cursor exits non-zero with a message on stderr and may emit no `result`; the adapter classifies that message.
- No usage or cost is reported.
- Authentication: `agent login` (set `NO_OPEN_BROWSER=1` on a server) or `CURSOR_API_KEY`. The healthcheck runs `agent status --format json`; its JSON shape is undocumented, so the result may be "auth unknown".

## The agent's own Unix user

An allow-listed environment keeps secrets out of the agent's *own* environment, but on Linux a process can read the environment of every other process of the same user (`/proc/<pid>/environ`), signal it and read its files. The bridge's environment holds the Telegram token and the read/write database URL, so in the container the agent runs as a second user. This is a setting of the shared subprocess layer, off by default (local development needs no second user):

```
COACH_AGENT_USER=agent                                         # on in compose.yml
COACH_AGENT_USER_HELPER=/usr/local/libexec/thresy/run-as-agent # the default
```

With it, every CLI process the runner starts (runs and health checks) goes through a small setuid helper, [`docker/run-as-agent.c`](../docker/run-as-agent.c):

```
run-as-agent -- /usr/bin/claude -p --output-format stream-json ...   # the run
run-as-agent --signal 2 <pgid>                                        # stopping it
```

The helper is owned by root, group `coach`, mode 4750, so only the bridge's user can run it, and it also refuses every caller whose real user id is not the bridge's. It switches for good to the agent user (supplementary groups, real, effective and saved ids; it checks that root cannot be regained), sets `no_new_privs` so neither the CLI nor anything it starts can gain privileges again (setuid programs such as `su` lose their effect), and then either executes the CLI or, with `--signal`, sends a signal to a process group. As the agent user, `kill(-pgid)` reaches only the agent's own processes; process groups 0 and 1 are refused. The user and group ids and the credential file's path are compiled in (`docker/build-run-as-agent.sh`, the same values as `docker/setup-users.sh`).

What changes for the runner:

| | Without `COACH_AGENT_USER` | With it |
|---|---|---|
| Command line | the CLI | `[helper, "--", *cli]`; the environment travels through `execve`, never through arguments |
| `HOME`, `USER`, `LOGNAME` | the service's | the agent user's (password database) |
| Model credential | from the allow-listed environment | also from the agent's credential file `<agent home>/.thresy/credentials.env` (below) |
| `TMPDIR`, `LD_*` and similar | passed if allow-listed | removed by the C library because the helper is setuid (glibc's secure mode) |
| umask | inherited | `007`: files in the shared workspace stay readable and writable for the group, private otherwise |
| Stopping the group | `killpg` | `run-as-agent --signal`, because the service's user may not signal another user's processes. Whether the group still exists is checked with `killpg(pgid, 0)` (`EPERM` means it exists) |
| `--append-system-prompt-file` | mode 0600 in a 0700 directory | mode 0640 in a 0750 directory, group = the agent's group |

Neither the stop sequence nor its timing changes: SIGINT, SIGTERM, SIGKILL with the same grace periods. If the helper cannot deliver a signal, the next one is tried anyway and the failure is logged.

**The agent's credential file.** `<agent home>/.thresy/credentials.env` (`/home/agent/.thresy/credentials.env` in the image) holds `NAME=value` lines (`#` comments, an optional `export `, one pair of quotes around a value). The helper reads it as the agent user, after the switch, and adds the variables to the CLI's environment (overriding variables of the same name). The bridge's user cannot read it (the agent's home is mode 0700), so the model credential (`CLAUDE_CODE_OAUTH_TOKEN`, `CODEX_API_KEY`, `CURSOR_API_KEY`) does not have to be in the bridge's environment at all. Values are never printed. docs/deploy.md shows how to write it.

**Files and groups.** The image has user `coach` (10001) and user `agent` (10002, group `agent` 10002); `coach` is also a member of group `agent`, never the other way round. The workspace (`/workspace`) belongs to `agent:agent` with mode 2770 (setgid, so everything created inside keeps the group), and `coach workspace init` creates directories 2770 and files 0660 there. So the bridge writes `inbox/` and reads `out/`, the agent reads `inbox/` and writes `out/`, and both can edit what the other created. The agent's home (`/home/agent`) is 0700; the bridge's home (`/home/coach`) is 0700; `/data` (read-only in the bridge) and `/app` are not writable for the agent.

**Checking it.** `coach runner check --isolation` (or `python -m thresy.runner check --isolation`) starts a probe the way an agent run is started and prints one line per check: another user, `no_new_privs`, no access to the environment of the bridge, PID 1 or any other user's process, no signal to the bridge, no private file of the bridge, the helper not executable for the agent, the environment exactly the allow-list, no value of a non-allow-listed bridge variable in the agent's environment, no writes outside the workspace except its home and `/tmp` (in particular not to the working directory of `coach`, which reads a `.env` file from there; the image runs everything from `/app`), the workspace exchange in both directions, and the bridge stopping the agent's process group (once with processes that exit on SIGINT, once with processes that ignore SIGINT and SIGTERM). It ends with `RESULT: PASS` or `RESULT: FAIL`, exits 0 or 1, and prints JSON with `--json`. Run it in the bridge container: `docker compose exec bridge coach runner check --isolation`.

**Tested** on Linux in a test image built from the runtime stage's base image with the same users, helper and container settings as the bridge (`init`, all capabilities dropped except `SETUID` and `SETGID`): `sh docker/isolation-test.sh` builds it, runs `tests/runner/test_runner_process.py`, `tests/runner/test_isolation.py` and `tests/runner/test_isolation_linux.py` (real process groups across the two users: timeout, SIGKILL escalation, cancellation, consumer cancellation, leftovers after exit, a lingering CLI, the credential file, the shared workspace) and the self-test, and removes the image. Elsewhere `test_isolation.py` covers the runner's side with a stand-in helper.

**Limits.** The agent still has everything an agent run needs: its database roles (`coach_agent`, `coach_ro`), its model credential, the workspace, its home, `/tmp` and the network. Processes it moves out of its process group (`setsid`) are not stopped with the group; they run as the agent user, without access to the bridge, until the container stops. It shares the bridge container's memory and process limits, so a runaway agent can make the bridge run out of memory.

## Process handling

- Each CLI runs in its own session and process group (`start_new_session=True`). stdout is read in chunks and split into lines (lines over 16 MiB are dropped and counted); stderr is drained concurrently into a 16 KiB tail, so a chatty child never blocks.
- On timeout, cancellation or consumer cancellation the whole group gets SIGINT, then SIGTERM, then SIGKILL, with 3 s between steps. The same happens when the CLI has not exited `exit_wait_s` after its final result, and to processes left in the group after a normal exit (for example a server the agent started in the background).
- Run the container with an init process (`init: true` in Compose). Otherwise orphaned grandchildren become zombies of PID 1, are never reaped, and keep their process group "alive", which makes every stop wait for the full grace periods.

## Command line

```
coach runner check [--runner NAME | --all] [--json]
coach runner check --isolation [--json]                                     # see above
coach runner run --cwd DIR [--runner NAME] [--resume ID] [--tier fast|smart]
                 [--effort low|medium|high|xhigh|max]
                 [--timeout S] [--system-append TEXT] [--json] PROMPT     # "-" reads stdin
```

`python -m thresy.runner check|run …` takes the same arguments and needs nothing outside the standard library. `check` exits 0 when the runner looks usable, 1 when not, 2 on a configuration error. `run` prints events as they arrive (with `--json`, one JSON object per line) and exits 0 on success; the first Ctrl+C cancels the run cleanly.

## Tests and fixtures

`tests/runner/fixtures/claude/claude_new_session.jsonl`, `claude_resume_tool_call.jsonl` and `claude_auth_invalid_token.jsonl` are real Claude Code output with ids, paths, tool lists and usage figures replaced by synthetic values. All other fixtures are synthetic, built from the documentation of each CLI. The subprocess tests generate a small fake CLI in Python and check streaming, garbage lines, non-zero exits, timeouts, SIGKILL escalation, cancellation, a CLI that lingers after its result, the environment allow-list, the private system-prompt file, the retry without an effort level, and that the process group is gone afterwards. Run them alone with `uv run pytest tests/runner` or as part of `uv run pytest`.

To capture a new fixture from a real CLI: run it in an empty temporary directory with the cheapest model and a trivial prompt, then replace everything personal (ids, paths, e-mail addresses, organisation ids, host names, tool and plugin lists) before committing.

## Adding another harness

1. Read the CLI's current documentation for non-interactive mode, JSON event output, session resume, model selection, reasoning effort, unattended approvals, an auth-status command, the environment variables it reads for auth and configuration, and a stdin or file route for the prompt and extra instructions. Confirm every flag with `--help`.
2. Add `src/thresy/runner/<name>.py` with a `StreamParser` subclass (`feed` maps one JSON object to `Started`/`Progress` events and sets `terminal_seen` on the final result; `finish` builds the `RunResult` from what it saw and the `ProcessExit`) and a `CliRunner` subclass (`name`, `capabilities`, `default_binaries`, `default_models`, `default_permission_args`, `env_names`/`env_prefixes`, `verified`, `build_invocation`, `new_parser`, `check_auth`, and `static_warnings` if personal text must go on the command line; map `RunRequest.effort` in `build_invocation` and set `capabilities.supports_effort`, or leave it `False` when the CLI has no such setting). Use `classify_text`, `summarize_tool`, `failure_from_exit`, `private_file` and `prepend_system` from the base modules.
3. Register it in `registry.py`, add its name to `RUNNER_NAMES` and a field to `RunnerConfig`.
4. Add fixtures and tests under `tests/runner/`, and a section here. Mark it unverified until it has been run against the real CLI.
