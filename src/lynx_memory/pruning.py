"""Prune turns the summarizer judges to carry no long-term value — reversibly.

With PRUNE_NO_VALUE=1 the note prompt lets the model answer exactly
`NO_VALUE` (greetings, small talk, empty/aborted exchanges, pure harness text,
templated batch jobs with no decision or finding). Such a turn is soft-deleted
(row kept with `deleted_at`, vector dropped) and recorded in
`~/.openlynx/worklog/pruned.jsonl`, so `lynx-memory restore-pruned` can bring
it back (undelete + re-embed).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

NO_VALUE = "NO_VALUE"

PRUNE_CLAUSE = (
    "\n\nPRUNING: if this turn holds nothing worth remembering long-term — greetings or "
    "small talk, an empty, aborted or failed exchange with nothing learned, only "
    "harness/system text, or a templated batch job whose input/output carries no "
    "decision, finding or reusable fact — output exactly NO_VALUE and nothing else. "
    "When in doubt, write the note instead."
)

_lock = threading.Lock()


def prune_enabled() -> bool:
    return os.environ.get("PRUNE_NO_VALUE", "").strip().lower() in ("1", "true", "yes", "on")


def is_no_value(text: Optional[str]) -> bool:
    t = (text or "").strip().strip("`*\"' .").upper()
    return t == NO_VALUE or (t.startswith(NO_VALUE) and len(t) < 40)


def log_path() -> Path:
    from .worklog import worklog_dir

    return worklog_dir() / "pruned.jsonl"


def record(entries: Iterable[dict]) -> None:
    lines = [json.dumps({"pruned_at": time.time(), **e}, ensure_ascii=False) for e in entries]
    if not lines:
        return
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def load() -> List[dict]:
    p = log_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def restore(*, store: Optional[str] = None, since: Optional[float] = None, dry_run: bool = False,
            log: Callable[[str], None] = print) -> Dict[str, int]:
    """Undelete pruned turns (set-based, one commit per store) and re-embed them."""
    from .resummarize import _embed_paced, _open

    entries = load()
    chosen = [e for e in entries
              if (not store or store.lower() in e.get("store", "").lower())
              and (since is None or float(e.get("pruned_at") or 0) >= since)]
    by_store: Dict[str, List[str]] = {}
    for e in chosen:
        by_store.setdefault(e["store"], []).append(e["turn_id"])
    stats = {"selected": len(chosen), "restored": 0}
    if dry_run:
        for s, ids in by_store.items():
            log(f"  {s}: {len(ids)} turn(s)")
        return stats
    restored: set = set()
    for s, ids in by_store.items():
        d = Path(s)
        if not (d / "db").is_dir():
            log(f"  ! store missing: {s}")
            continue
        mem = _open(d)
        try:
            rows = []
            for i in range(0, len(ids), 800):
                ch = ids[i : i + 800]
                ph = ",".join("?" * len(ch))
                mem.db.execute(f"UPDATE turns SET deleted_at = NULL WHERE id IN ({ph})", ch)
                rows += [dict(r) for r in mem.db.execute(
                    f"SELECT id, session_id, ts, cwd, user_msg, assistant_msg FROM turns WHERE id IN ({ph})",
                    ch).fetchall()]
            mem.db.commit()
        finally:
            mem.close()
        docs = [f"User: {r['user_msg']}\n\nAssistant: {r['assistant_msg']}" for r in rows]
        vecs = _embed_paced(docs, log)
        ok = [(r, v, doc) for r, v, doc in zip(rows, vecs, docs) if v is not None]
        if ok:
            mem = _open(d)
            try:
                mem.turns.upsert(
                    ids=[r["id"] for r, _, _ in ok], embeddings=[v for _, v, _ in ok],
                    documents=[doc for _, _, doc in ok],
                    metadatas=[{"session_id": r["session_id"], "ts": r["ts"], "cwd": r["cwd"] or ""}
                               for r, _, _ in ok],
                )
                mem.db.commit()
            finally:
                mem.close()
        restored |= {r["id"] for r in rows}
        stats["restored"] += len(rows)
        log(f"  {s}: restored {len(rows)} turn(s), re-embedded {len(ok)}")
    keep = [e for e in entries if e.get("turn_id") not in restored]
    p = log_path()
    with _lock:
        if keep:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in keep) + "\n", encoding="utf-8")
            tmp.replace(p)
        elif p.exists():
            p.unlink()
    return stats
