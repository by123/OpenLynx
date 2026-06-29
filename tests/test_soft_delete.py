import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FAKE_VEC = [0.1] * 16


class SoftDeleteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _memory(self):
        with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
            from lynx_memory.storage import Memory

            return Memory(data_dir=self.data_dir)

    def _add(self, m, msg):
        with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
            return m.add_turn("s1", user_msg="u:" + msg, assistant_msg="a:" + msg, cwd="/x")

    def test_forget_hides_but_preserves(self):
        m = self._memory()
        try:
            t1 = self._add(m, "one")
            t2 = self._add(m, "two")
            self.assertEqual(m.count_turns(), 2)

            self.assertTrue(m.forget_turn(t1))
            # idempotent — nothing live left to delete
            self.assertFalse(m.forget_turn(t1))

            # hidden from every read path
            self.assertEqual(m.count_turns(), 1)
            self.assertEqual({r["id"] for r in m.list_turns()}, {t2})
            self.assertEqual({r["id"] for r in m.list_recent()}, {t2})
            self.assertIsNone(m.get_turn(t1))
            self.assertEqual(set(m.get_turns_by_ids([t1, t2])), {t2})
            self.assertEqual({r["id"] for r in m.get_session_turns("s1")}, {t2})
            self.assertEqual(m.stats()["turns"], 1)

            # but the data is still there (raw read, no filter) with a stamp
            raw = m.db.execute(
                "SELECT deleted_at, user_msg FROM turns WHERE id=?", (t1,)
            ).fetchone()
            self.assertIsNotNone(raw, "row was hard-deleted")
            self.assertIsNotNone(raw["deleted_at"])
            self.assertEqual(raw["user_msg"], "u:one")
        finally:
            m.close()

    def test_forget_excludes_from_search(self):
        m = self._memory()
        try:
            t1 = self._add(m, "alpha")
            self._add(m, "beta")
            with mock.patch("lynx_memory.storage._search.embed_one", return_value=FAKE_VEC):
                ids_before = {r["id"] for r in m.search("q", top_k=5, min_score=0.0)}
                self.assertIn(t1, ids_before)
                m.forget_turn(t1)
                ids_after = {r["id"] for r in m.search("q", top_k=5, min_score=0.0)}
            self.assertNotIn(t1, ids_after)
        finally:
            m.close()

    def test_reingest_revives_soft_deleted_turn(self):
        m = self._memory()
        try:
            with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
                tid, _ = m.upsert_turn("s1", "uuid-1", "hi", "first", cwd="/x")
            self.assertTrue(m.forget_turn(tid))
            self.assertIsNone(m.get_turn(tid))
            # re-ingesting the same turn (changed content) un-deletes it
            with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
                tid2, action = m.upsert_turn("s1", "uuid-1", "hi", "second", cwd="/x")
            self.assertEqual(tid2, tid)
            self.assertEqual(action, "update")
            self.assertIsNotNone(m.get_turn(tid))
            self.assertEqual(m.count_turns(), 1)
        finally:
            m.close()

    def test_schema_is_v4_with_deleted_at(self):
        m = self._memory()
        try:
            version = m.db.execute("PRAGMA user_version").fetchone()[0]
            self.assertGreaterEqual(version, 4)
            for table in ("turns", "summaries"):
                cols = {r[1] for r in m.db.execute(f"PRAGMA table_info({table})")}
                self.assertIn("deleted_at", cols)
        finally:
            m.close()

    def test_migration_v3_to_v4_adds_column(self):
        db_dir = self.data_dir / "db"
        db_dir.mkdir(parents=True)
        db = sqlite3.connect(db_dir / "memory.db")
        db.execute(
            "CREATE TABLE turns(id TEXT PRIMARY KEY, session_id TEXT, ts REAL, "
            "user_msg TEXT, assistant_msg TEXT)"
        )
        db.execute(
            "CREATE TABLE summaries(id TEXT PRIMARY KEY, session_id TEXT, ts REAL, summary TEXT)"
        )
        db.execute("INSERT INTO turns VALUES('x','s',1.0,'u','a')")
        db.execute("PRAGMA user_version=3")
        db.commit()
        db.close()

        m = self._memory()
        try:
            self.assertEqual(m.db.execute("PRAGMA user_version").fetchone()[0], 4)
            cols = {r[1] for r in m.db.execute("PRAGMA table_info(turns)")}
            self.assertIn("deleted_at", cols)
            # the pre-existing row survives migration and stays visible
            self.assertEqual(m.count_turns(), 1)
        finally:
            m.close()


if __name__ == "__main__":
    unittest.main()
