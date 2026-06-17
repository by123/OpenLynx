"""Daily digest: summarize a store's turns for a day, optionally push a notice.

Backs `lynx-memory daily`. Reuses the summarizer provider plumbing to turn the
day's turns (preferring each turn's existing summary) into a short "what did I
do today" recap, steered by the store's goal when one is set.

Notifier backends (auto-detected, or forced via DAILY_NOTIFY_BACKEND):
  serverchan  → WeChat via ServerChan (Server酱); needs SERVERCHAN_SENDKEY
  webhook     → generic JSON POST {"title","body"} to DAILY_WEBHOOK_URL
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

_SYSTEM = (
    "你是一个开发工作日志助手。下面是用户今天与编码助手的对话记忆（可能已是逐条摘要）。"
    "请用中文写一份简洁的『今天我做了什么』日报，要求：\n"
    "1. 开头一句话总览今天的主线。\n"
    "2. 然后用 3-8 条要点列出具体完成的事（功能、修复、决策、发布等），保留关键的文件/命令/版本号。\n"
    "3. 如有未完成或明天要继续的，单列『待办』。\n"
    "4. 不要寒暄、不要编造对话里没有的内容，控制在 ~300 字。"
)


def _local_midnight() -> float:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def collect_turns(data_dir: Path, since_hours: Optional[float]) -> Tuple[List[dict], Optional[str]]:
    """Return (turns since the window start, goal text) for a store."""
    from .storage import Memory

    cutoff = (time.time() - since_hours * 3600) if since_hours else _local_midnight()
    m = Memory(data_dir=data_dir)
    try:
        rows = m.db.execute(
            "SELECT ts, user_msg, assistant_msg, summary FROM turns "
            "WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
        turns = [dict(r) for r in rows]
        goal = (m.get_goal() or {}).get("text")
    finally:
        m.close()
    return turns, goal


def _build_body(turns: List[dict]) -> str:
    parts = []
    for t in turns:
        if t.get("summary"):
            parts.append((t["summary"] or "").strip())
        else:
            u = (t["user_msg"] or "").strip()[:400]
            a = (t["assistant_msg"] or "").strip()[:600]
            parts.append(f"用户：{u}\n助手：{a}")
    return "\n\n---\n\n".join(p for p in parts if p)[:40000]


# Dirs we never descend into while hunting for project stores — big/noisy and
# never hold a .lynx-memory marker in practice. Keeps the home scan fast.
_PRUNE_DIRS = {
    "node_modules", ".git", ".hg", ".svn", "Library", ".Trash", "__pycache__",
    ".cache", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    ".npm", ".cargo", ".rustup", "Pictures", "Movies", "Music", "Applications",
    ".gradle", ".m2", "go", "Pods", "DerivedData", ".next", "target",
}

_SYSTEM_GLOBAL = (
    "你是一个开发工作日志助手。下面是用户今天在【多个项目】里与编码助手的对话记忆，"
    "已按项目分组（可能已是逐条摘要）。请用中文写一份跨项目的『今天我做了什么』日报：\n"
    "1. 开头一句话总览今天整体在忙什么、涉及哪些项目。\n"
    "2. 然后【按项目分小节】，每个项目用 2-5 条要点列出具体进展，保留关键文件/命令/版本号。\n"
    "3. 如有未完成或明天要继续的，单列『待办』。\n"
    "4. 不要寒暄、不要编造对话里没有的内容，整体控制在 ~400 字。"
)


def discover_stores(roots: Optional[List[Path]] = None, max_depth: int = 6) -> List[Path]:
    """Find every memory store on the machine: the global store + each project's
    .lynx-memory/ (with a populated db). Scans `roots` (default: $HOME, or the
    colon-separated env LYNX_SCAN_ROOTS), pruning big/noisy directories and not
    descending past `max_depth` levels below each root (projects live shallow;
    this keeps the nightly scan fast). Override depth via env LYNX_SCAN_DEPTH.
    """
    from .config import GLOBAL_DATA_DIR, PROJECT_MARKER

    found: List[Path] = []
    seen = set()

    if (GLOBAL_DATA_DIR / "db" / "memory.db").exists():
        found.append(GLOBAL_DATA_DIR)
        seen.add(GLOBAL_DATA_DIR.resolve())

    if roots is None:
        env = os.environ.get("LYNX_SCAN_ROOTS", "").strip()
        roots = [Path(p) for p in env.split(":") if p] if env else [Path.home()]

    env_depth = os.environ.get("LYNX_SCAN_DEPTH", "").strip()
    if env_depth.isdigit():
        max_depth = int(env_depth)

    for root in roots:
        if not root.exists():
            continue
        base = len(root.resolve().parts)
        for dirpath, dirnames, _files in os.walk(root, onerror=lambda e: None):
            if PROJECT_MARKER in dirnames:
                cand = Path(dirpath) / PROJECT_MARKER
                if (cand / "db" / "memory.db").exists():
                    rp = cand.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        found.append(cand)
                dirnames.remove(PROJECT_MARKER)  # never descend into a store
            dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
            if len(Path(dirpath).resolve().parts) - base >= max_depth:
                dirnames[:] = []  # reached depth limit; stop descending
    return found


def _store_label(data_dir: Path) -> str:
    from .config import GLOBAL_DATA_DIR

    if data_dir.resolve() == GLOBAL_DATA_DIR.resolve():
        return "全局"
    # .../<project>/.lynx-memory -> <project>
    return data_dir.parent.name or str(data_dir)


def build_global_digest(since_hours: Optional[float] = None) -> Tuple[str, int, int]:
    """Aggregate today's turns across ALL stores on the machine into one recap.

    Returns (digest_text, total_turns, n_stores_with_turns).
    """
    from .summarizer import _chat, provider_api_key, provider_order

    sections = []
    total = 0
    n_stores = 0
    for d in discover_stores():
        turns, _goal = collect_turns(d, since_hours)
        if not turns:
            continue
        n_stores += 1
        total += len(turns)
        sections.append(f"## 项目：{_store_label(d)}\n{_build_body(turns)}")

    if total == 0:
        return "", 0, 0

    body = "\n\n".join(sections)[:60000]
    for provider in provider_order():
        if provider_api_key(provider):
            out = _chat(provider, _SYSTEM_GLOBAL, body, max_tokens=4000, timeout=120)
            if out:
                return out.strip(), total, n_stores
    return "", total, n_stores


def build_digest(data_dir: Path, since_hours: Optional[float] = None) -> Tuple[str, int, Optional[str]]:
    """Return (digest_text, n_turns, goal).

    digest_text is "" when there are no turns in the window, or when no
    summarization provider key is configured / the call fails.
    """
    turns, goal = collect_turns(data_dir, since_hours)
    if not turns:
        return "", 0, goal

    from .summarizer import _chat, provider_api_key, provider_order

    system = _SYSTEM
    if goal:
        system += f"\n\n用户为该项目设定的目标是：{goal}\n请优先突出与该目标相关的进展。"
    body = _build_body(turns)
    for provider in provider_order():
        if provider_api_key(provider):
            out = _chat(provider, system, body, max_tokens=4000, timeout=120)
            if out:
                return out.strip(), len(turns), goal
    return "", len(turns), goal


# --------------------------------------------------------------------- notify

def notify_backend() -> str:
    forced = os.environ.get("DAILY_NOTIFY_BACKEND", "").strip().lower()
    if forced:
        return forced
    if os.environ.get("SERVERCHAN_SENDKEY", "").strip():
        return "serverchan"
    if os.environ.get("DAILY_WEBHOOK_URL", "").strip():
        return "webhook"
    return ""


def _notify_serverchan(title: str, body: str) -> Tuple[bool, str]:
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        return False, "SERVERCHAN_SENDKEY not set"
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = urllib.parse.urlencode({"title": title[:32], "desp": body}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if resp.get("code") == 0:
        return True, "ok"
    return False, f"serverchan returned {resp}"


def _notify_webhook(title: str, body: str) -> Tuple[bool, str]:
    url = os.environ.get("DAILY_WEBHOOK_URL", "").strip()
    if not url:
        return False, "DAILY_WEBHOOK_URL not set"
    payload = json.dumps({"title": title, "body": body}).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, "ok"


def notify(title: str, body: str) -> Tuple[bool, str]:
    """Dispatch a push to the configured backend. Returns (ok, detail)."""
    backend = notify_backend()
    if backend == "serverchan":
        return _notify_serverchan(title, body)
    if backend == "webhook":
        return _notify_webhook(title, body)
    return False, "no notifier configured (set SERVERCHAN_SENDKEY or DAILY_WEBHOOK_URL)"
