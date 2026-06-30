"""Storage layer: SQLite (source of truth) + Chroma (vector index).

Public surface: `Memory` (per-store facade) and `search_scoped`
(cross-store search). Internals are split by responsibility into the
`_base`, `_crud`, `_tags`, `_search`, `_retrievals` modules and composed
here as mixins.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, Optional

from ._base import _MemoryBase, TAG_KIND_WEIGHTS, SCHEMA
from ._crud import _CrudMixin
from ._retrievals import _RetrievalsMixin
from ._scoped_search import search_scoped
from ._search import _SearchMixin
from ._tags import _TagsMixin


class Memory(
    _CrudMixin,
    _TagsMixin,
    _SearchMixin,
    _RetrievalsMixin,
    _MemoryBase,
):
    """Per-store memory facade. Compose all mixins with the base last."""


# ---- Per-process Memory cache ---------------------------------------------
# chromadb.PersistentClient init isn't cheap and isn't safe to instantiate
# concurrently against the same path, so for long-lived processes (web UI,
# repeated search_scoped calls) we keep one Memory per resolved data_dir.
# Hook commands run in short-lived subprocesses and don't benefit; they can
# keep using `Memory(data_dir=...)` directly.
_shared_memory_cache: Dict[Path, Memory] = {}
_shared_memory_locks: Dict[Path, threading.RLock] = {}
_cache_guard = threading.Lock()
# chromadb.PersistentClient can deadlock/hang if multiple DB paths initialize concurrently (Web UI).
_chroma_ctor_lock = threading.Lock()


def get_shared_memory(data_dir: Optional[Path] = None) -> Memory:
    """Return a process-wide cached Memory for `data_dir`.

    Callers MUST NOT call `.close()` on the returned instance — it lives
    until process exit. Use `memory_lock(data_dir)` to serialize access if
    you need to do multi-statement work without other threads interleaving.
    """
    from ..config import GLOBAL_DATA_DIR
    key = Path(data_dir or GLOBAL_DATA_DIR).resolve()
    with _cache_guard:
        if key in _shared_memory_cache:
            return _shared_memory_cache[key]
    with _chroma_ctor_lock:
        with _cache_guard:
            if key in _shared_memory_cache:
                return _shared_memory_cache[key]
            # Long-lived (never .close()'d) — don't hold the replica's
            # cross-process lock for the whole process lifetime or hooks would
            # starve; it re-locks briefly inside sync() instead.
            mem = Memory(data_dir=key, hold_replica_lock=False)
            _shared_memory_cache[key] = mem
            _shared_memory_locks[key] = threading.RLock()
            return mem


def memory_lock(data_dir: Optional[Path] = None) -> threading.RLock:
    """Return the per-store lock created alongside the cached Memory."""
    from ..config import GLOBAL_DATA_DIR
    key = Path(data_dir or GLOBAL_DATA_DIR).resolve()
    # Must not call get_shared_memory while holding _cache_guard — it also takes
    # _cache_guard (non-reentrant Lock) and would deadlock the web UI on first use.
    get_shared_memory(key)
    with _cache_guard:
        return _shared_memory_locks[key]


__all__ = [
    "Memory",
    "search_scoped",
    "get_shared_memory",
    "memory_lock",
    "TAG_KIND_WEIGHTS",
    "SCHEMA",
]
