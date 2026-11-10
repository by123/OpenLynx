import _hermetic  # noqa: F401  (must precede lynx_memory imports)
"""Test: a stored turn is recalled and injected into the hook's stdout output."""
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

# Fixed embedding used for every embed_one call in these tests.
# Cosine similarity between two identical unit vectors = 1.0,
# so the stored turn will always score 1.0 against the query.
FAKE_VEC = [1.0] + [0.0] * 767  # 768-dim


class TestMemoryInjection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _store_turn(self, user_msg: str, assistant_msg: str) -> str:
        """Insert one turn into the isolated test Memory store."""
        with patch("lynx_memory.storage._base.embed_one", return_value=FAKE_VEC):
            from lynx_memory.storage import Memory
            m = Memory(data_dir=self.data_dir)
            try:
                m.ensure_session("sess-past", cwd="/project")
                return m.add_turn(
                    session_id="sess-past",
                    user_msg=user_msg,
                    assistant_msg=assistant_msg,
                    cwd="/project",
                )
            finally:
                m.close()

    def _run_hook(self, prompt: str) -> tuple[int, str]:
        """Run on_prompt._main() with the given prompt, return (exit_code, stdout)."""
        hook_input = json.dumps({
            "session_id": "sess-new",
            "cwd": "/project",
            "transcript_path": "",
            "prompt": prompt,
        })
        fake_stdout = StringIO()

        with (
            patch("lynx_memory.storage._search.embed_one", return_value=FAKE_VEC),
            patch("lynx_memory.config.GLOBAL_DATA_DIR", self.data_dir),
            patch("lynx_memory.config.find_project_root", return_value=None),
            patch("lynx_memory.transcript.persist_last_turn"),
            patch("sys.stdin", StringIO(hook_input)),
            patch("sys.stdout", fake_stdout),
        ):
            from lynx_memory.hooks.on_prompt import _main
            exit_code = _main()

        return exit_code, fake_stdout.getvalue()

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_recalled_turn_is_wrapped_in_memory_tags(self):
        """Hook output is enclosed in <memory>…</memory> when a turn is found."""
        self._store_turn(
            user_msg="How do I configure the database connection?",
            assistant_msg="Set DATABASE_URL in your .env file, e.g. postgresql://localhost/mydb",
        )

        code, output = self._run_hook("What is the database connection string format?")

        self.assertEqual(code, 0)
        self.assertIn("<memory>", output)
        self.assertIn("</memory>", output)

    def test_recalled_turn_content_appears_in_output(self):
        """The stored user question and assistant answer appear verbatim in the injection."""
        self._store_turn(
            user_msg="How do I configure the database connection?",
            assistant_msg="Set DATABASE_URL in your .env file, e.g. postgresql://localhost/mydb",
        )

        _, output = self._run_hook("What is the database connection string format?")

        self.assertIn("DATABASE_URL", output)
        self.assertIn("postgresql://localhost/mydb", output)

    def test_score_is_reported_in_output(self):
        """Each recalled turn is annotated with a score= field."""
        self._store_turn(
            user_msg="How do I configure the database connection?",
            assistant_msg="Set DATABASE_URL in your .env file, e.g. postgresql://localhost/mydb",
        )

        _, output = self._run_hook("What is the database connection string format?")

        self.assertIn("score=", output)

    def test_no_injection_when_store_is_empty(self):
        """When no turns exist the hook produces no output (nothing to inject)."""
        code, output = self._run_hook("What is the database connection string format?")

        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_hook_uses_claude_code_plain_text_format_by_default(self):
        """Without --target=codex the output is plain text, not a JSON envelope."""
        self._store_turn(
            user_msg="How do I configure the database connection?",
            assistant_msg="Set DATABASE_URL in your .env file, e.g. postgresql://localhost/mydb",
        )

        _, output = self._run_hook("What is the database connection string format?")

        # Plain-text format: must NOT be a JSON envelope
        try:
            json.loads(output)
            is_json = True
        except (json.JSONDecodeError, ValueError):
            is_json = False
        self.assertFalse(is_json, "default target should emit plain text, not JSON")


if __name__ == "__main__":
    unittest.main()
