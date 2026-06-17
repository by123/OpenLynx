"""lynx-memory CLI: init, uninstall, status, doctor, merge."""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

from . import __version__
from .config import (
    CLAUDE_SETTINGS_PATH,
    CODEX_CONFIG_PATH,
    CODEX_HOME,
    CODEX_HOOKS_PATH,
    DATA_DIR,
    DB_PATH,
    ENV_FILE,
    GLOBAL_DATA_DIR,
    LEGACY_GLOBAL_DATA_DIR,
    LOG_PATH,
    OPENLYNX_COMMANDS_DIR,
    OPENLYNX_SKILLS_DIR,
    PROJECT_MARKER,
    SUPPORTED_TARGETS,
    ensure_dirs,
    find_project_root,
    load_env,
    paths_for,
    resolve_data_dir,
)

CLAUDE_COMMANDS_DIR = Path(os.path.expanduser("~/.claude/commands"))
CLAUDE_SKILLS_DIR = Path(os.path.expanduser("~/.claude/skills"))
SLASH_COMMAND_NAMES = (
    "lynx-memory-status.md",
    "lynx-memory-pull-global.md",
    "lynx-memory-push-global.md",
    "lynx-memory-delete.md",
    "lynx-memory-history.md",
    "lynx-memory-goal.md",
)
OPENLYNX_SKILL_NAME = "openlynx"

HOOK_COMMANDS = {
    "UserPromptSubmit": ("lynx-memory-on-prompt", 10000),
    "Stop": ("lynx-memory-on-stop", 15000),
    "SessionEnd": ("lynx-memory-on-session-end", 60000),
}

# Codex CLI has no SessionEnd; we use SessionStart to summarize the previous
# session on entry. Hook commands also receive `--target codex` so the entry
# points pick the right output format and session-summary semantics.
CODEX_HOOK_COMMANDS = {
    "UserPromptSubmit": ("lynx-memory-on-prompt --target codex", 10),
    "Stop": ("lynx-memory-on-stop --target codex", 15),
    "SessionStart": ("lynx-memory-on-session-end --target codex", 60),
}

HOOK_MARKER_COMMANDS = {cmd for cmd, _ in HOOK_COMMANDS.values()}
CODEX_HOOK_MARKER_COMMANDS = {cmd for cmd, _ in CODEX_HOOK_COMMANDS.values()}


def _migrate_legacy_global_store() -> bool:
    """Move the old Claude-owned global store to the OpenLynx home once."""
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


# --------------------------------------------------------------------- helpers

def _print_ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _print_warn(msg: str) -> None:
    print(f"  ! {msg}")


