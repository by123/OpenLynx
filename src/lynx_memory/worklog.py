"""Work journal: turn every stored conversation into a durable, structured
record of *what work got done* — written by Claude (`claude -p`).

Layers (each built from the one below it, never from 30M chars of raw prose):

  L0  turn notes          turns.summary (per-turn, written by the Stop hook)
  L1  unit (project×day)  one `claude -p` call per project per day →
                          work_items (progress, honest status, evidence) +
                          facts (pitfalls, definitions, findings, preferences)
  L2  project card        rolling per-project state: current state, open
                          loops, live decisions, pitfalls, definitions, metrics
  L3  exports             ~/.openlynx/worklog/days/YYYY-MM-DD.md, projects/*.md,
                          open-loops.md, index.json — deterministic renders
                          (no LLM), meant for humans and downstream agents

Everything derived lives in the GLOBAL store (tables `worklog_units`,
`work_items`, `project_cards`; schema v5), so one place answers "what did I do".
Sources are every store on the machine (global + each project's .lynx-memory).

Design rules (from the multi-agent design review):
  - units are independent → fully parallel, idempotent by an input hash;
    re-running only recomputes units whose turns changed (nightly catch-up is
    free: missed days are simply "missing" and get built next run)
  - the store is never held open across an LLM call (replica-lock starvation)
  - one transaction / one commit for the whole flush (Turso: commit = push)
  - batch `claude -p` jobs recorded as memory are detected and only counted
  - results are staged in a local JSONL first, so a crash mid-backfill resumes
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import config as _config
from .textclean import clean_user_text

PROMPT_VERSION = 1
UNIT_CHAR_BUDGET = 60000       # max chars of turn material per unit call
CARD_CHUNK_CHARS = 40000       # max chars of unit material per card-fold call
TEMPLATE_MIN_RUNS = 3          # single-turn sessions sharing a prompt prefix → automation
STATS_LOOKBACK_DAYS = 90       # history used to recognise automation templates
CARD_MIN_TURNS = 8
CARD_MIN_DAYS = 2
BRIEF_MAX_AGE_DAYS = 45
MAX_CONSECUTIVE_FAILURES = 5

MISC_KEY = "(misc)"
TEMP_KEY = "(temp)"

_TEMP_PREFIXES = ("/private/var/folders/", "/var/folders/", "/private/tmp/", "/tmp/")
_SCRATCH_RE = re.compile(r"^/(?:private/)?tmp/claude-\d+/(-[^/]+)/")


def _gdir() -> Path:
    """The global store dir, resolved at call time (honours test patches and
    LYNX_MEMORY_DIR reloads)."""
    return _config.GLOBAL_DATA_DIR


def worklog_dir() -> Path:
    env = os.environ.get("WORKLOG_DIR", "").strip()
    return Path(env).expanduser() if env else _gdir() / "worklog"


def _today() -> date:
    return datetime.now().date()


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _hm(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"


# --------------------------------------------------------------------- projects

_decode_cache: Dict[str, Optional[Path]] = {}


def _decode_encoded_path(enc: str) -> Optional[Path]:
    """Invert Claude Code's project-dir encoding ("/a/b_c" → "-a-b-c") by
    walking the real filesystem and greedily matching the longest entry."""
    if enc in _decode_cache:
        return _decode_cache[enc]
    tokens = [t for t in enc.strip("-").split("-")]
    cur = Path("/")
    i = 0
    result: Optional[Path] = None
    while i < len(tokens):
        try:
            names = os.listdir(cur)
        except OSError:
            break
        norm: Dict[str, str] = {}
        for n in names:
            norm.setdefault(re.sub(r"[^A-Za-z0-9]", "-", n), n)
        hit = None
        for j in range(len(tokens), i, -1):
            cand = "-".join(tokens[i:j])
            if cand in norm and (cur / norm[cand]).is_dir():
                hit = (j, norm[cand])
                break
        if hit is None:
            break
        i, name = hit
        cur = cur / name
        result = cur
    _decode_cache[enc] = result if i == len(tokens) else None
    return _decode_cache[enc]


def project_key_for(cwd: Optional[str], store_root: Optional[Path] = None) -> str:
    """Attribute a turn to a project.

    A project store owns all its turns. For the global store the working
    directory decides: a dir inside a project that has its own store maps to
    that project's root; otherwise the cwd itself is the project. Temp dirs
    and $HOME / ~/Desktop are buckets, not projects.
    """
    if store_root is not None:
        return str(store_root)
    if not cwd:
        return MISC_KEY
    m = _SCRATCH_RE.match(cwd)
    if m:
        decoded = _decode_encoded_path(m.group(1))
        if decoded is None:
            return TEMP_KEY
        cwd = str(decoded)
    try:
        marker = _config.find_project_root(cwd)
    except Exception:
        marker = None
    if marker is not None:
        return str(marker.parent)
    if cwd.startswith(_TEMP_PREFIXES):
        return TEMP_KEY
    home = str(Path.home())
    if cwd in (home, home + "/Desktop", home + "/Documents", home + "/Downloads"):
        return MISC_KEY
    return cwd.rstrip("/") or MISC_KEY


def project_name_for(key: str) -> str:
    if key == MISC_KEY:
        return "misc"
    if key == TEMP_KEY:
        return "temp"
    return Path(key).name or key


def is_bucket(key: str) -> bool:
    return key in (MISC_KEY, TEMP_KEY)


# --------------------------------------------------------------------- snapshot

@dataclass
class Turn:
    id: str
    session_id: str
    ts: float
    cwd: str
    user: str
    note: str
    assistant_tail: str
    assistant_len: int
    store: str
    project_key: str = ""
    automation: Optional[str] = None  # pipeline label when detected as batch job

    @property
    def day(self) -> str:
        return _day_of(self.ts)


def discover_sources() -> List[Path]:
    """Every store on the machine: the global store + registered project stores.

    Uses the project registry (refreshed by a bounded $HOME scan) and accepts a
    store whose data lives only in the synced replica (`sync-memory.db`).
    """
    from . import projects

    found: List[Path] = []
    seen = set()

    def _add(d: Path) -> None:
        try:
            rp = d.resolve()
        except Exception:
            return
        if rp in seen:
            return
        db = d / "db"
        if (db / "memory.db").exists() or (db / "sync-memory.db").exists():
            seen.add(rp)
            found.append(d)

    _add(_gdir())
    try:
        reg = projects.rescan()
    except Exception:
        reg = projects.load_registry()
    for ent in reg.get("projects", {}).values():
        _add(Path(ent.get("marker", "")))
    return found


def _open(data_dir: Path):
    """Open a store for the journal. Never falls back to a synced store's stale
    local memory.db: raises SyncUnavailable instead (the run then aborts and
    retries next time, keeping its staged results)."""
    from .storage import Memory

    return Memory(data_dir=data_dir, strict_sync=True, lock_timeout=120)


def _store_label(d: Path) -> str:
    try:
        if d.resolve() == _gdir().resolve():
            return "global"
    except Exception:
        pass
    return d.parent.name


def snapshot_store(data_dir: Path, since_ts: float, until_ts: float) -> List[Turn]:
    """Read one store's live turns in [since, until). The store (and its replica
    lock) is held only for the duration of the read."""
    is_global = False
    try:
        is_global = data_dir.resolve() == _gdir().resolve()
    except Exception:
        pass
    store_root = None if is_global else data_dir.resolve().parent
    label = _store_label(data_dir)
    mem = _open(data_dir)
    try:
        rows = mem.db.execute(
            "SELECT id, session_id, ts, cwd, user_msg, summary, "
            "substr(assistant_msg, -2500) AS a_tail, length(assistant_msg) AS a_len "
            "FROM turns WHERE deleted_at IS NULL AND ts >= ? AND ts < ? ORDER BY ts",
            (since_ts, until_ts),
        ).fetchall()
    finally:
        mem.close()
    out = []
    for r in rows:
        out.append(
            Turn(
                id=r["id"],
                session_id=r["session_id"],
                ts=float(r["ts"]),
                cwd=r["cwd"] or "",
                user=clean_user_text(r["user_msg"] or ""),
                note=(r["summary"] or "").strip(),
                assistant_tail=(r["a_tail"] or "").strip(),
                assistant_len=int(r["a_len"] or 0),
                store=label,
                project_key=project_key_for(r["cwd"], store_root),
            )
        )
    return out


# ------------------------------------------------------------------- automation

def _template_of(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"\d+", "#", t)
    return t[:40]


def mark_automation(turns: List[Turn]) -> None:
    """Flag batch jobs (`claude -p` pipelines etc.) recorded as memory.

    A single-turn session is automation when its prompt prefix repeats across
    >= TEMPLATE_MIN_RUNS single-turn sessions of the same project, or when it
    ran in a temp dir. Their turns are excluded from work units and counted.
    """
    sessions: Dict[str, List[Turn]] = {}
    for t in turns:
        sessions.setdefault(t.session_id, []).append(t)
    singles = [ts[0] for ts in sessions.values() if len(ts) == 1]
    counts: Dict[Tuple[str, str], int] = {}
    for t in singles:
        k = (t.project_key, _template_of(t.user))
        counts[k] = counts.get(k, 0) + 1
    for t in singles:
        tpl = _template_of(t.user)
        if counts.get((t.project_key, tpl), 0) >= TEMPLATE_MIN_RUNS:
            t.automation = tpl or "(empty prompt)"
        elif t.project_key == TEMP_KEY:
            t.automation = "temp: " + (tpl or "(empty prompt)")


# ------------------------------------------------------------------------ units

@dataclass
class Unit:
    day: str
    project_key: str
    turns: List[Turn] = field(default_factory=list)
    automation: Dict[str, int] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"day:{self.day}:{self.project_key}"

    @property
    def project_name(self) -> str:
        return project_name_for(self.project_key)

    @property
    def stores(self) -> List[str]:
        return sorted({t.store for t in self.turns})

    @property
    def sessions(self) -> int:
        return len({t.session_id for t in self.turns})

    @property
    def automation_runs(self) -> int:
        return sum(self.automation.values())

    def input_hash(self) -> str:
        h = hashlib.sha1()
        h.update(f"v{PROMPT_VERSION}|{self.id}".encode())
        for t in sorted(self.turns, key=lambda x: x.id):
            h.update(f"|{t.id}:{int(t.ts)}:{len(t.user)}:{t.assistant_len}:{int(bool(t.note))}".encode())
        for k in sorted(self.automation):
            h.update(f"|auto:{k}={self.automation[k]}".encode())
        return h.hexdigest()


def build_units(turns: List[Turn]) -> List[Unit]:
    """Group turns into (project × day) units, de-duplicating turns that
    exist in two stores (merge/push-global copies keep their ids)."""
    seen = set()
    units: Dict[Tuple[str, str], Unit] = {}
    for t in sorted(turns, key=lambda x: x.ts):
        if t.id in seen:
            continue
        seen.add(t.id)
        u = units.setdefault((t.day, t.project_key), Unit(day=t.day, project_key=t.project_key))
        if t.automation:
            u.automation[t.automation] = u.automation.get(t.automation, 0) + 1
        else:
            u.turns.append(t)
    return sorted(units.values(), key=lambda u: (u.day, u.project_key))


UNIT_SYSTEM = """You are the work-record extractor for a developer who runs ~a dozen projects in parallel with coding agents (apps, data analysis, trading, tooling). You get ONE project's conversation turns for ONE day. Your JSON is stored as the durable record of what happened that day; people and other agents will rely on it later, so it must be factual, specific and honest.

