"""UserPromptSubmit hook — inject top-K relevant memories into context.

Claude Code and Codex CLI both pass hook input on stdin as JSON, with the
exact same field names (session_id, cwd, transcript_path, prompt). They
differ in how to inject context:

  - Claude Code accepts plain stdout text (it gets appended to the prompt).
  - Codex CLI requires a JSON envelope:
      {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                              "additionalContext": "<text>"}}

We pick the format from `--target {claude_code,codex}` (default claude_code).
We fail silently on any error.
"""
import json
import os
import sys
import traceback

import logging

from ._log import log


def _setup_file_logging() -> None:
    from ..config import LOG_PATH
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(LOG_PATH), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s | %(message)s"))
    logging.getLogger("lynx_memory").addHandler(handler)
    logging.getLogger("lynx_memory").setLevel(logging.INFO)


def _parse_target() -> str:
    for a in sys.argv[1:]:
        if a.startswith("--target="):
            return a.split("=", 1)[1]
    if "--target" in sys.argv:
        i = sys.argv.index("--target")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return os.environ.get("LYNX_MEMORY_TARGET", "claude_code")


def _main() -> int:
    if os.environ.get("LYNX_MEMORY_NO_HOOK"):
        return 0
    _setup_file_logging()
    target = _parse_target()
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = (data.get("prompt") or "").strip()
    session_id = data.get("session_id") or "unknown"
    cwd = data.get("cwd") or ""
    transcript_path = data.get("transcript_path") or ""

    # Safety-net persistence for the previous turn: Stop hook is the primary
    # writer, but it can fire before the assistant's prose is flushed. By the
    # time the user submits the next prompt the transcript is definitely
    # complete, so we re-persist (loose: tool-only counts). The state file in
    # persist_last_turn dedups against Stop's write — if it already saved this
    # turn, we exit before opening the DB.
    try:
        from ..transcript import persist_last_turn
        persist_last_turn(transcript_path, session_id, cwd, require_prose=False)
    except Exception as e:
        log(f"[on_prompt] persist ERROR: {e}\n{traceback.format_exc()}")

    if not prompt or len(prompt) < 4:
        return 0
    try:
        from ..textclean import is_noise_prompt

        if is_noise_prompt(prompt):
            # e.g. a bare <task-notification> from a finished subagent — not a
            # user question; recall would only inject unrelated context.
            log("[on_prompt] prompt is harness noise only, skipping recall")
            return 0
    except Exception:
        pass

    brief = _session_brief(session_id, cwd)

    try:
        from ..config import load_env, resolve_data_dir
        from ..storage import search_scoped
        load_env(resolve_data_dir(cwd))

        top_k = int(os.environ.get("TOP_K", 5))
        min_score = float(os.environ.get("MIN_SCORE", 0.7))
        scope = os.environ.get("LYNX_MEMORY_SCOPE", "auto")
        embedding_backend = os.environ.get("EMBEDDING_BACKEND", "voyage")
        log(f"[on_prompt] search start | scope={scope} top_k={top_k} min_score={min_score} "
            f"embedding_backend={embedding_backend} prompt={prompt[:80]!r}")
        results = search_scoped(
            prompt, cwd=cwd, scope=scope, top_k=top_k, min_score=min_score
        )
        score_str = ", ".join(f"{r['score']:.3f}" for r in results)
        log(f"[on_prompt] search done | results={len(results)} scores=[{score_str}]")
        print(
            f"[lynx-memory] {len(results)} hit(s)"
            + (f": [{score_str}]" if results else ""),
            file=sys.stderr,
        )
    except Exception as e:
        log(f"[on_prompt] ERROR: {e}\n{traceback.format_exc()}")
        if brief:
            _emit(brief, target)
        return 0

    if not results:
        log("[on_prompt] no results above min_score, skipping injection")
        if brief:
            _emit(brief, target)
        return 0

    try:
        from ..config import GLOBAL_DATA_DIR, find_project_root
        from ..storage import Memory

        proj_dir = find_project_root(cwd) if cwd else None
        any_project_hit = any(r.get("scope") == "project" for r in results)
        target_dir = proj_dir if (proj_dir is not None and any_project_hit) else GLOBAL_DATA_DIR
        scope_used = "project" if target_dir == proj_dir else "global"

        m = Memory(data_dir=target_dir)
        try:
            m.record_retrieval(
                prompt=prompt,
                hits=results,
                session_id=session_id,
                cwd=cwd,
                scope_used=scope_used,
            )
        finally:
            m.close()
    except Exception as e:
        log(f"[on_prompt] record_retrieval ERROR: {e}\n{traceback.format_exc()}")

    lines = ["<memory>", "Relevant context from prior conversations (auto-retrieved):"]
    for r in results:
        score = f"{r['score']:.2f}"
        kind = r["kind"]
        sc = r.get("scope", "")
        summary = r.get("summary")
        if summary:
            tag = f"{kind}" + (f" · {sc}" if sc else "") + " · summary"
            text = summary
        else:
            tag = f"{kind}" + (f" · {sc}" if sc else "")
            text = r["text"]
        if len(text) > 1200:
            text = text[:1200] + "…"
        lines.append(f"\n[{tag} · score={score}]\n{text}")
    lines.append("</memory>")
    body = "\n".join(lines)
    if brief:
        body = brief + "\n\n" + body
    _emit(body, target)
    return 0


def _emit(body: str, target: str) -> None:
    if target == "codex":
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": body,
            }
        }))
    else:
        sys.stdout.write(body + "\n")


def _session_brief(session_id: str, cwd: str):
    """Project card from the work journal, injected once per session (on its
    first real prompt). Reads exported files only — no DB, no lock.
    Disable with WORKLOG_INJECT_BRIEF=0."""
    if not cwd or not session_id or session_id == "unknown":
        return None
    try:
        from ..config import load_env, resolve_data_dir

        load_env(resolve_data_dir(cwd))
        if os.environ.get("WORKLOG_INJECT_BRIEF", "1").strip().lower() in ("0", "false", "off", "no"):
            return None
        from ..worklog import brief_for_cwd, worklog_dir

        state_path = worklog_dir() / ".briefed.json"
        try:
            seen = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            seen = {}
        if session_id in seen:
            return None
        brief = brief_for_cwd(cwd)
        if not brief:
            return None
        import time as _t

        seen[session_id] = _t.time()
        if len(seen) > 500:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-400:])
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(seen), encoding="utf-8")
        os.replace(tmp, state_path)
        log(f"[on_prompt] injecting project brief ({len(brief)} chars)")
        return brief
    except Exception as e:
        log(f"[on_prompt] brief ERROR: {e}")
        return None


def main() -> None:
    sys.exit(_main())


if __name__ == "__main__":
    main()
