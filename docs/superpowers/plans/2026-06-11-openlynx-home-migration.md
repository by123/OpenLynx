# OpenLynx Home Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move global OpenLynx state and reusable artifacts to `~/.openlynx/`, while Claude Code and Codex use host-specific hooks plus symlinked commands/skills.

**Architecture:** `src/lynx_memory/config.py` owns path constants and legacy detection. `src/lynx_memory/cli.py` owns one-time migration, shared artifact installation, host symlink installation, and uninstall cleanup. Web UI APIs keep using `GLOBAL_DATA_DIR` and `resolve_data_dir`, so tests must prove settings and scopes read/write the new home.

**Tech Stack:** Python 3.10+, argparse CLI, pathlib/shutil filesystem operations, unittest/pytest tests, FastAPI TestClient for Web UI API verification.

---

### Task 1: Path Constants And Legacy Migration Unit

**Files:**
- Modify: `src/lynx_memory/config.py`
- Modify: `src/lynx_memory/cli.py`
- Test: `tests/test_openlynx_home.py`

- [ ] **Step 1: Write failing tests**

Add `tests/test_openlynx_home.py`:

```python
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class OpenLynxHomeTest(unittest.TestCase):
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
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/test_openlynx_home.py -q`

Expected: failures because `LEGACY_GLOBAL_DATA_DIR` and `_migrate_legacy_global_store` do not exist and default path is still `~/.claude/lynx-memory`.

- [ ] **Step 3: Implement constants and migration**

In `src/lynx_memory/config.py`, change global defaults:

```python
OPENLYNX_HOME = Path(os.environ.get("OPENLYNX_HOME", os.path.expanduser("~/.openlynx")))
LEGACY_GLOBAL_DATA_DIR = Path(os.path.expanduser("~/.claude/lynx-memory"))

GLOBAL_DATA_DIR = Path(os.environ.get("LYNX_MEMORY_DIR", str(OPENLYNX_HOME)))
```

Update the module docstring to describe `~/.openlynx/`.

In `src/lynx_memory/cli.py`, import `LEGACY_GLOBAL_DATA_DIR` and add:

```python
def _migrate_legacy_global_store() -> bool:
    if os.environ.get("LYNX_MEMORY_DIR"):
        return False
    if not LEGACY_GLOBAL_DATA_DIR.exists():
        return False
    if GLOBAL_DATA_DIR.exists():
        _print_warn(
            f"Legacy data dir still exists at {LEGACY_GLOBAL_DATA_DIR}; "
            f"using {GLOBAL_DATA_DIR}"
        )
        return False
    GLOBAL_DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(LEGACY_GLOBAL_DATA_DIR), str(GLOBAL_DATA_DIR))
    _print_ok(f"Migrated legacy data dir: {LEGACY_GLOBAL_DATA_DIR} → {GLOBAL_DATA_DIR}")
    return True
```

Call `_migrate_legacy_global_store()` in `cmd_init` before `ensure_dirs()`.

- [ ] **Step 4: Run tests and verify pass**

Run: `uv run pytest tests/test_openlynx_home.py -q`

Expected: all tests pass.

### Task 2: Shared Commands, Skills, And Host Symlinks

**Files:**
- Modify: `src/lynx_memory/config.py`
- Modify: `src/lynx_memory/cli.py`
- Test: `tests/test_openlynx_home.py`

- [ ] **Step 1: Write failing tests**

Append tests to `tests/test_openlynx_home.py`:

```python
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
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/test_openlynx_home.py -q`

Expected: failures because shared command constants and symlink logic do not exist.

- [ ] **Step 3: Implement shared artifact helpers**

In `src/lynx_memory/config.py`, add:

```python
OPENLYNX_COMMANDS_DIR = GLOBAL_DATA_DIR / "commands"
OPENLYNX_SKILLS_DIR = GLOBAL_DATA_DIR / "skills"
```

In `src/lynx_memory/cli.py`, import those constants and add shared link helpers:

```python
CODEX_COMMANDS_DIR = CODEX_HOME / "commands"
CLAUDE_SKILLS_DIR = Path(os.path.expanduser("~/.claude/skills"))
CODEX_SKILLS_DIR = CODEX_HOME / "skills"

def _install_shared_file_link(src_text: str, shared_path: Path, host_path: Path) -> bool:
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    host_path.parent.mkdir(parents=True, exist_ok=True)
    changed = False
    if not shared_path.exists() or shared_path.read_text(encoding="utf-8") != src_text:
        shared_path.write_text(src_text, encoding="utf-8")
        changed = True
    if host_path.is_symlink() and host_path.resolve() == shared_path.resolve():
        return changed
    if host_path.exists() or host_path.is_symlink():
        bak = host_path.with_suffix(f"{host_path.suffix}.bak.{int(time.time())}")
        shutil.copy2(host_path, bak, follow_symlinks=False)
        host_path.unlink()
    try:
        host_path.symlink_to(shared_path)
    except OSError:
        _print_warn(f"Could not symlink {host_path}; copied shared file instead.")
        host_path.write_text(src_text, encoding="utf-8")
    return True
```