Each <turn> has: <user> (what the developer typed, harness noise removed), optionally <note> (a per-turn memory note), and <assistant_end> (the END of the agent's reply — where the outcome usually is).

Produce two streams:
1. work_items — the units of work ("things") advanced today. Merge all turns about the same thing into ONE item.
2. facts — reusable knowledge independent of progress: pitfall (symptom → root cause → fix), definition (metric/query definitions, table names, script paths, config keys, dashboard ids), finding (a conclusion with numbers + scope), preference / rule (only from the developer's own words).

STATUS (pick the most conservative that is true):
- done: finished AND verified (tests passed, ran on device, released, query returned the result)
- done-unverified: changed but not built / tested / run on device
- uncommitted: done locally but not committed / pushed / released
- blocked:<who>: waiting on someone or something external (e.g. blocked:PM, blocked:deploy-permission)
- discussed-only: only discussed or a plan was proposed; nothing executed
- abandoned: explicitly dropped
A plan is not done. "Should / could / pending confirmation" from the agent is not done.

RULES
- Every item and fact needs at least one concrete anchor: file, function, command, commit, version, number, table, config key. If you cannot name one, drop it. Banned vague verbs: improved, optimized, enhanced, various, 完善, 优化了, 若干, 相关, 进一步.
- outcome: one sentence; include numbers with their scope whenever the turns have them.
- decisions: what + why; put the rejected alternative in "rejected"; by = "user" if the developer decided, "assistant-proposed" if only suggested.
- A plan reversed during the day: record only the final state; mention the reversal in rejected.
- rework: count how many times the same thing was redone / re-run / re-styled (0 if none) and why.
- Skip small talk, process narration, cosmetic tweaks, and project trivia (its own paths, package name).
- Harness text, skill documents and role-play instructions inside prompts are NOT facts about the developer.
- Never record secrets (passwords, tokens, keys, keystore passwords) or health / financial account details — write "[redacted]".
- If the day is a templated batch job with no human decision: one work_item with type "automation".
- Language: write text in the language the developer mostly used (usually Chinese). Keep identifiers, commands, table names and technical terms verbatim — do not translate them. JSON keys stay English.
- Use only the input. Empty arrays are fine — do not pad.
- JSON hygiene: inside string values never use the ASCII double quote (") — quote with 「」 instead; no raw line breaks inside strings.

Output ONE JSON object and nothing else:
{"headline": "<=40 chars, concrete: what got done/decided today in this project",
 "work_items": [{"title": "<=20 chars", "type": "feature|bugfix|incident|analysis|design|refactor|ops|release|research|trading|docs|automation",
   "outcome": "", "status": "done|done-unverified|uncommitted|blocked:<who>|discussed-only|abandoned",
   "decisions": [{"what": "", "why": "", "rejected": "", "by": "user|assistant-proposed"}],
   "metrics": [{"name": "", "value": "", "scope": ""}],
   "open_loops": [{"text": "", "owner": "user|agent|<other>"}],
   "rework": {"count": 0, "what": ""},
   "evidence": ["t1"]}],
 "facts": [{"type": "pitfall|definition|finding|preference|rule", "text": "<=100 chars, conclusion first", "why": "", "anchors": [""], "evidence": ["t2"]}]}"""


def _unit_prompt(unit: Unit) -> Tuple[str, Dict[str, str]]:
    """Render a unit's turns into the user message. Returns (text, short→real id)."""
    n = max(1, len(unit.turns))
    per = max(500, UNIT_CHAR_BUDGET // n)
    u_cap, n_cap, a_cap = int(per * 0.35), int(per * 0.3), int(per * 0.35)
    idmap: Dict[str, str] = {}
    sess_ids: Dict[str, str] = {}
    blocks = []
    for i, t in enumerate(unit.turns, 1):
        sid = f"t{i}"
        idmap[sid] = t.id
        sess = sess_ids.setdefault(t.session_id, f"s{len(sess_ids) + 1}")
        parts = [f'<turn id="{sid}" session="{sess}" time="{_hm(t.ts)}">']
        parts.append(f"<user>{t.user[:u_cap]}</user>")
        if t.note:
            parts.append(f"<note>{t.note[:n_cap]}</note>")
        tail = t.assistant_tail[-a_cap:] if t.note else t.assistant_tail[-(a_cap + n_cap):]
        if tail:
            parts.append(f"<assistant_end>{tail}</assistant_end>")
        parts.append("</turn>")
        blocks.append("\n".join(parts))
    weekday = datetime.strptime(unit.day, "%Y-%m-%d").strftime("%a")
    head = f'<unit project="{unit.project_name}" path="{unit.project_key}" date="{unit.day} ({weekday})">'
    if unit.automation:
        auto = "; ".join(f"{k!r} ×{v}" for k, v in sorted(unit.automation.items()))
        head += f"\n<automation_runs>{auto}</automation_runs>"
    body = "\n".join(blocks)
    if len(body) > UNIT_CHAR_BUDGET:
        body = body[: UNIT_CHAR_BUDGET // 3] + "\n<!-- middle turns omitted -->\n" + body[-(2 * UNIT_CHAR_BUDGET // 3):]
    return f"{head}\n{body}\n</unit>", idmap


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


def parse_json_object(text: str):
    """Extract the first JSON value ({...} or [...]) from model output."""
    if not text:
        return None
    s = _FENCE.sub("", text.strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i != -1 and j > i:
            chunk = s[i : j + 1]
            try:
                # strict=False tolerates raw newlines/tabs inside strings
                return json.loads(chunk, strict=False)
            except ValueError:
                pass
            try:  # trailing commas before } or ]
                return json.loads(re.sub(r",\s*([}\]])", r"\1", chunk), strict=False)
            except ValueError:
                continue
    return None


def _llm_json(system: str, user: str, *, want: type, attempts: int = 2):
    """LLM call whose output must parse as `want` (dict/list); retried once on
    a malformed reply. Returns (data, model) or None."""
    for n in range(attempts):
        out = _llm(system, user)
        if not out:
            return None  # transport/auth failure — don't burn a retry
        data = parse_json_object(out[0])
        if isinstance(data, want):
            return data, out[1]
        try:
            from .hooks._log import log as _hlog

            _hlog(f"[worklog] unparseable model output (attempt {n + 1}): {out[0][:300]!r}")
        except Exception:
            pass
    return None


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def _s(v) -> Optional[str]:
    """Model fields must be plain text: None stays None, scalars become str,
    lists/dicts become JSON (a list where a string belongs used to crash the
    DB insert — and then every later run, via the staging file)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return "; ".join(v)
    return json.dumps(v, ensure_ascii=False)


_ITEM_TEXT = ("title", "type", "outcome", "status")
_FACT_TEXT = ("type", "text", "why")


def normalize_unit_result(data: dict, idmap: Dict[str, str]) -> dict:
    """Validate model output; map short evidence ids back to real turn ids."""
    def ev(ids) -> List[str]:
        return [idmap[i] for i in _as_list(ids) if isinstance(i, str) and i in idmap]

    def subs(xs) -> List[dict]:
        return [{k: _s(v) for k, v in x.items()} for x in _as_list(xs) if isinstance(x, dict)]

    items = []
    for it in _as_list(data.get("work_items")):
        if not isinstance(it, dict) or not (it.get("title") or it.get("outcome")):
            continue
        it = dict(it)
        for k in _ITEM_TEXT:
            it[k] = _s(it.get(k))
        it["evidence"] = ev(it.get("evidence"))
        it["status"] = it["status"] or "discussed-only"
        for k in ("decisions", "metrics", "open_loops"):
            it[k] = subs(it.get(k))
        if not isinstance(it.get("rework"), dict):
            it["rework"] = {"count": 0, "what": ""}
        items.append(it)
    facts = []
    for f in _as_list(data.get("facts")):
        if not isinstance(f, dict) or not f.get("text"):
            continue
        f = dict(f)
        for k in _FACT_TEXT:
            f[k] = _s(f.get(k))
        f["evidence"] = ev(f.get("evidence"))
        f["anchors"] = [_s(a) for a in _as_list(f.get("anchors")) if a]
        facts.append(f)
    return {
        "headline": (_s(data.get("headline")) or "").strip(),
        "work_items": items,
        "facts": facts,
    }


def _cap_hit() -> bool:
    from . import claude_cli

    return claude_cli.CAP_HIT.is_set()


def _llm(system: str, user: str, *, fast: bool = False) -> Optional[Tuple[str, str]]:
    """One summarization call on the configured backend → (text, model)."""
    from .summarizer import _chat, active_provider

    provider = active_provider()
    if provider is None:
        return None
    if provider == "claude":
        from . import claude_cli

        return claude_cli.run(system, user, fast=fast)
    out = _chat(provider, system, user, max_tokens=4000, timeout=180)
    return (out, provider) if out else None


def run_unit(unit: Unit) -> Optional[dict]:
    """LLM-extract one unit. Returns a DB-ready row dict (with `items`) or None."""
    now = time.time()
    status = "final" if unit.day < _today().isoformat() else "partial"
    base = {
        "id": unit.id,
        "day": unit.day,
        "project_key": unit.project_key,
        "project_name": unit.project_name,
        "status": status,
        "turn_count": len(unit.turns),
        "session_count": unit.sessions,
        "automation_runs": unit.automation_runs,
        "first_ts": unit.turns[0].ts if unit.turns else None,
        "last_ts": unit.turns[-1].ts if unit.turns else None,
        "source_stores": json.dumps(unit.stores),
        "input_hash": unit.input_hash(),
        "prompt_version": PROMPT_VERSION,
        "host": socket.gethostname(),
        "created_at": now,
        "updated_at": now,
    }
    if not unit.turns:  # automation-only day: deterministic, no LLM
        body = {"headline": "", "work_items": [], "facts": [], "automation": unit.automation}
        return {**base, "headline": "", "body": json.dumps(body, ensure_ascii=False),
                "model": None, "items": []}

    prompt, idmap = _unit_prompt(unit)
    got = _llm_json(UNIT_SYSTEM, prompt, want=dict)
    if not got:
        return None
    data, used_model = got
    result = normalize_unit_result(data, idmap)
    result["automation"] = unit.automation
    items = []
    for i, it in enumerate(result["work_items"]):
        items.append({
            "id": f"{unit.id}#w{i}", "kind": "work", "type": it.get("type"),
            "title": it.get("title"), "text": it.get("outcome") or it.get("title") or "",
            "status": it.get("status"), "data": json.dumps(it, ensure_ascii=False),
        })
    for i, f in enumerate(result["facts"]):
        items.append({
            "id": f"{unit.id}#f{i}", "kind": "fact", "type": f.get("type"),
            "title": None, "text": f.get("text") or "", "status": None,
            "data": json.dumps(f, ensure_ascii=False),
        })
    return {**base, "headline": result["headline"], "body": json.dumps(result, ensure_ascii=False),
            "model": used_model, "items": items}


# ------------------------------------------------------------------------ cards

CARD_SYSTEM = """You maintain the long-lived PROJECT CARD for one software project. The card is what a developer (or a coding agent starting a new session) reads to get back into the project in 30 seconds. It is regenerated incrementally: you get the current card (may be empty) and the newest daily work records for this project, oldest first.

Return the full updated card as ONE JSON object:
{"summary": "one sentence: what this project is",
 "state": ["<=6 bullets: current version/branch, main thread of work, known blockers (say who blocks)"],
 "open_loops": [{"text": "", "owner": "user|agent|<other>", "since": "YYYY-MM-DD", "last_seen": "YYYY-MM-DD", "stale": false}],
 "decisions": [{"text": "", "why": "", "as_of": "YYYY-MM-DD"}],
 "pitfalls": [{"text": "symptom → root cause", "fix": "", "as_of": "YYYY-MM-DD"}],
 "definitions": [{"text": "metric/query definition, table, script path, config key…", "as_of": "YYYY-MM-DD"}],
 "metrics": [{"name": "", "value": "", "prev": "", "scope": "", "as_of": "YYYY-MM-DD"}],
 "preferences": [{"text": "", "as_of": "YYYY-MM-DD"}],
 "milestones": [{"text": "release / launch / shipped thing", "date": "YYYY-MM-DD"}]}

RULES
- Keep existing entries unless new records supersede or close them. Never invent facts that are not in the card or the records.
- decisions: only those still in force. When a new decision replaces an old one, drop the old one and write "(replaces: …)" in the new one's why.
- open_loops: close (remove) one only when a record shows it done; otherwise update last_seen when it reappears. Mark stale=true when last_seen is >14 days before the newest record date. Do not delete stale loops.
- pitfalls / definitions: deduplicate; keep the reusable ones. metrics: per metric keep only the latest value plus the previous one in "prev", always with scope.
- Caps: state 6, open_loops 10, decisions 12, pitfalls 12, definitions 12, metrics 10, preferences 8, milestones 10 — keep the most recent / most reusable.
- Be concrete (files, versions, numbers, tables). Language: same as the records (usually Chinese); keep identifiers verbatim.
- Inside string values never use the ASCII double quote (") — use 「」 instead.
- Output JSON only."""

_CARD_SECTIONS = ("state", "open_loops", "decisions", "pitfalls", "definitions",
                  "metrics", "preferences", "milestones")


def _compact_unit_for_card(row: dict) -> dict:
    body = row.get("body")
    try:
        data = json.loads(body) if isinstance(body, str) else (body or {})
    except ValueError:
        data = {}
    items = []
    for it in _as_list(data.get("work_items")):
        items.append({k: it.get(k) for k in ("title", "type", "outcome", "status", "decisions",
                                              "metrics", "open_loops") if it.get(k)})
    facts = [{k: f.get(k) for k in ("type", "text", "why", "anchors") if f.get(k)}
             for f in _as_list(data.get("facts"))]
    return {"date": row["day"], "headline": data.get("headline") or "", "work_items": items, "facts": facts}


def card_eligible(rows: List[dict]) -> bool:
    turns = sum(int(r.get("turn_count") or 0) for r in rows)
    days = len({r["day"] for r in rows if int(r.get("turn_count") or 0) > 0})
    return turns >= CARD_MIN_TURNS or days >= CARD_MIN_DAYS


def fold_card(project_key: str, card: Optional[dict], unit_rows: List[dict]) -> Optional[Tuple[dict, str]]:
    """Fold unit rows (oldest first) into the card. Returns (card, model)."""
    cur = card or {}
    model = ""
    chunk: List[dict] = []
    size = 0

    def _flush(cur_card: dict, ch: List[dict]) -> Optional[Tuple[dict, str]]:
        payload = (
            f"<project name=\"{project_name_for(project_key)}\" path=\"{project_key}\"/>\n"
            f"<current_card>{json.dumps(cur_card, ensure_ascii=False)}</current_card>\n"
            f"<new_records>{json.dumps(ch, ensure_ascii=False)}</new_records>"
        )
        got = _llm_json(CARD_SYSTEM, payload, want=dict)
        if not got:
            return None
        data, used_model = got
        clean = {"summary": str(data.get("summary") or cur_card.get("summary") or "")}
        for sec in _CARD_SECTIONS:
            clean[sec] = _as_list(data.get(sec))
        return clean, used_model

    for row in unit_rows:
        c = _compact_unit_for_card(row)
        if not c["work_items"] and not c["facts"]:
            continue
        n = len(json.dumps(c, ensure_ascii=False))
        if chunk and size + n > CARD_CHUNK_CHARS:
            res = _flush(cur, chunk)
            if res is None:
                return None
            cur, model = res
            chunk, size = [], 0
        chunk.append(c)
        size += n
    if chunk:
        res = _flush(cur, chunk)
        if res is None:
            return None
        cur, model = res
    return cur, model


# ---------------------------------------------------------------------- staging

def _staging_path() -> Path:
    return worklog_dir() / ".staging.jsonl"


def _load_staging() -> Dict[str, dict]:
    p = _staging_path()
    out: Dict[str, dict] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        out[rec.get("key", "")] = rec
    return out


_stage_lock = threading.Lock()


def _stage(key: str, fingerprint: str, row: dict) -> None:
    p = _staging_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"key": key, "fp": fingerprint, "row": row}, ensure_ascii=False)
    with _stage_lock, open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _unstage(keys: Iterable[str]) -> None:
    """Drop flushed entries from the staging file, keeping everything else
    (e.g. a crashed backfill's results when a narrower nightly run flushes)."""
    p = _staging_path()
    if not p.exists():
        return
    done = set(keys)
    with _stage_lock:
        keep = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                k = json.loads(line).get("key", "")
            except ValueError:
                continue
            if k not in done:
                keep.append(line)
        if keep:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, p)
        else:
            p.unlink()


# ------------------------------------------------------------------------- DB io

_UNIT_COLS = ["id", "day", "project_key", "project_name", "headline", "body", "status",
              "turn_count", "session_count", "automation_runs", "first_ts", "last_ts",
              "source_stores", "input_hash", "prompt_version", "model", "host",
              "created_at", "updated_at"]
_ITEM_COLS = ["id", "unit_id", "day", "project_key", "kind", "type", "title", "text",
              "status", "data", "created_at"]
_CARD_COLS = ["project_key", "project_name", "body", "folded", "through_day", "model",
              "host", "created_at", "updated_at"]


def _multi_upsert(db, table: str, cols: List[str], rows: List[tuple], conflict: str,
                  keep: Iterable[str] = ()) -> None:
    if not rows:
        return
    tup = "(" + ",".join("?" * len(cols)) + ")"
    per = max(1, 900 // len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != conflict and c not in keep)
    for i in range(0, len(rows), per):
        chunk = rows[i : i + per]
        sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES " + ",".join([tup] * len(chunk))
               + f" ON CONFLICT({conflict}) DO UPDATE SET {updates}")
        db.execute(sql, [v for r in chunk for v in r])


def load_state() -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """Existing unit fingerprints and project cards from the global store."""
    from .storage import Memory

    mem = _open(_gdir())
    try:
        units = {
            r["id"]: dict(r)
            for r in mem.db.execute(
                "SELECT id, day, project_key, input_hash, status, turn_count FROM worklog_units "
                "WHERE deleted_at IS NULL"
            ).fetchall()
        }
        cards = {r["project_key"]: dict(r) for r in mem.db.execute("SELECT * FROM project_cards").fetchall()}
    finally:
        mem.close()
    return units, cards


_UNIT_NUM = {"turn_count", "session_count", "automation_runs", "prompt_version"}
_UNIT_REAL = {"first_ts", "last_ts", "created_at", "updated_at"}


def _sanitize_unit(row: dict) -> dict:
    """Last line of defence before the insert (also covers staged rows written
    by older code): every column gets a type SQLite accepts."""
    out = dict(row)
    for c in _UNIT_COLS:
        v = out.get(c)
        if v is None:
            continue
        if c in _UNIT_NUM:
            try:
                out[c] = int(v)
            except (TypeError, ValueError):
                out[c] = 0
        elif c in _UNIT_REAL:
            try:
                out[c] = float(v)
            except (TypeError, ValueError):
                out[c] = None
        else:
            out[c] = _s(v)
    items = []
    for it in out.get("items") or []:
        it = dict(it)
        for k in ("id", "kind", "type", "title", "text", "status", "data"):
            it[k] = _s(it.get(k))
        it["text"] = it["text"] or ""
        items.append(it)
    out["items"] = items
    return out


def flush(unit_rows: List[dict], finalize_ids: List[str], card_rows: List[dict]) -> int:
    """Write everything in ONE transaction with ONE commit (one remote push on
    a synced global store). Returns the number of statements' worth of rows."""
    if not unit_rows and not finalize_ids and not card_rows:
        return 0
    from .storage import Memory

    now = time.time()
    unit_rows = [_sanitize_unit(r) for r in unit_rows]
    card_rows = [{k: (_s(v) if k not in ("created_at", "updated_at") else v) for k, v in r.items()}
                 for r in card_rows]
    mem = _open(_gdir())
    try:
        db = mem.db
        ids = [r["id"] for r in unit_rows]
        for i in range(0, len(ids), 900):
            chunk = ids[i : i + 900]
            db.execute(f"DELETE FROM work_items WHERE unit_id IN ({','.join('?' * len(chunk))})", chunk)
        _multi_upsert(db, "worklog_units", _UNIT_COLS,
                      [tuple(r.get(c) for c in _UNIT_COLS) for r in unit_rows],
                      "id", keep=("created_at",))
        item_rows = []
        for r in unit_rows:
            for it in r.get("items") or []:
                item_rows.append((it["id"], r["id"], r["day"], r["project_key"], it["kind"],
                                  it.get("type"), it.get("title"), it.get("text") or "",
                                  it.get("status"), it.get("data"), now))
        _multi_upsert(db, "work_items", _ITEM_COLS, item_rows, "id")
        for i in range(0, len(finalize_ids), 800):
            chunk = finalize_ids[i : i + 800]
            db.execute(
                f"UPDATE worklog_units SET status='final', updated_at=? WHERE id IN ({','.join('?' * len(chunk))})",
                [now, *chunk],
            )
        _multi_upsert(db, "project_cards", _CARD_COLS,
                      [tuple(r.get(c) for c in _CARD_COLS) for r in card_rows],
                      "project_key", keep=("created_at",))
        db.commit()
    finally:
        mem.close()
    return len(unit_rows) + len(finalize_ids) + len(card_rows)


# ----------------------------------------------------------------------- export

_STATUS_ICON = {
    "done": "✅", "done-unverified": "🧪", "uncommitted": "📝", "discussed-only": "💬",
    "abandoned": "🗑️",
}

_LABELS = {
    "zh": {"title": "工作日志", "projects": "个项目", "turns": "轮对话", "auto": "自动化任务",
           "partial": "当天未结束，会在下次运行时更新", "facts": "知识", "decision": "决策",
           "rejected": "否决", "metric": "指标", "todo": "待办", "rework": "返工",
           "automation": "自动化", "card_state": "现状", "card_loops": "未闭环",
           "card_decisions": "现行决策", "card_pitfalls": "踩坑", "card_defs": "口径 / 定义",
           "card_metrics": "指标", "card_prefs": "偏好 / 约定", "card_ms": "里程碑",
           "updated": "更新", "through": "覆盖至", "open_title": "未闭环事项", "stale": "久未推进",
           "weekdays": "一二三四五六日", "sessions": "个会话"},
    "en": {"title": "Work log", "projects": "projects", "turns": "turns", "auto": "automation runs",
           "partial": "day not over — refreshed on the next run", "facts": "Knowledge",
           "decision": "Decision", "rejected": "rejected", "metric": "Metric", "todo": "Todo",
           "rework": "Rework", "automation": "Automation", "card_state": "State",
           "card_loops": "Open loops", "card_decisions": "Decisions in force",
           "card_pitfalls": "Pitfalls", "card_defs": "Definitions", "card_metrics": "Metrics",
           "card_prefs": "Preferences", "card_ms": "Milestones", "updated": "updated",
           "through": "through", "open_title": "Open loops", "stale": "stale",
           "weekdays": "MTWTFSS", "sessions": "sessions"},
}


def _lang(texts: Iterable[str]) -> str:
    forced = os.environ.get("WORKLOG_LANG", "").strip().lower()
    if forced in _LABELS:
        return forced
    sample = "".join(t for t in texts if t)[:4000]
    cjk = sum(1 for ch in sample if "一" <= ch <= "鿿")
    return "zh" if cjk > len(sample) * 0.05 else "en"


def _status_icon(status: str) -> str:
    s = status or ""
    if s.startswith("blocked"):
        return "⛔"
    return _STATUS_ICON.get(s, "•")


def slug_map(keys: Iterable[str]) -> Dict[str, str]:
    """Stable, readable, collision-free file names for project keys."""
    out: Dict[str, str] = {}
    used: Dict[str, int] = {}
    for k in sorted(set(keys)):
        base = re.sub(r"[^\w.-]+", "-", project_name_for(k), flags=re.U).strip("-") or "project"
        used[base] = used.get(base, 0) + 1
        out[k] = base
    for k, base in list(out.items()):
        if used[base] > 1:
            out[k] = f"{base}-{hashlib.sha1(k.encode()).hexdigest()[:6]}"
    return out


def render_day(day: str, rows: List[dict], lang: str) -> str:
    L = _LABELS[lang]
    rows = sorted(rows, key=lambda r: -(r.get("turn_count") or 0))
    turns = sum(r.get("turn_count") or 0 for r in rows)
    auto = sum(r.get("automation_runs") or 0 for r in rows)
    firsts = [r["first_ts"] for r in rows if r.get("first_ts")]
    lasts = [r["last_ts"] for r in rows if r.get("last_ts")]
    d = datetime.strptime(day, "%Y-%m-%d")
    wd = L["weekdays"][d.weekday()]
    wd = f"周{wd}" if lang == "zh" else d.strftime("%a")
    active = [r for r in rows if (r.get("turn_count") or 0) > 0]
    lines = [f"# {L['title']} · {day} {wd}", ""]
    meta = f"> {len(active)} {L['projects']} · {turns} {L['turns']}"
    if firsts:
        meta += f" · {_hm(min(firsts))}–{_hm(max(lasts))}"
    if auto:
        meta += f" · {L['auto']} {auto}"
    lines.append(meta)
    if any(r.get("status") == "partial" for r in rows):
        lines.append(f"> ⏳ {L['partial']}")
    for r in rows:
        try:
            body = json.loads(r["body"])
        except (ValueError, TypeError):
            body = {}
        n = r.get("turn_count") or 0
        if not n and not body.get("automation"):
            continue
        lines.append("")
        head = f"## {r.get('project_name') or project_name_for(r['project_key'])}"
        if body.get("headline"):
            head += f" — {body['headline']}"
        lines.append(head)
        if n:
            lines.append(f"<sub>{n} {L['turns']} · {r.get('session_count') or 0} {L['sessions']} · "
                         f"{_hm(r.get('first_ts'))}–{_hm(r.get('last_ts'))}</sub>")
        for it in _as_list(body.get("work_items")):
            st = it.get("status") or ""
            lines.append(f"- {_status_icon(st)} **{it.get('title') or ''}** — {it.get('outcome') or ''} `{st}`")
            for dcs in _as_list(it.get("decisions")):
                s = f"  - {L['decision']}: {dcs.get('what') or ''}"
                if dcs.get("why"):
                    s += f" ({dcs['why']})"
                if dcs.get("rejected"):
                    s += f"; {L['rejected']}: {dcs['rejected']}"
                if dcs.get("by") == "assistant-proposed":
                    s += " [proposed]"
                lines.append(s)
            for m in _as_list(it.get("metrics")):
                lines.append(f"  - {L['metric']}: {m.get('name') or ''} = {m.get('value') or ''}"
                             + (f" ({m['scope']})" if m.get("scope") else ""))
            for ol in _as_list(it.get("open_loops")):
                lines.append(f"  - [ ] {L['todo']}: {ol.get('text') or ''}"
                             + (f" ({ol['owner']})" if ol.get("owner") else ""))
            rw = it.get("rework") if isinstance(it.get("rework"), dict) else {}
            if (rw.get("count") or 0) >= 2:
                lines.append(f"  - ↻ {L['rework']} ×{rw['count']}: {rw.get('what') or ''}")
        facts = _as_list(body.get("facts"))
        if facts:
            lines.append(f"\n**{L['facts']}**")
            for f in facts:
                s = f"- [{f.get('type') or 'fact'}] {f.get('text') or ''}"
                if f.get("why"):
                    s += f" — {f['why']}"
                anchors = [a for a in _as_list(f.get("anchors")) if a]
                if anchors:
                    s += " " + " ".join(f"`{a}`" for a in anchors[:4])
                lines.append(s)
        auto_map = body.get("automation") or {}
        if auto_map:
            lines.append(f"\n<sub>{L['automation']}: " + "; ".join(
                f"{k} ×{v}" for k, v in sorted(auto_map.items(), key=lambda kv: -kv[1])) + "</sub>")
    return "\n".join(lines).rstrip() + "\n"


def render_card(key: str, row: dict, lang: str) -> str:
    L = _LABELS[lang]
    try:
        card = json.loads(row["body"])
    except (ValueError, TypeError):
        card = {}
    lines = [f"# {row.get('project_name') or project_name_for(key)}", ""]
    if card.get("summary"):
        lines.append(f"> {card['summary']}")
    lines.append(f"<sub>{key} · {L['updated']} {datetime.fromtimestamp(row['updated_at']).strftime('%Y-%m-%d')}"
                 f" · {L['through']} {row.get('through_day') or '-'}</sub>")

    def sec(title: str, items: list, fmt: Callable[[dict], str]) -> None:
        items = [x for x in items if x]
        if not items:
            return
        lines.append(f"\n## {title}")
        for x in items:
            lines.append(fmt(x))

    sec(L["card_state"], _as_list(card.get("state")), lambda x: f"- {x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)}")
    sec(L["card_loops"], _as_list(card.get("open_loops")), lambda x: (
        f"- [ ] {x.get('text', '')}" + (f" ({x['owner']})" if x.get("owner") else "")
        + (f" · {x['since']}" if x.get("since") else "") + (f" ⚠️{L['stale']}" if x.get("stale") else "")
    ) if isinstance(x, dict) else f"- [ ] {x}")
    sec(L["card_decisions"], _as_list(card.get("decisions")), lambda x: (
        f"- {x.get('text', '')}" + (f" — {x['why']}" if x.get("why") else "")
        + (f" ({x['as_of']})" if x.get("as_of") else "")) if isinstance(x, dict) else f"- {x}")
    sec(L["card_pitfalls"], _as_list(card.get("pitfalls")), lambda x: (
        f"- {x.get('text', '')}" + (f" → {x['fix']}" if x.get("fix") else "")
        + (f" ({x['as_of']})" if x.get("as_of") else "")) if isinstance(x, dict) else f"- {x}")
    sec(L["card_defs"], _as_list(card.get("definitions")), lambda x: (
        f"- {x.get('text', '')}" + (f" ({x['as_of']})" if x.get("as_of") else "")) if isinstance(x, dict) else f"- {x}")
    sec(L["card_metrics"], _as_list(card.get("metrics")), lambda x: (
        f"- {x.get('name', '')}: {x.get('value', '')}" + (f" (prev {x['prev']})" if x.get("prev") else "")
        + (f" · {x['scope']}" if x.get("scope") else "") + (f" · {x['as_of']}" if x.get("as_of") else ""))
        if isinstance(x, dict) else f"- {x}")
    sec(L["card_prefs"], _as_list(card.get("preferences")), lambda x: (
        f"- {x.get('text', '')}") if isinstance(x, dict) else f"- {x}")
    sec(L["card_ms"], _as_list(card.get("milestones")), lambda x: (
        f"- {x.get('date', '')} {x.get('text', '')}") if isinstance(x, dict) else f"- {x}")
    return "\n".join(lines).rstrip() + "\n"


def _write_if_changed(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def export(out_dir: Optional[Path] = None) -> dict:
    """Render the journal from the DB into Markdown/JSON files (no LLM)."""
    from .storage import Memory

    out = out_dir or worklog_dir()
    mem = _open(_gdir())
    try:
        units = [dict(r) for r in mem.db.execute(
            "SELECT * FROM worklog_units WHERE deleted_at IS NULL ORDER BY day").fetchall()]
        cards = [dict(r) for r in mem.db.execute("SELECT * FROM project_cards").fetchall()]
    finally:
        mem.close()

    lang = _lang(r.get("headline") or "" for r in units)
    by_day: Dict[str, List[dict]] = {}
    for r in units:
        by_day.setdefault(r["day"], []).append(r)
    slugs = slug_map([c["project_key"] for c in cards])
    written = 0
    index = {"generated_at": time.time(), "lang": lang, "days": {}, "projects": {}}
    for day, rows in by_day.items():
        if _write_if_changed(out / "days" / f"{day}.md", render_day(day, rows, lang)):
            written += 1
        index["days"][day] = {
            "status": "partial" if any(r["status"] == "partial" for r in rows) else "final",
            "turns": sum(r.get("turn_count") or 0 for r in rows),
            "projects": sorted({r.get("project_name") or "" for r in rows if (r.get("turn_count") or 0) > 0}),
            "updated_at": max(r["updated_at"] for r in rows),
            "file": f"days/{day}.md",
        }
    loops_lines: List[str] = []
    for c in sorted(cards, key=lambda c: -(c.get("updated_at") or 0)):
        key, slug = c["project_key"], slugs[c["project_key"]]
        if _write_if_changed(out / "projects" / f"{slug}.md", render_card(key, c, lang)):
            written += 1
        try:
            card = json.loads(c["body"])
        except (ValueError, TypeError):
            card = {}
        _write_if_changed(out / "projects" / f"{slug}.json", json.dumps(
            {"project_key": key, "project_name": c.get("project_name"), "updated_at": c["updated_at"],
             "through_day": c.get("through_day"), "card": card}, ensure_ascii=False, indent=1))
        index["projects"][key] = {"name": c.get("project_name"), "slug": slug,
                                  "through_day": c.get("through_day"), "updated_at": c["updated_at"]}
        loops = [x for x in _as_list(card.get("open_loops")) if isinstance(x, dict)]
        if loops:
            loops_lines.append(f"\n## {c.get('project_name') or project_name_for(key)}")
            for x in loops:
                loops_lines.append(
                    f"- [ ] {x.get('text', '')}" + (f" ({x['owner']})" if x.get("owner") else "")
                    + (f" · {x['since']}" if x.get("since") else "")
                    + (f" ⚠️{_LABELS[lang]['stale']}" if x.get("stale") else ""))
    _write_if_changed(out / "open-loops.md",
                      f"# {_LABELS[lang]['open_title']}\n" + "\n".join(loops_lines) + "\n")
    latest = max(by_day) if by_day else None
    if latest:
        _write_if_changed(out / "latest.md", (out / "days" / f"{latest}.md").read_text(encoding="utf-8"))
    index["latest_day"] = latest
    _write_if_changed(out / "index.json", json.dumps(index, ensure_ascii=False, indent=1))
    return {"days": len(by_day), "cards": len(cards), "written": written, "dir": str(out)}


# ------------------------------------------------------------- session brief

def brief_for_cwd(cwd: str, max_chars: int = 1400) -> Optional[str]:
    """Compact project card for injection at the start of a session.

    Reads only the exported files (no DB, no lock) — safe on the prompt hook's
    hot path. Returns None when there is no fresh card for this project.
    """
    try:
        idx = json.loads((worklog_dir() / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key = project_key_for(cwd)
    ent = (idx.get("projects") or {}).get(key)
    if not ent:
        return None
    if time.time() - float(ent.get("updated_at") or 0) > BRIEF_MAX_AGE_DAYS * 86400:
        return None
    try:
        data = json.loads((worklog_dir() / "projects" / f"{ent['slug']}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    card = data.get("card") or {}
    lines = [f'<project-brief project="{ent.get("name")}" through="{ent.get("through_day")}">',
             "Background from the OpenLynx work journal — may be outdated; the code and git are the source of truth."]
    if card.get("summary"):
        lines.append(card["summary"])

    def add(title: str, xs: list, fmt: Callable, n: int) -> None:
        xs = [x for x in _as_list(xs) if x][:n]
        if xs:
            lines.append(f"{title}:")
            lines.extend(f"- {fmt(x)}" for x in xs)

    add("State", card.get("state"), lambda x: x if isinstance(x, str) else json.dumps(x, ensure_ascii=False), 4)
    add("Open loops", [x for x in _as_list(card.get("open_loops")) if isinstance(x, dict) and not x.get("stale")],
        lambda x: x.get("text", "") + (f" (since {x['since']})" if x.get("since") else ""), 4)
    add("Decisions", card.get("decisions"), lambda x: (x.get("text", "") + (f" — {x['why']}" if x.get("why") else ""))
        if isinstance(x, dict) else str(x), 3)
    add("Pitfalls", card.get("pitfalls"), lambda x: (x.get("text", "") + (f" → {x['fix']}" if x.get("fix") else ""))
        if isinstance(x, dict) else str(x), 3)
    lines.append("</project-brief>")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 20] + "\n…</project-brief>"


# -------------------------------------------------------------------- run

@dataclass
class RunStats:
    stores: int = 0
    turns: int = 0
    automation_turns: int = 0
    units_total: int = 0
    units_todo: int = 0
    units_done: int = 0
    units_failed: int = 0
    units_finalized: int = 0
    cards_todo: int = 0
    cards_done: int = 0
    cards_failed: int = 0
    aborted: bool = False
    exported: Optional[dict] = None
    reconciled: Dict[str, dict] = field(default_factory=dict)


class RunLocked(RuntimeError):
    pass


def _acquire_run_lock():
    import fcntl

    p = worklog_dir() / ".run.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RunLocked("another `lynx-memory worklog run` is in progress")
    return fd


def _excluded(key: str) -> bool:
    pats = [p.strip() for p in os.environ.get("WORKLOG_EXCLUDE", "").split(",") if p.strip()]
    return any(fnmatch.fnmatch(key, p) for p in pats)


def run(
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    days: int = 14,
    all_history: bool = False,
    jobs: int = 4,
    dry_run: bool = False,
    force: bool = False,
    limit: Optional[int] = None,
    project: Optional[str] = None,
    cards: bool = True,
    reconcile: bool = True,
    do_export: bool = True,
    log: Callable[[str], None] = print,
) -> RunStats:
    """Build / refresh the work journal.

    Default window: the last `days` days (catch-up for missed nights). Only
    units whose input changed (or are missing) are sent to the LLM.
    """
    from .config import load_env

    load_env(_gdir())
    stats = RunStats()
    lock_fd = None if dry_run else _acquire_run_lock()
    try:
        today = _today()
        if all_history:
            start_day = date(2000, 1, 1)
        elif since:
            start_day = datetime.strptime(since, "%Y-%m-%d").date()
        else:
            start_day = today - timedelta(days=max(0, days - 1))
        end_day = datetime.strptime(until, "%Y-%m-%d").date() if until else today
        win_lo = datetime.combine(start_day, datetime.min.time()).timestamp()
        win_hi = datetime.combine(end_day + timedelta(days=1), datetime.min.time()).timestamp()
        stats_lo = min(win_lo, datetime.combine(today - timedelta(days=STATS_LOOKBACK_DAYS),
                                                datetime.min.time()).timestamp())

        sources = discover_sources()
        stats.stores = len(sources)
        if reconcile and not dry_run:
            from .reconcile import reconcile_store

            for d in sources:
                try:
                    res = reconcile_store(d)
                except Exception as exc:
                    res = {"error": f"{type(exc).__name__}: {exc}"}
                if res.get("inserted") or res.get("updated") or res.get("error"):
                    stats.reconciled[str(d)] = res

        from .storage import SyncUnavailable

        by_id: Dict[str, Turn] = {}
        for d in sources:
            try:
                for t in snapshot_store(d, stats_lo, win_hi):
                    by_id.setdefault(t.id, t)  # merge/push-global copies keep their ids
            except SyncUnavailable as exc:
                # Building from a partial snapshot would overwrite good units
                # with thinner ones — stop and let the next run retry.
                log(f"  ! {exc} — aborting this run; it will be retried")
                stats.aborted = True
                return stats
            except Exception as exc:
                log(f"  ! skipped store {d}: {type(exc).__name__}: {exc}")
        all_turns = list(by_id.values())
        mark_automation(all_turns)
        window_turns = [t for t in all_turns if win_lo <= t.ts < win_hi and not _excluded(t.project_key)]
        if project:
            window_turns = [t for t in window_turns if project.lower() in t.project_key.lower()]
        stats.turns = len(window_turns)
        stats.automation_turns = sum(1 for t in window_turns if t.automation)

        units = build_units(window_turns)
        stats.units_total = len(units)
        existing, cards_state = load_state()
        today_s = today.isoformat()
        todo: List[Unit] = []
        finalize: List[str] = []
        for u in units:
            ex = existing.get(u.id)
            h = u.input_hash()
            if force or ex is None or ex["input_hash"] != h:
                todo.append(u)
            elif ex["status"] == "partial" and u.day < today_s:
                finalize.append(u.id)
        # newest first: today/yesterday land before a long backfill finishes
        todo.sort(key=lambda u: (u.day, len(u.turns)), reverse=True)
        if limit is not None:
            todo = todo[: max(0, limit)]
        stats.units_todo = len(todo)
        stats.units_finalized = len(finalize)
        llm_units = sum(1 for u in todo if u.turns)
        log(f"worklog: {stats.stores} stores · {stats.turns} turns in window "
            f"({stats.automation_turns} automation) · {stats.units_total} units · "
            f"{stats.units_todo} to build ({llm_units} LLM calls) · {len(finalize)} to finalize")
        if dry_run:
            per_project: Dict[str, int] = {}
            for u in todo:
                per_project[u.project_name] = per_project.get(u.project_name, 0) + 1
            for name, n in sorted(per_project.items(), key=lambda kv: -kv[1])[:25]:
                log(f"  {n:4d}  {name}")
            return stats

        staged = _load_staging()
        results: Dict[str, dict] = {}
        pending: List[Unit] = []
        for u in todo:
            rec = staged.get(u.id)
            if rec and rec.get("fp") == u.input_hash() and not force:
                results[u.id] = rec["row"]
            else:
                pending.append(u)
        if len(results):
            log(f"  resumed {len(results)} unit(s) from staging")

        fail_streak = 0
        abort = threading.Event()
        t0 = time.time()

        def _work(u: Unit) -> Tuple[Unit, Optional[dict], float]:
            if abort.is_set():
                return u, None, 0.0
            s = time.time()
            try:
                row = run_unit(u)
            except Exception as exc:
                log(f"  ! {u.day} {u.project_name}: {type(exc).__name__}: {exc}")
                row = None
            return u, row, time.time() - s

        done_n = 0
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futs = [pool.submit(_work, u) for u in pending]
            for fut in as_completed(futs):
                u, row, dt = fut.result()
                done_n += 1
                if row is not None:
                    results[u.id] = row
                    _stage(u.id, u.input_hash(), row)
                    fail_streak = 0
                    log(f"  [{done_n}/{len(pending)}] {u.day} {u.project_name} ✓ "
                        f"{len(u.turns)} turns, {len(row.get('items') or [])} items ({dt:.0f}s)")
                elif not abort.is_set() and _cap_hit():
                    abort.set()
                    stats.aborted = True
                    log("  ! Claude usage cap reached — stopping; built units are kept, the rest resume next run")
                elif not abort.is_set():
                    stats.units_failed += 1
                    fail_streak += 1
                    log(f"  [{done_n}/{len(pending)}] {u.day} {u.project_name} ✗ failed ({dt:.0f}s)")
                    if fail_streak >= MAX_CONSECUTIVE_FAILURES and not abort.is_set():
                        # queued jobs see the flag and return at once; results
                        # built so far are still flushed below
                        abort.set()
                        stats.aborted = True
                        log(f"  ! {fail_streak} consecutive failures — stopping (results so far are kept)")
        stats.units_done = len(results)
        log(f"  units: {stats.units_done} built, {stats.units_failed} failed in {time.time() - t0:.0f}s")

        card_rows: List[dict] = []
        if cards and not stats.aborted:
            card_rows = _update_cards(results, cards_state, staged, jobs, stats, project, log)

        flush(list(results.values()), finalize, card_rows)
        _unstage(list(results) + [f"card:{r['project_key']}" for r in card_rows])
        if do_export:
            stats.exported = export()
            log(f"  exported {stats.exported['days']} day(s), {stats.exported['cards']} card(s) → {stats.exported['dir']}")
        return stats
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _update_cards(results: Dict[str, dict], cards_state: Dict[str, dict], staged: Dict[str, dict],
                  jobs: int, stats: RunStats, project: Optional[str],
                  log: Callable[[str], None]) -> List[dict]:
    """Fold every not-yet-folded unit into its project's card."""
    from .storage import Memory

    mem = _open(_gdir())
    try:
        db_rows = {r["id"]: dict(r) for r in mem.db.execute(
            "SELECT id, day, project_key, project_name, body, input_hash, turn_count "
            "FROM worklog_units WHERE deleted_at IS NULL").fetchall()}
    finally:
        mem.close()
    rows = dict(db_rows)
    rows.update(results)  # fresh results win over the stored version

    by_project: Dict[str, List[dict]] = {}
    for r in rows.values():
        if is_bucket(r["project_key"]) or _excluded(r["project_key"]):
            continue
        if project and project.lower() not in r["project_key"].lower():
            continue
        by_project.setdefault(r["project_key"], []).append(r)

    plans = []
    for key, prow in by_project.items():
        if not card_eligible(prow):
            continue
        cur = cards_state.get(key)
        folded = {}
        if cur and cur.get("folded"):
            try:
                folded = json.loads(cur["folded"])
            except ValueError:
                folded = {}
        new = sorted([r for r in prow if folded.get(r["id"]) != r["input_hash"]], key=lambda r: r["day"])
        if not new:
            continue
        target = dict(folded)
        target.update({r["id"]: r["input_hash"] for r in new})
        plans.append((key, cur, new, target))
    stats.cards_todo = len(plans)
    if plans:
        log(f"  cards: {len(plans)} project(s) to update")

    out: List[dict] = []

    def _work(plan):
        key, cur, new, target = plan
        fp = hashlib.sha1(json.dumps(target, sort_keys=True).encode()).hexdigest()
        rec = staged.get(f"card:{key}")
        if rec and rec.get("fp") == fp:
            return rec["row"], True
        card = None
        if cur:
            try:
                card = json.loads(cur["body"])
            except ValueError:
                card = None
        res = fold_card(key, card, new)
        if res is None:
            return None, False
        now = time.time()
        row = {
            "project_key": key, "project_name": project_name_for(key),
            "body": json.dumps(res[0], ensure_ascii=False), "folded": json.dumps(target),
            "through_day": max(r["day"] for r in new) if not cur or not cur.get("through_day")
            else max(cur["through_day"], max(r["day"] for r in new)),
            "model": res[1], "host": socket.gethostname(),
            "created_at": (cur or {}).get("created_at") or now, "updated_at": now,
        }
        _stage(f"card:{key}", fp, row)
        return row, False

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futs = {pool.submit(_work, p): p[0] for p in plans}
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                row, cached = fut.result()
            except Exception as exc:
                row, cached = None, False
                log(f"  ! card {project_name_for(key)}: {type(exc).__name__}: {exc}")
            if row is None:
                stats.cards_failed += 1
                log(f"  card {project_name_for(key)} ✗ failed")
            else:
                stats.cards_done += 1
                out.append(row)
                log(f"  card {project_name_for(key)} ✓" + (" (staged)" if cached else ""))
    return out


# ------------------------------------------------------------------- status

def status() -> dict:
    from .storage import Memory

    mem = _open(_gdir())
    try:
        r = mem.db.execute(
            "SELECT COUNT(*) AS n, MIN(day) AS lo, MAX(day) AS hi, SUM(turn_count) AS turns, "
            "SUM(status='partial') AS partial, MAX(updated_at) AS upd FROM worklog_units "
            "WHERE deleted_at IS NULL").fetchone()
        items = mem.db.execute(
            "SELECT kind, COUNT(*) AS n FROM work_items WHERE deleted_at IS NULL GROUP BY kind").fetchall()
        cards = mem.db.execute("SELECT COUNT(*) AS n FROM project_cards").fetchone()
    finally:
        mem.close()
    return {
        "units": r["n"] or 0, "first_day": r["lo"], "last_day": r["hi"], "turns": r["turns"] or 0,
        "partial": r["partial"] or 0, "updated_at": r["upd"],
        "items": {x["kind"]: x["n"] for x in items}, "cards": cards["n"] or 0,
        "dir": str(worklog_dir()), "staged": len(_load_staging()),
    }
