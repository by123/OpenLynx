import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

FAKE_VEC = [0.1] * 16


class BuildDigestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _memory(self):
        with mock.patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
            from lynx_memory.storage import Memory

            return Memory(data_dir=self.data_dir)

    def _add_turn(self, ts, user, asst, summary=None):
        m = self._memory()
        try:
            m.db.execute(
                "INSERT INTO turns(id, session_id, ts, user_msg, assistant_msg, summary) "
                "VALUES(?,?,?,?,?,?)",
                (f"id-{ts}", "s1", ts, user, asst, summary),
            )
            m.db.commit()
        finally:
            m.close()

    def test_no_turns_returns_empty(self):
        from lynx_memory import daily

        with mock.patch.dict(os.environ, {}, clear=True):
            digest, n, goal = daily.build_digest(self.data_dir, since_hours=24)
        self.assertEqual((digest, n, goal), ("", 0, None))

    def test_only_counts_turns_in_window(self):
        from lynx_memory import daily

        now = time.time()
        self._add_turn(now - 3600, "today A", "ans", summary="did A")  # within 24h
        self._add_turn(now - 3 * 24 * 3600, "old B", "ans")            # 3 days ago
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}, clear=True), mock.patch(
            "lynx_memory.summarizer._chat", return_value="今天做了 A"
        ) as chat:
            digest, n, goal = daily.build_digest(self.data_dir, since_hours=24)
        self.assertEqual(digest, "今天做了 A")
        self.assertEqual(n, 1)  # only the recent turn
        # the prompt body should be built from the summary, not raw prose
        body_arg = chat.call_args.args[2]
        self.assertIn("did A", body_arg)

    def test_goal_injected_into_prompt(self):
        from lynx_memory import daily

        now = time.time()
        self._add_turn(now - 60, "u", "a", summary="s")
        m = self._memory()
        try:
            m.set_goal("ship the API")
        finally:
            m.close()
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}, clear=True), mock.patch(
            "lynx_memory.summarizer._chat", return_value="digest"
        ) as chat:
            digest, n, goal = daily.build_digest(self.data_dir, since_hours=24)
        self.assertEqual(goal, "ship the API")
        system_arg = chat.call_args.args[1]
        self.assertIn("ship the API", system_arg)


class NotifyTest(unittest.TestCase):
    def test_backend_autodetect(self):
        from lynx_memory import daily

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(daily.notify_backend(), "")
        with mock.patch.dict(os.environ, {"SERVERCHAN_SENDKEY": "x"}, clear=True):
            self.assertEqual(daily.notify_backend(), "serverchan")
        with mock.patch.dict(os.environ, {"DAILY_WEBHOOK_URL": "http://x"}, clear=True):
            self.assertEqual(daily.notify_backend(), "webhook")
        with mock.patch.dict(
            os.environ, {"DAILY_NOTIFY_BACKEND": "webhook", "SERVERCHAN_SENDKEY": "x"}, clear=True
        ):
            self.assertEqual(daily.notify_backend(), "webhook")  # forced wins

    def test_serverchan_success_and_failure(self):
        from lynx_memory import daily

        class FakeResp:
            def __init__(self, payload):
                self._p = payload

            def read(self):
                return self._p

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.dict(os.environ, {"SERVERCHAN_SENDKEY": "sk"}, clear=True):
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(b'{"code":0}')):
                ok, _ = daily.notify("t", "b")
                self.assertTrue(ok)
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(b'{"code":40001,"message":"bad"}')):
                ok, detail = daily.notify("t", "b")
                self.assertFalse(ok)
                self.assertIn("40001", detail)

    def test_notify_without_backend(self):
        from lynx_memory import daily

        with mock.patch.dict(os.environ, {}, clear=True):
            ok, detail = daily.notify("t", "b")
        self.assertFalse(ok)
        self.assertIn("no notifier", detail)


if __name__ == "__main__":
    unittest.main()
