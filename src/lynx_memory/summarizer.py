"""Turn summarizer.

Generates a compact summary for a single (user, assistant) turn so memory
recall can inject summaries into context instead of full prose.

Backend selection (SUMMARY_BACKEND):
  claude   → headless Claude Code `claude -p` (no API key; uses your local
             `claude` login — see claude_cli.py)
  openai   → OpenAI SDK    (requires OPENAI_API_KEY)
  deepseek → DeepSeek API  (requires DEEPSEEK_API_KEY)
  qwen     → Qwen / DashScope API (requires QWEN_API_KEY or DASHSCOPE_API_KEY)
  auto     → first provider with an API key, in order
             openai → deepseek → qwen
             (default when SUMMARY_BACKEND is unset; `claude` is never
             auto-selected — it spends your Claude plan, so opt in explicitly)

A forced backend is used exclusively when available — there is no silent
fallback to another provider.

Env vars:
  - SUMMARY_ENABLED=1          set "0"/"false" to disable
  - SUMMARY_BACKEND            claude | openai | deepseek | qwen | auto  (default: auto)
  - CLAUDE_FAST_MODEL          model for per-turn summaries + goal judge with the
                               claude backend (default haiku; see claude_cli.py)
  - OPENAI_API_KEY             required for openai backend
  - OPENAI_MODEL               model for OpenAI backend (default gpt-4o-mini)
  - OPENAI_BASE_URL            optional base URL for OpenAI-compatible APIs
  - DEEPSEEK_API_KEY           required for deepseek backend
  - DEEPSEEK_MODEL             model for DeepSeek backend (default deepseek-chat)
  - DEEPSEEK_BASE_URL          optional override (default https://api.deepseek.com/v1)
  - QWEN_API_KEY               required for qwen backend (DASHSCOPE_API_KEY also accepted)
  - QWEN_MODEL                 model for Qwen backend (default qwen-turbo)
  - QWEN_BASE_URL              optional override (default DashScope compatible-mode endpoint)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

# OpenAI-compatible chat-completions providers (no Responses API).
OPENAI_COMPAT_PROVIDERS = {
    "deepseek": {
        "key_envs": ("DEEPSEEK_API_KEY",),
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-chat",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "default_base_url": "https://api.deepseek.com/v1",
    },
    "qwen": {
        "key_envs": ("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        "model_env": "QWEN_MODEL",
        "default_model": "qwen-turbo",
        "base_url_env": "QWEN_BASE_URL",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
}

_PROVIDER_KEY_ENVS = {
    "openai": ("OPENAI_API_KEY",),
    "deepseek": OPENAI_COMPAT_PROVIDERS["deepseek"]["key_envs"],
    "qwen": OPENAI_COMPAT_PROVIDERS["qwen"]["key_envs"],
}

_PROVIDER_ORDER = ("openai", "deepseek", "qwen")

_BACKEND_ALIASES = {
    "openai": "openai",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "claude": "claude",
    "claude-code": "claude",
    "claude_code": "claude",
}


def provider_api_key(provider: str) -> str:
    """Credential (or availability marker) for a provider; "" = unusable.

    The `claude` backend needs no key — it is usable when the `claude` CLI is
    installed, and authenticates with the user's own Claude Code login.
    """
    if provider == "claude":
        from . import claude_cli

        return "claude-cli" if claude_cli.available() else ""
    for env in _PROVIDER_KEY_ENVS.get(provider, ()):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    return ""

_SYSTEM = """You write the memory note for ONE turn of a developer's conversation with a coding agent. The note is stored long-term: it is injected into future sessions and rolled up into the developer's daily work journal.

