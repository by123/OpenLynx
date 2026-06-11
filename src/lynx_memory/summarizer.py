"""Turn summarizer.

Generates a compact summary for a single (user, assistant) turn so memory
recall can inject summaries into context instead of full prose.

Backend selection (SUMMARY_BACKEND):
  openai   → OpenAI SDK    (requires OPENAI_API_KEY)
  deepseek → DeepSeek API  (requires DEEPSEEK_API_KEY)
  qwen     → Qwen / DashScope API (requires QWEN_API_KEY or DASHSCOPE_API_KEY)
  auto     → first provider with an API key set, in order
             openai → deepseek → qwen
             (default when SUMMARY_BACKEND is unset)

Env vars:
  - SUMMARY_ENABLED=1          set "0"/"false" to disable
  - SUMMARY_BACKEND            openai | deepseek | qwen | auto  (default: auto)
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
}


def provider_api_key(provider: str) -> str:
    for env in _PROVIDER_KEY_ENVS.get(provider, ()):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    return ""

_SYSTEM = """You are an AI memory retrieval assistant. Extract memories worth preserving long-term from the following conversation.

Your goal is not to summarize everything, but to determine which information will still be valuable to the user in the future.

Please adhere to the following rules:

1. Only extract information that is useful in the long term.
2. Do not save temporary states, one-off questions, or small talk with no long-term value.
3. Do not save sensitive personal information unless explicitly requested by the user.
4. Do not fabricate content that did not appear in the conversation.
5. Each memory must be concise, clear, and retrievable in the future.
6. If the information is only short-term task progress, mark it as temporary.
7. If the information is user preferences, long-term rules, project background, technology stack, or business decisions, mark it as long_term.

Write the output in the SAME LANGUAGE as the original turn (do not translate).
Start with one sentence stating the user's request and final outcome/action.
Then include 2-5 short bullets. Prefix each bullet with [long_term] or [temporary].
Preserve concrete details when useful for retrieval: file paths, function/variable names, commands, numeric thresholds, reasons for decisions.
Do not repeat long sentences verbatim, no pleasantries, no extra headings; keep the total under ~400 characters (or ~120 English words).
Output the memory summary body directly, with no surrounding explanation."""


def is_enabled() -> bool:
    v = os.environ.get("SUMMARY_ENABLED", "1").strip().lower()
    return v not in ("0", "false", "off", "no", "")


def _backend() -> str:
    return os.environ.get("SUMMARY_BACKEND", "auto").strip().lower()


SummaryResult = Tuple[str, str, Optional[str]]  # (summary, source, model)
_LAST_ERROR = ""


def _conversation_body(user_msg: str, assistant_msg: str) -> str:
    return f"User:\n{user_msg[:6000]}\n\n---\n\nAssistant:\n{assistant_msg[:10000]}"


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


def _summarize_via_openai(user_msg: str, assistant_msg: str) -> Optional[SummaryResult]:
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
    content = _conversation_body(user_msg, assistant_msg)
    try:
        kwargs: dict = {"api_key": key, "base_url": base_url}
        client = _OpenAI(**kwargs)
        # Newer models (gpt-5.x series) use the Responses API; older models use Chat Completions.
        # Try Responses API first, fall back to Chat Completions on failure.
        try:
            resp = client.responses.create(
                model=model,
                instructions=_SYSTEM,
                input=content,
                max_output_tokens=600,
            )
            text = (resp.output_text or "").strip()
        except Exception as exc:
            _log_failure("openai responses request", exc)
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": content},
                ],
                max_tokens=600,
            )
            text = (resp.choices[0].message.content or "").strip()
        return (text, "openai", model) if text else None
    except Exception as exc:
        _log_failure("openai request", exc)
        return None


def _summarize_via_compat(provider: str, user_msg: str, assistant_msg: str) -> Optional[SummaryResult]:
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
    content = _conversation_body(user_msg, assistant_msg)
    try:
        client = _OpenAI(api_key=key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": content},
            ],
            max_tokens=600,
        )
        text = (resp.choices[0].message.content or "").strip()
        return (text, provider, model) if text else None
    except Exception as exc:
        _log_failure(f"{provider} request", exc)
        return None


def _summarize_via_deepseek(user_msg: str, assistant_msg: str) -> Optional[SummaryResult]:
    return _summarize_via_compat("deepseek", user_msg, assistant_msg)


def _summarize_via_qwen(user_msg: str, assistant_msg: str) -> Optional[SummaryResult]:
    return _summarize_via_compat("qwen", user_msg, assistant_msg)


def _call_provider(provider: str, user_msg: str, assistant_msg: str) -> Optional[SummaryResult]:
    if provider == "openai":
        return _summarize_via_openai(user_msg, assistant_msg)
    if provider == "deepseek":
        return _summarize_via_deepseek(user_msg, assistant_msg)
    if provider == "qwen":
        return _summarize_via_qwen(user_msg, assistant_msg)
    return None


def provider_order() -> list:
    """Provider try-order: the forced SUMMARY_BACKEND first, then the rest."""
    order = list(_PROVIDER_ORDER)
    preferred = _BACKEND_ALIASES.get(_backend())
    if preferred:
        order.remove(preferred)
        order.insert(0, preferred)
    return order


def summarize_with_source(user_msg: str, assistant_msg: str) -> Optional[SummaryResult]:
    """Return (summary, source, model), or None if disabled / not configured.

    Backend is selected by SUMMARY_BACKEND (openai | deepseek | qwen | auto).
    The forced backend is tried first; otherwise the first provider with an
    API key set wins (openai → deepseek → qwen).
    """
    if not is_enabled():
        return None
    user_msg = (user_msg or "").strip()
    assistant_msg = (assistant_msg or "").strip()
    if not user_msg or not assistant_msg:
        return None

    for provider in provider_order():
        if provider_api_key(provider):
            return _call_provider(provider, user_msg, assistant_msg)
    return None


def summarize(user_msg: str, assistant_msg: str) -> Optional[str]:
    result = summarize_with_source(user_msg, assistant_msg)
    return result[0] if result else None


def spawn_background(data_dir: str, turn_id: str) -> None:
    """Detach a child process to summarize a turn without blocking the hook."""
    if not is_enabled():
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "lynx_memory.summarizer", data_dir, turn_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass


def _run_one(data_dir: str, turn_id: str) -> int:
    from .config import GLOBAL_DATA_DIR, load_env
    from .storage import Memory

    ddir = Path(data_dir)
    load_env(GLOBAL_DATA_DIR)
    mem = Memory(data_dir=ddir)
    try:
        t = mem.get_turn(turn_id)
        if t is None:
            return 1
        if t.get("summary"):
            return 0  # already summarized
        result = summarize_with_source(t["user_msg"], t["assistant_msg"])
        if not result:
            return 2
        summary, source, used_model = result
        mem.set_summary(turn_id, summary, source=source, model=used_model)
    finally:
        mem.close()
    return 0


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(64)
    sys.exit(_run_one(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
