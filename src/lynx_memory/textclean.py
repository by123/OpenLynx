"""Strip harness-injected noise from stored user messages.

Claude Code / Codex append machine text to what the user typed: CLAUDE.md
dumps in <system-reminder>, subagent <task-notification> envelopes, skill
bodies, slash-command wrappers, local command stdout. Stored verbatim in
`turns.user_msg`, this noise crowds out the real request when summarizing
and triggers pointless recall when it *is* the whole "prompt".
"""
from __future__ import annotations

import re

_BLOCKS = [
    # whole envelopes we drop entirely; an envelope left unclosed (the stored
    # message was truncated inside it) runs to the end of the text
    re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S),
    re.compile(r"<task-notification>.*?(?:</task-notification>|\Z)", re.S),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.S),
    re.compile(r"<local-command-stderr>.*?</local-command-stderr>", re.S),
    re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S),
    re.compile(r"<memory>\s*Relevant context from prior conversations.*?</memory>", re.S),
]

# skill body injected as a user message → keep only its name
_SKILL = re.compile(r"<skill>\s*<name>(.*?)</name>.*?</skill>", re.S)
_SKILL_BASE = re.compile(r"Base directory for this skill:.*", re.S)
# slash command wrapper → "/name args"
_CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_CMD_MISC = re.compile(r"</?command-(message|contents)>", re.S)
_IMAGE = re.compile(r"\[Image: original \d+x\d+[^\]]*\]")
_MULTI_NL = re.compile(r"\n{3,}")


def clean_user_text(text: str) -> str:
    """Return the human-authored part of a stored user message."""
    if not text:
        return ""
    s = text
    for pat in _BLOCKS:
        s = pat.sub("", s)
    s = _SKILL.sub(lambda m: f"[skill: {m.group(1).strip()}]", s)
    s = _SKILL_BASE.sub("[skill body omitted]", s)
    name = _CMD_NAME.search(s)
    if name:
        args = _CMD_ARGS.search(s)
        cmd = name.group(1).strip()
        if args and args.group(1).strip():
            cmd += " " + args.group(1).strip()
        s = _CMD_NAME.sub("", s)
        s = _CMD_ARGS.sub("", s)
        s = f"{cmd}\n{s}"
    s = _CMD_MISC.sub("", s)
    s = _IMAGE.sub("[image]", s)
    s = _MULTI_NL.sub("\n\n", s)
    return s.strip()


def is_noise_prompt(text: str) -> bool:
    """True when a prompt carries no human-authored text (e.g. only a
    <task-notification> envelope) — recall for it is wasted context."""
    return len(clean_user_text(text or "")) < 4