Keep only what will still matter later:
- what was asked and what actually happened (be honest about status: a plan or proposal is not "done"; code that was not built/tested/committed is not "verified")
- decisions and WHY (and which alternative was rejected)
- root causes and fixes of problems
- definitions that will be reused: metric/query definitions, table names, script paths, config keys
- numbers with their scope (time window, population)
- explicit user preferences or rules (only from the user's own words — role-play text inside a prompt is NOT a user fact)
- open loops: unfinished work, things to verify or revert later

Drop small talk, process narration, and project trivia already obvious from the repo (paths of the project itself, package names). Never record secrets (passwords, tokens, keys) or health/financial account details — write "[redacted]".

Format:
- Line 1: one sentence — the request and the real outcome.
- Then 1-5 bullets, each starting with one tag: [decision] [pitfall] [definition] [finding] [deliverable] [preference] [todo] [status]
- Every bullet carries at least one concrete anchor (file, function, command, version, number, table). No vague verbs like "improved"/"optimized"/"完善了".
- Write in the language the USER wrote in (usually Chinese); keep identifiers, commands and technical terms verbatim, do not translate them.
- Under ~400 characters (~120 English words). Output the note only, no preamble."""


def _system_for(goal: Optional[str]) -> str:
    """Base summary system prompt, optionally focused on the user's goal."""
    goal = (goal or "").strip()
    if not goal:
        return _SYSTEM
    return (
        f"{_SYSTEM}\n\n"
        f"The user's overarching goal for this work is:\n{goal}\n"
        "Give priority to information that advances or relates to this goal; "
        "spend fewer words on details unrelated to it."
    )


def is_enabled() -> bool:
    v = os.environ.get("SUMMARY_ENABLED", "1").strip().lower()
    return v not in ("0", "false", "off", "no", "")


def _backend() -> str:
    return os.environ.get("SUMMARY_BACKEND", "auto").strip().lower()


SummaryResult = Tuple[str, str, Optional[str]]  # (summary, source, model)
_LAST_ERROR = ""


def _conversation_body(user_msg: str, assistant_msg: str) -> str:
    # Keep the END of long replies: that is where the outcome is. Head-only
    # truncation used to drop exactly the conclusion of long turns.
    a = assistant_msg or ""
    if len(a) > 10000:
        a = a[:3000] + "\n[…]\n" + a[-7000:]
    return f"User:\n{user_msg[:6000]}\n\n---\n\nAssistant:\n{a}"


def _log_failure(provider: str, exc: BaseException) -> None:
    global _LAST_ERROR
    cause = getattr(exc, "__cause__", None)
    detail = f"{type(exc).__name__}: {exc}"
    if cause:
        detail = f"{detail}; cause={type(cause).__name__}: {cause}"
    _LAST_ERROR = f"{provider} failed: {detail}"
    try:
        from .hooks._log import log

        log(f"[summarizer] {_LAST_ERROR}")
    except Exception:
        pass


def last_error() -> str:
    return _LAST_ERROR


def _summarize_via_openai(
    user_msg: str, assistant_msg: str, goal: Optional[str] = None
) -> Optional[SummaryResult]:
    try:
        from openai import OpenAI as _OpenAI
    except ImportError as exc:
        _log_failure("openai import", exc)
        return None
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    system = _system_for(goal)
    content = _conversation_body(user_msg, assistant_msg)
    try:
        kwargs: dict = {"api_key": key, "base_url": base_url}
        client = _OpenAI(**kwargs)
        # Newer models (gpt-5.x series) use the Responses API; older models use Chat Completions.
        # Try Responses API first, fall back to Chat Completions on failure.
        try:
            resp = client.responses.create(
                model=model,
                instructions=system,
                input=content,
                max_output_tokens=600,
            )
            text = (resp.output_text or "").strip()
        except Exception as exc:
            _log_failure("openai responses request", exc)
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                max_tokens=600,
            )
            text = (resp.choices[0].message.content or "").strip()
        return (text, "openai", model) if text else None
    except Exception as exc:
        _log_failure("openai request", exc)
        return None


