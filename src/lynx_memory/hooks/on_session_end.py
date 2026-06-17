"""Session-summary hook.

Claude Code: registered as `SessionEnd` — `session_id` in stdin is the session
that just ended; we summarize it.

Codex CLI: registered as `SessionStart` — `session_id` is the *new* session
about to begin (Codex has no SessionEnd event). We instead summarize the most
recent unsummarized session in the DB.
"""
import json
import os
import sys
import traceback

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

SUMMARIZE_PROMPT = """You are an AI memory retrieval assistant. Extract memories worth preserving long-term from the following conversation.

Your goal is not to summarize everything, but to determine which information will still be valuable to the user in the future.

Please adhere to the following rules:

1. Only extract information that is useful in the long term.
2. Do not save temporary states, one-off questions, or small talk with no long-term value.
3. Do not save sensitive personal information unless explicitly requested by the user.
4. Do not fabricate content that did not appear in the conversation.
5. Each memory must be concise, clear, and retrievable in the future.
6. If the information is only short-term task progress, mark it as temporary.
7. If the information is user preferences, long-term rules, project background, technology stack, or business decisions, mark it as long_term.

Produce a concise memory summary under 250 words.
Use bullets prefixed with [long_term] or [temporary].
Prefer user preferences, long-term rules, project background, technology stack, business decisions, final outcomes, and still-relevant follow-ups.
Be specific with names, paths, tools, and decisions. Write in third person, plain prose, no extra headers.

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


def _summarize_via_openai(conversation: str, goal=None) -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return ""
    try:
        from openai import OpenAI

        kwargs = {
            "api_key": key,
            "base_url": os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1",
        }
        client = OpenAI(**kwargs)
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        prompt = _build_prompt(conversation, goal)
        try:
            resp = client.responses.create(
                model=model,
                input=prompt,
                max_output_tokens=800,
            )
            return (resp.output_text or "").strip()
        except Exception:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
            )
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log(f"[on_session_end] OpenAI summary failed: {type(e).__name__}: {e}")
        return ""


def _summarize_via_compat(provider: str, conversation: str, goal=None) -> str:
    """Summarize via an OpenAI-compatible provider (deepseek, qwen)."""
    from ..summarizer import OPENAI_COMPAT_PROVIDERS, provider_api_key

    cfg = OPENAI_COMPAT_PROVIDERS[provider]
    key = provider_api_key(provider)
    if not key:
        return ""
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=key,
            base_url=os.environ.get(cfg["base_url_env"], "").strip() or cfg["default_base_url"],
        )
        model = os.environ.get(cfg["model_env"], cfg["default_model"])
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _build_prompt(conversation, goal)}],
            max_tokens=800,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log(f"[on_session_end] {provider} summary failed: {type(e).__name__}: {e}")
        return ""


def _summarize_via_deepseek(conversation: str, goal=None) -> str:
    return _summarize_via_compat("deepseek", conversation, goal)


def _summarize_via_qwen(conversation: str, goal=None) -> str:
    return _summarize_via_compat("qwen", conversation, goal)


def _call_provider(provider: str, conversation: str, goal=None) -> str:
    if provider == "openai":
        return _summarize_via_openai(conversation, goal=goal)
    if provider == "deepseek":
        return _summarize_via_deepseek(conversation, goal=goal)
    if provider == "qwen":
        return _summarize_via_qwen(conversation, goal=goal)
    return ""


def _summarize(conversation: str, goal=None) -> str:
    """Try the configured summary backend. Return empty string on failure."""
    from ..summarizer import provider_order

    for provider in provider_order():
        out = _call_provider(provider, conversation, goal)
        if out:
            return out
    return ""


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
        from ..config import GLOBAL_DATA_DIR, load_env, resolve_data_dir
        from ..storage import Memory
        data_dir = resolve_data_dir(cwd)
        load_env(GLOBAL_DATA_DIR)

        mem = Memory(data_dir=data_dir)

        # Codex fires SessionStart for the *new* session — summarize the
        # previous one instead.
        if target == "codex":
            session_id = mem.find_unsummarized_session(
                exclude_session_id=incoming_session_id, min_turns=2
            )
            if not session_id:
                mem.close()
                return 0
        else:
            session_id = incoming_session_id
            if not session_id:
                mem.close()
                return 0

        turns = mem.get_session_turns(session_id)
        if len(turns) < 2:
            mem.end_session(session_id)
            mem.close()
            return 0

        parts = []
        for t in turns:
            u = (t["user_msg"] or "").strip()[:2000]
            a = (t["assistant_msg"] or "").strip()[:2000]
            parts.append(f"USER: {u}\nASSISTANT: {a}")
        conversation = "\n\n---\n\n".join(parts)
        if len(conversation) > 60000:
            conversation = conversation[:60000] + "\n\n[...truncated]"

        goal = (mem.get_goal() or {}).get("text")
        summary = _summarize(conversation, goal)
        if summary:
            mem.add_summary(session_id, summary, len(turns))

        mem.end_session(session_id)
        mem.close()
    except Exception as e:
        log(f"[on_session_end] ERROR: {e}\n{traceback.format_exc()}")
        return 0
    return 0


def main() -> None:
    sys.exit(_main())


if __name__ == "__main__":
    main()
