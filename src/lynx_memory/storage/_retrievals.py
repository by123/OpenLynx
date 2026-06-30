"""Retrieval logging + analytics (top-referenced turns, hit counts)."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


class _RetrievalsMixin:
    def record_retrieval(
        self,
        prompt: str,
        hits: List[Dict[str, Any]],
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        scope_used: Optional[str] = None,
    ) -> str:
        rid = str(uuid.uuid4())
        ts = time.time()
        self.db.execute(
            "INSERT INTO retrievals(id, ts, session_id, cwd, prompt, scope_used, hit_count) "
            "VALUES(?,?,?,?,?,?,?)",
            (rid, ts, session_id, cwd, prompt, scope_used, len(hits)),
        )
        # All hits in one set-based multi-row INSERT (6 bind vars/row, chunked
        # under SQLite's variable limit) instead of one INSERT per hit.
        if hits:
            rows = [
                (rid, h.get("id"), h.get("scope"), h.get("kind"),
                 float(h.get("score") or 0.0), rank)
                for rank, h in enumerate(hits)
            ]
            for start in range(0, len(rows), 150):
                chunk = rows[start : start + 150]
                self.db.execute(
                    "INSERT OR IGNORE INTO retrieval_hits"
                    "(retrieval_id, turn_id, scope, kind, score, rank) VALUES "
                    + ",".join(["(?,?,?,?,?,?)"] * len(chunk)),
                    [v for row in chunk for v in row],
                )
        self.db.commit()
        return rid

    def list_retrievals(
        self,
        limit: int = 20,
        offset: int = 0,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT id, ts, session_id, cwd, prompt, scope_used, hit_count FROM retrievals"
        params: list = []
        if query:
            sql += " WHERE prompt LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def count_retrievals(self, query: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM retrievals"
        params: list = []
        if query:
            sql += " WHERE prompt LIKE ?"
            params.append(f"%{query}%")
        return int(self.db.execute(sql, params).fetchone()[0])

    def get_retrieval(self, retrieval_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute(
            "SELECT id, ts, session_id, cwd, prompt, scope_used, hit_count "
            "FROM retrievals WHERE id = ?",
            (retrieval_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        hits = [
            dict(r)
            for r in self.db.execute(
                "SELECT turn_id, scope, kind, score, rank FROM retrieval_hits "
                "WHERE retrieval_id = ? ORDER BY rank",
                (retrieval_id,),
            ).fetchall()
        ]
        out["hits"] = hits
        return out

    def hit_counts_for_turns(self, turn_ids: List[str]) -> Dict[str, int]:
        if not turn_ids:
            return {}
        placeholders = ",".join("?" for _ in turn_ids)
        rows = self.db.execute(
            f"SELECT turn_id, COUNT(*) AS c FROM retrieval_hits "
            f"WHERE turn_id IN ({placeholders}) GROUP BY turn_id",
            turn_ids,
        ).fetchall()
        return {r["turn_id"]: int(r["c"]) for r in rows}

    def top_referenced_turns(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT t.id, t.session_id, t.ts, t.cwd, t.user_msg, t.assistant_msg, "
            "t.summary, t.summary_source, t.summary_model, t.summary_ts, "
            "COUNT(h.retrieval_id) AS retrieval_count "
            "FROM turns t JOIN retrieval_hits h ON h.turn_id = t.id "
            "WHERE t.deleted_at IS NULL "
            "GROUP BY t.id "
            "ORDER BY retrieval_count DESC, t.ts DESC "
            "LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        items = [dict(r) for r in rows]
        return self._attach_tags(items)

    def list_retrievals_for_turn(
        self, turn_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT r.id, r.ts, r.session_id, r.cwd, r.prompt, r.scope_used, r.hit_count, "
            "h.score, h.rank "
            "FROM retrieval_hits h JOIN retrievals r ON r.id = h.retrieval_id "
            "WHERE h.turn_id = ? ORDER BY r.ts DESC LIMIT ?",
            (turn_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