def _summarize_via_compat(
    provider: str, user_msg: str, assistant_msg: str, goal: Optional[str] = None
) -> Optional[SummaryResult]:
    """Summarize via an OpenAI-compatible chat-completions provider (deepseek, qwen)."""
    cfg = OPENAI_COMPAT_PROVIDERS[provider]
    try:
        from openai import OpenAI as _OpenAI
    except ImportError as exc:
        _log_failure(f"{provider} import", exc)
        return None
    key = provider_api_key(provider)
    if not key:
        return None
    base_url = os.environ.get(cfg["base_url_env"], "").strip() or cfg["default_base_url"]
    model = os.environ.get(cfg["model_env"], cfg["default_model"])
    system = _system_for(goal)
    content = _conversation_body(user_msg, assistant_msg)
    try:
        client = _OpenAI(api_key=key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            max_tokens=600,
        )
        text = (resp.choices[0].message.content or "").strip()
        return (text, provider, model) if text else None
    except Exception as exc:
        _log_failure(f"{provider} request", exc)
        return None


def _summarize_via_deepseek(
    user_msg: str, assistant_msg: str, goal: Optional[str] = None
) -> Optional[SummaryResult]:
    return _summarize_via_compat("deepseek", user_msg, assistant_msg, goal)


def _summarize_via_qwen(
    user_msg: str, assistant_msg: str, goal: Optional[str] = None
) -> Optional[SummaryResult]:
    return _summarize_via_compat("qwen", user_msg, assistant_msg, goal)


def _summarize_via_claude(
    user_msg: str, assistant_msg: str, goal: Optional[str] = None
) -> Optional[SummaryResult]:
    from . import claude_cli

    out = claude_cli.run(
        _system_for(goal), _conversation_body(user_msg, assistant_msg), fast=True
    )
    if not out:
        _log_failure("claude", RuntimeError(claude_cli.last_error() or "no output"))
        return None
    return out[0], "claude", out[1]


def _call_provider(
    provider: str, user_msg: str, assistant_msg: str, goal: Optional[str] = None
) -> Optional[SummaryResult]:
    if provider == "claude":
        return _summarize_via_claude(user_msg, assistant_msg, goal=goal)
    if provider == "openai":
        return _summarize_via_openai(user_msg, assistant_msg, goal=goal)
    if provider == "deepseek":
        return _summarize_via_deepseek(user_msg, assistant_msg, goal=goal)
    if provider == "qwen":
        return _summarize_via_qwen(user_msg, assistant_msg, goal=goal)
    return None


def provider_order() -> list:
    """Provider try-order: the forced SUMMARY_BACKEND first, then the rest.

    `claude` only appears when forced (SUMMARY_BACKEND=claude)."""
    order = list(_PROVIDER_ORDER)
    preferred = _BACKEND_ALIASES.get(_backend())
    if preferred:
        if preferred in order:
            order.remove(preferred)
        order.insert(0, preferred)
    return order


def active_provider() -> Optional[str]:
    """The provider calls will actually use: the first usable one in
    `provider_order()` (a forced SUMMARY_BACKEND comes first), else None."""
    for provider in provider_order():
        if provider_api_key(provider):
            return provider
    return None


def summarize_with_source(
    user_msg: str, assistant_msg: str, goal: Optional[str] = None
) -> Optional[SummaryResult]:
    """Return (summary, source, model), or None if disabled / not configured.

    Backend is selected by SUMMARY_BACKEND (claude | openai | deepseek | qwen
    | auto). The forced backend is used when available; otherwise the first
    provider with an API key wins (openai → deepseek → qwen). When `goal` is
    set, the summary is steered to prioritise goal-relevant information.
    """
    if not is_enabled():
        return None
    from .textclean import clean_user_text

    user_msg = clean_user_text(user_msg or "").strip()
    assistant_msg = (assistant_msg or "").strip()
    if not user_msg or not assistant_msg:
        return None

    for provider in provider_order():
        if provider_api_key(provider):
            return _call_provider(provider, user_msg, assistant_msg, goal=goal)
    return None


