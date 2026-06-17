import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FAKE_VEC = [0.1] * 16


class GoalCrudTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _memory(self):
        with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
            from lynx_memory.storage import Memory

            return Memory(data_dir=self.data_dir)

    def test_set_get_clear_goal(self):
        m = self._memory()
        try:
            self.assertIsNone(m.get_goal())
            self.assertTrue(m.set_goal("Ship the billing API"))
            g = m.get_goal()
            self.assertIsNotNone(g)
            self.assertEqual(g["text"], "Ship the billing API")
            # upsert replaces text
            m.set_goal("Migrate customers")
            self.assertEqual(m.get_goal()["text"], "Migrate customers")
            # blank is ignored
            self.assertFalse(m.set_goal("   "))
            self.assertEqual(m.get_goal()["text"], "Migrate customers")
            self.assertTrue(m.clear_goal())
            self.assertIsNone(m.get_goal())
            self.assertFalse(m.clear_goal())
        finally:
            m.close()

    def test_schema_is_v2_with_goals_table(self):
        m = self._memory()
        try:
            version = m.db.execute("PRAGMA user_version").fetchone()[0]
            self.assertGreaterEqual(version, 2)
            tables = {
                r[0]
                for r in m.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("goals", tables)
        finally:
            m.close()

    def test_get_goal_text_reads_without_chroma(self):
        from lynx_memory import goals

        # No DB yet → None.
        self.assertIsNone(goals.get_goal_text(self.data_dir))
        m = self._memory()
        try:
            m.set_goal("Refactor auth to passkeys")
        finally:
            m.close()
        self.assertEqual(
            goals.get_goal_text(self.data_dir), "Refactor auth to passkeys"
        )


class JudgeRelevanceTest(unittest.TestCase):
    def test_parse_verdict_orders_irrelevant_first(self):
        from lynx_memory import summarizer

        self.assertIs(summarizer._parse_verdict("IRRELEVANT"), False)
        self.assertIs(summarizer._parse_verdict("RELEVANT"), True)
        self.assertIs(summarizer._parse_verdict("  relevant.\n"), True)
        self.assertIsNone(summarizer._parse_verdict("maybe"))
        self.assertIsNone(summarizer._parse_verdict(""))
        self.assertIsNone(summarizer._parse_verdict(None))

    def test_judge_relevance_uses_first_keyed_provider(self):
        from lynx_memory import summarizer

        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "sk-test"}, clear=True
        ), mock.patch.object(summarizer, "_chat", return_value="IRRELEVANT") as chat:
            verdict = summarizer.judge_relevance("goal", "user", "assistant")
            self.assertIs(verdict, False)
            self.assertEqual(chat.call_args.args[0], "deepseek")

    def test_judge_relevance_none_without_goal_or_provider(self):
        from lynx_memory import summarizer

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(summarizer.judge_relevance("", "u", "a"))
            self.assertIsNone(summarizer.judge_relevance("goal", "u", "a"))


class EvaluateTurnRelevanceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _set_goal(self, text):
        with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
            from lynx_memory.storage import Memory

            m = Memory(data_dir=self.data_dir)
            try:
                m.set_goal(text)
            finally:
                m.close()

    def test_store_when_no_goal(self):
        from lynx_memory import goals

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                goals.evaluate_turn_relevance(self.data_dir, "u", "a"), "store"
            )

    def test_store_when_gating_disabled(self):
        from lynx_memory import goals

        self._set_goal("the goal")
        with mock.patch.dict(os.environ, {"GOAL_GATING_ENABLED": "0"}, clear=True):
            # judge should never be consulted when gating is off
            with mock.patch(
                "lynx_memory.summarizer.judge_relevance",
                side_effect=AssertionError("should not be called"),
            ):
                self.assertEqual(
                    goals.evaluate_turn_relevance(self.data_dir, "u", "a"), "store"
                )

    def test_drop_on_irrelevant_verdict(self):
        from lynx_memory import goals

        self._set_goal("the goal")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "lynx_memory.summarizer.judge_relevance", return_value=False
        ):
            self.assertEqual(
                goals.evaluate_turn_relevance(self.data_dir, "off topic", "chatter"),
                "drop",
            )

    def test_fail_open_on_undecided(self):
        from lynx_memory import goals

        self._set_goal("the goal")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "lynx_memory.summarizer.judge_relevance", return_value=None
        ):
            self.assertEqual(
                goals.evaluate_turn_relevance(self.data_dir, "u", "a"), "store"
            )

    def test_fail_open_on_exception(self):
        from lynx_memory import goals

        self._set_goal("the goal")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "lynx_memory.summarizer.judge_relevance",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(
                goals.evaluate_turn_relevance(self.data_dir, "u", "a"), "store"
            )


if __name__ == "__main__":
    unittest.main()
