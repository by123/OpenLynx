"""Memory base: schema, connection, chroma collections, indexing helpers."""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

import chromadb
from chromadb.config import Settings

from ..config import GLOBAL_DATA_DIR, ensure_dirs, paths_for
from ..embeddings import embed_one

logger = logging.getLogger(__name__)

TAG_KIND_WEIGHTS = {
    "user.role": 0.22,
    "user.preference": 0.18,
    "project.repo": 0.12,
    "project.stack": 0.08,
    "module.feature": 0.04,
    "custom": 0.0,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    cwd TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    cwd TEXT,
    user_msg TEXT NOT NULL,
    assistant_msg TEXT NOT NULL,
    user_uuid TEXT,
    summary TEXT,
    summary_source TEXT,
    summary_model TEXT,
    summary_ts REAL
);
CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    summary TEXT NOT NULL,
    turn_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id);
CREATE TABLE IF NOT EXISTS tags (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'custom',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_tags (
    turn_id TEXT NOT NULL,
    tag_name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    confidence REAL,
    PRIMARY KEY (turn_id, tag_name)
);
CREATE INDEX IF NOT EXISTS idx_turn_tags_turn ON turn_tags(turn_id);
CREATE INDEX IF NOT EXISTS idx_turn_tags_tag ON turn_tags(tag_name);
CREATE TABLE IF NOT EXISTS retrievals (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT,
    cwd TEXT,
    prompt TEXT NOT NULL,
    scope_used TEXT,
    hit_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS retrieval_hits (
    retrieval_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    scope TEXT,
    kind TEXT,
    score REAL,
    rank INTEGER,
    PRIMARY KEY (retrieval_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_retrievals_ts ON retrievals(ts);
CREATE INDEX IF NOT EXISTS idx_retrieval_hits_turn ON retrieval_hits(turn_id);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_vectors (
    id TEXT PRIMARY KEY,
    dim INTEGER,
    vec BLOB
);
CREATE TABLE IF NOT EXISTS summary_vectors (
    id TEXT PRIMARY KEY,
    dim INTEGER,
    vec BLOB
);
"""


# Schema version stored in `PRAGMA user_version`. Bump when adding a
# migration. Fresh DBs jump straight to TARGET after SCHEMA runs (SCHEMA
# already includes every column); pre-existing DBs get patched by the
# corresponding migration step.
TARGET_SCHEMA_VERSION = 3


def _migrate_to_v1(db: sqlite3.Connection) -> None:
    """Idempotent bridge for DBs created before user_version tracking.

    Adds the columns/indexes that SCHEMA now declares but older stores may
    be missing. Safe to run on a fresh DB (every check is "if missing").
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(turns)")}
    for col, ddl in (
        ("user_uuid", "ALTER TABLE turns ADD COLUMN user_uuid TEXT"),
        ("summary", "ALTER TABLE turns ADD COLUMN summary TEXT"),
        ("summary_source", "ALTER TABLE turns ADD COLUMN summary_source TEXT"),
        ("summary_model", "ALTER TABLE turns ADD COLUMN summary_model TEXT"),
        ("summary_ts", "ALTER TABLE turns ADD COLUMN summary_ts REAL"),
    ):
        if col not in cols:
            db.execute(ddl)
    tag_cols = {r[1] for r in db.execute("PRAGMA table_info(tags)")}
    if "kind" not in tag_cols:
        db.execute("ALTER TABLE tags ADD COLUMN kind TEXT NOT NULL DEFAULT 'custom'")
    turn_tag_cols = {r[1] for r in db.execute("PRAGMA table_info(turn_tags)")}
    if "source" not in turn_tag_cols:
        db.execute(
            "ALTER TABLE turn_tags ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
        )
    if "confidence" not in turn_tag_cols:
        db.execute("ALTER TABLE turn_tags ADD COLUMN confidence REAL")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_user_uuid "
        "ON turns(session_id, user_uuid) WHERE user_uuid IS NOT NULL"
    )


def _migrate_to_v2(db: sqlite3.Connection) -> None:
    """Add the per-store singleton `goals` table (goal-aware gating/summaries)."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS goals ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "text TEXT NOT NULL, "
        "created_at REAL NOT NULL, "
        "updated_at REAL NOT NULL)"
    )


def _migrate_to_v3(db) -> None:
    """Add vector tables for libSQL-synced semantic search (one source of
    truth that travels with the DB). Harmless/unused for local Chroma stores."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS turn_vectors ("
        "id TEXT PRIMARY KEY, dim INTEGER, vec BLOB)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS summary_vectors ("
        "id TEXT PRIMARY KEY, dim INTEGER, vec BLOB)"
    )


# Map version -> migration function. To add a v4: write _migrate_to_v4,
# add entry here, bump TARGET_SCHEMA_VERSION.
_MIGRATIONS = {1: _migrate_to_v1, 2: _migrate_to_v2, 3: _migrate_to_v3}


def _apply_migrations(db: sqlite3.Connection) -> None:
    current = db.execute("PRAGMA user_version").fetchone()[0]
    if current >= TARGET_SCHEMA_VERSION:
        return
    for version in range(current + 1, TARGET_SCHEMA_VERSION + 1):
        migration = _MIGRATIONS.get(version)
        if migration is not None:
            migration(db)
        db.execute(f"PRAGMA user_version = {version}")


class _MemoryBase:
    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = data_dir or GLOBAL_DATA_DIR
        paths = paths_for(self.data_dir)
        ensure_dirs(self.data_dir)
        paths["chroma_dir"].mkdir(parents=True, exist_ok=True)

        self.db_path = paths["db_path"]
        self.chroma_dir = paths["chroma_dir"]

        self.synced, init_schema = self._open_db()
        if init_schema:
            self.db.executescript(SCHEMA)
            if self.synced:
                # make the new schema visible in the local replica before
                # migrations read it (writes go to the primary, reads are local)
                self.db.commit()
                self.db.sync()
        # migrations are cheap (a local user_version read) and only write on a
        # real upgrade, so run them every open — this lets a new migration (e.g.
        # v3 vector tables) reach an already-initialized synced replica.
        _apply_migrations(self.db)
        self.db.commit()
        if self.synced and init_schema:
            self.db.sync()

        if self.synced:
            # vectors live in Turso (sync) — brute-force cosine, no Chroma.
            from ._libsql import LibsqlVectorCollection

            self.chroma = None
            self.turns = LibsqlVectorCollection(self.db, "turn")
            self.summaries = LibsqlVectorCollection(self.db, "summary")
        else:
            self.chroma = chromadb.PersistentClient(
                path=str(self.chroma_dir),
                settings=Settings(anonymized_telemetry=False),
            )
            self.turns = self.chroma.get_or_create_collection(
                "turns", embedding_function=None, metadata={"hnsw:space": "cosine"}
            )
            self.summaries = self.chroma.get_or_create_collection(
                "summaries", embedding_function=None, metadata={"hnsw:space": "cosine"}
            )

    def _resolve_sync(self) -> "tuple[str, str, bool]":
        """Resolve this store's sync config as (url, token, enabled).

        A project store reads `<data_dir>/sync.json` (its own Turso DB, written
        by `lynx-memory sync init`); the GLOBAL store falls back to the
        OPENLYNX_SYNC_* env vars. Per-store config avoids the env-var collision
        that would happen if project and global shared OPENLYNX_SYNC_URL in one
        process (e.g. the web server). Unconfigured stores get ("", "", False).
        """
        import json

        cfg = self.data_dir / "sync.json"
        if cfg.exists():
            try:
                d = json.loads(cfg.read_text())
                return (
                    str(d.get("url", "")).strip(),
                    str(d.get("token", "")).strip(),
                    bool(d.get("enabled", True)),
                )
            except Exception:
                logger.exception("invalid sync.json at %s", cfg)
                return "", "", False

        try:
            is_global = self.data_dir.resolve() == GLOBAL_DATA_DIR.resolve()
        except Exception:
            is_global = self.data_dir == GLOBAL_DATA_DIR
        if is_global:
            return (
                os.environ.get("OPENLYNX_SYNC_URL", "").strip(),
                os.environ.get("OPENLYNX_SYNC_TOKEN", "").strip(),
                os.environ.get("OPENLYNX_SYNC_ENABLED", "").strip().lower()
                in ("1", "true", "yes", "on"),
            )
        return "", "", False

    def _open_db(self) -> "tuple[bool, bool]":
        """Open self.db. Returns (synced, init_schema).

        `synced` is True when self.db is a libSQL embedded replica. `init_schema`
        is True when the caller should run SCHEMA + migrations (always for local
        sqlite; for a replica only on first creation, so periodic opens skip the
        network write-through of `CREATE TABLE IF NOT EXISTS`).

        Each store resolves its own sync config (see _resolve_sync): the GLOBAL
        store from OPENLYNX_SYNC_* env vars, a project store from its own
        `<data_dir>/sync.json` (so it syncs to its own Turso DB without colliding
        with the global env vars). Unconfigured stores stay on local sqlite3, so
        the feature is fully opt-in per store.

        To keep the latency-sensitive read path (on_prompt) fast, a remote pull
        happens at most once per OPENLYNX_SYNC_INTERVAL seconds (default 60),
        tracked by a sentinel file's mtime; other opens read the local replica.
        """
        import time

        sync_url, sync_token, enabled = self._resolve_sync()

        if enabled and sync_url and sync_token:
            try:
                from ._libsql import connect_replica

                replica = self.db_path.with_name("sync-" + self.db_path.name)
                sentinel = replica.with_name(replica.name + ".synced")
                first_time = not replica.exists()
                try:
                    interval = float(os.environ.get("OPENLYNX_SYNC_INTERVAL", "60") or 60)
                except ValueError:
                    interval = 60.0
                stale = (not sentinel.exists()) or (time.time() - sentinel.stat().st_mtime >= interval)
                do_sync = first_time or stale

                self.db = connect_replica(str(replica), sync_url, sync_token, do_sync=do_sync)
                if do_sync:
                    sentinel.touch()
                return True, first_time
            except Exception:
                logger.exception("libSQL sync connect failed; falling back to local sqlite")

        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        return False, True

    def sync(self) -> None:
        """Pull/push the synced replica (no-op for a local sqlite store)."""
        if self.synced:
            try:
                self.db.sync()
            except Exception:
                logger.exception("libSQL sync failed")

    def close(self) -> None:
        self.db.close()

    # ---- chroma indexing helpers (used by turns/summaries CRUD) ----
    def _index_turn_document(
        self,
        turn_id: str,
        *,
        session_id: str,
        ts: float,
        cwd: Optional[str],
        user_msg: str,
        assistant_msg: str,
        action: str = "upsert",
    ) -> None:
        doc = f"User: {user_msg}\n\nAssistant: {assistant_msg}"
        meta = {"session_id": session_id, "ts": ts, "cwd": cwd or ""}
        try:
            vec = embed_one(doc, input_type="document")
            if action == "insert":
                self.turns.add(ids=[turn_id], embeddings=[vec], documents=[doc], metadatas=[meta])
            else:
                self.turns.upsert(
                    ids=[turn_id],
                    embeddings=[vec],
                    documents=[doc],
                    metadatas=[meta],
                )
        except Exception:
            logger.exception("turn indexing failed for %s", turn_id)

    def _index_summary_document(
        self, summary_id: str, *, session_id: str, ts: float, summary: str
    ) -> None:
        try:
            vec = embed_one(summary, input_type="document")
            self.summaries.add(
                ids=[summary_id],
                embeddings=[vec],
                documents=[summary],
                metadatas=[{"session_id": session_id, "ts": ts}],
            )
        except Exception:
            logger.exception("summary indexing failed for %s", summary_id)

    @staticmethod
    def _tag_weight_for_kinds(kinds: List[str]) -> float:
        if not kinds:
            return 0.0
        return max(TAG_KIND_WEIGHTS.get(kind, 0.0) for kind in kinds)
