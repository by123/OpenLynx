"""Tags + auto-tag refresh."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..autotag import suggest_tags


class _TagsMixin:
    def _attach_tags(self, turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not turns:
            return turns
        ids = [t["id"] for t in turns]
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT tt.turn_id, tt.tag_name, tt.source, tt.confidence, tg.kind "
            f"FROM turn_tags tt JOIN tags tg ON tg.name = tt.tag_name "
            f"WHERE tt.turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        by_id: Dict[str, List[Dict[str, Any]]] = {i: [] for i in ids}
        for r in rows:
            by_id[r["turn_id"]].append(
                {
                    "name": r["tag_name"],
                    "kind": r["kind"] or "custom",
                    "source": r["source"] or "manual",
                    "confidence": r["confidence"],
                }
            )
        for t in turns:
            t["tags"] = sorted(
                by_id.get(t["id"], []),
                key=lambda item: (item["kind"], item["name"]),
            )
        return turns

    def list_tags(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = (
            "SELECT t.name, t.kind, t.created_at, COUNT(tt.turn_id) AS count "
            "FROM tags t LEFT JOIN turn_tags tt ON tt.tag_name = t.name "
        )
        params: list = []
        if kind:
            sql += "WHERE t.kind = ? "
            params.append(kind)
        sql += "GROUP BY t.name, t.kind ORDER BY t.kind, count DESC, t.name"
        rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _clean_tag_name(tag: str) -> str:
        return " ".join(tag.strip().lstrip("#").split())

    def add_tag(
        self,
        turn_id: str,
        tag: str,
        *,
        kind: str = "custom",
        source: str = "manual",
        confidence: Optional[float] = None,
    ) -> bool:
        tag = self._clean_tag_name(tag)
        if not tag:
            return False
        row = self.db.execute(
            "SELECT id FROM turns WHERE id = ? AND deleted_at IS NULL", (turn_id,)
        ).fetchone()
        if row is None:
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO tags(name, kind, created_at) VALUES(?, ?, ?)",
            (tag, kind, time.time()),
        )
        self.db.execute(
            "UPDATE tags SET kind = ? "
            "WHERE name = ? AND (kind IS NULL OR kind = '' OR kind = 'custom')",
            (kind, tag),
        )
        cur = self.db.execute(
            "INSERT OR IGNORE INTO turn_tags(turn_id, tag_name, source, confidence) VALUES(?, ?, ?, ?)",
            (turn_id, tag, source, confidence),
        )
        self.db.commit()
        return cur.rowcount > 0

    def remove_tag(self, turn_id: str, tag: str) -> bool:
        tag = self._clean_tag_name(tag)
        cur = self.db.execute(
            "DELETE FROM turn_tags WHERE turn_id = ? AND tag_name = ?",
            (turn_id, tag),
        )
        self.db.commit()
        if cur.rowcount > 0:
            left = self.db.execute(
                "SELECT 1 FROM turn_tags WHERE tag_name = ? LIMIT 1", (tag,)
            ).fetchone()
            if left is None:
                self.db.execute("DELETE FROM tags WHERE name = ?", (tag,))
                self.db.commit()
        return cur.rowcount > 0

    def replace_tags(
        self,
        turn_id: str,
        tags: List[Dict[str, Any]],
        *,
        source: str,
    ) -> None:
        self.db.execute(
            "DELETE FROM turn_tags WHERE turn_id = ? AND source = ?",
            (turn_id, source),
        )
        self.db.commit()
        for item in tags:
            self.add_tag(
                turn_id,
                item["name"],
                kind=item.get("kind") or "custom",
                source=source,
                confidence=item.get("confidence"),
            )

    def refresh_auto_tags(
        self,
        turn_id: str,
        *,
        user_msg: str,
        assistant_msg: str,
        cwd: Optional[str] = None,
    ) -> None:
        self.replace_tags(
            turn_id,
            suggest_tags(user_msg=user_msg, assistant_msg=assistant_msg, cwd=cwd),
            source="auto",
        )