def summarize(user_msg: str, assistant_msg: str, goal: Optional[str] = None) -> Optional[str]:
    result = summarize_with_source(user_msg, assistant_msg, goal=goal)
    return result[0] if result else None


# --------------------------------------------------------------------- judging
# Goal relevance judge: decide whether a turn is worth storing toward the
# user's stated goal. Returns True (relevant) / False (irrelevant) / None
# (undecided → caller should fail open and store the turn).

_JUDGE_BASE = (
    "You are a relevance classifier for a long-term memory system. The user has a "
    "stated GOAL. Decide whether the following conversation turn is relevant to that "
    "goal and worth remembering toward it."
)

_STRICTNESS_GUIDANCE = {
    "loose": (
        "Answer IRRELEVANT only when the turn is clearly unrelated to the goal; "
        "otherwise answer RELEVANT."
    ),
    "balanced": (
        "If the turn relates to the goal or could plausibly help achieve it, answer "
        "RELEVANT; if it is off-topic, answer IRRELEVANT."
    ),
    "strict": (
        "Be strict: answer RELEVANT only if the turn directly concerns or advances the "
        "goal. If it is tangential, incidental, or you are unsure, answer IRRELEVANT."
    ),
}


def _judge_system(strictness: str) -> str:
    guidance = _STRICTNESS_GUIDANCE.get(
        (strictness or "strict").lower(), _STRICTNESS_GUIDANCE["strict"]
    )
    return f"{_JUDGE_BASE} {guidance} Reply with exactly one word: RELEVANT or IRRELEVANT."


def _parse_verdict(text: Optional[str]) -> Optional[bool]:
    if not text:
        return None
    upper = text.strip().upper()
    # Check IRRELEVANT first — it contains "RELEVANT" as a substring.
    if "IRRELEVANT" in upper:
        return False
    if "RELEVANT" in upper:
        return True
    return None


def _chat(
    provider: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    timeout: Optional[float] = None,
    fast: bool = False,
) -> Optional[str]:
    """Minimal chat completion across providers; returns text or None on error.

    `fast` picks the cheaper model tier for the claude backend (judge-style
    calls); HTTP providers ignore it.
    """
    if provider == "claude":
        from . import claude_cli

        # A cold `claude -p` start alone takes a few seconds, so short HTTP-style
        # timeouts (the 8s judge default) don't apply; use the CLI default.
        out = claude_cli.run(
            system, user, fast=fast, timeout=timeout if timeout and timeout >= 60 else None
        )
        return out[0] if out else None
    try:
        from openai import OpenAI as _OpenAI
    except ImportError as exc:
        _log_failure(f"{provider} import", exc)
        return None

    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    else:
        cfg = OPENAI_COMPAT_PROVIDERS[provider]
        key = provider_api_key(provider)
        base_url = os.environ.get(cfg["base_url_env"], "").strip() or cfg["default_base_url"]
        model = os.environ.get(cfg["model_env"], cfg["default_model"])
    if not key:
        return None

    try:
        kwargs: dict = {"api_key": key, "base_url": base_url}
        if timeout:
            kwargs["timeout"] = timeout
        client = _OpenAI(**kwargs)
        if provider == "openai":
            try:
                resp = client.responses.create(
                    model=model,
                    instructions=system,
                    input=user,
                    max_output_tokens=max(16, max_tokens),
                )
                return (resp.output_text or "").strip()
            except Exception as exc:
                _log_failure("openai responses judge", exc)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        _log_failure(f"{provider} judge request", exc)
        return None


