"""Per-scope memory goals + LLM relevance gating for turn persistence.

A goal is an optional, per-store (project or global) statement of what the
user is trying to accomplish. When set:

  - the persistence hooks drop turns the LLM judges irrelevant to the goal
    (see `evaluate_turn_relevance`), so off-topic chatter never enters the DB;
  - per-turn and per-session summaries are steered toward the goal.

When no goal is set (or no summarization LLM key is configured), memory
behaves exactly as before — every turn is stored and summarized normally.

Env vars:
  - GOAL_GATING_ENABLED   "1" (default) / "0" — master switch for the gate
  - GOAL_STRICTNESS       loose | balanced | strict (default: strict)
  - GOAL_JUDGE_TIMEOUT    seconds for the judge LLM call (default: 8; 0 = none)
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from .config import paths_for

DEFAULT_STRICTNESS = "strict"
DEFAULT_JUDGE_TIMEOUT = 8.0


def gating_enabled() -> bool:
    v = os.environ.get("GOAL_GATING_ENABLED", "1").strip().lower()
    return v not in ("0", "false", "off", "no", "")


def strictness() -> str:
    return os.environ.get("GOAL_STRICTNESS", DEFAULT_STRICTNESS).strip().lower() or DEFAULT_STRICTNESS


def judge_timeout() -> Optional[float]:
    raw = os.environ.get("GOAL_JUDGE_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_JUDGE_TIMEOUT
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_JUDGE_TIMEOUT
    return val if val > 0 else None


def get_goal_text(data_dir: Path) -> Optional[str]:
    """Read the goal text for a store directly from SQLite (no Chroma).

    Returns None if the store, table, or goal row is absent — keeping this
    cheap on the hook hot path, which opens its own Chroma-backed Memory only
    after a turn survives the gate.
    """
    db_path = paths_for(data_dir)["db_path"]
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(db_path)
        try:
            row = con.execute("SELECT text FROM goals WHERE id = 1").fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    text = (row[0] or "").strip()
    return text or None


def evaluate_turn_relevance(data_dir: Path, user_msg: str, assistant_msg: str) -> str:
    """Decide whether a turn should be persisted: returns "store" or "drop".

    Fails open ("store") whenever there is no goal, gating is disabled, no LLM
    is configured, or the judge errors out — memory is never lost silently on
    an API hiccup. Only an explicit IRRELEVANT verdict drops the turn.
    """
    from .hooks._log import log

    if not gating_enabled():
        return "store"
    goal = get_goal_text(data_dir)
    if not goal:
        return "store"

    # Load provider keys for this scope (project .env overrides, global .env
    # fills in shared keys) before calling the judge.
    try:
        from .config import load_env

        load_env(data_dir)
    except Exception:
        pass

    try:
        from .summarizer import judge_relevance

        verdict = judge_relevance(
            goal,
            user_msg,
            assistant_msg,
            strictness=strictness(),
            timeout=judge_timeout(),
        )
    except Exception as exc:  # never let the gate break persistence
        log(f"[goals] relevance judge raised ({exc}); storing turn")
        return "store"

    if verdict is False:
        log(f"[goals] turn judged IRRELEVANT to goal — dropping. user={user_msg[:120]!r}")
        return "drop"
    if verdict is None:
        log("[goals] relevance undecided (no LLM key / unparseable) — storing turn")
    return "store"
