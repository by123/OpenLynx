"""Machine-wide registry of OpenLynx project memory stores.

The Web UI shows one tab per discovered `.lynx-memory/` directory on this
machine, not just the current project + global store. This module keeps a
small JSON registry in the global data dir and fills it two ways:

  - a bounded scan of $HOME for `.lynx-memory/` markers (seeded on first
    Web UI launch, refreshable on demand), and
  - the store hook calling `register_project()` whenever it writes a turn to
    a project store, so the list stays fresh without re-scanning.

Each project is keyed by a stable short hash of its resolved marker path, so
the id is URL-safe and survives across processes.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import List, Optional

from .config import GLOBAL_DATA_DIR, PROJECT_MARKER, find_project_root

_REGISTRY_NAME = "projects.json"
_REGISTRY_VERSION = 1

# In-process guard for read-modify-write of the registry. Cross-process writes
# rely on atomic os.replace + last-writer-wins (a briefly-missing entry is
# self-healing on the next scan or hook call).
_lock = threading.RLock()

# Directories never worth descending into when scanning for project markers.
# Skipping these keeps a full $HOME walk to a second or two on typical machines.
_SKIP_NAMES = {
    "node_modules", "Library", "Applications", ".git", ".hg", ".svn",
    ".venv", "venv", "env", ".env", "__pycache__", ".cache", ".npm",
    ".cargo", ".rustup", ".gradle", ".m2", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", "out", ".next",
    ".nuxt", "target", "Pods", ".Trash", ".terraform", "vendor",
    ".pnpm-store", ".yarn", "Caches", "site-packages",
}
_MAX_DEPTH = 8


# ---------------------------------------------------------------------------
# paths + ids
# ---------------------------------------------------------------------------
def registry_path() -> Path:
    return GLOBAL_DATA_DIR / _REGISTRY_NAME


def project_id(marker: os.PathLike | str) -> str:
    """Stable short id for a `.lynx-memory` marker directory."""
    s = str(Path(marker).resolve())
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _normalize_marker(path: os.PathLike | str) -> Optional[Path]:
    """Coerce `path` to a resolved `.lynx-memory` marker dir, or None."""
    try:
        p = Path(path).resolve()
    except Exception:
        return None
    if p.name != PROJECT_MARKER:
        cand = p / PROJECT_MARKER
        if cand.is_dir():
            return cand
        return None
    return p


# ---------------------------------------------------------------------------
# registry I/O
# ---------------------------------------------------------------------------
def _default_registry() -> dict:
    return {"version": _REGISTRY_VERSION, "scanned_at": None, "projects": {}, "hidden": []}


def load_registry() -> dict:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except Exception:
        return _default_registry()
    if not isinstance(data, dict):
        return _default_registry()
    data.setdefault("version", _REGISTRY_VERSION)
    data.setdefault("scanned_at", None)
    if not isinstance(data.get("projects"), dict):
        data["projects"] = {}
    if not isinstance(data.get("hidden"), list):
        data["hidden"] = []
    return data


def save_registry(reg: dict) -> None:
    GLOBAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = registry_path()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def is_scanned() -> bool:
    return load_registry().get("scanned_at") is not None


# ---------------------------------------------------------------------------
# registration (hook hot-path) + visibility
# ---------------------------------------------------------------------------
def register_project(path: os.PathLike | str, source: str = "hook") -> Optional[str]:
    """Add a project store to the registry if not already present.

    Cheap on the hook hot-path: only writes the file when the project is new.
    Safe to call with either a project root or its `.lynx-memory` marker.
    """
    marker = _normalize_marker(path)
    if marker is None:
        return None
    pid = project_id(marker)
    with _lock:
        reg = load_registry()
        if pid not in reg["projects"]:
            now = time.time()
            reg["projects"][pid] = {
                "root": str(marker.parent),
                "marker": str(marker),
                "source": source,
                "first_seen": now,
                "last_seen": now,
            }
            save_registry(reg)
    return pid


def set_hidden(pid: str, hidden: bool) -> bool:
    """Hide/show a project's tab. Returns False if the id is unknown."""
    with _lock:
        reg = load_registry()
        if pid not in reg["projects"]:
            return False
        hid = set(reg["hidden"])
        if hidden:
            hid.add(pid)
        else:
            hid.discard(pid)
        reg["hidden"] = sorted(hid)
        save_registry(reg)
    return True


def marker_for_id(pid: str) -> Optional[Path]:
    """Resolve a discovered project id to its marker dir (validated)."""
    ent = load_registry()["projects"].get(pid)
    if not ent:
        return None
    marker = Path(ent.get("marker", ""))
    return marker if marker.is_dir() else None


def ensure_current(cwd: os.PathLike | str) -> str:
    """Register the cwd's project (if any) and return its id, else 'global'."""
    marker = find_project_root(cwd)
    if marker is None:
        return "global"
    return register_project(marker, source="cwd") or "global"


# ---------------------------------------------------------------------------
# discovery scan
# ---------------------------------------------------------------------------
def _walk(d: Path, depth: int, home: Path, found: List[Path], seen: set) -> None:
    if depth > _MAX_DEPTH:
        return
    try:
        entries = list(os.scandir(d))
    except (PermissionError, OSError):
        return
    subdirs: List[str] = []
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        name = e.name
        if name == PROJECT_MARKER:
            marker = Path(e.path).resolve()
            root = marker.parent
            key = str(marker)
            # Skip a marker directly under $HOME (mirrors find_project_root,
            # which refuses to globalise the whole machine) and require a
            # `db/` subdir so empty/stray marker folders aren't listed.
            if root != home and key not in seen and (marker / "db").is_dir():
                seen.add(key)
                found.append(marker)
            continue  # never descend into the marker itself
        if name in _SKIP_NAMES or name.startswith("."):
            continue
        subdirs.append(e.path)
    for sd in subdirs:
        _walk(Path(sd), depth + 1, home, found, seen)


def scan(roots: Optional[List[os.PathLike | str]] = None) -> List[Path]:
    """Bounded walk for `.lynx-memory` marker dirs. Defaults to $HOME."""
    home = Path.home().resolve()
    if roots is None:
        roots = [home]
    found: List[Path] = []
    seen: set = set()
    for root in roots:
        try:
            base = Path(root).expanduser().resolve()
        except Exception:
            continue
        _walk(base, 0, home, found, seen)
    return found


def rescan(roots: Optional[List[os.PathLike | str]] = None) -> dict:
    """Re-scan for projects, merge into the registry, persist, and return it.

    Hook-registered entries are kept (as long as their marker still exists);
    entries whose marker has vanished are pruned, and hidden flags for pruned
    projects are dropped.
    """
    found = scan(roots)
    with _lock:
        reg = load_registry()
        now = time.time()
        kept: dict = {}
        for pid, ent in reg["projects"].items():
            try:
                if Path(ent.get("marker", "")).is_dir():
                    kept[pid] = ent
            except Exception:
                continue
        for marker in found:
            pid = project_id(marker)
            if pid in kept:
                kept[pid]["last_seen"] = now
                continue
            kept[pid] = {
                "root": str(marker.parent),
                "marker": str(marker),
                "source": "scan",
                "first_seen": now,
                "last_seen": now,
            }
        reg["projects"] = kept
        reg["hidden"] = [h for h in reg["hidden"] if h in kept]
        reg["scanned_at"] = now
        save_registry(reg)
        return reg