def _print_err(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


def _build_web_ui() -> bool:
    """Build the web UI if the web/ directory is present and not already built.

    For pip-installed packages, use the pre-built assets from the package.
    For development, try to build from the web/ directory.
    """
    # First check if pre-built assets are available (pip install path)
    assets_web = Path(__file__).parent / "assets" / "web"
    index_html = assets_web / "index.html"
    if index_html.exists():
        _print_ok("Web UI ready (pre-built)")
        return True

    # Development mode: try to build from web/ directory
    repo_root = Path(__file__).parent.parent.parent
    web_dir = repo_root / "web"
    if not web_dir.is_dir():
        _print_warn("web/ directory not found — Web UI not available")
        return False

    node_modules = web_dir / "node_modules"
    if not node_modules.is_dir():
        _print_warn("web/node_modules not found — running npm install...")
        try:
            result = subprocess.run(
                ["npm", "install"],
                cwd=str(web_dir),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                _print_err(f"npm install failed: {result.stderr.strip()}")
                return False
            _print_ok("npm install completed")
        except subprocess.TimeoutExpired:
            _print_err("npm install timed out (5 min)")
            return False
        except FileNotFoundError:
            _print_err("npm not found — install Node.js to build web UI")
            return False

    _print_ok("Building web UI...")
    try:
        result = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(web_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            _print_err(f"npm run build failed: {result.stderr.strip()}")
            return False
        _print_ok("Web UI built successfully")
        return True
    except subprocess.TimeoutExpired:
        _print_err("npm run build timed out (2 min)")
        return False
    except FileNotFoundError:
        _print_err("npm not found — install Node.js to build web UI")
        return False


def _read_settings() -> dict:
    if not CLAUDE_SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(CLAUDE_SETTINGS_PATH.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"Failed to parse {CLAUDE_SETTINGS_PATH}: {e}\n"
            "Please fix the JSON manually before running this command."
        )


def _backup_settings() -> Path | None:
    if not CLAUDE_SETTINGS_PATH.exists():
        return None
    bak = CLAUDE_SETTINGS_PATH.with_suffix(f".json.bak.{int(time.time())}")
    shutil.copy2(CLAUDE_SETTINGS_PATH, bak)
    return bak


def _write_settings(settings: dict) -> None:
    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


def _hook_block_has_command(block: dict, command: str) -> bool:
    for h in block.get("hooks", []) or []:
        if h.get("type") == "command" and h.get("command") == command:
            return True
    return False


def _ensure_hook(settings: dict, event: str, command: str, timeout: int) -> bool:
    """Insert our hook command for `event` if not already present.

    Returns True if a change was made, False if already present.
    """
    hooks = settings.setdefault("hooks", {})
    blocks = hooks.setdefault(event, [])

    for block in blocks:
        if _hook_block_has_command(block, command):
            return False

    blocks.append(
        {
            "hooks": [
                {"type": "command", "command": command, "timeout": timeout}
            ]
        }
    )
    return True


def _remove_hook(settings: dict, event: str, command: str) -> bool:
    hooks = settings.get("hooks", {})
    blocks = hooks.get(event)
    if not blocks:
        return False
    new_blocks = []
    changed = False
    for block in blocks:
        inner = block.get("hooks", []) or []
        kept = [h for h in inner if not (h.get("type") == "command" and h.get("command") == command)]
        if len(kept) != len(inner):
            changed = True
        if kept:
            new_blocks.append({**block, "hooks": kept})
    if changed:
        if new_blocks:
            hooks[event] = new_blocks
        else:
            hooks.pop(event, None)
    if hooks == {}:
        settings.pop("hooks", None)
    return changed


# --------------------------------------------------------------------- codex

def _read_codex_hooks() -> dict:
    if not CODEX_HOOKS_PATH.exists():
        return {"hooks": {}}
    try:
        return json.loads(CODEX_HOOKS_PATH.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"Failed to parse {CODEX_HOOKS_PATH}: {e}\n"
            "Please fix the JSON manually before running this command."
        )


def _write_codex_hooks(payload: dict) -> None:
    CODEX_HOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_HOOKS_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def _ensure_codex_hook(payload: dict, event: str, command: str, timeout: int) -> bool:
    hooks = payload.setdefault("hooks", {})
    blocks = hooks.setdefault(event, [])
    for block in blocks:
        for h in block.get("hooks", []) or []:
            if h.get("type") == "command" and h.get("command") == command:
                return False
    blocks.append({
        "hooks": [{"type": "command", "command": command, "timeout": timeout}]
    })
    return True


def _remove_codex_hook(payload: dict, event: str, command: str) -> bool:
    hooks = payload.get("hooks", {})
    blocks = hooks.get(event)
    if not blocks:
        return False
    new_blocks = []
    changed = False
    for block in blocks:
        inner = block.get("hooks", []) or []
        kept = [h for h in inner if not (h.get("type") == "command" and h.get("command") == command)]
        if len(kept) != len(inner):
            changed = True
        if kept:
            new_blocks.append({**block, "hooks": kept})
    if changed:
        if new_blocks:
            hooks[event] = new_blocks
        else:
            hooks.pop(event, None)
    return changed


def _ensure_codex_feature_flag() -> bool:
    """Make sure ~/.codex/config.toml has `[features] hooks = true`.

    We do a minimal text-level merge: migrate the deprecated `codex_hooks`
    flag when present, add `hooks` to an existing `[features]` block when
    possible, and otherwise append a new block. We never reformat the rest of
    the file.
    """
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    text = CODEX_CONFIG_PATH.read_text() if CODEX_CONFIG_PATH.exists() else ""
    lines = text.splitlines(keepends=True)
    in_features = False
    has_features = False
    has_hooks = False
    has_legacy_hooks = False
    changed = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = stripped == "[features]"
            has_features = has_features or in_features
            continue
        if not in_features or stripped.startswith("#"):
            continue
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key == "hooks":
            has_hooks = True
        elif key == "codex_hooks":
            has_legacy_hooks = True

    if has_hooks and not has_legacy_hooks:
        return False

    if has_features:
        out = []
        in_features = False
        inserted = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if in_features and not has_hooks and not inserted:
                    out.append("hooks = true\n")
                    inserted = True
                    changed = True
                in_features = stripped == "[features]"
                out.append(line)
                continue
            if in_features and not stripped.startswith("#") and "=" in stripped:
                key, rest = line.split("=", 1)
                if key.strip() == "codex_hooks":
                    changed = True
                    if has_hooks:
                        continue
                    out.append(f"{key.replace('codex_hooks', 'hooks')}={rest}")
                    has_hooks = True
                    continue
            out.append(line)
        if in_features and not has_hooks and not inserted:
            out.append("hooks = true\n")
            changed = True
        new_text = "".join(out)
    else:
        suffix = "\n" if text and not text.endswith("\n") else ""
        new_text = f"{text}{suffix}\n[features]\nhooks = true\n"
        changed = True

    bak = None
    if changed:
        if CODEX_CONFIG_PATH.exists():
            bak = CODEX_CONFIG_PATH.with_suffix(f".toml.bak.{int(time.time())}")
            shutil.copy2(CODEX_CONFIG_PATH, bak)
        CODEX_CONFIG_PATH.write_text(new_text)
    if bak:
        _print_ok(f"Backed up config.toml → {bak.name}")
    return changed


def _ensure_env_file() -> bool:
    """Make sure ENV_FILE exists with at least a VOYAGE_API_KEY line.

    If the file is missing or has no VOYAGE_API_KEY, prompt the user.
    Returns True if it now contains a non-empty key.
    """
    ensure_dirs()
    existing = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    if not existing.get("VOYAGE_API_KEY"):
        try:
            key = input(
                "Enter your VOYAGE_API_KEY (get one at https://www.voyageai.com/): "
            ).strip()
        except EOFError:
            key = ""
        if not key:
            _print_warn(
                "VOYAGE_API_KEY not provided. Hooks will fail until you set it in "
                f"{ENV_FILE}"
            )
            existing.setdefault("VOYAGE_API_KEY", "")
        else:
            existing["VOYAGE_API_KEY"] = key

    existing.setdefault("TOP_K", "5")
    existing.setdefault("MIN_SCORE", "0.7")
    existing.setdefault("SUMMARY_ENABLED", "1")
    existing.setdefault("SUMMARY_BACKEND", "auto")
    existing.setdefault("GOAL_GATING_ENABLED", "1")
    existing.setdefault("GOAL_STRICTNESS", "strict")

    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_FILE, 0o600)
    except Exception:
        pass
    return bool(existing.get("VOYAGE_API_KEY"))


# --------------------------------------------------------------------- commands

def _install_claude_code() -> None:
    print(f"  settings: {CLAUDE_SETTINGS_PATH}")
    settings = _read_settings()
    bak = _backup_settings()
    if bak:
        _print_ok(f"Backed up settings.json → {bak.name}")

    changed = False
    for event, (command, timeout) in HOOK_COMMANDS.items():
        if _ensure_hook(settings, event, command, timeout):
            _print_ok(f"Registered Claude Code hook: {event} → {command}")
            changed = True
        else:
            _print_ok(f"Claude Code hook already registered: {event}")

    if changed:
        _write_settings(settings)
        _print_ok(f"Updated {CLAUDE_SETTINGS_PATH}")

    for name in SLASH_COMMAND_NAMES:
        if _install_slash_command(name):
            _print_ok(f"Installed slash command: /{name[:-3]}")
    if _install_openlynx_skill():
        _print_ok(f"Installed skill link: {CLAUDE_SKILLS_DIR / OPENLYNX_SKILL_NAME}")


def _install_codex_commands() -> None:
    codex_commands_dir = CODEX_HOME / "commands"
    for name in SLASH_COMMAND_NAMES:
        if _install_slash_command(name, codex_commands_dir):
            _print_ok(f"Installed Codex command link: {codex_commands_dir / name}")


def _install_codex() -> None:
    print(f"  hooks:    {CODEX_HOOKS_PATH}")
    print(f"  config:   {CODEX_CONFIG_PATH}")

    if _ensure_codex_feature_flag():
        _print_ok(f"Enabled `[features] hooks = true` in {CODEX_CONFIG_PATH.name}")
    else:
        _print_ok("`hooks` already enabled")

    payload = _read_codex_hooks()
    changed = False
    for event, (command, timeout) in CODEX_HOOK_COMMANDS.items():
        if _ensure_codex_hook(payload, event, command, timeout):
            _print_ok(f"Registered Codex hook: {event} → {command}")
            changed = True
        else:
            _print_ok(f"Codex hook already registered: {event}")
    if changed:
        _write_codex_hooks(payload)
        _print_ok(f"Updated {CODEX_HOOKS_PATH}")

    _install_codex_commands()
    codex_skills_dir = CODEX_HOME / "skills"
    if _install_openlynx_skill(codex_skills_dir):
        _print_ok(f"Installed Codex skill link: {codex_skills_dir / OPENLYNX_SKILL_NAME}")
    print("  ! Restart any running `codex` process for hooks to take effect.")


def _any_summary_key() -> bool:
    """True if any summarization/judge provider key is configured."""
    from .summarizer import _PROVIDER_KEY_ENVS

    return any(
        os.environ.get(env, "").strip()
        for envs in _PROVIDER_KEY_ENVS.values()
        for env in envs
    )


def _set_goal_for(data_dir: Path, text: str) -> None:
    from .storage import Memory

    mem = Memory(data_dir=data_dir)
    try:
        mem.set_goal(text)
    finally:
        mem.close()


def _prompt_goal(scope_label: str) -> str:
    """Interactively ask for an optional goal. Empty/no-stdin → skip."""
    print()
    print(f"Optionally set a {scope_label} memory goal.")
    print("When set, OpenLynx only stores turns relevant to it (strict, LLM-judged)")
    print("and focuses summaries on it. Press Enter to skip (memory behaves as before).")
    try:
        return input("Goal: ").strip()
    except EOFError:
        return ""


def _apply_init_goal(data_dir: Path, scope_label: str, goal_arg: str | None) -> None:
    """Resolve the goal for `init`/`init-project` (flag or prompt) and store it.

    On re-runs the existing goal is shown; pressing Enter at the prompt keeps it.
    """
    if goal_arg:
        goal = goal_arg.strip()
    else:
        from .goals import get_goal_text

        existing = get_goal_text(data_dir)
        if existing:
            print()
            print(f"Current {scope_label} goal: {existing}")
            print("Press Enter to keep it, or type a new goal to replace it.")
            try:
                goal = input("Goal: ").strip()
            except EOFError:
                goal = ""
        else:
            goal = _prompt_goal(scope_label)
    if not goal:
        return
    _set_goal_for(data_dir, goal)
    _print_ok(f"Recorded {scope_label} goal.")
    load_env(data_dir)
    if not _any_summary_key():
        _print_warn(
            "No summarization LLM key set (OPENAI/DEEPSEEK/QWEN). Relevance gating "
            "and goal-aware summaries stay inactive until one is configured."
        )


def cmd_init(args: argparse.Namespace) -> int:
    targets = _resolve_targets(args.target)

    print(f"lynx-memory v{__version__} — installing for: {', '.join(targets)}")
    print(f"  data dir: {DATA_DIR}")
    print()

    _migrate_legacy_global_store()
    ensure_dirs()
    OPENLYNX_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    OPENLYNX_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    _print_ok(f"Created {DATA_DIR}")

    has_key = _ensure_env_file()
    if has_key:
        _print_ok(f"Wrote {ENV_FILE}")
    else:
        _print_warn(f"Wrote {ENV_FILE} (VOYAGE_API_KEY still empty)")

    _apply_init_goal(GLOBAL_DATA_DIR, "global", getattr(args, "goal", None))

    if "claude_code" in targets:
        print()
        _install_claude_code()
    if "codex" in targets:
        print()
        _install_codex()

    print()
    print("Done. Open a new session in your CLI and chat for a few turns.")
    print("Verify with:  lynx-memory status")
    return 0


def _resolve_targets(target_arg: str) -> list:
    if target_arg == "all":
        return list(SUPPORTED_TARGETS)
    if target_arg in SUPPORTED_TARGETS:
        return [target_arg]
    raise SystemExit(f"Unknown --target: {target_arg!r} (allowed: {SUPPORTED_TARGETS} or 'all')")


def _uninstall_claude_code() -> None:
    if not CLAUDE_SETTINGS_PATH.exists():
        _print_warn(f"{CLAUDE_SETTINGS_PATH} does not exist; nothing to do.")
    else:
        settings = _read_settings()
        bak = _backup_settings()
        if bak:
            _print_ok(f"Backed up settings.json → {bak.name}")

        any_removed = False
        for event, (command, _) in HOOK_COMMANDS.items():
            if _remove_hook(settings, event, command):
                _print_ok(f"Removed Claude Code hook: {event} → {command}")
                any_removed = True

        if any_removed:
            _write_settings(settings)
            _print_ok(f"Updated {CLAUDE_SETTINGS_PATH}")
        else:
            _print_warn("No lynx-memory hooks were present in settings.json.")

    for name in SLASH_COMMAND_NAMES:
        if _remove_slash_command(name):
            _print_ok(f"Removed slash command: {CLAUDE_COMMANDS_DIR / name}")
    if _remove_openlynx_skill():
        _print_ok(f"Removed skill link: {CLAUDE_SKILLS_DIR / OPENLYNX_SKILL_NAME}")


def _uninstall_codex() -> None:
    codex_commands_dir = CODEX_HOME / "commands"
    if not CODEX_HOOKS_PATH.exists():
        _print_warn(f"{CODEX_HOOKS_PATH} does not exist; nothing to do.")
    else:
        payload = _read_codex_hooks()
        any_removed = False
        for event, (command, _) in CODEX_HOOK_COMMANDS.items():
            if _remove_codex_hook(payload, event, command):
                _print_ok(f"Removed Codex hook: {event} → {command}")
                any_removed = True
        if any_removed:
            _write_codex_hooks(payload)
            _print_ok(f"Updated {CODEX_HOOKS_PATH}")
        else:
            _print_warn("No lynx-memory hooks were present in hooks.json.")

    for name in SLASH_COMMAND_NAMES:
        if _remove_slash_command(name, codex_commands_dir):
            _print_ok(f"Removed Codex command link: {codex_commands_dir / name}")
    codex_skills_dir = CODEX_HOME / "skills"
    if _remove_openlynx_skill(codex_skills_dir):
        _print_ok(f"Removed Codex skill link: {codex_skills_dir / OPENLYNX_SKILL_NAME}")


def cmd_uninstall(args: argparse.Namespace) -> int:
    targets = _resolve_targets(args.target)
    print(f"lynx-memory — removing hooks for: {', '.join(targets)}")

    if "claude_code" in targets:
        _uninstall_claude_code()
    if "codex" in targets:
        _uninstall_codex()

    print()
    print(f"Your memory data is preserved at: {DATA_DIR}")
    print("Delete it manually if you want a clean slate:")
    print(f"  rm -rf {DATA_DIR}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(f"lynx-memory v{__version__}")
    cwd = Path.cwd()
    proj = find_project_root(cwd)
    active = resolve_data_dir(cwd)
    active_paths = paths_for(active)
    scope = "project" if proj else "global"

    print(f"  cwd            : {cwd}")
    print(f"  scope          : {scope}  (active dir: {active})")
    if proj:
        print(f"  project marker : {proj}")

    from .goals import gating_enabled, get_goal_text, strictness

    load_env(active)  # so GOAL_* settings reflect this scope's .env
    goal_text = get_goal_text(active)
    if goal_text:
        gate = "on" if gating_enabled() else "off"
        print(f"  goal ({scope})    : {goal_text}")
        print(f"  goal gating    : {gate} (strictness={strictness()})")
    else:
        print(f"  goal ({scope})    : (none — storing all turns)")
    print(f"  openlynx home  : {DATA_DIR}  (exists={DATA_DIR.exists()})")
    print(f"  legacy dir     : {LEGACY_GLOBAL_DATA_DIR}  (exists={LEGACY_GLOBAL_DATA_DIR.exists()})")
    print(f"  env file       : {ENV_FILE}  (exists={ENV_FILE.exists()})")
    print(f"  database       : {active_paths['db_path']}  (exists={active_paths['db_path'].exists()})")
    print(f"  hook log       : {LOG_PATH}")
    print(f"  settings.json  : {CLAUDE_SETTINGS_PATH}  (exists={CLAUDE_SETTINGS_PATH.exists()})")

    load_env()
    have_key = bool(os.environ.get("VOYAGE_API_KEY"))
    print(f"  VOYAGE_API_KEY : {'set' if have_key else 'MISSING'}")

    if CLAUDE_SETTINGS_PATH.exists():
        try:
            settings = _read_settings()
        except SystemExit as e:
            _print_err(str(e))
            return 1
        print("  Claude Code hooks:")
        for event, (command, _) in HOOK_COMMANDS.items():
            blocks = settings.get("hooks", {}).get(event, [])
            present = any(_hook_block_has_command(b, command) for b in blocks)
            print(f"    {event:<18} {'✓' if present else '✗'}  {command}")

    if CODEX_HOOKS_PATH.exists():
        try:
            payload = _read_codex_hooks()
        except SystemExit as e:
            _print_err(str(e))
            return 1
        print(f"  Codex hooks ({CODEX_HOOKS_PATH}):")
        for event, (command, _) in CODEX_HOOK_COMMANDS.items():
            blocks = payload.get("hooks", {}).get(event, [])
            present = any(_hook_block_has_command(b, command) for b in blocks)
            print(f"    {event:<18} {'✓' if present else '✗'}  {command}")

    if active_paths["db_path"].exists() and have_key:
        try:
            from .storage import Memory
            mem = Memory(data_dir=active)
            try:
                stats = mem.stats()
            finally:
                mem.close()
            print(f"  stats ({scope}):")
            for k, v in stats.items():
                print(f"    {k:<20} {v}")
        except Exception as e:
            _print_warn(f"Could not read stats: {e}")
    return 0


def cmd_init_project(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser().resolve() if args.path else Path.cwd()
    if not target.exists():
        _print_err(f"Path does not exist: {target}")
        return 1
    if not target.is_dir():
        _print_err(f"Path is not a directory: {target}")
        return 1

    marker = target / PROJECT_MARKER
    if marker.exists():
        _print_warn(f"Project store already initialised at {marker}")
    else:
        marker.mkdir(parents=True)
        _print_ok(f"Created {marker}")

    (marker / "db").mkdir(parents=True, exist_ok=True)

    gitignore = marker / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n!.gitignore\n")
        _print_ok(f"Wrote {gitignore} (project memory is git-ignored by default)")

    project_commands_dir = target / ".claude" / "commands"
    for name in SLASH_COMMAND_NAMES:
        if _install_slash_command(name, project_commands_dir):
            _print_ok(f"Installed slash command: /{name[:-3]} → {project_commands_dir / name}")

    _apply_init_goal(marker, "project", getattr(args, "goal", None))

    print()
    print(f"Project memory will live in: {marker}")
    print("Run inside this directory:  lynx-memory status")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print("lynx-memory doctor")
    rc = 0

    print(f"  python         : {sys.version.split()[0]}")
    if sys.version_info < (3, 10):
        _print_err("Python 3.10+ required.")
        rc = 1
    else:
        _print_ok("Python version OK.")

    for mod in ("chromadb", "voyageai", "mcp", "openai", "dotenv"):
        try:
            __import__(mod)
            _print_ok(f"import {mod}")
        except Exception as e:
            _print_err(f"import {mod} failed: {e}")
            rc = 1

    ensure_dirs()
    test = DATA_DIR / ".write_test"
    try:
        test.write_text("ok")
        test.unlink()
        _print_ok(f"Data dir writable: {DATA_DIR}")
    except Exception as e:
        _print_err(f"Data dir not writable: {e}")
        rc = 1

    load_env()
    if not os.environ.get("VOYAGE_API_KEY"):
        _print_err("VOYAGE_API_KEY is not set (run `lynx-memory init`).")
        rc = 1
    else:
        try:
            from .embeddings import embed_one
            v = embed_one("hello world", input_type="query")
            if isinstance(v, list) and len(v) > 0:
                _print_ok(f"Voyage API call OK (embedding dim={len(v)}).")
            else:
                _print_err("Voyage API returned an empty embedding.")
                rc = 1
        except Exception as e:
            _print_err(f"Voyage API call failed: {e}")
            rc = 1

    if CLAUDE_SETTINGS_PATH.exists():
        try:
            _read_settings()
            _print_ok(f"settings.json is valid JSON.")
        except SystemExit as e:
            _print_err(str(e))
            rc = 1
    else:
        _print_warn(f"settings.json does not exist yet (run `lynx-memory init`).")

    return rc


# --------------------------------------------------------------------- slash command

def _read_bundled_command(name: str) -> str:
    """Read a bundled slash-command markdown file shipped inside the package."""
    return (
        resources.files("lynx_memory.assets.commands")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def _read_bundled_skill_files(name: str = OPENLYNX_SKILL_NAME) -> dict[str, str]:
    """Read bundled skill text files shipped inside the package."""
    try:
        skill_dir = resources.files("lynx_memory.assets").joinpath("skills", name)
    except Exception:
        return {}
    if not skill_dir.is_dir():
        return {}

    files: dict[str, str] = {}
    for item in skill_dir.iterdir():
        if item.is_file():
            files[item.name] = item.read_text(encoding="utf-8")
    return files


def _backup_path(path: Path) -> Path:
    return path.with_suffix(f"{path.suffix}.bak.{int(time.time())}")


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
        shutil.copy2(host_path, _backup_path(host_path), follow_symlinks=False)
        host_path.unlink()

    try:
        host_path.symlink_to(shared_path)
    except OSError:
        _print_warn(f"Could not symlink {host_path}; copied shared file instead.")
        host_path.write_text(src_text, encoding="utf-8")
    return True


def _install_shared_dir_link(files: dict[str, str], shared_dir: Path, host_dir: Path) -> bool:
    if not files:
        return False

    shared_dir.mkdir(parents=True, exist_ok=True)
    host_dir.parent.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, text in files.items():
        target = shared_dir / name
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
            changed = True

    if host_dir.is_symlink() and host_dir.resolve() == shared_dir.resolve():
        return changed

    if host_dir.exists() or host_dir.is_symlink():
        backup = _backup_path(host_dir)
        shutil.move(str(host_dir), str(backup))

    try:
        host_dir.symlink_to(shared_dir, target_is_directory=True)
    except OSError:
        _print_warn(f"Could not symlink {host_dir}; copied shared skill instead.")
        shutil.copytree(shared_dir, host_dir)
    return True


def _install_slash_command(name: str, target_dir: Path | None = None) -> bool:
    """Install a bundled slash command through the shared OpenLynx command store.

    Returns True if a write happened, False if the existing file already matches.
    """
    src = _read_bundled_command(name)
    shared = OPENLYNX_COMMANDS_DIR / name
    dest_dir = target_dir or CLAUDE_COMMANDS_DIR
    return _install_shared_file_link(src, shared, dest_dir / name)


def _install_openlynx_skill(target_dir: Path | None = None) -> bool:
    files = _read_bundled_skill_files(OPENLYNX_SKILL_NAME)
    shared = OPENLYNX_SKILLS_DIR / OPENLYNX_SKILL_NAME
    dest_dir = target_dir or CLAUDE_SKILLS_DIR
    return _install_shared_dir_link(files, shared, dest_dir / OPENLYNX_SKILL_NAME)


def _remove_slash_command(name: str, target_dir: Path | None = None) -> bool:
    dst = (target_dir or CLAUDE_COMMANDS_DIR) / name
    if not dst.exists():
        return False

    if not dst.is_symlink():
        _print_warn(f"{dst} is not an OpenLynx-managed symlink; left it alone.")
        return False

    try:
        target = dst.resolve()
        shared_root = OPENLYNX_COMMANDS_DIR.resolve()
    except OSError:
        return False

    try:
        target.relative_to(shared_root)
    except ValueError:
        _print_warn(f"{dst} does not point into {OPENLYNX_COMMANDS_DIR}; left it alone.")
        return False

    dst.unlink()
    return True


def _remove_openlynx_skill(target_dir: Path | None = None) -> bool:
    dst = (target_dir or CLAUDE_SKILLS_DIR) / OPENLYNX_SKILL_NAME
    if not dst.exists():
        return False

    if not dst.is_symlink():
        _print_warn(f"{dst} is not an OpenLynx-managed symlink; left it alone.")
        return False

    try:
        target = dst.resolve()
        shared_root = OPENLYNX_SKILLS_DIR.resolve()
    except OSError:
        return False

    try:
        target.relative_to(shared_root)
    except ValueError:
        _print_warn(f"{dst} does not point into {OPENLYNX_SKILLS_DIR}; left it alone.")
        return False

    dst.unlink()
    return True


# --------------------------------------------------------------------- merge

def _resolve_scope_dir(name: str, cwd: Path) -> Path | None:
    """Map 'project'/'global' to a concrete data dir for the given cwd."""
    if name == "global":
        return GLOBAL_DATA_DIR
    if name == "project":
        return find_project_root(cwd)
    raise ValueError(f"Unknown scope: {name!r}")


def _copy_sqlite_rows(src_db: Path, dst_db: Path) -> dict:
    """Copy sessions/turns/summaries from src to dst, ignoring id collisions."""
    counts = {"sessions": 0, "turns": 0, "summaries": 0}
    if not src_db.exists():
        return counts
    s = sqlite3.connect(src_db)
    s.row_factory = sqlite3.Row
    d = sqlite3.connect(dst_db)
    try:
        for table in ("sessions", "turns", "summaries"):
            try:
                rows = s.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                cols = row.keys()
                placeholders = ",".join("?" for _ in cols)
                col_list = ",".join(cols)
                cur = d.execute(
                    f"INSERT OR IGNORE INTO {table}({col_list}) VALUES({placeholders})",
                    tuple(row[c] for c in cols),
                )
                counts[table] += cur.rowcount
        d.commit()
    finally:
        s.close()
        d.close()
    return counts


def _copy_chroma(src_dir: Path, dst_dir: Path) -> dict:
    """Copy embeddings + docs from src chroma to dst chroma using upsert."""
    import chromadb
    from chromadb.config import Settings

    counts = {"turns": 0, "summaries": 0}
    src = chromadb.PersistentClient(path=str(src_dir), settings=Settings(anonymized_telemetry=False))
    dst = chromadb.PersistentClient(path=str(dst_dir), settings=Settings(anonymized_telemetry=False))

    for name in ("turns", "summaries"):
        try:
            src_col = src.get_collection(name)
        except Exception:
            continue
        dst_col = dst.get_or_create_collection(name, embedding_function=None, metadata={"hnsw:space": "cosine"})
        n = src_col.count()
        if n == 0:
            continue
        data = src_col.get(include=["embeddings", "documents", "metadatas"], limit=n)
        ids = data.get("ids") or []
        if not ids:
            continue
        dst_col.upsert(
            ids=ids,
            embeddings=data.get("embeddings"),
            documents=data.get("documents"),
            metadatas=data.get("metadatas"),
        )
        counts[name] = len(ids)
    return counts


def cmd_merge(args: argparse.Namespace) -> int:
    if args.from_ == args.to:
        _print_err("`--from` and `--to` must differ.")
        return 1

    cwd = Path.cwd()
    src_dir = _resolve_scope_dir(args.from_, cwd)
    dst_dir = _resolve_scope_dir(args.to, cwd)

    if src_dir is None:
        _print_err(f"Source scope '{args.from_}' not found (no project marker walking up from {cwd}).")
        return 1
    if dst_dir is None:
        # Target = project but no marker yet: create one in cwd.
        marker = cwd / PROJECT_MARKER
        marker.mkdir(parents=True, exist_ok=True)
        (marker / "db").mkdir(parents=True, exist_ok=True)
        dst_dir = marker
        _print_ok(f"Created project store at {marker}")

    src_paths = paths_for(src_dir)
    dst_paths = paths_for(dst_dir)

    print(f"lynx-memory merge  {args.from_} → {args.to}")
    print(f"  source : {src_dir}")
    print(f"  target : {dst_dir}")

    if not src_paths["db_path"].exists():
        _print_warn(f"Source has no database yet ({src_paths['db_path']}); nothing to merge.")
        return 0

    if args.dry_run:
        s = sqlite3.connect(src_paths["db_path"])
        try:
            for t in ("sessions", "turns", "summaries"):
                try:
                    n = s.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    print(f"  would copy {n} rows from {t}")
                except sqlite3.OperationalError:
                    pass
        finally:
            s.close()
        return 0

    # Touch target schema by opening a Memory once (creates tables + chroma dirs).
    from .storage import Memory
    m = Memory(data_dir=dst_dir)
    m.close()

    sql_counts = _copy_sqlite_rows(src_paths["db_path"], dst_paths["db_path"])
    _print_ok(
        f"SQLite copied: sessions={sql_counts['sessions']}, "
        f"turns={sql_counts['turns']}, summaries={sql_counts['summaries']}"
    )

    try:
        ch_counts = _copy_chroma(src_paths["chroma_dir"], dst_paths["chroma_dir"])
        _print_ok(f"Chroma copied: turns={ch_counts['turns']}, summaries={ch_counts['summaries']}")
    except Exception as e:
        _print_err(f"Chroma copy failed: {e}")
        return 1

    if args.delete_source:
        if not args.yes:
            try:
                ans = input(f"Delete source store at {src_dir}? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans != "y":
                _print_warn("Skipped --delete-source (not confirmed).")
                return 0
        for sub in ("db",):
            p = src_dir / sub
            if p.exists():
                shutil.rmtree(p)
        _print_ok(f"Cleared source data dirs under {src_dir}")

    return 0


# --------------------------------------------------------------------- retag

def _retag_scope(
    scope: str,
    data_dir: Path,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> tuple[int, int]:
    from .autotag import suggest_tags
    from .storage import Memory

    mem = Memory(data_dir=data_dir)
    try:
        turns = mem.iter_turns_for_retag(limit=limit)
        changed = 0
        for turn in turns:
            tag_suggestions = suggest_tags(
                user_msg=turn["user_msg"],
                assistant_msg=turn["assistant_msg"],
                cwd=turn.get("cwd"),
            )
            current_auto = {
                (row["tag_name"], row["kind"] or "custom")
                for row in mem.db.execute(
                    "SELECT tt.tag_name, tg.kind "
                    "FROM turn_tags tt JOIN tags tg ON tg.name = tt.tag_name "
                    "WHERE tt.turn_id = ? AND tt.source = 'auto'",
                    (turn["id"],),
                ).fetchall()
            }
            suggested_auto = {
                (item["name"], item.get("kind") or "custom") for item in tag_suggestions
            }
            if dry_run:
                if current_auto != suggested_auto:
                    changed += 1
                continue
            mem.refresh_auto_tags(
                turn["id"],
                user_msg=turn["user_msg"],
                assistant_msg=turn["assistant_msg"],
                cwd=turn.get("cwd"),
            )
            if current_auto != suggested_auto:
                changed += 1
        return len(turns), changed
    finally:
        mem.close()


def cmd_retag(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    targets: list[tuple[str, Path]] = []

    if args.scope in ("project", "both"):
        proj = _resolve_scope_dir("project", cwd)
        if proj is None:
            _print_warn(f"project: no project marker found walking up from {cwd}; skipping.")
        else:
            targets.append(("project", proj))
    if args.scope in ("global", "both"):
        targets.append(("global", GLOBAL_DATA_DIR))

    if not targets:
        _print_warn("Nothing to retag.")
        return 0

    print("lynx-memory retag")
    print(f"  dry-run : {'yes' if args.dry_run else 'no'}")
    if args.limit:
        print(f"  limit   : {args.limit}")

    total_scanned = 0
    total_changed = 0
    for scope, data_dir in targets:
        db_path = paths_for(data_dir)["db_path"]
        if not db_path.exists():
            _print_warn(f"{scope}: no database at {db_path}; skipping.")
            continue
        scanned, changed = _retag_scope(
            scope,
            data_dir,
            dry_run=args.dry_run,
            limit=args.limit,
        )
        total_scanned += scanned
        total_changed += changed
        verb = "would update" if args.dry_run else "updated"
        _print_ok(f"{scope}: scanned {scanned} turns, {verb} {changed}")

    if total_scanned == 0:
        _print_warn("No turns found.")
        return 0

    if args.dry_run:
        print("Re-run without `--dry-run` to write structured auto-tags.")
    else:
        print(f"Done. Retagged {total_changed} / {total_scanned} turns.")
    return 0


# --------------------------------------------------------------------- delete

def cmd_delete(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    scopes: list[tuple[str, Path | None]] = []
    if args.scope in ("project", "both"):
        scopes.append(("project", _resolve_scope_dir("project", cwd)))
    if args.scope in ("global", "both"):
        scopes.append(("global", GLOBAL_DATA_DIR))

    targets: list[tuple[str, Path]] = []
    for label, d in scopes:
        if d is None:
            _print_warn(f"{label}: no project marker found walking up from {cwd}; skipping.")
            continue
        if not (d / "db").exists():
            _print_warn(f"{label}: nothing to delete at {d}/db.")
            continue
        targets.append((label, d))

    if not targets:
        _print_warn("Nothing to delete.")
        return 0

    print("lynx-memory delete — the following data will be permanently removed:")
    for label, d in targets:
        print(f"  - {label}: {d}/db  (sqlite + chroma)")

    if not args.yes:
        try:
            ans1 = input("Type the word DELETE to confirm: ").strip()
        except EOFError:
            ans1 = ""
        if ans1 != "DELETE":
            _print_warn("Aborted (confirmation phrase not entered).")
            return 1
        try:
            ans2 = input("Are you absolutely sure? [y/N] ").strip().lower()
        except EOFError:
            ans2 = ""
        if ans2 != "y":
            _print_warn("Aborted (second confirmation declined).")
            return 1

    for label, d in targets:
        db_dir = d / "db"
        if db_dir.exists():
            shutil.rmtree(db_dir)
            _print_ok(f"{label}: removed {db_dir}")
    return 0


# --------------------------------------------------------------------- goal

def _resolve_goal_dir(scope: str, cwd: Path) -> tuple[str, Path | None]:
    """Map a goal scope to (label, data_dir). 'auto' picks project if in one."""
    if scope == "global":
        return ("global", GLOBAL_DATA_DIR)
    if scope == "project":
        return ("project", find_project_root(cwd))
    proj = find_project_root(cwd)
    return ("project", proj) if proj else ("global", GLOBAL_DATA_DIR)


def cmd_goal_show(args: argparse.Namespace) -> int:
    from .goals import gating_enabled, get_goal_text, strictness

    cwd = Path.cwd()
    scope = getattr(args, "scope", "both")
    targets: list[tuple[str, Path]] = []
    if scope == "both":
        proj = find_project_root(cwd)
        if proj:
            targets.append(("project", proj))
        targets.append(("global", GLOBAL_DATA_DIR))
    else:
        label, d = _resolve_goal_dir(scope, cwd)
        if d is None:
            _print_warn(f"{label}: no project store found walking up from {cwd}.")
        else:
            targets.append((label, d))

    print("lynx-memory goal")
    load_env(GLOBAL_DATA_DIR)
    gate = "on" if gating_enabled() else "off"
    for label, d in targets:
        text = get_goal_text(d)
        if text:
            print(f"  {label} ({d}):")
            print(f"    {text}")
        else:
            print(f"  {label}: (no goal set)")
    if any(get_goal_text(d) for _, d in targets):
        print(f"  gating: {gate} (strictness={strictness()})")
        if not _any_summary_key():
            _print_warn(
                "No summarization LLM key set; gating and goal-aware summaries are inactive."
            )
    return 0


def cmd_goal_set(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    text = " ".join(args.text).strip()
    if not text:
        _print_err("Goal text is empty.")
        return 1
    label, d = _resolve_goal_dir(args.scope, cwd)
    if d is None:
        _print_err(
            f"No project store found walking up from {cwd}. "
            "Run `lynx-memory init-project` first, or use --scope global."
        )
        return 1
    _set_goal_for(d, text)
    _print_ok(f"{label} goal set ({d}):")
    print(f"  {text}")
    load_env(d)
    if not _any_summary_key():
        _print_warn(
            "No summarization LLM key set (OPENAI/DEEPSEEK/QWEN). Relevance gating "
            "and goal-aware summaries stay inactive until one is configured."
        )
    return 0


def cmd_goal_clear(args: argparse.Namespace) -> int:
    from .goals import get_goal_text
    from .storage import Memory

    cwd = Path.cwd()
    label, d = _resolve_goal_dir(args.scope, cwd)
    if d is None:
        _print_err(f"{label}: no project store found walking up from {cwd}.")
        return 1
    if not get_goal_text(d):
        _print_warn(f"{label}: no goal set; nothing to clear.")
        return 0
    if not args.yes:
        try:
            ans = input(f"Clear the {label} goal? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            _print_warn("Aborted.")
            return 1
    mem = Memory(data_dir=d)
    try:
        mem.clear_goal()
    finally:
        mem.close()
    _print_ok(f"{label} goal cleared.")
    return 0


# --------------------------------------------------------------------- daily

def cmd_daily(args: argparse.Namespace) -> int:
    from .daily import build_digest, notify, notify_backend

    target = Path(args.project).expanduser().resolve() if args.project else Path.cwd()
    data_dir = resolve_data_dir(target)
    load_env(data_dir)

    name = target.name or "global"
    today = time.strftime("%m-%d")
    title = f"今日日报 · {name} · {today}"

    digest, n_turns, _goal = build_digest(data_dir, since_hours=args.since_hours)
    if n_turns == 0:
        body = "今天这个项目没有对话记录。"
    elif not digest:
        _print_warn("Could not build digest (no summarization LLM key, or the call failed).")
        body = "（生成摘要失败：未配置可用的 LLM key，或调用出错）"
    else:
        body = digest
    body = f"{body}\n\n———\n来源：{data_dir}\n记录 {n_turns} 轮对话"

    print(f"[{title}]\n{body}\n")

    if not args.notify:
        return 0
    if not notify_backend():
        _print_err(
            "No notifier configured. Set SERVERCHAN_SENDKEY (WeChat via ServerChan) "
            "or DAILY_WEBHOOK_URL in your .env."
        )
        return 1
    ok, detail = notify(title, body)
    if ok:
        _print_ok(f"Pushed via {notify_backend()}.")
        return 0
    _print_err(f"Push failed ({notify_backend()}): {detail}")
    return 1


# --------------------------------------------------------------------- web

def cmd_web(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        _print_err(
            "uvicorn is not installed. Reinstall with `pip install -U lynx-memory`."
        )
        return 1

    # Build web UI before starting server
    web_ok = _build_web_ui()
    if not web_ok:
        _print_err("Web UI build failed. Cannot start web server.")
        return 1

    host = args.host
    port = args.port

    import socket
    if port != 0:
        # Find the next available port starting from the requested port
        original_port = port
        while True:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.settimeout(1)
            result = test_sock.connect_ex((host, port))
            test_sock.close()
            if result != 0:
                break  # Port is free
            if port - original_port >= 100:
                _print_err(f"No available port found after {original_port}")
                return 1
            port += 1
        if port != original_port:
            _print_warn(f"Port {original_port} is in use, using {port} instead")
    else:
        # Pre-bind to grab a free port so we can print/open it before uvicorn starts.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            port = s.getsockname()[1]

    url = f"http://{host}:{port}"
    print(f"lynx-memory web — serving at {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_open:
        import threading
        import webbrowser

        def _open_later() -> None:
            import time as _t
            _t.sleep(0.6)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_open_later, daemon=True).start()

    import uvicorn
    uvicorn.run("lynx_memory.web:app", host=host, port=port, log_level="warning")
    return 0


# --------------------------------------------------------------------- entrypoint

def main() -> None:
    p = argparse.ArgumentParser(
        prog="lynx-memory",
        description="Persistent semantic memory for Claude Code.",
    )
    p.add_argument("--version", action="version", version=f"lynx-memory {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="Install hooks for one or more CLI hosts")
    sp.add_argument(
        "--target",
        default="claude_code",
        choices=list(SUPPORTED_TARGETS) + ["all"],
        help="Which CLI host to install for (default: claude_code)",
    )
    sp.add_argument(
        "--goal",
        default=None,
        help="Set a global memory goal non-interactively (skips the prompt)",
    )
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("uninstall", help="Remove hooks for one or more CLI hosts")
    sp.add_argument(
        "--target",
        default="claude_code",
        choices=list(SUPPORTED_TARGETS) + ["all"],
        help="Which CLI host to uninstall from (default: claude_code)",
    )
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser(
        "init-project",
        help="Create a project-scoped memory store (.lynx-memory/) in this directory",
    )
    sp.add_argument("path", nargs="?", default=None, help="Project root (default: cwd)")
    sp.add_argument(
        "--goal",
        default=None,
        help="Set the project memory goal non-interactively (skips the prompt)",
    )
    sp.set_defaults(func=cmd_init_project)

    sp = sub.add_parser("status", help="Show installation status and stats")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("doctor", help="Check environment and Voyage API key")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser(
        "merge",
        help="Merge memory between project and global stores",
    )
    sp.add_argument("--from", dest="from_", required=True, choices=["project", "global"])
    sp.add_argument("--to", required=True, choices=["project", "global"])
    sp.add_argument("--dry-run", action="store_true", help="Show counts without writing")
    sp.add_argument("--delete-source", action="store_true", help="Wipe source data after copy (destructive)")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation for --delete-source")
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser(
        "retag",
        help="Backfill structured auto-tags onto existing turns",
    )
    sp.add_argument("--scope", default="both", choices=["project", "global", "both"])
    sp.add_argument("--dry-run", action="store_true", help="Preview how many turns would be retagged")
    sp.add_argument("--limit", type=int, default=None, help="Only process the most recent N turns per scope")
    sp.set_defaults(func=cmd_retag)

    sp = sub.add_parser(
        "delete",
        help="Permanently delete memory data (sqlite + chroma) for a scope",
    )
    sp.add_argument("--scope", required=True, choices=["project", "global", "both"])
    sp.add_argument("-y", "--yes", action="store_true", help="Skip the interactive double-confirmation")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("web", help="Launch a local Web UI to browse memory")
    sp.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    sp.add_argument("--port", type=int, default=9527, help="Bind port (default 9527; use 0 for a random free port)")
    sp.add_argument("--no-open", action="store_true", help="Do not auto-open a browser tab")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser(
        "goal",
        help="View or configure the per-scope memory goal (gating + summaries)",
    )
    sp.set_defaults(func=cmd_goal_show, scope="both")
    gsub = sp.add_subparsers(dest="goal_action")

    g_show = gsub.add_parser("show", help="Show the goal(s)")
    g_show.add_argument("--scope", default="both", choices=["auto", "project", "global", "both"])
    g_show.set_defaults(func=cmd_goal_show)

    g_set = gsub.add_parser("set", help="Set the goal text for a scope")
    g_set.add_argument("text", nargs="+", help="Goal description")
    g_set.add_argument("--scope", default="auto", choices=["auto", "project", "global"])
    g_set.set_defaults(func=cmd_goal_set)

    g_clear = gsub.add_parser("clear", help="Remove the goal for a scope")
    g_clear.add_argument("--scope", default="auto", choices=["auto", "project", "global"])
    g_clear.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    g_clear.set_defaults(func=cmd_goal_clear)

    sp = sub.add_parser(
        "daily",
        help="Summarize a project's turns for today and optionally push to your phone",
    )
    sp.add_argument("--project", default=None, help="Project root to read (default: cwd)")
    sp.add_argument(
        "--since-hours",
        type=float,
        default=None,
        help="Look back N hours instead of since local midnight",
    )
    sp.add_argument(
        "--notify",
        action="store_true",
        help="Push the digest (ServerChan/webhook); without it, only prints",
    )
    sp.set_defaults(func=cmd_daily)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