def judge_relevance(
    goal: str,
    user_msg: str,
    assistant_msg: str,
    *,
    strictness: str = "strict",
    timeout: Optional[float] = None,
) -> Optional[bool]:
    """Judge whether a turn is relevant to `goal`.

    Returns True/False, or None when there is no goal, no usable content, no
    configured provider, or the response can't be parsed — the caller treats
    None as "store" (fail open) so memory is never lost on an LLM hiccup.
    """
    goal = (goal or "").strip()
    if not goal:
        return None
    user_msg = (user_msg or "").strip()
    assistant_msg = (assistant_msg or "").strip()
    if not user_msg and not assistant_msg:
        return None

    system = _judge_system(strictness)
    body = (
        f"GOAL:\n{goal}\n\n"
        f"CONVERSATION TURN:\nUser:\n{user_msg[:4000]}\n\n"
        f"Assistant:\n{assistant_msg[:6000]}\n\n"
        "Answer with exactly one word: RELEVANT or IRRELEVANT."
    )
    for provider in provider_order():
        if provider_api_key(provider):
            # Reasoning models (e.g. deepseek-reasoner) spend the budget on hidden
            # reasoning before emitting the verdict, so the ceiling must be generous
            # or `content` comes back empty (finish_reason=length). This is only a
            # ceiling — non-reasoning models still stop after one word, so it does
            # not raise their cost.
            return _parse_verdict(
                _chat(provider, system, body, max_tokens=1024, timeout=timeout, fast=True)
            )
    return None


def spawn_background(data_dir: str, turn_id: str, *, judge: bool = False) -> None:
    """Detach a child process to summarize a turn without blocking the hook.

    With `judge=True` the child first runs the goal-relevance judge (used when
    the judge backend is too slow for the hook's hot path, e.g. `claude -p`)
    and soft-deletes the turn if it is judged irrelevant.
    """
    if not is_enabled() and not judge:
        return
    args = [sys.executable, "-m", "lynx_memory.summarizer", data_dir, turn_id]
    if judge:
        args.append("--judge")
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass


def _run_one(data_dir: str, turn_id: str, judge: bool = False) -> int:
    """Summarize (and optionally goal-judge) one stored turn.

    The store is opened only around the read and the write — never across the
    LLM call. On a synced store an open Memory holds the cross-process replica
    lock, and a slow provider call used to starve the next hooks into the
    local-sqlite fallback (writes that never reached the synced replica).
    """
    from .config import load_env
    from .storage import Memory

    ddir = Path(data_dir)
    load_env(ddir)  # project .env first, global .env fills shared keys
    mem = Memory(data_dir=ddir)
    try:
        t = mem.get_turn(turn_id)
        goal = (mem.get_goal() or {}).get("text")
    finally:
        mem.close()
    if t is None:
        return 1

    if judge and goal:
        from . import goals as _goals

        verdict = judge_relevance(
            goal,
            t["user_msg"],
            t["assistant_msg"],
            strictness=_goals.strictness(),
            timeout=None,
        )
        if verdict is False:
            mem = Memory(data_dir=ddir)
            try:
                # only if the row wasn't re-ingested with newer content meanwhile
                deleted = mem.forget_turn_if_unchanged(turn_id, t["ts"])
            finally:
                mem.close()
            try:
                from .hooks._log import log

                log(f"[goals] turn {turn_id} judged IRRELEVANT (deferred) — "
                    + ("soft-deleted" if deleted else "kept: row changed since judging"))
            except Exception:
                pass
            return 0

    if t.get("summary") or not is_enabled():
        return 0
    result = summarize_with_source(t["user_msg"], t["assistant_msg"], goal=goal)
    if not result:
        return 2
    summary, source, used_model = result
    mem = Memory(data_dir=ddir)
    try:
        mem.set_summary(turn_id, summary, source=source, model=used_model)
    finally:
        mem.close()
    return 0


def main() -> None:
    args = sys.argv[1:]
    judge = "--judge" in args
    args = [a for a in args if a != "--judge"]
    if len(args) != 2:
        sys.exit(64)
    sys.exit(_run_one(args[0], args[1], judge=judge))


if __name__ == "__main__":
    main()
