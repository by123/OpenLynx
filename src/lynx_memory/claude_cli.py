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
  - CLAUDE_USAGE_CAP    stop calling Claude once the plan's usage window
                        reaches this utilization, e.g. 0.8 or 80% — until that
                        window resets.
  - CLAUDE_USAGE_CAP_WINDOWS  which windows the cap watches, comma-separated
                        (default: five_hour; e.g. "five_hour,seven_day"). Usage is read from each
                        call's `rate_limit_event` and shared across processes
                        via <global store>/db/claude_usage.json.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
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

# Set once a call is refused because the plan usage cap was reached; batch
# jobs check it to stop submitting work (results so far are kept).
CAP_HIT = threading.Event()
_usage_lock = threading.Lock()


def usage_cap() -> Optional[float]:
    raw = os.environ.get("CLAUDE_USAGE_CAP", "").strip().rstrip("%")
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if v > 1:
        v /= 100.0
    return v if 0 < v <= 1 else None


def usage_path() -> Path:
    from . import config

    return config.GLOBAL_DATA_DIR / "db" / "claude_usage.json"


def read_usage() -> dict:
    try:
        return json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_usage(info: dict) -> None:
    windows = info.get("unifiedWindows") or {}
    if not windows and info.get("rateLimitType"):
        windows = {info["rateLimitType"]: {"utilization": info.get("utilization"),
                                           "resetsAt": info.get("resetsAt")}}
    if not windows:
        return
    data = {"seen_at": time.time(), "windows": windows}
    p = usage_path()
    try:
        with _usage_lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, p)
    except OSError:
        pass


def capped() -> Optional[str]:
    """Why calls are currently refused (a window at/over the cap that has not
    reset yet), or None."""
    cap = usage_cap()
    if cap is None:
        return None
    watched = {w.strip() for w in os.environ.get("CLAUDE_USAGE_CAP_WINDOWS", "five_hour").split(",")
               if w.strip()}
    now = time.time()
    for name, w in (read_usage().get("windows") or {}).items():
        if name not in watched:
            continue
        try:
            util = float(w.get("utilization") or 0)
            reset = float(w.get("resetsAt") or 0)
        except (TypeError, ValueError):
            continue
        if util >= cap and reset > now:
            when = time.strftime("%m-%d %H:%M", time.localtime(reset))
            return f"Claude {name} usage {util:.0%} ≥ cap {cap:.0%} (resets {when})"
    return None


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
        # stream-json carries a rate_limit_event with plan utilization
        "--output-format", "stream-json", "--verbose",
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
    reason = capped()
    if reason:
        if not CAP_HIT.is_set():
            _fail(f"usage cap reached — {reason}; not calling Claude")
        CAP_HIT.set()
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
    data: dict = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "rate_limit_event":
            _record_usage(ev.get("rate_limit_info") or {})
        elif ev.get("type") == "result" or "result" in ev and "is_error" in ev:
            data = ev
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
