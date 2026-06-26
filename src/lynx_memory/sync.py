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

def migrate_local_to_remote(data_dir: Path, sync_url: str, sync_token: str) -> dict:
    """Copy a local store (sqlite rows + Chroma vectors) into its remote DB.

    Idempotent: rows use INSERT OR IGNORE; vectors upsert by id. Returns a
    per-table count summary.
    """
    from .storage._base import SCHEMA, _apply_migrations
    from .storage._libsql import LibsqlVectorCollection, connect_replica

    db_path = paths_for(data_dir)["db_path"]
    chroma_dir = paths_for(data_dir)["chroma_dir"]
    replica = str(db_path.with_name("sync-" + db_path.name))

    remote = connect_replica(replica, sync_url, sync_token, do_sync=True)
    remote.executescript(SCHEMA)
    remote.commit()
    remote.sync()  # make the new schema visible locally before migrations read it
    _apply_migrations(remote)
    remote.commit()
    remote.sync()

    summary: dict = {}
    if db_path.exists():
        local = sqlite3.connect(str(db_path))
        local.row_factory = sqlite3.Row
        tables = [
            r[0]
            for r in local.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != '_meta'"
            )
        ]
        for t in tables:
            cols = [r[1] for r in local.execute(f"PRAGMA table_info({t})")]
            cl = ",".join(cols)
            ph = ",".join(["?"] * len(cols))
            for row in local.execute(f"SELECT {cl} FROM {t}").fetchall():
                remote.execute(f"INSERT OR IGNORE INTO {t}({cl}) VALUES ({ph})", tuple(row))
            remote.commit()
            summary[t] = remote.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        local.close()

    # copy vectors from the local Chroma collections (no re-embedding)
    try:
        import chromadb
        from chromadb.config import Settings

        cli = chromadb.PersistentClient(
            path=str(chroma_dir), settings=Settings(anonymized_telemetry=False)
        )
        for name, kind in (("turns", "turn"), ("summaries", "summary")):
            try:
                src = cli.get_collection(name)
            except Exception:
                continue
            got = src.get(include=["embeddings"])
            ids = got.get("ids")
            embs = got.get("embeddings")
            ids = [] if ids is None else list(ids)
            embs = [] if embs is None else list(embs)
            if ids:
                LibsqlVectorCollection(remote, kind).add(ids, embs)
        remote.sync()
        summary["turn_vectors"] = remote.execute("SELECT COUNT(*) FROM turn_vectors").fetchone()[0]
        summary["summary_vectors"] = remote.execute(
            "SELECT COUNT(*) FROM summary_vectors"
        ).fetchone()[0]
    except Exception as e:
        summary["vectors_error"] = str(e)

    remote.close()
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


def cmd_sync_init(args: argparse.Namespace) -> int:
    data_dir = find_project_root(os.getcwd())
    if data_dir is None:
        print("Not inside a project store. Run `lynx-memory init-project` here first.")
        return 1
    root = data_dir.parent  # the actual repo root (for the git remote)
    load_env(data_dir)

    api_token = os.environ.get("TURSO_API_TOKEN", "").strip()
    org = os.environ.get("TURSO_ORG", "").strip()
    group = os.environ.get("TURSO_GROUP", "default").strip() or "default"
    if not api_token or not org:
        print("Missing TURSO_API_TOKEN / TURSO_ORG in ~/.openlynx/.env.")
        return 1

    pid, db_name, remote = project_identity(root)
    print(f"project: {root.name}  remote: {remote or '(none)'}")
    print(f"project_id: {pid}  db: {db_name}")

    cfg = data_dir / "sync.json"
    if cfg.exists() and not getattr(args, "force", False):
        print(f"{cfg} already exists. Use --force to re-provision.")
        return 1

    print("provisioning Turso database…")
    sync_url, db_token = provision_db(db_name, token=api_token, org=org, group=group)
    print(f"  url: {sync_url}")

    print("migrating local store → remote…")
    summary = migrate_local_to_remote(data_dir, sync_url, db_token)
    print("  " + ", ".join(f"{k}={v}" for k, v in summary.items()))

    _write_sync_json(
        data_dir,
        {"url": sync_url, "token": db_token, "project_id": pid, "remote": remote, "enabled": True},
    )
    print(f"wrote {cfg} (gitignored). This project now syncs to its own Turso DB.")
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
