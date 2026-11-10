"""Merge stranded fallback writes back into a synced store.

A synced store reads/writes its libSQL embedded replica (`db/sync-memory.db`).
When a hook cannot get the replica lock in time (or the remote is
unreachable) it degrades to the plain local `db/memory.db` so the turn isn't
lost — but nothing ever merged those rows back, so they were invisible to
recall, the Web UI and every summary built from the replica.

`reconcile_store` copies them over:
  - only rows newer than the *watermark* — the newest local row that already
    exists in the replica (i.e. the last write before sync was enabled). Older
    local-only rows are rows that were deleted after sync started (pre-0.7.1
    deletes were hard deletes) and must not be resurrected.
  - a turn whose (session_id, user_uuid) already exists in the replica is the
    same turn written twice: if the local copy is newer and differs, only its
    *content* (user/assistant text, ts, cwd) is copied onto the replica row —
    the replica's note, tags and deletion state are kept; a row already
    deleted in the replica stays deleted. Identical copies are skipped.
  - their sessions, session summaries, tags and (from the local Chroma index)
    vectors come along.
Everything is written in one transaction with one commit (one remote push).
`memory.db` itself is never modified, so re-running is a no-op.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from typing import Dict, List, Optional


def _cols(conn, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _upsert(conn, table: str, cols: List[str], rows: List[tuple], conflict: Optional[str]) -> None:
    if not rows:
        return
    tup = "(" + ",".join("?" * len(cols)) + ")"
    per = max(1, 900 // len(cols))
    tail = ""
    verb = "INSERT OR IGNORE"
    if conflict:
        verb = "INSERT"
        tail = f" ON CONFLICT({conflict}) DO UPDATE SET " + ",".join(
            f"{c}=excluded.{c}" for c in cols if c != conflict
        )
    for i in range(0, len(rows), per):
        chunk = rows[i : i + per]
        conn.execute(
            f"{verb} INTO {table}({','.join(cols)}) VALUES " + ",".join([tup] * len(chunk)) + tail,
            [v for r in chunk for v in r],
        )


def _chroma_vectors(chroma_dir: Optional[Path], collection: str, ids: List[str]) -> List[tuple]:
    if not chroma_dir or not ids or not Path(chroma_dir).exists():
        return []
    try:
        import chromadb
        from chromadb.config import Settings

        cli = chromadb.PersistentClient(path=str(chroma_dir), settings=Settings(anonymized_telemetry=False))
        got = cli.get_collection(collection).get(ids=ids, include=["embeddings"])
    except Exception:
        return []
    out = []
    embs = got.get("embeddings")
    embs = [] if embs is None else list(embs)
    for i, vec in zip(list(got.get("ids") or []), embs):
        vals = [float(x) for x in vec]
        out.append((i, len(vals), struct.pack(f"<{len(vals)}f", *vals)))
    return out


def reconcile_rows(local, target, *, chroma_dir: Optional[Path] = None, dry_run: bool = False,
                   replica_created: Optional[float] = None) -> Dict:
    """Plan (and unless dry_run, apply) the merge of `local` into `target`.

    Both are DB-API connections (sqlite3 / the libSQL wrapper) with rows that
    support `dict(row)`. The caller commits.
    """
    t_cols = _cols(target, "turns")
    l_cols = _cols(local, "turns")
    cols = [c for c in l_cols if c in t_cols]
    if "id" not in cols or "ts" not in cols:
        return {"skipped": "turns table missing"}

    target_ids = {r[0] for r in target.execute("SELECT id FROM turns").fetchall()}
    t_has_deleted = "deleted_at" in t_cols
    by_uuid = {
        (r[0], r[1]): (r[2], r[3], r[4], r[5])
        for r in target.execute(
            "SELECT session_id, user_uuid, id, ts, assistant_msg, "
            + ("deleted_at" if t_has_deleted else "NULL")
            + " FROM turns WHERE user_uuid IS NOT NULL"
        ).fetchall()
    }
    local_rows = [dict(zip(cols, r)) for r in local.execute(f"SELECT {','.join(cols)} FROM turns").fetchall()]
    shared = [r["ts"] for r in local_rows if r["id"] in target_ids]
    if shared:
        watermark = max(shared)
    elif replica_created:
        # memory.db was recreated after sync started: nothing in it predates
        # the replica, so the replica's creation time is the boundary
        watermark = replica_created
    else:
        return {"skipped": "no overlap with replica and replica age unknown"}

    inserts: List[dict] = []
    updates: List[dict] = []
    for r in local_rows:
        if r["id"] in target_ids or float(r["ts"]) <= watermark:
            continue
        key = (r.get("session_id"), r.get("user_uuid"))
        if r.get("user_uuid") and key in by_uuid:
            tid, tts, tmsg, tdel = by_uuid[key]
            if (
                tdel is None
                and float(r["ts"]) > float(tts or 0)
                and (r.get("assistant_msg") or "") != (tmsg or "")
            ):
                updates.append({**r, "id": tid})
            continue
        inserts.append(r)

    ins_ids = [r["id"] for r in inserts]
    sess_ids = sorted({r["session_id"] for r in inserts})
    res: Dict = {"watermark": watermark, "inserted": len(inserts), "updated": len(updates)}

    # session summaries written to the fallback DB
    s_cols = [c for c in _cols(local, "summaries") if c in _cols(target, "summaries")]
    sum_rows: List[tuple] = []
    if s_cols and "id" in s_cols:
        t_sum = {r[0] for r in target.execute("SELECT id FROM summaries").fetchall()}
        for r in local.execute(f"SELECT {','.join(s_cols)} FROM summaries").fetchall():
            d = dict(zip(s_cols, r))
            if d["id"] not in t_sum and float(d["ts"]) > watermark:
                sum_rows.append(tuple(d[c] for c in s_cols))
    res["summaries"] = len(sum_rows)
    if dry_run or (not inserts and not updates and not sum_rows):
        return res

    _upsert(target, "turns", cols, [tuple(r[c] for c in cols) for r in inserts], None)
    # content-only update: never copy summary*/deleted_at over the replica's
    u_cols = [c for c in ("id", "session_id", "user_uuid", "ts", "cwd", "user_msg", "assistant_msg")
              if c in cols]
    _upsert(target, "turns", u_cols, [tuple(r[c] for c in u_cols) for r in updates], "id")

    sc = [c for c in _cols(local, "sessions") if c in _cols(target, "sessions")]
    if sess_ids and sc:
        ph = ",".join("?" * len(sess_ids))
        rows = local.execute(f"SELECT {','.join(sc)} FROM sessions WHERE id IN ({ph})", sess_ids).fetchall()
        _upsert(target, "sessions", sc, [tuple(r) for r in rows], None)
    _upsert(target, "summaries", s_cols, sum_rows, None)

    if ins_ids:
        ph = ",".join("?" * len(ins_ids))
        tt = [c for c in _cols(local, "turn_tags") if c in _cols(target, "turn_tags")]
        if tt:
            tag_rows = local.execute(f"SELECT {','.join(tt)} FROM turn_tags WHERE turn_id IN ({ph})",
                                     ins_ids).fetchall()
            names = sorted({dict(zip(tt, r)).get("tag_name") for r in tag_rows} - {None})
            tg = [c for c in _cols(local, "tags") if c in _cols(target, "tags")]
            if names and tg:
                ph2 = ",".join("?" * len(names))
                _upsert(target, "tags", tg, [tuple(r) for r in local.execute(
                    f"SELECT {','.join(tg)} FROM tags WHERE name IN ({ph2})", names).fetchall()], None)
            _upsert(target, "turn_tags", tt, [tuple(r) for r in tag_rows], None)
        if _cols(target, "turn_vectors"):
            vecs = _chroma_vectors(chroma_dir, "turns", ins_ids)
            _upsert(target, "turn_vectors", ["id", "dim", "vec"], vecs, "id")
            res["vectors"] = len(vecs)
    if sum_rows and _cols(target, "summary_vectors"):
        sid = [r[s_cols.index("id")] for r in sum_rows]
        _upsert(target, "summary_vectors", ["id", "dim", "vec"], _chroma_vectors(chroma_dir, "summaries", sid), "id")
    return res


def reconcile_store(data_dir: Path, *, dry_run: bool = False) -> Dict:
    """Merge a synced store's stranded `memory.db` rows into its replica."""
    from .config import paths_for

    paths = paths_for(Path(data_dir))
    local_path = paths["db_path"]
    replica = local_path.with_name("sync-" + local_path.name)
    if not (local_path.exists() and replica.exists()):
        return {}
    from .storage import Memory

    local = sqlite3.connect(f"file:{local_path}?mode=ro", uri=True)
    try:
        mem = Memory(data_dir=Path(data_dir), strict_sync=True, lock_timeout=120)
        try:
            if not mem.synced:
                return {"skipped": "store not synced right now"}
            born = getattr(replica.stat(), "st_birthtime", None)  # macOS/BSD only
            res = reconcile_rows(local, mem.db, chroma_dir=paths["chroma_dir"], dry_run=dry_run,
                                 replica_created=born)
            if not dry_run and (res.get("inserted") or res.get("updated") or res.get("summaries")):
                mem.db.commit()
                mem.sync()
            return res
        finally:
            mem.close()
    finally:
        local.close()
