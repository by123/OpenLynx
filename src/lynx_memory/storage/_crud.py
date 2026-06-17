"""Sessions / turns / summaries CRUD."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


class _CrudMixin:
    # ---------- sessions ----------
    def ensure_session(self, session_id: str, cwd: Optional[str] = None) -> None:
        cur = self.db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,))
        if cur.fetchone():
            return
        self.db.execute(
            "INSERT INTO sessions(id, started_at, cwd) VALUES(?,?,?)",
            (session_id, time.time(), cwd),
        )
        self.db.commit()

    def end_session(self, session_id: str) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (time.time(), session_id),
        )
        self.db.commit()

    # ---------- turns ----------
    def add_turn(
        self,
        session_id: str,
        user_msg: str,
        assistant_msg: str,
        cwd: Optional[str] = None,
    ) -> str:
        turn_id = str(uuid.uuid4())
        ts = time.time()
        self.db.execute(
            "INSERT INTO turns(id, session_id, ts, cwd, user_msg, assistant_msg) VALUES(?,?,?,?,?,?)",
            (turn_id, session_id, ts, cwd, user_msg, assistant_msg),
        )
        self.db.commit()
        self._index_turn_document(
            turn_id,
            session_id=session_id,
            ts=ts,
            cwd=cwd,
            user_msg=user_msg,
            assistant_msg=assistant_msg,
            action="insert",
        )
        self.refresh_auto_tags(turn_id, user_msg=user_msg, assistant_msg=assistant_msg, cwd=cwd)
        return turn_id

    def upsert_turn(
        self,
        session_id: str,
        user_uuid: str,
        user_msg: str,
        assistant_msg: str,
        cwd: Optional[str] = None,
    ) -> tuple:
        row = self.db.execute(
            "SELECT id, assistant_msg FROM turns WHERE session_id = ? AND user_uuid = ?",
            (session_id, user_uuid),
        ).fetchone()
        ts = time.time()

        if row is None:
            turn_id = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO turns(id, session_id, ts, cwd, user_msg, assistant_msg, user_uuid) "
                "VALUES(?,?,?,?,?,?,?)",
                (turn_id, session_id, ts, cwd, user_msg, assistant_msg, user_uuid),
            )
            self.db.commit()
            action = "insert"
        else:
            turn_id = row["id"]
            if row["assistant_msg"] == assistant_msg:
                return turn_id, "skip"
            self.db.execute(
                "UPDATE turns SET assistant_msg = ?, ts = ?, user_msg = ?, cwd = ? WHERE id = ?",
                (assistant_msg, ts, user_msg, cwd, turn_id),
            )
            self.db.commit()
            action = "update"

        self._index_turn_document(
            turn_id,
            session_id=session_id,
            ts=ts,
            cwd=cwd,
            user_msg=user_msg,
            assistant_msg=assistant_msg,
            action=action,
        )
        self.refresh_auto_tags(turn_id, user_msg=user_msg, assistant_msg=assistant_msg, cwd=cwd)
        return turn_id, action

    def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute(
            "SELECT id, session_id, ts, cwd, user_msg, assistant_msg, summary, summary_source, "
            "summary_model, summary_ts FROM turns WHERE id = ?",
            (turn_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_turns_by_ids(self, turn_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not turn_ids:
            return {}
        placeholders = ",".join("?" for _ in turn_ids)
        rows = self.db.execute(
            f"SELECT id, session_id, ts, cwd, user_msg, assistant_msg, "
            f"summary, summary_source, summary_model, summary_ts "
            f"FROM turns WHERE id IN ({placeholders})",
            turn_ids,
        ).fetchall()
        return {r["id"]: dict(r) for r in rows}

    def list_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, session_id, ts, user_msg, assistant_msg, summary, summary_source, "
            "summary_model, summary_ts "
            "FROM turns ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_turns(
        self,
        limit: int = 20,
        offset: int = 0,
        query: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = (
            "SELECT t.id, t.session_id, t.ts, t.cwd, t.user_msg, t.assistant_msg, "
            "t.summary, t.summary_source, t.summary_model, t.summary_ts "
            "FROM turns t"
        )
        params: list = []
        wheres: list = []
        if tag:
            sql += " JOIN turn_tags tt ON tt.turn_id = t.id"
            wheres.append("tt.tag_name = ?")
            params.append(tag)
        if query:
            wheres.append("(t.user_msg LIKE ? OR t.assistant_msg LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like])
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY t.ts DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = [dict(r) for r in self.db.execute(sql, params).fetchall()]
        return self._attach_tags(rows)

    def count_turns(self, query: Optional[str] = None, tag: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM turns t"
        params: list = []
        wheres: list = []
        if tag:
            sql += " JOIN turn_tags tt ON tt.turn_id = t.id"
            wheres.append("tt.tag_name = ?")
            params.append(tag)
        if query:
            wheres.append("(t.user_msg LIKE ? OR t.assistant_msg LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like])
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        return int(self.db.execute(sql, params).fetchone()[0])

    def get_session_turns(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, ts, user_msg, assistant_msg FROM turns WHERE session_id = ? ORDER BY ts",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def iter_turns_for_retag(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT id, cwd, user_msg, assistant_msg FROM turns ORDER BY ts DESC"
        params: list = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def set_summary(
        self,
        turn_id: str,
        summary: str,
        *,
        source: Optional[str] = None,
        model: Optional[str] = None,
    ) -> bool:
        cur = self.db.execute(
            "UPDATE turns SET summary = ?, summary_source = ?, summary_model = ?, summary_ts = ? "
            "WHERE id = ?",
            (summary, source, model, time.time(), turn_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    def forget_turn(self, turn_id: str) -> bool:
        """Delete a single turn + its tags + retrieval-hit links + chroma vec."""
        affected_tags = [
            r["tag_name"]
            for r in self.db.execute(
                "SELECT tag_name FROM turn_tags WHERE turn_id = ?", (turn_id,)
            ).fetchall()
        ]
        self.db.execute("DELETE FROM turn_tags WHERE turn_id = ?", (turn_id,))
        self.db.execute("DELETE FROM retrieval_hits WHERE turn_id = ?", (turn_id,))
        cur = self.db.execute("DELETE FROM turns WHERE id = ?", (turn_id,))
        for tag in affected_tags:
            left = self.db.execute(
                "SELECT 1 FROM turn_tags WHERE tag_name = ? LIMIT 1", (tag,)
            ).fetchone()
            if left is None:
                self.db.execute("DELETE FROM tags WHERE name = ?", (tag,))
        self.db.commit()
        if cur.rowcount == 0:
            return False
        try:
            self.turns.delete(ids=[turn_id])
        except Exception:
            pass
        return True

    # ---------- summaries ----------
    def add_summary(self, session_id: str, summary: str, turn_count: int) -> str:
        sid = str(uuid.uuid4())
        ts = time.time()
        self.db.execute(
            "INSERT INTO summaries(id, session_id, ts, summary, turn_count) VALUES(?,?,?,?,?)",
            (sid, session_id, ts, summary, turn_count),
        )
        self.db.commit()
        self._index_summary_document(sid, session_id=session_id, ts=ts, summary=summary)
        return sid

    def find_unsummarized_session(
        self, exclude_session_id: Optional[str] = None, min_turns: int = 2
    ) -> Optional[str]:
        """Most recent session with >= min_turns turns and no summary yet.

        Used by the Codex SessionStart hook to summarize the previous session
        on the way into a new one (Codex has no SessionEnd event).
        """
        rows = self.db.execute(
            """
            SELECT s.id
              FROM sessions s
              LEFT JOIN summaries m ON m.session_id = s.id
             WHERE m.id IS NULL
               AND (? IS NULL OR s.id != ?)
             ORDER BY s.started_at DESC
             LIMIT 20
            """,
            (exclude_session_id, exclude_session_id),
        ).fetchall()
        for r in rows:
            sid = r["id"]
            n = self.db.execute(
                "SELECT COUNT(*) AS c FROM turns WHERE session_id = ?", (sid,)
            ).fetchone()["c"]
            if n >= min_turns:
                return sid
        return None

    def forget_summary(self, summary_id: str) -> bool:
        cur = self.db.execute("DELETE FROM summaries WHERE id = ?", (summary_id,))
        self.db.commit()
        if cur.rowcount == 0:
            return False
        try:
            self.summaries.delete(ids=[summary_id])
        except Exception:
            pass
        return True

    def forget(self, item_id: str) -> bool:
        """Back-compat dispatcher: delete a turn or, failing that, a summary.

        Prefer `forget_turn` / `forget_summary` for new call sites.
        """
        if self.forget_turn(item_id):
            return True
        return self.forget_summary(item_id)

    # ---------- goals ----------
    def get_goal(self) -> Optional[Dict[str, Any]]:
        """Return the per-store goal {text, created_at, updated_at} or None."""
        try:
            row = self.db.execute(
                "SELECT text, created_at, updated_at FROM goals WHERE id = 1"
            ).fetchone()
        except Exception:
            return None
        if not row or not (row["text"] or "").strip():
            return None
        return dict(row)

    def set_goal(self, text: str) -> bool:
        """Upsert the singleton goal for this store. Empty text is ignored."""
        text = (text or "").strip()
        if not text:
            return False
        now = time.time()
        self.db.execute(
            "INSERT INTO goals(id, text, created_at, updated_at) VALUES(1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at",
            (text, now, now),
        )
        self.db.commit()
        return True

    def clear_goal(self) -> bool:
        cur = self.db.execute("DELETE FROM goals WHERE id = 1")
        self.db.commit()
        return cur.rowcount > 0

    # ---------- stats (admin) ----------
    def stats(self) -> Dict[str, int]:
        n_sessions = self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_turns = self.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        n_sum = self.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        return {
            "sessions": n_sessions,
            "turns": n_turns,
            "summaries": n_sum,
            "chroma_turns": self.turns.count(),
            "chroma_summaries": self.summaries.count(),
        }
