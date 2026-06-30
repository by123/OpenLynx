"""Cloud sync (libSQL / Turso): per-project database provisioning + migration.

Implements method-A project sync: each synced project gets its own Turso DB,
auto-created via the Turso platform API, with its sync config written to
`<project>/.lynx-memory/sync.json` (gitignored). The global store keeps using
the OPENLYNX_SYNC_* env vars (see storage/_base._resolve_sync).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from .config import find_project_root, load_env, paths_for

API_BASE = "https://api.turso.tech/v1/organizations"


# ──────────────────────────── Turso platform API ────────────────────────────

def _api(method: str, path: str, token: str, body: Optional[dict] = None) -> Tuple[int, dict]:
    req = urllib.request.Request(
        f"{API_BASE}/{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except Exception:
            return e.code, {}


def provision_db(name: str, *, token: str, org: str, group: str) -> Tuple[str, str]:
    """Create the database (idempotent) and mint a full-access token.

    Returns (sync_url, db_token). Raises RuntimeError on failure.
    """
    status, resp = _api("POST", f"{org}/databases", token, {"name": name, "group": group})
    if status not in (200, 201):
        # already exists? fall back to fetching it
        s2, info = _api("GET", f"{org}/databases/{name}", token)
        if s2 != 200:
            raise RuntimeError(f"create db failed ({status}): {resp}")
        hostname = (info.get("database") or {}).get("Hostname")
    else:
        hostname = (resp.get("database") or {}).get("Hostname")
    if not hostname:
        raise RuntimeError("could not determine database hostname")

    st, tok = _api(
        "POST",
        f"{org}/databases/{name}/auth/tokens?expiration=never&authorization=full-access",
        token,
    )
    jwt = tok.get("jwt") if isinstance(tok, dict) else None
    if st != 200 or not jwt:
        raise RuntimeError(f"mint token failed ({st}): {tok}")
    return f"libsql://{hostname}", jwt


# ──────────────────────────── project identity ──────────────────────────────

def _git_remote(root: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _normalize_remote(url: str) -> str:
    u = url.strip()
    u = re.sub(r"\.git$", "", u)
    u = re.sub(r"^(https?|ssh)://", "", u)
    u = re.sub(r"^git@", "", u)
    u = u.replace(":", "/")
    u = re.sub(r"/{2,}", "/", u)
    return u.lower()


def project_identity(root: Path) -> Tuple[str, str, Optional[str]]:
    """Return (project_id, db_name, remote).

    project_id is derived from the git remote (stable across machines) when
    present, else from the absolute path (machine-local fallback).
    """
    remote = _git_remote(root)
    key = _normalize_remote(remote) if remote else str(root.resolve())
    pid = hashlib.sha1(key.encode()).hexdigest()[:12]
    base = key.split("/")[-1] if remote else root.name
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:24] or "proj"
    return pid, f"openlynx-{slug}-{pid[:8]}", remote


# ──────────────────────────── migration ─────────────────────────────────────

VECTOR_TABLES = ("turn_vectors", "summary_vectors")


def migrate_local_to_remote(data_dir: Path, sync_url: str, sync_token: str) -> dict:
    """Copy a local store (sqlite rows + vectors) into its remote DB.

    Idempotent: rows use INSERT OR IGNORE; vectors upsert by id. Returns a
    per-table count summary.

    Source selection matters on a re-bind. Once a store has been synced, its
    freshest state — post-sync writes AND (in sync mode) the vector tables —
    lives in the embedded replica `sync-<db>`, not in `memory.db`/Chroma. So
    when a replica is present we read from it, then wipe it, so the freshly
    provisioned remote starts at a clean libSQL generation. Reusing a stale
    replica file against a new remote makes `raw.sync()` fail with
    "server returned a lower generation than local".
    """
    from .storage._base import (
        SCHEMA,
        _acquire_replica_lock,
        _apply_migrations,
        _release_replica_lock,
    )
    from .storage._libsql import connect_replica

    db_path = paths_for(data_dir)["db_path"]
    chroma_dir = paths_for(data_dir)["chroma_dir"]
    replica = db_path.with_name("sync-" + db_path.name)

    # Hold the same cross-process lock the hooks use: this re-provision deletes
    # and recreates the replica, which would corrupt a hook mid-write. Released
    # at the end (and on process exit, since flock is fd-scoped).
    _lock_fd = _acquire_replica_lock(replica, 60)

    # 1. Read every table from the freshest local source into memory *before*
    #    touching the replica file (it doubles as the new remote's replica path).
    rebind = replica.exists()
    source_db = replica if rebind else db_path
    captured = []  # list of (table, cols, rows)
    have_vector_rows = False
    if source_db.exists():
        local = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
        tables = [
            r[0]
            for r in local.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != '_meta'"
            )
        ]
        for t in tables:
            cols = [r[1] for r in local.execute(f"PRAGMA table_info({t})")]
            rows = [tuple(r) for r in local.execute(f"SELECT {','.join(cols)} FROM {t}").fetchall()]
            captured.append((t, cols, rows))
            if t in VECTOR_TABLES and rows:
                have_vector_rows = True
        local.close()

    # 2. On a re-bind, drop the stale replica + libSQL sidecars so the new
    #    remote starts a fresh generation. The local memory.db (and the old
    #    remote it still points at) keep the data, so this is recoverable.
    if rebind:
        for suffix in ("", "-info", "-shm", "-wal", ".synced",
                       "-client_wal_index", "-journal"):
            f = replica.with_name(replica.name + suffix)
            if f.exists():
                f.unlink()

    remote = connect_replica(str(replica), sync_url, sync_token, do_sync=True)
    remote.executescript(SCHEMA)
    remote.commit()
    remote.sync()  # make the new schema visible locally before migrations read it
    _apply_migrations(remote)
    remote.commit()
    remote.sync()

    def insert_many(table: str, cols: list, rows: list, conflict: str = "INSERT OR IGNORE") -> None:
        """Multi-row INSERT in chunks — one network round-trip per chunk
        instead of per row (the whole point: avoid N synchronous writes)."""
        if not rows:
            return
        cl = ",".join(cols)
        tup = "(" + ",".join(["?"] * len(cols)) + ")"
        per = max(1, 800 // max(1, len(cols)))  # stay well under SQLite's var limit
        tail = (
            " ON CONFLICT(id) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
            if conflict == "UPSERT"
            else ""
        )
        verb = "INSERT" if conflict == "UPSERT" else conflict
        for i in range(0, len(rows), per):
            chunk = rows[i : i + per]
            sql = f"{verb} INTO {table}({cl}) VALUES " + ",".join([tup] * len(chunk)) + tail
            params = [v for row in chunk for v in row]
            remote.execute(sql, params)
        remote.commit()

    summary: dict = {}
    for t, cols, rows in captured:
        # vector tables key on `id` → UPSERT; relational tables may have
        # composite keys → plain INSERT OR IGNORE.
        conflict = "UPSERT" if t in VECTOR_TABLES else "INSERT OR IGNORE"
        insert_many(t, cols, rows, conflict=conflict)
        summary[t] = remote.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    # 3. First-time sync straight from memory.db: vectors aren't in the source
    #    yet, so pull them from the local Chroma collections (no re-embedding).
    if not have_vector_rows:
        try:
            import struct

            import chromadb
            from chromadb.config import Settings

            cli = chromadb.PersistentClient(
                path=str(chroma_dir), settings=Settings(anonymized_telemetry=False)
            )
            for name, table in (("turns", "turn_vectors"), ("summaries", "summary_vectors")):
                try:
                    src = cli.get_collection(name)
                except Exception:
                    continue
                got = src.get(include=["embeddings"])
                ids = got.get("ids")
                embs = got.get("embeddings")
                ids = [] if ids is None else list(ids)
                embs = [] if embs is None else list(embs)
                rows = []
                for i, vec in zip(ids, embs):
                    vals = [float(x) for x in vec]
                    rows.append((i, len(vals), struct.pack(f"<{len(vals)}f", *vals)))
                insert_many(table, ["id", "dim", "vec"], rows, conflict="UPSERT")
                summary[table] = remote.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception as e:
            summary["vectors_error"] = str(e)

    remote.sync()  # push everything we just wrote up to the remote
    remote.close()
    _release_replica_lock(_lock_fd)
    return summary


# ──────────────────────────── CLI commands ──────────────────────────────────

def _write_sync_json(data_dir: Path, payload: dict) -> Path:
    cfg = data_dir / "sync.json"
    cfg.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    cfg.chmod(0o600)
    # never commit the token
    gi = data_dir / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if "sync.json" not in lines:
        lines.append("sync.json")
        gi.write_text("\n".join(lines) + "\n")
    return cfg


def sync_store(data_dir: Path, *, api_token: str, org: str, group: str, force: bool = False) -> dict:
    """Provision a Turso DB for one project store, migrate it, write sync.json.

    Returns a result dict; sets "skipped" when already configured (no --force).
    """
    root = data_dir.parent  # the actual repo root (for the git remote)
    cfg = data_dir / "sync.json"
    if cfg.exists() and not force:
        return {"project": root.name, "skipped": "already configured"}

    pid, db_name, remote = project_identity(root)
    sync_url, db_token = provision_db(db_name, token=api_token, org=org, group=group)
    summary = migrate_local_to_remote(data_dir, sync_url, db_token)
    _write_sync_json(
        data_dir,
        {"url": sync_url, "token": db_token, "project_id": pid, "remote": remote, "enabled": True},
    )
    return {"project": root.name, "db": db_name, "remote": remote, "summary": summary}


def cmd_sync_init(args: argparse.Namespace) -> int:
    from .config import GLOBAL_DATA_DIR

    load_env(GLOBAL_DATA_DIR)  # TURSO_* live in the global .env
    api_token = os.environ.get("TURSO_API_TOKEN", "").strip()
    org = os.environ.get("TURSO_ORG", "").strip()
    group = os.environ.get("TURSO_GROUP", "default").strip() or "default"
    if not api_token or not org:
        print("Missing TURSO_API_TOKEN / TURSO_ORG in ~/.openlynx/.env.")
        return 1

    if getattr(args, "all", False):
        from .daily import discover_stores

        stores = [d for d in discover_stores() if d.resolve() != GLOBAL_DATA_DIR.resolve()]
        print(f"discovered {len(stores)} project store(s):")
        for d in stores:
            print("  -", d.parent)
        synced = 0
        for d in stores:
            try:
                res = sync_store(d, api_token=api_token, org=org, group=group, force=args.force)
                if res.get("skipped"):
                    print(f"• {res['project']}: skip ({res['skipped']})")
                else:
                    n = res.get("summary", {}).get("turns", "?")
                    print(f"✓ {res['project']} → {res['db']}  ({n} turns)")
                    synced += 1
            except Exception as e:
                print(f"✗ {d.parent.name}: {type(e).__name__}: {e}")
        print(f"done: {synced} newly synced, {len(stores)} total")
        return 0

    data_dir = find_project_root(os.getcwd())
    if data_dir is None:
        print("Not inside a project store. Run `init-project` here first, or use --all.")
        return 1
    if (data_dir / "sync.json").exists() and not args.force:
        print(f"{data_dir / 'sync.json'} already exists. Use --force to re-provision.")
        return 1
    print(f"provisioning + migrating {data_dir.parent.name}…")
    res = sync_store(data_dir, api_token=api_token, org=org, group=group, force=args.force)
    print(f"✓ {res['project']} → {res.get('db')}: " + ", ".join(f"{k}={v}" for k, v in res.get("summary", {}).items()))
    print("wrote sync.json (gitignored).")
    return 0


def cmd_sync_status(args: argparse.Namespace) -> int:
    from .config import GLOBAL_DATA_DIR

    def show(label: str, data_dir: Path) -> None:
        cfg = data_dir / "sync.json"
        if cfg.exists():
            d = json.loads(cfg.read_text())
            host = (d.get("url", "") or "").replace("libsql://", "")
            print(f"{label}: sync.json -> {host} (enabled={d.get('enabled')}, project_id={d.get('project_id')})")
        elif data_dir.resolve() == GLOBAL_DATA_DIR.resolve():
            url = os.environ.get("OPENLYNX_SYNC_URL", "")
            en = os.environ.get("OPENLYNX_SYNC_ENABLED", "")
            print(f"{label}: env -> {url.replace('libsql://','') or '(unset)'} (enabled={en or '0'})")
        else:
            print(f"{label}: local only (no sync.json)")

    load_env(GLOBAL_DATA_DIR)
    show("global ", GLOBAL_DATA_DIR)
    data_dir = find_project_root(os.getcwd())
    if data_dir is not None:
        show("project", data_dir)
    return 0
