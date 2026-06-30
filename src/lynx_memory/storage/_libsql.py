"""libSQL (Turso) embedded-replica connection adapter.

The libSQL Python client returns plain tuples and has no `sqlite3.Row`
equivalent, while the rest of the storage layer relies on `dict(row)`,
`row["name"]`, `row[i]` and iterating the result of `.execute(...)`. This
module wraps a libSQL connection so it quacks like the stdlib `sqlite3`
connection the code already expects.

Only the surface actually used by the storage layer is implemented:
`execute`, `executescript`, `commit`, `close`, plus `sync()` for the
embedded replica.
"""
from __future__ import annotations

import math
import struct
from typing import Any, Dict, Iterator, List, Optional, Sequence


class _Row:
    """sqlite3.Row-like view over a tuple + column names.

    Supports positional (`row[0]`), named (`row["id"]`) access, `keys()`,
    iteration over values, and `dict(row)` (via the mapping protocol).
    """

    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        self._names = names
        self._values = values
        self._index = {n: i for i, n in enumerate(names)}

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def keys(self) -> List[str]:
        return list(self._names)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"_Row({dict(zip(self._names, self._values))!r})"


class _Result:
    """Cursor-like proxy: rows come back as `_Row`, and it is iterable."""

    def __init__(self, cursor: Any) -> None:
        self._cur = cursor
        desc = getattr(cursor, "description", None)
        self._names = [d[0] for d in desc] if desc else []

    def fetchone(self) -> Optional[_Row]:
        row = self._cur.fetchone()
        return _Row(self._names, row) if row is not None else None

    def fetchall(self) -> List[_Row]:
        return [_Row(self._names, r) for r in self._cur.fetchall()]

    def __iter__(self) -> Iterator[_Row]:
        for r in self._cur.fetchall():
            yield _Row(self._names, r)

    @property
    def rowcount(self) -> int:
        rc = getattr(self._cur, "rowcount", -1)
        return rc if isinstance(rc, int) else -1

    @property
    def lastrowid(self) -> Any:
        return getattr(self._cur, "lastrowid", None)


