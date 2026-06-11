"""Centralized paths and environment loading for lynx-memory.

Supports two storage scopes:
  - global: ~/.openlynx/ (default, also overridable via LYNX_MEMORY_DIR)
  - project: <project_root>/.lynx-memory/ when walked-up from cwd

`resolve_data_dir(cwd)` picks project if a marker dir is present, else global.
"""
import os
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv

PROJECT_MARKER = ".lynx-memory"

OPENLYNX_HOME = Path(os.environ.get("OPENLYNX_HOME", os.path.expanduser("~/.openlynx")))
LEGACY_GLOBAL_DATA_DIR = Path(os.path.expanduser("~/.claude/lynx-memory"))
GLOBAL_DATA_DIR = Path(os.environ.get("LYNX_MEMORY_DIR", str(OPENLYNX_HOME)))

# Backward-compatible aliases — point to the global store.
DATA_DIR = GLOBAL_DATA_DIR
ENV_FILE = GLOBAL_DATA_DIR / ".env"
DB_DIR = GLOBAL_DATA_DIR / "db"
DB_PATH = DB_DIR / "memory.db"
CHROMA_DIR = DB_DIR / "chroma"
LOG_PATH = DB_DIR / "hook.log"
STATE_PATH = DB_DIR / "last_turn.json"
OPENLYNX_COMMANDS_DIR = GLOBAL_DATA_DIR / "commands"
OPENLYNX_SKILLS_DIR = GLOBAL_DATA_DIR / "skills"

CLAUDE_SETTINGS_PATH = Path(os.path.expanduser("~/.claude/settings.json"))

# Codex CLI integration. Codex auto-loads ~/.codex/hooks.json when
# `[features] hooks = true` is set in config.toml.
CODEX_HOME = Path(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")))
CODEX_CONFIG_PATH = CODEX_HOME / "config.toml"
CODEX_HOOKS_PATH = CODEX_HOME / "hooks.json"

SUPPORTED_TARGETS = ("claude_code", "codex")


def paths_for(data_dir: Path) -> dict:
    """Derive all per-store paths from a base data directory."""
    db_dir = data_dir / "db"
    return {
        "data_dir": data_dir,
        "env_file": data_dir / ".env",
        "db_dir": db_dir,
        "db_path": db_dir / "memory.db",
        "chroma_dir": db_dir / "chroma",
        "log_path": db_dir / "hook.log",
        "state_path": db_dir / "last_turn.json",
    }


def find_project_root(cwd: Optional[Union[str, os.PathLike]] = None) -> Optional[Path]:
    """Walk up from `cwd` looking for a `.lynx-memory/` directory.

    Returns the marker directory path if found, else None. The user's $HOME is
    skipped to prevent an accidental marker there from globalising the project
    scope across the whole machine.
    """
    if not cwd:
        return None
    try:
        start = Path(cwd).expanduser().resolve()
    except Exception:
        return None
    home = Path.home().resolve()

    cur = start if start.is_dir() else start.parent
    while True:
        if cur != home:
            cand = cur / PROJECT_MARKER
            if cand.is_dir():
                return cand
        if cur == cur.parent:
            return None
        cur = cur.parent


def resolve_data_dir(cwd: Optional[Union[str, os.PathLike]] = None) -> Path:
    """Return the data dir to use given a cwd: project marker if found, else global."""
    proj = find_project_root(cwd)
    return proj if proj is not None else GLOBAL_DATA_DIR


def load_env(data_dir: Optional[Path] = None) -> None:
    """Load .env from the given data_dir into os.environ.

    When `data_dir` is a project store, the global .env is also loaded as a
    fallback so a single VOYAGE_API_KEY in the global store works everywhere.
    """
    target = (data_dir or GLOBAL_DATA_DIR) / ".env"
    if target.exists():
        load_dotenv(target, override=True)
    if data_dir is not None and data_dir != GLOBAL_DATA_DIR:
        global_env = GLOBAL_DATA_DIR / ".env"
        if global_env.exists():
            load_dotenv(global_env, override=False)


def ensure_dirs(data_dir: Optional[Path] = None) -> None:
    target = data_dir or GLOBAL_DATA_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / "db").mkdir(parents=True, exist_ok=True)
