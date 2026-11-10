"""Rewrite every stored note with the active summarization backend.

`lynx-memory resummarize` re-runs the per-turn notes (turns.summary) and the
per-session notes (summaries) for every store on the machine, e.g. to move a
history written by an older/cheaper model onto Claude:

  - turns: every live turn whose note was not written by the target backend
    (resumable: already-rewritten turns are skipped unless --force)
  - sessions: every session with >= 2 live turns, rebuilt from the (new) turn
    notes + the end of each reply; the session's live note is updated in place
    (duplicates soft-deleted) and its vector re-embedded, paced for
    rate-limited embedding keys

LLM calls run in a thread pool (`--jobs`); the store is opened only to read
and to flush a batch (one transaction, one commit per batch — one remote push
on a synced store), never across an LLM call.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

FLUSH_EVERY = 100
MAX_CONSECUTIVE_FAILURES = 25


def _open(data_dir: Path):
    from .storage import Memory

    return Memory(data_dir=data_dir, strict_sync=True, lock_timeout=120)


def _state_path() -> Path:
    from .worklog import worklog_dir

    return worklog_dir() / ".resummarized-sessions.json"


def _load_done_sessions() -> Dict[str, List[str]]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_done_sessions(done: Dict[str, List[str]]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(done), encoding="utf-8")
    tmp.replace(p)


def _turn_body(user: str, a_head: str, a_tail: str, a_len: int) -> Tuple[str, str]:
    from .textclean import clean_user_text

    asst = a_head if a_len <= len(a_head) else f"{a_head}\n[…]\n{a_tail}"
    return clean_user_text(user or "")[:6000], asst


def _rewrite_turn(provider: str, model: Optional[str], goal: Optional[str],
                  user: str, asst: str) -> Optional[Tuple[str, str]]:
    from . import summarizer

    if not user or not asst:
        return None
    if provider == "claude":
        from . import claude_cli

        out = claude_cli.run(summarizer._system_for(goal),
                             summarizer._conversation_body(user, asst), model=model)
        return (out[0], out[1]) if out else None
    res = summarizer.summarize_with_source(user, asst, goal=goal)
    return (res[0], res[2] or provider) if res else None


def _flush_turn_notes(data_dir: Path, rows: List[Tuple[str, str, str]], source: str,
                      prune: Optional[List[dict]] = None) -> None:
    """One UPDATE … CASE statement per chunk (+ one set-based soft-delete for
    pruned turns), one commit for the whole batch."""
    prune = prune or []
    if not rows and not prune:
        return
    now = time.time()
    mem = _open(data_dir)
    try:
        if prune:
            ids = [p["turn_id"] for p in prune]
            for i in range(0, len(ids), 800):
                ch = ids[i : i + 800]
                mem.db.execute(
                    f"UPDATE turns SET deleted_at = ? WHERE id IN ({','.join('?' * len(ch))}) "
                    "AND deleted_at IS NULL", [now, *ch])
            try:
                mem.turns.delete(ids=ids)
            except Exception:
                pass
        per = 150  # 5 bound params per row, well under SQLite's limit
        for i in range(0, len(rows), per):
            chunk = rows[i : i + per]
            case_s = " ".join("WHEN ? THEN ?" for _ in chunk)
            params: list = []
            for tid, text, _m in chunk:
                params += [tid, text]
            for tid, _t, model in chunk:
                params += [tid, model]
            ids = [r[0] for r in chunk]
            params += [source, now, *ids]
            mem.db.execute(
                f"UPDATE turns SET summary = CASE id {case_s} END, "
                f"summary_model = CASE id {case_s} END, "
                f"summary_source = ?, summary_ts = ? "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                params,
            )
        mem.db.commit()
    finally:
        mem.close()
    if prune:
        from . import pruning

        pruning.record(prune)


def rewrite_turns(data_dir: Path, *, provider: str, model: Optional[str], jobs: int,
                  force: bool, limit: Optional[int], dry_run: bool,
                  log: Callable[[str], None]) -> Dict[str, int]:
    mem = _open(data_dir)
    try:
        goal = (mem.get_goal() or {}).get("text")
        where = "deleted_at IS NULL"
        params: list = []
        if not force:
            where += " AND (summary IS NULL OR summary_source IS NULL OR summary_source != ?)"
            params.append(provider)
        rows = mem.db.execute(
            "SELECT id, session_id, user_msg, substr(assistant_msg, 1, 3000) AS a_head, "
            "substr(assistant_msg, -7000) AS a_tail, length(assistant_msg) AS a_len "
            f"FROM turns WHERE {where} ORDER BY ts DESC",
            params,
        ).fetchall()
        total = mem.db.execute("SELECT COUNT(*) FROM turns WHERE deleted_at IS NULL").fetchone()[0]
    finally:
        mem.close()
    todo = [dict(r) for r in rows]
    if limit is not None:
        todo = todo[:limit]
    stats = {"total": int(total), "todo": len(todo), "done": 0, "failed": 0}
    log(f"  turns: {stats['total']} live, {stats['todo']} to rewrite")
    if dry_run or not todo:
        return stats

    from . import pruning

    prune_on = pruning.prune_enabled()
    by_id = {r["id"]: r for r in todo}
    store_key = str(data_dir.resolve())
    pending: List[Tuple[str, str, str]] = []
    pruned: List[dict] = []
    stats["pruned"] = 0
    lock = threading.Lock()
    last_flush = time.time()
    fail_streak = 0
    abort = threading.Event()

    def work(r: dict):
        if abort.is_set():
            return r["id"], None
        u, a = _turn_body(r["user_msg"], r["a_head"] or "", r["a_tail"] or "", int(r["a_len"] or 0))
        if not a.strip():
            return r["id"], "SKIP"
        if not u.strip():
            # e.g. a bare subagent <task-notification>: the reply may still
            # carry real work (a synthesis of the agent's findings)
            u = "[no user text — the turn was triggered by a harness/system message]"
        try:
            return r["id"], _rewrite_turn(provider, model, goal, u, a)
        except Exception:
            return r["id"], None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futs = [pool.submit(work, r) for r in todo]
        for n, fut in enumerate(as_completed(futs), 1):
            tid, res = fut.result()
            if res == "SKIP":
                stats["skipped"] = stats.get("skipped", 0) + 1
            elif res and prune_on and pruning.is_no_value(res[0]):
                fail_streak = 0
                stats["pruned"] += 1
                r0 = by_id[tid]
                pruned.append({"store": store_key, "turn_id": tid, "session_id": r0.get("session_id"),
                               "source": "resummarize", "user": (r0.get("user_msg") or "")[:200]})
            elif res:
                fail_streak = 0
                stats["done"] += 1
                with lock:
                    pending.append((tid, res[0], res[1]))
            elif not abort.is_set():
                stats["failed"] += 1
                fail_streak += 1
                if fail_streak >= MAX_CONSECUTIVE_FAILURES:
                    abort.set()
                    log(f"  ! {fail_streak} consecutive failures — stopping this store (re-run to resume)")
            if len(pending) + len(pruned) >= FLUSH_EVERY or (
                (pending or pruned) and time.time() - last_flush > 90
            ):
                batch, pending = pending, []
                pbatch, pruned = pruned, []
                _flush_turn_notes(data_dir, batch, provider, pbatch)
                last_flush = time.time()
            if n % 50 == 0 or n == len(todo):
                rate = n / max(1e-6, time.time() - t0) * 60
                log(f"  turns {n}/{len(todo)} ({stats['pruned']} pruned, {stats['failed']} failed, "
                    f"{rate:.0f}/min)")
    _flush_turn_notes(data_dir, pending, provider, pruned)
    return stats


# ------------------------------------------------------------------ sessions

def _embed_paced(texts: List[str], log: Callable[[str], None]) -> List[Optional[list]]:
    """Embed in small batches, backing off on rate limits (free Voyage keys
    allow ~3 requests / 10K tokens per minute). Failed items → None."""
    from .embeddings import embed

    out: List[Optional[list]] = [None] * len(texts)
    batch = 20
    i = 0
    while i < len(texts):
        chunk = texts[i : i + batch]
        for attempt in range(6):
            try:
                vecs = embed(chunk, input_type="document")
                for j, v in enumerate(vecs):
                    out[i + j] = v
                break
            except Exception as exc:
                msg = str(exc).lower()
                if "rate" in msg or "limit" in msg or "429" in msg:
                    time.sleep(25 * (attempt + 1))
                    continue
                log(f"  ! embedding failed: {type(exc).__name__}: {exc}")
                break
        i += batch
    return out


def rewrite_sessions(data_dir: Path, *, jobs: int, force: bool, limit: Optional[int],
                     dry_run: bool, done_state: Dict[str, List[str]],
                     log: Callable[[str], None]) -> Dict[str, int]:
    from .hooks.on_session_end import _summarize, build_conversation

    key = str(data_dir.resolve())
    done = set(done_state.get(key, []))
    mem = _open(data_dir)
    try:
        goal = (mem.get_goal() or {}).get("text")
        rows = mem.db.execute(
            "SELECT id, session_id, ts, user_msg, summary, "
            "substr(assistant_msg, -4000) AS assistant_msg FROM turns "
            "WHERE deleted_at IS NULL ORDER BY session_id, ts"
        ).fetchall()
        existing: Dict[str, List[Tuple[str, float]]] = {}
        for r in mem.db.execute(
            "SELECT id, session_id, ts FROM summaries WHERE deleted_at IS NULL ORDER BY ts"
        ).fetchall():
            existing.setdefault(r["session_id"], []).append((r["id"], float(r["ts"])))
    finally:
        mem.close()
    sessions: Dict[str, List[dict]] = {}
    for r in rows:
        sessions.setdefault(r["session_id"], []).append(dict(r))
    todo = [(sid, ts) for sid, ts in sessions.items() if len(ts) >= 2 and (force or sid not in done)]
    todo.sort(key=lambda x: -x[1][-1]["ts"])
    if limit is not None:
        todo = todo[:limit]
    stats = {"total": sum(1 for t in sessions.values() if len(t) >= 2), "todo": len(todo),
             "done": 0, "failed": 0}
    log(f"  sessions: {stats['total']} with >= 2 turns, {stats['todo']} to rewrite")
    if dry_run or not todo:
        return stats

    results: List[Tuple[str, str, int]] = []
    fail_streak = 0
    abort = threading.Event()

    def work(item):
        sid, turns = item
        if abort.is_set():
            return sid, None, len(turns)
        try:
            return sid, _summarize(build_conversation(turns), goal), len(turns)
        except Exception:
            return sid, None, len(turns)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futs = [pool.submit(work, it) for it in todo]
        for n, fut in enumerate(as_completed(futs), 1):
            sid, text, nturns = fut.result()
            if text:
                fail_streak = 0
                results.append((sid, text, nturns))
            elif not abort.is_set():
                stats["failed"] += 1
                fail_streak += 1
                if fail_streak >= MAX_CONSECUTIVE_FAILURES:
                    abort.set()
                    log(f"  ! {fail_streak} consecutive failures — stopping this store (re-run to resume)")
            if n % 25 == 0 or n == len(todo):
                log(f"  sessions {n}/{len(todo)} ({stats['failed']} failed)")
    if not results:
        return stats

    # one transaction: update each session's newest live note in place,
    # soft-delete duplicates, insert notes for sessions that had none
    import uuid

    now = time.time()
    updates: List[Tuple[str, str, int]] = []
    inserts: List[Tuple[str, str, float, str, int]] = []
    dups: List[str] = []
    for sid, text, nturns in results:
        rows_ = existing.get(sid) or []
        if rows_:
            keep = rows_[-1][0]
            dups += [r[0] for r in rows_[:-1]]
            updates.append((keep, text, nturns))
        else:
            inserts.append((str(uuid.uuid4()), sid, now, text, nturns))
    mem = _open(data_dir)
    try:
        db = mem.db
        per = 200
        for i in range(0, len(updates), per):
            ch = updates[i : i + per]
            case_s = " ".join("WHEN ? THEN ?" for _ in ch)
            params: list = []
            for sid_, text, _n in ch:
                params += [sid_, text]
            for sid_, _t, n_ in ch:
                params += [sid_, n_]
            ids = [u[0] for u in ch]
            db.execute(
                f"UPDATE summaries SET summary = CASE id {case_s} END, "
                f"turn_count = CASE id {case_s} END, ts = ? "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                params + [now] + ids,
            )
        for i in range(0, len(inserts), 150):
            ch = inserts[i : i + 150]
            db.execute(
                "INSERT INTO summaries(id, session_id, ts, summary, turn_count) VALUES "
                + ",".join(["(?,?,?,?,?)"] * len(ch)),
                [v for r in ch for v in r],
            )
        for i in range(0, len(dups), 800):
            ch = dups[i : i + 800]
            db.execute(f"UPDATE summaries SET deleted_at = ? WHERE id IN ({','.join('?' * len(ch))})",
                       [now, *ch])
        if dups:
            try:
                mem.summaries.delete(ids=dups)
            except Exception:
                pass
        db.commit()
    finally:
        mem.close()
    stats["done"] = len(results)
    done_state[key] = sorted(done | {r[0] for r in results})
    _save_done_sessions(done_state)

    # vectors for the rewritten notes (outside the write transaction: network)
    ids = [u[0] for u in updates] + [r[0] for r in inserts]
    texts = [u[1] for u in updates] + [r[3] for r in inserts]
    sids = [r[0] for r in results if (existing.get(r[0]) or [])] + [r[1] for r in inserts]
    vecs = _embed_paced(texts, log)
    ok = [(i, v, t, s) for i, v, t, s in zip(ids, vecs, texts, sids) if v is not None]
    if ok:
        mem = _open(data_dir)
        try:
            mem.summaries.upsert(
                ids=[o[0] for o in ok], embeddings=[o[1] for o in ok], documents=[o[2] for o in ok],
                metadatas=[{"session_id": o[3], "ts": now} for o in ok],
            )
            mem.db.commit()
        finally:
            mem.close()
    stats["embedded"] = len(ok)
    return stats


def run(*, what: Tuple[str, ...] = ("turns", "sessions"), jobs: int = 16,
        model: Optional[str] = None, force: bool = False, dry_run: bool = False,
        limit: Optional[int] = None, store: Optional[str] = None,
        log: Callable[[str], None] = print) -> Dict[str, Dict]:
    from .config import load_env
    from .summarizer import active_provider
    from .worklog import _gdir, discover_sources

    load_env(_gdir())
    provider = active_provider()
    if not provider:
        raise RuntimeError("no summarization backend configured (set SUMMARY_BACKEND)")
    if provider == "claude" and not model:
        from . import claude_cli

        model = claude_cli.model_for(fast=False)
    sources = discover_sources()
    if store:
        sources = [d for d in sources if store.lower() in str(d).lower()]
    log(f"resummarize: backend={provider}{' model=' + model if model else ''} · "
        f"{len(sources)} store(s) · jobs={jobs}")
    done_state = _load_done_sessions()
    report: Dict[str, Dict] = {}
    for d in sources:
        log(f"\n[{d}]")
        load_env(d)
        r: Dict[str, Dict] = {}
        if "turns" in what:
            r["turns"] = rewrite_turns(d, provider=provider, model=model, jobs=jobs, force=force,
                                       limit=limit, dry_run=dry_run, log=log)
        if "sessions" in what:
            r["sessions"] = rewrite_sessions(d, jobs=jobs, force=force, limit=limit,
                                             dry_run=dry_run, done_state=done_state, log=log)
        report[str(d)] = r
    return report
