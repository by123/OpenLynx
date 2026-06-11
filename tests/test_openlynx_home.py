import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class OpenLynxHomeTest(unittest.TestCase):
    def tearDown(self):
        if "lynx_memory.config" in sys.modules:
            import lynx_memory.config as config

            importlib.reload(config)
        if "lynx_memory.cli" in sys.modules:
            import lynx_memory.cli as cli

            importlib.reload(cli)

    def test_default_global_dir_is_openlynx_home(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "os.path.expanduser",
            side_effect=lambda p: p.replace("~", "/home/tester"),
        ):
            import lynx_memory.config as config

            config = importlib.reload(config)

        self.assertEqual(config.GLOBAL_DATA_DIR, Path("/home/tester/.openlynx"))
        self.assertEqual(config.ENV_FILE, Path("/home/tester/.openlynx/.env"))
        self.assertEqual(config.DB_PATH, Path("/home/tester/.openlynx/db/memory.db"))
        self.assertEqual(
            config.LEGACY_GLOBAL_DATA_DIR,
            Path("/home/tester/.claude/lynx-memory"),
        )

    def test_lynx_memory_dir_still_overrides_global_dir(self):
        with mock.patch.dict(os.environ, {"LYNX_MEMORY_DIR": "/tmp/custom-openlynx"}):
            import lynx_memory.config as config

            config = importlib.reload(config)

        self.assertEqual(config.GLOBAL_DATA_DIR, Path("/tmp/custom-openlynx"))

    def test_migrates_legacy_dir_when_new_home_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".claude" / "lynx-memory"
            home = root / ".openlynx"
            legacy.mkdir(parents=True)
            (legacy / ".env").write_text("VOYAGE_API_KEY=old\n")

            from lynx_memory import cli

            with (
                mock.patch.object(cli, "DATA_DIR", home),
                mock.patch.object(cli, "GLOBAL_DATA_DIR", home),
                mock.patch.object(cli, "ENV_FILE", home / ".env"),
                mock.patch.object(cli, "LEGACY_GLOBAL_DATA_DIR", legacy),
            ):
                changed = cli._migrate_legacy_global_store()

            self.assertTrue(changed)
            self.assertFalse(legacy.exists())
            self.assertEqual((home / ".env").read_text(), "VOYAGE_API_KEY=old\n")

    def test_does_not_merge_when_new_home_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".claude" / "lynx-memory"
            home = root / ".openlynx"
            legacy.mkdir(parents=True)
            home.mkdir()
            (legacy / ".env").write_text("VOYAGE_API_KEY=old\n")
            (home / ".env").write_text("VOYAGE_API_KEY=new\n")

            from lynx_memory import cli

            with (
                mock.patch.object(cli, "DATA_DIR", home),
                mock.patch.object(cli, "GLOBAL_DATA_DIR", home),
                mock.patch.object(cli, "ENV_FILE", home / ".env"),
                mock.patch.object(cli, "LEGACY_GLOBAL_DATA_DIR", legacy),
            ):
                changed = cli._migrate_legacy_global_store()

            self.assertFalse(changed)
            self.assertTrue(legacy.exists())
            self.assertEqual((home / ".env").read_text(), "VOYAGE_API_KEY=new\n")

    def test_install_slash_command_writes_shared_file_and_host_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_commands = root / ".openlynx" / "commands"
            host_commands = root / ".claude" / "commands"

            from lynx_memory import cli

            with (
                mock.patch.object(cli, "OPENLYNX_COMMANDS_DIR", shared_commands),
                mock.patch.object(cli, "CLAUDE_COMMANDS_DIR", host_commands),
                mock.patch.object(cli, "_read_bundled_command", return_value="body\n"),
            ):
                changed = cli._install_slash_command("lynx-memory-status.md")

            shared = shared_commands / "lynx-memory-status.md"
            host = host_commands / "lynx-memory-status.md"
            self.assertTrue(changed)
            self.assertEqual(shared.read_text(), "body\n")
            self.assertTrue(host.is_symlink())
            self.assertEqual(host.resolve(), shared.resolve())

    def test_install_slash_command_is_idempotent_for_existing_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_commands = root / ".openlynx" / "commands"
            host_commands = root / ".claude" / "commands"

            from lynx_memory import cli

            with (
                mock.patch.object(cli, "OPENLYNX_COMMANDS_DIR", shared_commands),
                mock.patch.object(cli, "CLAUDE_COMMANDS_DIR", host_commands),
                mock.patch.object(cli, "_read_bundled_command", return_value="body\n"),
            ):
                self.assertTrue(cli._install_slash_command("lynx-memory-status.md"))
                self.assertFalse(cli._install_slash_command("lynx-memory-status.md"))

    def test_uninstall_removes_only_openlynx_managed_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_commands = root / ".openlynx" / "commands"
            host_commands = root / ".claude" / "commands"

            from lynx_memory import cli

            with (
                mock.patch.object(cli, "OPENLYNX_COMMANDS_DIR", shared_commands),
                mock.patch.object(cli, "CLAUDE_COMMANDS_DIR", host_commands),
                mock.patch.object(cli, "_read_bundled_command", return_value="body\n"),
            ):
                cli._install_slash_command("lynx-memory-status.md")
                removed = cli._remove_slash_command("lynx-memory-status.md")

            self.assertTrue(removed)
            self.assertFalse((host_commands / "lynx-memory-status.md").exists())
            self.assertTrue((shared_commands / "lynx-memory-status.md").exists())

    def test_install_openlynx_skill_writes_shared_dir_and_host_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_skills = root / ".openlynx" / "skills"
            host_skills = root / ".claude" / "skills"

            from lynx_memory import cli

            with (
                mock.patch.object(cli, "OPENLYNX_SKILLS_DIR", shared_skills),
                mock.patch.object(cli, "CLAUDE_SKILLS_DIR", host_skills),
                mock.patch.object(
                    cli,
                    "_read_bundled_skill_files",
                    return_value={"SKILL.md": "# OpenLynx\n"},
                ),
            ):
                changed = cli._install_openlynx_skill()

            shared = shared_skills / "openlynx"
            host = host_skills / "openlynx"
            self.assertTrue(changed)
            self.assertEqual((shared / "SKILL.md").read_text(), "# OpenLynx\n")
            self.assertTrue(host.is_symlink())
            self.assertEqual(host.resolve(), shared.resolve())

    def test_uninstall_removes_only_openlynx_managed_skill_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_skills = root / ".openlynx" / "skills"
            host_skills = root / ".claude" / "skills"

            from lynx_memory import cli

            with (
                mock.patch.object(cli, "OPENLYNX_SKILLS_DIR", shared_skills),
                mock.patch.object(cli, "CLAUDE_SKILLS_DIR", host_skills),
                mock.patch.object(
                    cli,
                    "_read_bundled_skill_files",
                    return_value={"SKILL.md": "# OpenLynx\n"},
                ),
            ):
                cli._install_openlynx_skill()
                removed = cli._remove_openlynx_skill()

            self.assertTrue(removed)
            self.assertFalse((host_skills / "openlynx").exists())
            self.assertTrue((shared_skills / "openlynx" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