class LibsqlConnection:
    """Minimal `sqlite3.Connection`-compatible wrapper over a libSQL conn."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> _Result:
        # Turso/libSQL forbids `PRAGMA user_version = N` over its protocol
        # (reads are fine, writes are not). Redirect user_version through a
        # `_meta` table so the migration logic in _base.py works unchanged.
        stripped = sql.strip().rstrip(";").strip()
        low = stripped.lower()
        if low.startswith("pragma user_version"):
            if "=" in stripped:
                value = int(stripped.split("=", 1)[1].strip())
                self._raw.execute(
                    "INSERT INTO _meta(key, value) VALUES('user_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (value,),
                )
                return _Result(self._raw.execute("SELECT 1 WHERE 0"))
            cur = self._raw.execute(
                "SELECT COALESCE((SELECT value FROM _meta WHERE key='user_version'), 0)"
            )
            return _Result(cur)
        if params:
            cur = self._raw.execute(stripped, tuple(params))
        else:
            cur = self._raw.execute(stripped)
        return _Result(cur)

    def executescript(self, script: str) -> "LibsqlConnection":
        self._raw.executescript(script)
        return self

    def commit(self) -> None:
        self._raw.commit()

    def sync(self) -> None:
        self._raw.sync()

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass


class LibsqlVectorCollection:
    """A Chroma-collection-compatible view backed by a libSQL vector table.

    Embeddings are stored as packed float32 BLOBs in `turn_vectors` /
    `summary_vectors` (which sync via the embedded replica), and similarity
    is brute-force cosine in Python — fine at single-user scale and trivially
    correct. Documents/metadata are rebuilt from the turns/summaries tables on
    query, so the vector table only stores (id, dim, vec). Implements the
    subset Chroma exposes that the code uses: count/add/upsert/delete/query.
    """

    def __init__(self, conn: "LibsqlConnection", kind: str) -> None:
        self.conn = conn
        self.kind = kind  # "turn" | "summary"
        self.table = "turn_vectors" if kind == "turn" else "summary_vectors"

    def count(self) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]

    def add(self, ids, embeddings, documents=None, metadatas=None) -> None:
        self.upsert(ids, embeddings)

    def upsert(self, ids, embeddings, documents=None, metadatas=None) -> None:
        rows = []
        for i, vec in zip(ids, embeddings):
            vals = [float(x) for x in vec]
            rows.append((i, len(vals), struct.pack(f"<{len(vals)}f", *vals)))
        if not rows:
            return
        # One multi-row INSERT per chunk, not one per id: a single write
        # statement (and a single sync on commit) instead of N. 3 bind vars
        # per row, kept well under SQLite's variable limit.
        for start in range(0, len(rows), 300):
            chunk = rows[start : start + 300]
            self.conn.execute(
                f"INSERT INTO {self.table}(id, dim, vec) VALUES "
                + ",".join(["(?,?,?)"] * len(chunk))
                + " ON CONFLICT(id) DO UPDATE SET dim=excluded.dim, vec=excluded.vec",
                [v for row in chunk for v in row],
            )
        self.conn.commit()

    def delete(self, ids=None) -> None:
        ids = [i for i in (ids or []) if i is not None]
        if not ids:
            return
        # Set-based delete: one `DELETE ... WHERE id IN (...)` per chunk rather
        # than a round-trip per id (the whole point on a synced replica).
        for start in range(0, len(ids), 800):
            chunk = ids[start : start + 800]
            placeholders = ",".join("?" for _ in chunk)
            self.conn.execute(
                f"DELETE FROM {self.table} WHERE id IN ({placeholders})", chunk
            )
        self.conn.commit()

    def query(self, query_embeddings, n_results: int = 10, **_kw) -> Dict[str, list]:
        q = [float(x) for x in query_embeddings[0]]
        qn = math.sqrt(sum(x * x for x in q)) or 1.0
        scored = []
        for r in self.conn.execute(f"SELECT id, dim, vec FROM {self.table}"):
            dim = r["dim"]
            if dim != len(q):  # different embedding model/dim — skip
                continue
            vec = struct.unpack(f"<{dim}f", r["vec"])
            dot = 0.0
            vn2 = 0.0
            for a, b in zip(q, vec):
                dot += a * b
                vn2 += b * b
            cos = dot / (qn * (math.sqrt(vn2) or 1.0))
            scored.append((r["id"], 1.0 - cos))  # cosine distance, like Chroma
        scored.sort(key=lambda t: t[1])
        top = scored[: max(0, n_results)]
        ids = [t[0] for t in top]
        dists = [t[1] for t in top]
        docs, metas = self._docs_metas(ids)
        return {"ids": [ids], "documents": [docs], "metadatas": [metas], "distances": [dists]}

    def _docs_metas(self, ids: List[str]):
        if not ids:
            return [], []
        ph = ",".join("?" for _ in ids)
        docs: List[str] = []
        metas: List[dict] = []
        if self.kind == "turn":
            rowmap = {
                r["id"]: r
                for r in self.conn.execute(
                    f"SELECT id, ts, cwd, session_id, user_msg, assistant_msg "
                    f"FROM turns WHERE id IN ({ph})",
                    ids,
                )
            }
            for i in ids:
                r = rowmap.get(i)
                if r is None:
                    docs.append("")
                    metas.append({})
                    continue
                docs.append(f"User: {r['user_msg']}\n\nAssistant: {r['assistant_msg']}")
                metas.append({"session_id": r["session_id"], "ts": r["ts"], "cwd": r["cwd"] or ""})
        else:
            rowmap = {
                r["id"]: r
                for r in self.conn.execute(
                    f"SELECT id, ts, session_id, summary FROM summaries WHERE id IN ({ph})",
                    ids,
                )
            }
            for i in ids:
                r = rowmap.get(i)
                if r is None:
                    docs.append("")
                    metas.append({})
                    continue
                docs.append(r["summary"])
                metas.append({"session_id": r["session_id"], "ts": r["ts"]})
        return docs, metas


def connect_replica(
    local_path: str, sync_url: str, auth_token: str, do_sync: bool = True
) -> LibsqlConnection:
    """Open a libSQL embedded replica at `local_path` synced with `sync_url`.

    When `do_sync` is True, pull the latest remote state into the local
    replica (a network round-trip) and ensure the `_meta` version table
    exists. When False, just open the local replica for fast offline-ish
    reads — callers throttle how often a pull happens.
    """
    import libsql_experimental as libsql

    raw = libsql.connect(local_path, sync_url=sync_url, auth_token=auth_token)
    if do_sync:
        raw.sync()  # pull remote state into the local replica
        # version-tracking table (Turso disallows `PRAGMA user_version = N`)
        raw.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value INTEGER)")
        raw.commit()
    return LibsqlConnection(raw)
