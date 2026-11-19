"""The prompts the bridge gives the agent. Short and harness-neutral.

A prompt says who is writing (the athlete, or nobody for a proactive run), what triggered the
run, which playbook in the workspace to follow, and the language to answer in. The coaching
knowledge lives in the workspace (``AGENTS.md``, ``playbooks/``), which another package
writes; when a playbook file does not exist yet, the prompt carries a one-line fallback
instead, so the bridge works without it.

Per-run context goes into the prompt, never into ``RunRequest.system_append`` (some harnesses
keep the first system prompt of a session for all its runs).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NO_REPLY = "NO_REPLY"

LANGUAGE_NAMES = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
    "pl": "Polish",
    "sv": "Swedish",
    "da": "Danish",
    "nb": "Norwegian",
    "no": "Norwegian",
    "fi": "Finnish",
    "cs": "Czech",
}

CHAT_PLAYBOOK = "chat.md"
BRIEFING_PLAYBOOK = "morning-briefing.md"
DEBRIEF_PLAYBOOK = "post-run.md"

BRIEFING_FALLBACK = (
    "There is no playbooks/morning-briefing.md yet: give a short briefing on last night's"
    " sleep, HRV and resting heart rate against their recent range, recent training load and"
    " what today's plan should be."
)
DEBRIEF_FALLBACK = (
    "There is no playbooks/post-run.md yet: give a short debrief of the run compared with"
    " recent similar runs and the plan."
)


def language_name(language: str) -> str:
    raw = (language or "en").strip()
    code = raw.lower().replace("_", "-").split("-")[0]
    return LANGUAGE_NAMES.get(code, raw)


@dataclass(frozen=True)
class PromptEnv:
    workspace: Path
    zone: ZoneInfo
    language: str
    transport: str

    def local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.zone)

    def stamp(self, moment: datetime) -> str:
        """``Thu 2026-11-19 08:15``"""
        return self.local(moment).strftime("%a %Y-%m-%d %H:%M")

    def playbook(self, name: str, fallback: str | None = None) -> str | None:
        if (self.workspace / "playbooks" / name).is_file():
            return f"Follow playbooks/{name}."
        return fallback


@dataclass(frozen=True)
class PromptMessage:
    """One inbound message as the prompt shows it."""

    ts: datetime
    kind: str
    text: str
    attachments: Sequence[dict[str, Any]] = ()
    question: str | None = None
    """For a pressed button: the question it answered."""


@dataclass(frozen=True)
class ProactiveSection:
    """One trigger of a proactive run (several can share a run)."""

    trigger: str
    text: str
    playbook: str
    fallback: str


def _attachment_line(att: dict[str, Any]) -> str:
    kind = att.get("kind", "file")
    path = att.get("path")
    if att.get("error") or not path:
        return f"{kind} (could not be saved: {att.get('error') or 'unknown error'})"
    what = {"voice": "voice message", "audio": "audio file"}.get(kind, kind)
    extra = []
    if att.get("duration_s"):
        extra.append(f"{round(float(att['duration_s']))} s")
    if kind == "document" and att.get("file_name"):
        extra.append(str(att["file_name"]))
    detail = f" ({', '.join(extra)})" if extra else ""
    note = "; not transcribed" if kind in ("voice", "audio") else ""
    return f"{what}{detail} at {path}{note}"


def _message_body(msg: PromptMessage) -> str:
    if msg.kind == "choice_answer":
        line = f'Pressed the button "{msg.text}"'
        if msg.question:
            line += f', answering your question: "{_one_line(msg.question, 300)}"'
        return line
    parts = []
    if msg.text.strip():
        parts.append(msg.text.strip())
    for att in msg.attachments:
        parts.append(f"[{_attachment_line(att)}]")
    return "\n".join(parts) or "(empty message)"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def context_block(env: PromptEnv, rows: Sequence[dict[str, Any]], max_chars: int) -> str | None:
    """Earlier messages from the log for a fresh session (oldest first, bounded)."""
    lines: list[str] = []
    used = 0
    for row in reversed(rows):  # newest first, so the budget keeps the latest
        who = "Athlete" if row["direction"] == "in" else "Coach"
        text = row.get("text") or ""
        if row.get("kind") == "choice_answer":
            text = f'(pressed "{text}")'
        elif not text.strip():
            text = f"({row.get('kind', 'message')})"
        line = f"[{env.local(row['ts']).strftime('%m-%d %H:%M')}] {who}: {_one_line(text, 800)}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return None
    lines.reverse()
    return (
        "You are starting a new session. Earlier messages from the chat log, oldest first"
        f" (times {env.zone.key}):\n" + "\n".join(lines) + "\n(end of earlier messages)"
    )


def _footer(env: PromptEnv, *, proactive: bool, playbooks: Sequence[str]) -> str:
    lines = list(playbooks)
    lines.append(
        f"Reply in {language_name(env.language)}. Your final reply is sent to the athlete as one"
        " chat message. Send nothing in between unless it carries content (`coach send`, for"
        " plots, files or questions with buttons)."
    )
    if proactive:
        lines.append(f"If there is nothing worth sending, reply with exactly {NO_REPLY}.")
    return "\n".join(lines)


def chat_prompt(
    env: PromptEnv,
    messages: Sequence[PromptMessage],
    *,
    context: str | None = None,
) -> str:
    """Prompt for one or more messages from the athlete."""
    if not messages:
        raise ValueError("no messages")
    parts: list[str] = []
    if context:
        parts.append(context)
    if len(messages) == 1:
        msg = messages[0]
        who = (
            "The athlete pressed a button in the chat"
            if msg.kind == "choice_answer"
            else "The athlete wrote in the chat"
        )
        parts.append(f"{who} ({env.transport}, {env.stamp(msg.ts)}, {env.zone.key}):")
        parts.append(_message_body(msg))
    else:
        parts.append(
            f"The athlete sent {len(messages)} messages in the chat while you were busy"
            f" ({env.transport}, {env.zone.key}); answer them together:"
        )
        parts.append(
            "\n\n".join(
                f"[{env.local(m.ts).strftime('%H:%M')}] {_message_body(m)}" for m in messages
            )
        )
    playbook = env.playbook(CHAT_PLAYBOOK)
    parts.append(_footer(env, proactive=False, playbooks=[playbook] if playbook else []))
    return "\n\n".join(parts)


def briefing_section(
    env: PromptEnv,
    day: date,
    *,
    synced: bool,
    as_of: str,
    early_end: str | None = None,
    night_end_earliest: str | None = None,
    hrv_missing: bool = False,
) -> ProactiveSection:
    """The briefing trigger, describing the data as it is when the run starts (``as_of``,
    local HH:MM).

    ``early_end``: the stored night ends at this local time, before ``night_end_earliest``
    (the athlete may have slept again after a watch sync in the night). ``hrv_missing``: the
    night is stored but its HRV is not.
    """
    text = (
        f"Trigger: morning briefing for {day.strftime('%a %Y-%m-%d')}. Last night's sleep"
        f" (wake date {day.isoformat()})"
    )
    if not synced:
        text += (
            f" has not been synced yet (as of {as_of}): say so in one line and do not present"
            " an older night as last night."
        )
    else:
        text += " has arrived in the database."
        if early_end:
            text += (
                f" The stored night ends at {early_end}, before {night_end_earliest}, so it may"
                " be incomplete (awake in the night and asleep again later): say so where it"
                " matters."
            )
        if hrv_missing:
            text += (
                " HRV for last night is not in the database yet: say so instead of estimating it."
            )
    return ProactiveSection("morning_briefing", text, BRIEFING_PLAYBOOK, BRIEFING_FALLBACK)


def debrief_section(env: PromptEnv, activities: Sequence[dict[str, Any]]) -> ProactiveSection:
    described = []
    for act in activities:
        bits = [str(act.get("type") or "run")]
        if act.get("start_local"):
            bits.append(f"started {act['start_local']:%Y-%m-%d %H:%M} local")
        if act.get("distance_m"):
            bits.append(f"{act['distance_m'] / 1000:.1f} km")
        described.append(f"activity {act['id']} ({', '.join(bits)})")
    if len(described) == 1:
        text = f"Trigger: post-run debrief. New run uploaded: {described[0]}."
    else:
        text = (
            "Trigger: post-run debrief. New runs uploaded: "
            + "; ".join(described)
            + ". Debrief them in one message."
        )
    return ProactiveSection("post_run", text, DEBRIEF_PLAYBOOK, DEBRIEF_FALLBACK)


def proactive_prompt(
    env: PromptEnv,
    sections: Sequence[ProactiveSection],
    now: datetime,
    *,
    context: str | None = None,
) -> str:
    """Prompt for a run the bridge started on its own (no one wrote)."""
    if not sections:
        raise ValueError("no sections")
    parts: list[str] = []
    if context:
        parts.append(context)
    head = (
        f"Nobody wrote: the bridge started this run on its own ({env.stamp(now)}, {env.zone.key})."
    )
    if len(sections) > 1:
        head += " Several things are due; cover them together in one message."
    parts.append(head)
    parts.append("\n".join(s.text for s in sections))
    playbooks = []
    for s in sections:
        line = env.playbook(s.playbook, s.fallback)
        if line and line not in playbooks:
            playbooks.append(line)
    parts.append(_footer(env, proactive=True, playbooks=playbooks))
    return "\n\n".join(parts)


__all__ = [
    "NO_REPLY",
    "ProactiveSection",
    "PromptEnv",
    "PromptMessage",
    "briefing_section",
    "chat_prompt",
    "context_block",
    "debrief_section",
    "language_name",
    "proactive_prompt",
]
