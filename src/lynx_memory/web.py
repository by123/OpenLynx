"""Local Web UI for browsing lynx-memory.

Run with `lynx-memory web`.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import projects as projects_mod
from .config import GLOBAL_DATA_DIR, find_project_root, load_env
from .storage import Memory, get_shared_memory, memory_lock

logger = logging.getLogger("lynx_memory.web")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
logger.setLevel(logging.INFO)


def _scope_dir(scope: str) -> Optional[Path]:
    if scope == "global":
        return GLOBAL_DATA_DIR
    if scope == "project":
        # legacy alias: the project rooted at the server's cwd
        return find_project_root(Path.cwd())
    # otherwise a discovered project id — resolved (and validated) via the registry
    return projects_mod.marker_for_id(scope)


def _sqlite_turn_count(data_dir: Path) -> int:
    """Count turns without opening Chroma (avoids concurrent PersistentClient init).

    When a store is libSQL-synced, Memory reads/writes the embedded replica
    `sync-memory.db`; the bare `memory.db` is left frozen at its pre-sync state
    (often empty). Count whichever file Memory actually opens — preferring the
    replica when present — so the tab badge matches the listing instead of
    reporting 0 for a synced store.
    """
    base = data_dir / "db" / "memory.db"
    replica = base.with_name("sync-" + base.name)
    db_path = replica if replica.is_file() else base
    if not db_path.is_file():
        return 0
    try:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            # `deleted_at` is absent on stores predating the soft-delete column
            # (e.g. a frozen pre-sync memory.db); count everything there.
            cols = {r[1] for r in con.execute("PRAGMA table_info(turns)")}
            where = " WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
            row = con.execute(f"SELECT COUNT(*) FROM turns{where}").fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception:
        return 0


@contextmanager
def _memory_for(scope: str) -> Iterator[Memory]:
    """Yield a process-shared Memory for `scope`, serialized per data_dir.

    The instance is cached process-wide (chromadb init is expensive and
    racy if re-entered concurrently); the per-store RLock ensures we don't
    interleave multi-statement work across requests.
    """
    d = _scope_dir(scope)
    if d is None:
        raise HTTPException(status_code=404, detail=f"scope {scope!r} not available")
    with memory_lock(d):
        yield get_shared_memory(d)


def _static_dir() -> Optional[Path]:
    try:
        ref = resources.files("lynx_memory.assets") / "web"
    except Exception:
        return None
    p = Path(str(ref))
    return p if p.is_dir() and (p / "index.html").exists() else None


class TagBody(BaseModel):
    name: str
    kind: str = "custom"


class OpenFileBody(BaseModel):
    path: str
    line: Optional[int] = None


class VisibilityBody(BaseModel):
    hidden: bool


class BulkDeleteBody(BaseModel):
    ids: list[str]


class SettingsBody(BaseModel):
    summary_enabled: bool
    top_k: int
    min_score: float
    scope: str
    summary_backend: str
    openai_api_key: Optional[str] = None
    voyage_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    qwen_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = ""
    deepseek_model: str = "deepseek-chat"
    qwen_model: str = "qwen-turbo"
    embedding_backend: str = "voyage"
    openai_embedding_model: str = "text-embedding-3-large"
    voyage_model: str = "voyage-3.5"
    # cloud sync (Turso)
    sync_enabled: bool = False
    turso_api_token: Optional[str] = None
    turso_org: str = ""
    turso_group: str = "default"
    sync_url: str = ""
    sync_token: Optional[str] = None


def _try_open_with_command(cmd: list[str]) -> bool:
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _attach_retrieval_counts(mem: Memory, items: list) -> None:
    if not items:
        return
    counts = mem.hit_counts_for_turns([t["id"] for t in items])
    for t in items:
        t["retrieval_count"] = counts.get(t["id"], 0)


def create_app() -> FastAPI:
    load_env()
    app = FastAPI(title="lynx-memory web")
    logger.info("web app init cwd=%s", Path.cwd())

    @app.middleware("http")
    async def log_api_requests(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        started = time.perf_counter()
        logger.info("api request %s %s", request.method, request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "api error %s %s (%.1fms)",
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "api response %s %s -> %s (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    def _build_scope_list() -> dict:
        """Global store + every discovered project, one entry per tab."""
        cwd = Path.cwd()
        current_id = projects_mod.ensure_current(cwd)
        reg = projects_mod.load_registry()
        hidden = set(reg.get("hidden", []))

        scopes: list[dict] = [{
            "id": "global",
            "kind": "global",
            "name": "All projects",
            "dir": str(GLOBAL_DATA_DIR),
            "root": None,
            "turn_count": _sqlite_turn_count(GLOBAL_DATA_DIR),
            "hidden": False,
            "is_current": current_id == "global",
        }]

        projects: list[dict] = []
        for pid, ent in reg.get("projects", {}).items():
            marker = Path(ent.get("marker", ""))
            if not marker.is_dir():
                continue
            root = ent.get("root") or str(marker.parent)
            projects.append({
                "id": pid,
                "kind": "project",
                "name": Path(root).name or root,
                "dir": str(marker),
                "root": root,
                "turn_count": _sqlite_turn_count(marker),
                "hidden": pid in hidden,
                "is_current": pid == current_id,
            })
        # most memories first (global is prepended separately and stays first)
        projects.sort(key=lambda e: (-e["turn_count"], e["name"].lower()))
        scopes.extend(projects)

        return {
            "global_dir": str(GLOBAL_DATA_DIR),
            "cwd": str(cwd),
            "current_id": current_id,
            "scanned_at": reg.get("scanned_at"),
            "scopes": scopes,
        }

    @app.get("/api/scopes")
    def get_scopes() -> dict:
        # First-ever launch: seed the registry with a one-time $HOME scan so
        # all projects show up immediately. Later launches use the cache and
        # rely on the hook + manual rescan to stay fresh.
        if not projects_mod.is_scanned():
            try:
                projects_mod.rescan()
            except Exception:
                logger.exception("initial project scan failed")
        return _build_scope_list()

    @app.post("/api/projects/rescan")
    def rescan_projects() -> dict:
        try:
            projects_mod.rescan()
        except Exception as e:
            logger.exception("project rescan failed")
            raise HTTPException(status_code=500, detail=f"rescan failed: {e}") from e
        return _build_scope_list()

    @app.put("/api/projects/{pid}/visibility")
    def set_project_visibility(pid: str, body: VisibilityBody) -> dict:
        if not projects_mod.set_hidden(pid, body.hidden):
            raise HTTPException(status_code=404, detail="project not found")
        return {"ok": True}

    @app.get("/api/turns")
    def get_turns(
        scope: str = "global",
        page: int = 1,
        page_size: int = 20,
        q: Optional[str] = None,
        tag: Optional[str] = None,
        mode: str = "keyword",
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> dict:
        page = max(1, page)
        page_size = max(1, min(200, page_size))
        with _memory_for(scope) as mem:
            if mode == "semantic" and q:
                hits = mem.search(q, top_k=page_size, min_score=0.0, source="turns")
                ids = [h["id"] for h in hits]
                items: list[dict] = []
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    rows = mem.db.execute(
                        f"SELECT id, session_id, ts, cwd, user_msg, assistant_msg, "
                        f"summary, summary_source, summary_model, summary_ts "
                        f"FROM turns WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                        ids,
                    ).fetchall()
                    by_id = {r["id"]: dict(r) for r in rows}
                    ordered = [by_id[i] for i in ids if i in by_id]
                    mem._attach_tags(ordered)
                    score_by_id = {h["id"]: h["score"] for h in hits}
                    for it in ordered:
                        it["score"] = score_by_id.get(it["id"])
                    items = ordered
                if since is not None:
                    items = [it for it in items if it.get("ts", 0) >= since]
                if until is not None:
                    items = [it for it in items if it.get("ts", 0) < until]
                _attach_retrieval_counts(mem, items)
                return {"items": items, "total": len(items), "mode": "semantic"}
            offset = (page - 1) * page_size
            items = mem.list_turns(limit=page_size, offset=offset, query=q, tag=tag, since=since, until=until)
            total = mem.count_turns(query=q, tag=tag, since=since, until=until)
            _attach_retrieval_counts(mem, items)
            return {"items": items, "total": total, "mode": "keyword"}

    @app.get("/api/retrievals")
    def get_retrievals(
        scope: str = "global",
        page: int = 1,
        page_size: int = 20,
        q: Optional[str] = None,
    ) -> dict:
        page = max(1, page)
        page_size = max(1, min(200, page_size))
        offset = (page - 1) * page_size
        with _memory_for(scope) as mem:
            items = mem.list_retrievals(limit=page_size, offset=offset, query=q)
            total = mem.count_retrievals(query=q)
        return {"items": items, "total": total}

    @app.get("/api/retrievals/{scope}/{retrieval_id}")
    def get_retrieval_detail(scope: str, retrieval_id: str) -> dict:
        with _memory_for(scope) as mem:
            r = mem.get_retrieval(retrieval_id)
            if r is None:
                raise HTTPException(status_code=404, detail="retrieval not found")
            # Group hit turn_ids by their stored scope so we can fetch each from
            # the correct DB. A retrieval may reference turns in either store.
            ids_by_scope: dict[str, list[str]] = {}
            for h in r["hits"]:
                ids_by_scope.setdefault(h.get("scope") or scope, []).append(h["turn_id"])
            # Fetch turns from the same DB first (cheap path).
            turns_map: dict[str, dict] = {}
            same_ids = ids_by_scope.pop(scope, [])
            if same_ids:
                turns_map.update(mem.get_turns_by_ids(same_ids))
        # Fetch turns from other scopes outside the held memory context.
        for other_scope, ids in ids_by_scope.items():
            try:
                with _memory_for(other_scope) as other:
                    turns_map.update(other.get_turns_by_ids(ids))
            except HTTPException:
                continue
        for h in r["hits"]:
            t = turns_map.get(h["turn_id"])
            h["turn"] = t  # may be None if the turn was deleted
        return r

    @app.get("/api/top-referenced")
    def get_top_referenced(scope: str = "global", limit: int = 10) -> dict:
        limit = max(1, min(50, limit))
        with _memory_for(scope) as mem:
            items = mem.top_referenced_turns(limit=limit)
        return {"items": items}

    @app.get("/api/turns/{scope}/{turn_id}/retrievals")
    def get_turn_retrievals(scope: str, turn_id: str) -> dict:
        with _memory_for(scope) as mem:
            items = mem.list_retrievals_for_turn(turn_id)
        return {"items": items, "total": len(items)}

    @app.post("/api/turns/{scope}/bulk-delete")
    def bulk_delete_turns(scope: str, body: BulkDeleteBody) -> dict:
        ids = [i for i in body.ids if i]
        if not ids:
            return {"ok": True, "deleted": 0}
        # Set-based delete: one UPDATE per chunk + a single batched vector
        # delete + one commit, instead of a write/round-trip per id (which on a
        # synced store is one remote push per id).
        with _memory_for(scope) as mem:
            deleted = mem.forget_many(ids)
        return {"ok": True, "deleted": deleted}

    @app.delete("/api/turns/{scope}/{turn_id}")
    def delete_turn(scope: str, turn_id: str) -> dict:
        with _memory_for(scope) as mem:
            ok = mem.forget(turn_id)
        if not ok:
            raise HTTPException(status_code=404, detail="turn not found")
        return {"ok": True}

    @app.post("/api/turns/{scope}/{turn_id}/tags")
    def add_turn_tag(scope: str, turn_id: str, body: TagBody) -> dict:
        with _memory_for(scope) as mem:
            ok = mem.add_tag(turn_id, body.name, kind=body.kind, source="manual")
        if not ok:
            raise HTTPException(status_code=400, detail="could not add tag")
        return {"ok": True}

    @app.delete("/api/turns/{scope}/{turn_id}/tags/{tag}")
    def remove_turn_tag(scope: str, turn_id: str, tag: str) -> dict:
        with _memory_for(scope) as mem:
            ok = mem.remove_tag(turn_id, tag)
        if not ok:
            raise HTTPException(status_code=404, detail="tag not on turn")
        return {"ok": True}

    @app.post("/api/turns/{scope}/{turn_id}/summary")
    def regenerate_summary(scope: str, turn_id: str) -> dict:
        from .summarizer import last_error, summarize_with_source
        from .summarizer import is_enabled

        # Summary provider settings are global, regardless of which memory
        # store the turn belongs to.
        load_env(GLOBAL_DATA_DIR)

        if not is_enabled():
            raise HTTPException(status_code=400, detail="summarizer disabled (SUMMARY_ENABLED=0)")
        with _memory_for(scope) as mem:
            t = mem.get_turn(turn_id)
        if t is None:
            raise HTTPException(status_code=404, detail="turn not found")

        # The summarizer can block on an API call. Keep it outside the
        # shared Memory lock so the rest of the Web UI can continue reading.
        result = summarize_with_source(t["user_msg"], t["assistant_msg"])
        if not result:
            from .summarizer import provider_api_key
            has_any = any(provider_api_key(p) for p in ("openai", "deepseek", "qwen"))
            if not has_any:
                detail = (
                    "no API key configured — set OPENAI_API_KEY, "
                    "DEEPSEEK_API_KEY or QWEN_API_KEY"
                )
            else:
                err = last_error()
                detail = f"summarizer call failed — {err}" if err else "summarizer call failed"
            raise HTTPException(status_code=502, detail=detail)
        summary, source, used_model = result

        with _memory_for(scope) as mem:
            ok = mem.set_summary(turn_id, summary, source=source, model=used_model)
        if not ok:
            raise HTTPException(status_code=404, detail="turn not found")
        return {
            "ok": True,
            "summary": summary,
            "summary_source": source,
            "summary_model": used_model,
        }

    @app.get("/api/settings")
    def get_settings() -> dict:
        from dotenv import dotenv_values
        env_file = GLOBAL_DATA_DIR / ".env"
        stored = dotenv_values(str(env_file)) if env_file.exists() else {}

        def _get(key: str, default: str) -> str:
            return stored.get(key) or os.environ.get(key) or default

        def _key_set(key: str) -> bool:
            return bool(stored.get(key) or os.environ.get(key, "").strip())

        def _get_key(key: str) -> str:
            return stored.get(key) or os.environ.get(key, "") or ""

        raw_backend = _get("SUMMARY_BACKEND", "")
        backend = raw_backend if raw_backend in ("openai", "deepseek", "qwen") else "openai"
        raw_emb = _get("EMBEDDING_BACKEND", "voyage")
        embedding_backend = raw_emb if raw_emb in ("voyage", "openai") else "voyage"
        return {
            "summary_enabled": _get("SUMMARY_ENABLED", "1") not in ("0", "false", "off", "no"),
            "top_k": int(_get("TOP_K", "5")),
            "min_score": float(_get("MIN_SCORE", "0.7")),
            "scope": _get("LYNX_MEMORY_SCOPE", "auto"),
            "summary_backend": backend,
            "openai_api_key_set": _key_set("OPENAI_API_KEY"),
            "voyage_api_key_set": _key_set("VOYAGE_API_KEY"),
            "deepseek_api_key_set": _key_set("DEEPSEEK_API_KEY"),
            "qwen_api_key_set": _key_set("QWEN_API_KEY") or _key_set("DASHSCOPE_API_KEY"),
            "openai_api_key_value": _get_key("OPENAI_API_KEY"),
            "voyage_api_key_value": _get_key("VOYAGE_API_KEY"),
            "deepseek_api_key_value": _get_key("DEEPSEEK_API_KEY"),
            "qwen_api_key_value": _get_key("QWEN_API_KEY") or _get_key("DASHSCOPE_API_KEY"),
            "openai_model": _get("OPENAI_MODEL", "gpt-4o-mini"),
            "openai_base_url": _get("OPENAI_BASE_URL", ""),
            "deepseek_model": _get("DEEPSEEK_MODEL", "deepseek-chat"),
            "qwen_model": _get("QWEN_MODEL", "qwen-turbo"),
            "embedding_backend": embedding_backend,
            "openai_embedding_model": _get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"),
            "voyage_model": _get("VOYAGE_MODEL", "voyage-3.5"),
            "sync_enabled": (_get("OPENLYNX_SYNC_ENABLED", "") or "").strip().lower()
            in ("1", "true", "yes", "on"),
            "turso_org": _get("TURSO_ORG", ""),
            "turso_group": _get("TURSO_GROUP", "default"),
            "sync_url": _get("OPENLYNX_SYNC_URL", ""),
            "turso_api_token_set": _key_set("TURSO_API_TOKEN"),
            "turso_api_token_value": _get_key("TURSO_API_TOKEN"),
            "sync_token_set": _key_set("OPENLYNX_SYNC_TOKEN"),
            "sync_token_value": _get_key("OPENLYNX_SYNC_TOKEN"),
        }

    @app.put("/api/settings")
    def put_settings(body: SettingsBody) -> dict:
        from dotenv import set_key, unset_key
        env_file = GLOBAL_DATA_DIR / ".env"
        GLOBAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not env_file.exists():
            env_file.touch()

        set_key(str(env_file), "SUMMARY_ENABLED", "1" if body.summary_enabled else "0")
        set_key(str(env_file), "TOP_K", str(max(1, min(50, body.top_k))))
        set_key(str(env_file), "MIN_SCORE", f"{max(0.0, min(1.0, body.min_score)):.2f}")
        set_key(str(env_file), "LYNX_MEMORY_SCOPE", body.scope)
        set_key(str(env_file), "SUMMARY_BACKEND", body.summary_backend)
        set_key(str(env_file), "OPENAI_MODEL", body.openai_model.strip() or "gpt-4o-mini")
        set_key(str(env_file), "DEEPSEEK_MODEL", body.deepseek_model.strip() or "deepseek-chat")
        set_key(str(env_file), "QWEN_MODEL", body.qwen_model.strip() or "qwen-turbo")
        set_key(str(env_file), "EMBEDDING_BACKEND", body.embedding_backend)
        set_key(str(env_file), "OPENAI_EMBEDDING_MODEL", body.openai_embedding_model.strip() or "text-embedding-3-large")
        set_key(str(env_file), "VOYAGE_MODEL", body.voyage_model.strip() or "voyage-3.5")
        openai_base_url = body.openai_base_url.strip()
        if openai_base_url:
            set_key(str(env_file), "OPENAI_BASE_URL", openai_base_url)
        else:
            unset_key(str(env_file), "OPENAI_BASE_URL")

        os.environ["SUMMARY_ENABLED"] = "1" if body.summary_enabled else "0"
        os.environ["TOP_K"] = str(max(1, min(50, body.top_k)))
        os.environ["MIN_SCORE"] = f"{max(0.0, min(1.0, body.min_score)):.2f}"
        os.environ["LYNX_MEMORY_SCOPE"] = body.scope
        os.environ["SUMMARY_BACKEND"] = body.summary_backend
        os.environ["OPENAI_MODEL"] = body.openai_model.strip() or "gpt-4o-mini"
        os.environ["DEEPSEEK_MODEL"] = body.deepseek_model.strip() or "deepseek-chat"
        os.environ["QWEN_MODEL"] = body.qwen_model.strip() or "qwen-turbo"
        os.environ["EMBEDDING_BACKEND"] = body.embedding_backend
        os.environ["OPENAI_EMBEDDING_MODEL"] = body.openai_embedding_model.strip() or "text-embedding-3-large"
        os.environ["VOYAGE_MODEL"] = body.voyage_model.strip() or "voyage-3.5"
        if openai_base_url:
            os.environ["OPENAI_BASE_URL"] = openai_base_url
        else:
            os.environ.pop("OPENAI_BASE_URL", None)

        # legacy anthropic summarization settings — drop if still present
        for legacy in ("ANTHROPIC_API_KEY", "SUMMARY_MODEL", "LYNX_MEMORY_SUMMARY_MODEL"):
            unset_key(str(env_file), legacy)
            os.environ.pop(legacy, None)

        # cloud sync (Turso): enable flag + plain (non-secret) values
        set_key(str(env_file), "OPENLYNX_SYNC_ENABLED", "1" if body.sync_enabled else "0")
        os.environ["OPENLYNX_SYNC_ENABLED"] = "1" if body.sync_enabled else "0"
        for env_key, value in [
            ("TURSO_ORG", body.turso_org.strip()),
            ("TURSO_GROUP", body.turso_group.strip() or "default"),
            ("OPENLYNX_SYNC_URL", body.sync_url.strip()),
        ]:
            if value:
                set_key(str(env_file), env_key, value)
                os.environ[env_key] = value
            else:
                unset_key(str(env_file), env_key)
                os.environ.pop(env_key, None)

        # API keys + sync tokens: only write when provided; "" = clear the key
        for env_key, value in [
            ("OPENAI_API_KEY", body.openai_api_key),
            ("VOYAGE_API_KEY", body.voyage_api_key),
            ("DEEPSEEK_API_KEY", body.deepseek_api_key),
            ("QWEN_API_KEY", body.qwen_api_key),
            ("TURSO_API_TOKEN", body.turso_api_token),
            ("OPENLYNX_SYNC_TOKEN", body.sync_token),
        ]:
            if value is None:
                continue  # field not sent — leave unchanged
            val = value.strip()
            if val:
                set_key(str(env_file), env_key, val)
                os.environ[env_key] = val
            else:
                unset_key(str(env_file), env_key)
                os.environ.pop(env_key, None)

        return {"ok": True}

    @app.get("/api/tags")
    def get_tags(scope: str = "global", kind: Optional[str] = None) -> list[dict]:
        with _memory_for(scope) as mem:
            return mem.list_tags(kind=kind)

    @app.post("/api/open-file")
    def open_file(body: OpenFileBody) -> dict:
        raw = body.path.strip()
        if not raw:
            logger.warning("open-file rejected: empty path")
            raise HTTPException(status_code=400, detail="empty path")
        target = Path(raw).expanduser()
        if not target.is_absolute():
            logger.warning("open-file rejected: non-absolute path=%s", raw)
            raise HTTPException(status_code=400, detail="path must be absolute")
        if not target.exists() or not target.is_file():
            logger.warning("open-file rejected: missing file path=%s", target)
            raise HTTPException(status_code=404, detail=f"file not found: {target}")

        line = body.line if (body.line is not None and body.line > 0) else None
        logger.info("open-file request path=%s line=%s", target, line)
        if sys.platform == "darwin":
            try:
                subprocess.Popen(["open", str(target)])
                logger.info("open-file success method=open path=%s", target)
                return {"ok": True, "method": "open", "path": str(target), "line": line}
            except Exception as e:
                logger.exception("open-file failed method=open path=%s", target)
                raise HTTPException(status_code=500, detail=f"failed to open file: {e}") from e

        if os.name == "nt":
            try:
                os.startfile(str(target))  # type: ignore[attr-defined]
                logger.info("open-file success method=startfile path=%s", target)
                return {"ok": True, "method": "startfile", "path": str(target), "line": line}
            except Exception as e:
                logger.exception("open-file failed method=startfile path=%s", target)
                raise HTTPException(status_code=500, detail=f"failed to open file: {e}") from e

        if _try_open_with_command(["xdg-open", str(target)]):
            logger.info("open-file success method=xdg-open path=%s", target)
            return {"ok": True, "method": "xdg-open", "path": str(target), "line": line}

        logger.error("open-file failed: no available opener path=%s", target)
        raise HTTPException(status_code=500, detail="no available opener on this system")

    static_dir = _static_dir()
    if static_dir is not None:
        # Mount Vite "assets/" sub-folder + serve index.html on root and SPA fallbacks.
        app.mount(
            "/assets",
            StaticFiles(directory=str(static_dir / "assets")),
            name="assets",
        )

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(static_dir / "index.html")

        @app.get("/{path:path}")
        def spa_fallback(path: str) -> FileResponse:
            target = static_dir / path
            if target.is_file():
                return FileResponse(target)
            return FileResponse(static_dir / "index.html")
    else:
        @app.get("/")
        def missing_ui() -> dict:
            return {
                "error": "Web UI assets not built",
                "hint": "Run `cd web && npm install && npm run build` from the repo root.",
            }

    return app


app = create_app()