Update `_install_slash_command(name, target_dir=None)` so it writes to
`OPENLYNX_COMMANDS_DIR / name` and links into `target_dir or CLAUDE_COMMANDS_DIR`.
Update `_remove_slash_command` so it removes only symlinks resolving into
`OPENLYNX_COMMANDS_DIR`.

Add `_install_codex_commands()` and call it from `_install_codex()` so Codex gets
the same command links when its commands directory is supported.

- [ ] **Step 4: Run tests and verify pass**

Run: `uv run pytest tests/test_openlynx_home.py -q`

Expected: all tests pass.

### Task 3: Web UI Reads And Writes The New Global Home

**Files:**
- Modify: `src/lynx_memory/web.py` only if tests expose direct-path assumptions
- Test: `tests/test_web_openlynx_home.py`

- [ ] **Step 1: Write failing/protective Web UI tests**

Add `tests/test_web_openlynx_home.py`:

```python
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class WebOpenLynxHomeTest(unittest.TestCase):
    def test_settings_api_reads_and_writes_global_openlynx_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".openlynx"
            home.mkdir()
            (home / ".env").write_text("TOP_K=3\nMIN_SCORE=0.5\n")

            from lynx_memory import web
            with mock.patch.object(web, "GLOBAL_DATA_DIR", home):
                app = web.create_app()
                from fastapi.testclient import TestClient
                client = TestClient(app)

                settings = client.get("/api/settings").json()
                self.assertEqual(settings["top_k"], 3)
                self.assertEqual(settings["min_score"], 0.5)

                payload = settings | {
                    "top_k": 8,
                    "voyage_api_key": "pa-test",
                    "anthropic_api_key": "",
                    "openai_api_key": "",
                }
                response = client.put("/api/settings", json=payload)
                self.assertEqual(response.status_code, 200)
                text = (home / ".env").read_text()
                self.assertIn("TOP_K=8", text)
                self.assertIn("VOYAGE_API_KEY=pa-test", text)

    def test_scopes_api_reports_global_openlynx_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".openlynx"
            home.mkdir()

            from lynx_memory import web
            with mock.patch.object(web, "GLOBAL_DATA_DIR", home), mock.patch.object(
                web, "find_project_root", return_value=None
            ):
                app = web.create_app()
                from fastapi.testclient import TestClient
                client = TestClient(app)
                scopes = client.get("/api/scopes").json()

            self.assertEqual(scopes["global_dir"], str(home))
            self.assertFalse(scopes["project"])
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_web_openlynx_home.py -q`

Expected: pass if Web UI is already correctly using `GLOBAL_DATA_DIR`; fail only if hidden path assumptions remain.

- [ ] **Step 3: Fix Web UI path assumptions if needed**

If tests fail because `web.py` uses stale direct paths, replace direct path construction with `paths_for(GLOBAL_DATA_DIR)` or `GLOBAL_DATA_DIR / ".env"` consistently. The settings API must read and write the patched `GLOBAL_DATA_DIR`.

- [ ] **Step 4: Run tests again**

Run: `uv run pytest tests/test_web_openlynx_home.py -q`

Expected: all tests pass.

### Task 4: CLI Status, Doctor, Uninstall, And Docs

**Files:**
- Modify: `src/lynx_memory/cli.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `src/lynx_memory/assets/commands/lynx-memory-status.md`
- Modify: `src/lynx_memory/assets/commands/lynx-memory-delete.md`
- Test: existing test suite

- [ ] **Step 1: Update CLI user-facing paths**

Update `cmd_status` output to print `openlynx home`, `legacy dir`, `env file`,
and active database. Update `cmd_uninstall` to say data remains at
`~/.openlynx/`. Update `cmd_doctor` data-dir output through `DATA_DIR`, which now
resolves to `~/.openlynx/`.

- [ ] **Step 2: Update docs and bundled command text**

Replace global-store references from `~/.claude/lynx-memory/` to
`~/.openlynx/` in README files and bundled commands. Preserve references to
`~/.claude/settings.json`, `~/.claude/commands/`, and `.claude/commands/` where
they describe Claude host integration.

- [ ] **Step 3: Run focused tests**

Run:

```bash
uv run pytest tests/test_openlynx_home.py tests/test_web_openlynx_home.py tests/test_cli_codex_config.py -q
```

Expected: all tests pass.

### Task 5: Full Verification

**Files:**
- No new files expected

- [ ] **Step 1: Run full Python test suite**

Run: `uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Run CLI status in isolated env if practical**

Run: `PYTHONPATH=src python3 -m lynx_memory.cli status`

Expected: output shows `openlynx home` and no traceback.

- [ ] **Step 3: Review diff**

Run: `git diff --stat` and `git diff --check`

Expected: no whitespace errors and only planned files changed.
