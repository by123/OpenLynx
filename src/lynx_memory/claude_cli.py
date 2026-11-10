"""Headless Claude Code (`claude -p`) as an LLM backend.

Used for every summarization layer when `SUMMARY_BACKEND=claude` (per-turn
summaries, the goal judge, session work records, day logs, project cards).
Each call is one short-lived `claude -p` process:

  claude -p --model <m> --system-prompt <sys> --tools "" \
      --no-session-persistence --strict-mcp-config \
      --settings '{"disableAllHooks":true}' --output-format json   < user text

Isolation matters: a summarization call must never be recorded as a new
OpenLynx memory (that is how batch `claude -p` jobs polluted the global store),
so the child gets `LYNX_MEMORY_NO_HOOK=1` *and* `disableAllHooks`, no tools,
no MCP servers, no session file, and a neutral cwd (no project CLAUDE.md).

Auth is whatever the local `claude` uses (keychain OAuth, or
CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` for headless/launchd runs,
or ANTHROPIC_API_KEY). `--bare` is deliberately not used: it ignores OAuth.

Env vars:
  - CLAUDE_BIN          path to the `claude` executable (default: PATH lookup)
  - CLAUDE_MODEL        model for work records / day logs / cards (default: sonnet)
  - CLAUDE_FAST_MODEL   model for per-turn summaries + goal judge (default: haiku)
  - CLAUDE_TIMEOUT      seconds per call (default: 300)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

DEFAULT_MODEL = "sonnet"
DEFAULT_FAST_MODEL = "haiku"
DEFAULT_TIMEOUT = 300.0

# Variables that tie a process to the *parent* interactive Claude Code session.
# A detached summarizer must run as an independent headless session.
_PARENT_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SSE_PORT",
)

_NO_HOOKS_SETTINGS = json.dumps({"disableAllHooks": True})

_LAST_ERROR = ""


def last_error() -> str:
    return _LAST_ERROR


def claude_bin() -> str:
    """Resolve the `claude` executable, or "" when it isn't installed."""
    forced = os.environ.get("CLAUDE_BIN", "").strip()
    if forced:
        return forced if Path(forced).exists() else ""
    found = shutil.which("claude")
    if found:
        return found
    # launchd/cron run with a bare PATH — check the usual install locations.
    for cand in (
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if cand.exists():
            return str(cand)
    return ""


def available() -> bool:
    return bool(claude_bin())


def model_for(fast: bool = False) -> str:
    if fast:
        return os.environ.get("CLAUDE_FAST_MODEL", "").strip() or DEFAULT_FAST_MODEL
    return os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL


def _timeout(timeout: Optional[float]) -> float:
    if timeout:
        return float(timeout)
    raw = os.environ.get("CLAUDE_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _PARENT_SESSION_ENV}
    env["LYNX_MEMORY_NO_HOOK"] = "1"
    return env


def build_command(system: str, model: str) -> list:
    return [
        claude_bin(),
        "-p",
        "--model", model,
        "--system-prompt", system,
        "--tools", "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--settings", _NO_HOOKS_SETTINGS,
        "--output-format", "json",
    ]


def _fail(msg: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = msg
    try:
        from .hooks._log import log

        log(f"[claude_cli] {msg}")
    except Exception:
        pass


def run(
    system: str,
    user: str,
    *,
    model: Optional[str] = None,
    fast: bool = False,
    timeout: Optional[float] = None,
) -> Optional[Tuple[str, str]]:
    """Run one headless completion. Returns (text, model) or None on failure."""
    exe = claude_bin()
    if not exe:
        _fail("claude executable not found (set CLAUDE_BIN)")
        return None
    model = model or model_for(fast)
    cmd = build_command(system, model)
    try:
        # Feed the prompt from a file, not a pipe: `claude -p` gives up on
        # stdin after ~3s, and under heavy parallel load a pipe writer thread
        # can be scheduled too late ("no stdin data received in 3s").
        with tempfile.TemporaryFile("w+", encoding="utf-8") as fin:
            fin.write(user)
            fin.flush()
            fin.seek(0)
            proc = subprocess.run(
                cmd,
                stdin=fin,
                capture_output=True,
                text=True,
                timeout=_timeout(timeout),
                cwd=tempfile.gettempdir(),
                env=child_env(),
            )
    except subprocess.TimeoutExpired:
        _fail(f"timeout after {_timeout(timeout):.0f}s (model={model})")
        return None
    except Exception as exc:
        _fail(f"spawn failed: {type(exc).__name__}: {exc}")
        return None

    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out[out.find("{"):]) if "{" in out else {}
    except ValueError:
        data = {}
    if proc.returncode != 0 or not data or data.get("is_error"):
        detail = data.get("result") if data else (proc.stderr or out)[:300]
        _fail(f"exit={proc.returncode} model={model}: {detail}")
        return None
    text = (data.get("result") or "").strip()
    if not text:
        _fail(f"empty result (model={model}, subtype={data.get('subtype')})")
        return None
    used = model
    usage = data.get("modelUsage")
    if isinstance(usage, dict) and usage:
        used = next(iter(usage.keys()))
    return text, used
