import _hermetic  # noqa: F401  (must precede lynx_memory imports)
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lynx_memory import cli


class CodexConfigTest(unittest.TestCase):
    def test_migrates_legacy_codex_hooks_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("[features]\ncodex_hooks = true\n")

            with mock.patch.object(cli, "CODEX_HOME", Path(tmp)), mock.patch.object(
                cli, "CODEX_CONFIG_PATH", config_path
            ):
                changed = cli._ensure_codex_feature_flag()

            self.assertTrue(changed)
            self.assertEqual(config_path.read_text(), "[features]\nhooks = true\n")
            self.assertEqual(len(list(Path(tmp).glob("config.toml.bak.*"))), 1)

    def test_leaves_existing_hooks_flag_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("[features]\nhooks = true\n")

            with mock.patch.object(cli, "CODEX_HOME", Path(tmp)), mock.patch.object(
                cli, "CODEX_CONFIG_PATH", config_path
            ):
                changed = cli._ensure_codex_feature_flag()

            self.assertFalse(changed)
            self.assertEqual(config_path.read_text(), "[features]\nhooks = true\n")
            self.assertEqual(list(Path(tmp).glob("config.toml.bak.*")), [])


if __name__ == "__main__":
    unittest.main()
