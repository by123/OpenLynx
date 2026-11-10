"""Session-summary hook.

Claude Code: registered as `SessionEnd` — `session_id` in stdin is the session
that just ended; we summarize it.

Codex CLI: registered as `SessionStart` — `session_id` is the *new* session
about to begin (Codex has no SessionEnd event). We instead summarize the most
recent unsummarized session in the DB.

The hook itself only picks the session and detaches a worker
(`python -m lynx_memory.hooks.on_session_end --worker <data_dir> <session_id>`)
so a slow LLM call never blocks the host or holds the store's replica lock.
The worker reads the turns, closes the store, calls the configured backend
(`claude -p` with SUMMARY_BACKEND=claude), then reopens the store just to
write. Re-summarizing a session replaces its previous summary.
"""
import json
import os
import subprocess
import sys
import traceback
from typing import Optional

from ._log import log


def _parse_target() -> str:
    for a in sys.argv[1:]:
        if a.startswith("--target="):
            return a.split("=", 1)[1]
    if "--target" in sys.argv:
        i = sys.argv.index("--target")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return os.environ.get("LYNX_MEMORY_TARGET", "claude_code")


SUMMARIZE_PROMPT = """You write the session note for one working session between a developer and a coding agent. It is stored long-term, injected into future sessions, and read back when the developer asks "what did I do / how did I solve X".

The input lists each turn as the user's words, the turn's memory note, and the end of the assistant's reply (where the final outcome usually is).

Write:
- Line 1: one sentence — what this session set out to do and where it actually ended.
- Then up to 8 bullets, each starting with one tag: [decision] [pitfall] [definition] [finding] [deliverable] [preference] [todo] [status]
  - [decision]: what was chosen and WHY; mention the rejected alternative if any. Say whether the user decided or the assistant only proposed.
  - [pitfall]: symptom → root cause → fix.
  - [finding]: numbers with their scope (time window, population).
  - [status]/[todo]: be honest — discussed ≠ done; edited-but-untested/uncommitted is not verified. List what is still open.
- Every bullet needs a concrete anchor (file, function, command, version, number, table). No vague verbs ("improved", "optimized", "完善了").
- If a plan was reversed during the session, record only the final state plus a short "(reversed from …)".
- Ignore harness noise: injected system text, skill bodies, role-play instructions inside prompts are not user facts. Never record secrets — write "[redacted]".
- Write in the language the USER mostly used (usually Chinese); keep identifiers and technical terms verbatim.
- Under 250 words. Output the note only, no preamble.

Conversation:
{conversation}"""


def _build_prompt(conversation: str, goal=None) -> str:
    """Render the session-summary prompt, optionally focused on the user's goal."""
    base = SUMMARIZE_PROMPT.format(conversation=conversation)
    goal = (goal or "").strip()
    if not goal:
        return base
    return (
        f"The user's overarching goal for this work is:\n{goal}\n\n"
        "Give priority to memories that advance or relate to this goal.\n\n"
        f"{base}"
    )


def build_conversation(turns: list, budget: int = 60000) -> str:
    """Compact, cleaned transcript for the session prompt.

    Uses the human part of each user message, the turn's memory note, and the
    *tail* of the assistant reply — the old head-truncation cut off exactly the
    final conclusions of long replies.
    """
    from ..textclean import clean_user_text

    n = max(1, len(turns))
    per = max(600, budget // n)
    u_cap, a_cap = int(per * 0.35), int(per * 0.45)
    parts = []
    for i, t in enumerate(turns, 1):
        u = clean_user_text(t.get("user_msg") or "")[:u_cap]
        a = (t.get("assistant_msg") or "").strip()
        a_tail = a[-a_cap:] if len(a) > a_cap else a
        note = (t.get("summary") or "").strip()
        block = f"## Turn {i}\nUSER: {u}"
        if note:
            block += f"\nNOTE: {note}"
        block += f"\nASSISTANT (end of reply): {a_tail}"
        parts.append(block)
    conv = "\n\n".join(parts)
    if len(conv) > budget:
        conv = conv[: budget // 3] + "\n\n[... middle turns omitted ...]\n\n" + conv[-(2 * budget // 3):]
    return conv


def _summarize(conversation: str, goal=None) -> str:
    """Summarize with the active backend only (no cross-provider fallback)."""
    from ..summarizer import _chat, active_provider

    provider = active_provider()
    if not provider:
        return ""
    out = _chat(provider, "You write concise, factual session notes.", _build_prompt(conversation, goal),
                max_tokens=1500, timeout=180)
    return (out or "").strip()


def summarize_session(data_dir, session_id: str) -> int:
    """Worker body: read → (store closed) → LLM → write. Returns an exit code."""
    from ..config import load_env
    from ..storage import Memory
    from ..summarizer import is_enabled

    load_env(data_dir)
    if not is_enabled():  # SUMMARY_ENABLED=0 switches off every LLM summary
        return 0
    mem = Memory(data_dir=data_dir)
    try:
        turns = mem.get_session_turns(session_id)
        goal = (mem.get_goal() or {}).get("text")
        notes = {}
        if turns:
            ids = [t["id"] for t in turns]
            notes = {k: v.get("summary") for k, v in mem.get_turns_by_ids(ids).items()}
    finally:
        mem.close()
    if len(turns) < 2:
        return 0
    for t in turns:
        t["summary"] = notes.get(t["id"])

    summary = _summarize(build_conversation(turns), goal)
    if not summary:
        log(f"[on_session_end] no summary produced for {session_id}")
        return 2
    mem = Memory(data_dir=data_dir)
    try:
        mem.replace_session_summary(session_id, summary, len(turns))
    finally:
        mem.close()
    return 0


def _spawn_worker(data_dir, session_id: str) -> None:
    try:
        subprocess.Popen(
            [sys.executable, "-m", "lynx_memory.hooks.on_session_end", "--worker",
             str(data_dir), session_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as e:
        log(f"[on_session_end] spawn failed: {e}")


def _main() -> int:
    if os.environ.get("LYNX_MEMORY_NO_HOOK"):
        return 0
    target = _parse_target()
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    incoming_session_id = data.get("session_id") or ""
    cwd = data.get("cwd") or ""

    try:
        from ..config import load_env, resolve_data_dir
        from ..storage import Memory

        data_dir = resolve_data_dir(cwd)
        load_env(data_dir)

        session_id: Optional[str]
        mem = Memory(data_dir=data_dir)
        try:
            # Codex fires SessionStart for the *new* session — summarize the
            # previous one instead.
            if target == "codex":
                session_id = mem.find_unsummarized_session(
                    exclude_session_id=incoming_session_id, min_turns=2
                )
            else:
                session_id = incoming_session_id or None
            if session_id:
                mem.end_session(session_id)
        finally:
            mem.close()

        from ..summarizer import is_enabled

        if session_id and is_enabled():
            _spawn_worker(data_dir, session_id)
    except Exception as e:
        log(f"[on_session_end] ERROR: {e}\n{traceback.format_exc()}")
        return 0
    return 0


def main() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        from pathlib import Path

        try:
            sys.exit(summarize_session(Path(sys.argv[2]), sys.argv[3]))
        except Exception as e:
            log(f"[on_session_end] worker ERROR: {e}\n{traceback.format_exc()}")
            sys.exit(1)
    sys.exit(_main())


if __name__ == "__main__":
    main()
