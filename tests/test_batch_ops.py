"""Set-based batch operations: deletes/inserts that hit the DB once, not per row.

These guard the contract behind the Turso guideline — bulk writes go out as a
single statement/transaction rather than a write-per-id — by (a) asserting the
behaviour is correct and (b) counting how many times the connection commits.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FAKE_VEC = [0.1] * 16


class _CommitCounter:
    """Transparent proxy over a DB connection that counts `commit()` calls.

    `sqlite3.Connection.commit` is read-only so it can't be patched in place;
    swapping `mem.db` for this proxy works for both the sqlite3 and the
    libSQL-wrapper connections.
    """

    def __init__(self, real):
        self._real = real
        self.commits = 0

    def commit(self):
        self.commits += 1
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


class BatchOpsTest(unittest.TestCase):
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

    def test_forget_turns_deletes_all_in_one_commit(self):
        m = self._memory()
        try:
            ids = [self._add(m, str(i)) for i in range(5)]
            self.assertEqual(m.count_turns(), 5)

            m.db = _CommitCounter(m.db)
            deleted = m.forget_turns(ids[:3])
            self.assertEqual(deleted, 3)
            self.assertEqual(m.count_turns(), 2)
            # one set-based pass, not one commit per id
            self.assertEqual(m.db.commits, 1)
        finally:
            m.close()

    def test_forget_turns_counts_only_live_rows(self):
        m = self._memory()
        try:
            a, b = self._add(m, "a"), self._add(m, "b")
            m.forget_turns([a])
            # a already gone, b live, c bogus -> only b transitions
            deleted = m.forget_turns([a, b, "does-not-exist"])
            self.assertEqual(deleted, 1)
            self.assertEqual(m.count_turns(), 0)
        finally:
            m.close()

    def test_forget_turns_empty_and_dedup(self):
        m = self._memory()
        try:
            a = self._add(m, "a")
            self.assertEqual(m.forget_turns([]), 0)
            self.assertEqual(m.forget_turns([None, ""]), 0)
            # duplicate ids collapse to a single delete
            self.assertEqual(m.forget_turns([a, a, a]), 1)
        finally:
            m.close()

    def test_forget_many_mixes_turns_and_summaries(self):
        m = self._memory()
        try:
            t1, t2 = self._add(m, "t1"), self._add(m, "t2")
            with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
                sid = m.add_summary("s1", "a summary", turn_count=2)
            deleted = m.forget_many([t1, t2, sid])
            self.assertEqual(deleted, 3)
            self.assertEqual(m.count_turns(), 0)
            self.assertIsNone(m.get_turn(t1))
        finally:
            m.close()

    def test_replace_tags_single_commit_and_correct(self):
        m = self._memory()
        try:
            tid = self._add(m, "tagme")
            tags = [
                {"name": "alpha", "kind": "topic", "confidence": 0.9},
                {"name": "beta", "kind": "lang"},
                {"name": "alpha", "kind": "topic"},  # dup -> ignored
                {"name": "  ", "kind": "x"},          # blank -> ignored
            ]
            m.db = _CommitCounter(m.db)
            m.replace_tags(tid, tags, source="auto")
            # one transaction for the whole replace (delete + all inserts)
            self.assertEqual(m.db.commits, 1)

            rows = m.list_turns()
            names = {t["name"] for t in rows[0]["tags"]}
            self.assertEqual(names, {"alpha", "beta"})

            # replacing again clears the old set in the same single pass
            m.db.commits = 0
            m.replace_tags(tid, [{"name": "gamma"}], source="auto")
            self.assertEqual(m.db.commits, 1)
            names = {t["name"] for t in m.list_turns()[0]["tags"]}
            self.assertEqual(names, {"gamma"})
        finally:
            m.close()


if __name__ == "__main__":
    unittest.main()
