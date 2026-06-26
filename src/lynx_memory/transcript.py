"""Shared transcript parsing + turn persistence for Stop / UserPromptSubmit hooks."""
import json
import os

from .config import load_env, paths_for, resolve_data_dir
from .storage import Memory


def read_transcript(path: str) -> list:
    msgs = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return msgs


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "tool_use":
                parts.append(f"[tool_use: {block.get('name', '')}]")
            elif t == "tool_result":
                r = block.get("content", "")
                if isinstance(r, list):
                    r = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in r
                    )
                parts.append(f"[tool_result: {str(r)[:200]}]")
        return "\n".join(p for p in parts if p)
    return str(content)


def _first_present(d: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = d.get(key)
        if value:
            return str(value)
    return ""


def _markdown_code_block(text: str, lang: str = "") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    suffix = lang if lang else ""
    return f"{fence}{suffix}\n{text}\n{fence}"


def _prefixed_lines(text: str, prefix: str) -> list[str]:
    lines = str(text).splitlines()
    if not lines:
        return [prefix]
    return [f"{prefix}{line}" for line in lines]


def _edit_diff(old_text: str, new_text: str) -> str:
    lines = _prefixed_lines(old_text, "-") + _prefixed_lines(new_text, "+")
    return _markdown_code_block("\n".join(lines), "diff")


def _write_diff(content: str) -> str:
    return _markdown_code_block("\n".join(_prefixed_lines(content, "+")), "diff")


def _format_claude_tool_use(block: dict) -> str:
    """Return Claude Code tool calls with code-bearing inputs preserved."""
    name = str(block.get("name") or "")
    raw_input = block.get("input")
    if not isinstance(raw_input, dict):
        return f"[tool_use: {name}]"

    title = f"**Tool: {name or 'tool_use'}**"
    file_path = _first_present(raw_input, ("file_path", "path", "notebook_path"))
    file_line = f"\nFile: `{file_path}`" if file_path else ""

    if name in ("Write", "NotebookWrite"):
        content = raw_input.get("content")
        if content is not None:
            return f"{title}{file_line}\n\n{_write_diff(str(content))}"

    if name in ("Edit", "NotebookEdit"):
        old = raw_input.get("old_string")
        new = raw_input.get("new_string")
        if old is not None or new is not None:
            old_text = str(old or "")
            new_text = str(new or "")
            return f"{title}{file_line}\n\n{_edit_diff(old_text, new_text)}"

    if name == "MultiEdit":
        edits = raw_input.get("edits")
        if isinstance(edits, list):
            parts = [f"{title}{file_line}"]
            for idx, edit in enumerate(edits, start=1):
                if not isinstance(edit, dict):
                    continue
                old_text = str(edit.get("old_string") or "")
                new_text = str(edit.get("new_string") or "")
                parts.append(f"Edit {idx}\n\n{_edit_diff(old_text, new_text)}")
            if len(parts) > 1:
                return "\n\n".join(parts)

    if name == "Bash":
        command = raw_input.get("command")
        if command:
            return f"{title}\n\n{_markdown_code_block(str(command), 'bash')}"

    return f"[tool_use: {name}]"


def _extract_assistant(content) -> tuple:
    if isinstance(content, str):
        return content, ""
    text_parts = []
    tool_parts = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                txt = block.get("text", "")
                if txt:
                    text_parts.append(txt)
            elif t == "tool_use":
                tool_parts.append(_format_claude_tool_use(block))
    return "\n".join(text_parts), "\n".join(tool_parts)


def _is_codex_transcript(path: str) -> bool:
    """Codex rollout files live under ~/.codex/sessions/.../rollout-*.jsonl."""
    return "/.codex/sessions/" in path or "/sessions/" in path and "/rollout-" in path


def _codex_text(content) -> tuple:
    """Extract (text, tool_marker) from a Codex `payload.content` list.

    Codex content blocks: {"type": "input_text"|"output_text", "text": "..."}.
    """
    if isinstance(content, str):
        return content, ""
    text_parts = []
    tool_parts = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t in ("input_text", "output_text"):
                txt = block.get("text", "")
                if txt:
                    text_parts.append(txt)
            elif t == "tool_use" or (isinstance(t, str) and t.startswith("tool")):
                tool_parts.append(f"[tool: {block.get('name', t)}]")
    return "\n".join(text_parts), "\n".join(tool_parts)


def _codex_apply_patch_text(payload: dict) -> str:
    """Return apply_patch tool calls as readable Markdown diff blocks."""
    if payload.get("name") != "apply_patch":
        return ""

    raw = payload.get("input")
    if raw is None:
        raw = payload.get("arguments")
    if isinstance(raw, dict):
        raw = raw.get("patch") or raw.get("input") or json.dumps(raw, ensure_ascii=False)
    text = str(raw or "").strip()
    if not text:
        return ""
    return f"**Tool: apply_patch**\n\n```diff\n{text}\n```"


def find_last_turn_codex(msgs: list) -> tuple:
    """Codex rollout JSONL → (user_text, user_uuid, asst_text, asst_uuid, had_prose).

    Codex line shape: {"type": "response_item", "payload": {"type": "message",
    "role": "user|assistant|developer", "content": [...]}, "timestamp": "..."}.
    Each turn has a stable `turn_id` exposed via `event_msg` lines (task_started,
    task_complete) — we use it as the UUID for both halves of the turn.

    Walking backward, we pick the last *real* user prompt: skip developer role
    and skip the env-context user message that Codex injects on session start
    (its text starts with "<environment_context>").
    """
    last_user_idx = None
    user_text = None
    last_user_turn_id = None

    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("type") != "response_item":
            continue
        p = m.get("payload") or {}
        if p.get("type") != "message" or p.get("role") != "user":
            continue
        text, _ = _codex_text(p.get("content", ""))
        if not text:
            continue
        stripped = text.lstrip()
        if stripped.startswith("<environment_context>"):
            continue
        user_text = text
        last_user_idx = i
        break

    if last_user_idx is None:
        return None, None, None, None, False

    # Find the turn_id by scanning forward for a turn_context / event_msg.
    for j in range(last_user_idx, len(msgs)):
        m = msgs[j]
        p = m.get("payload") or {}
        tid = p.get("turn_id")
        if tid:
            last_user_turn_id = tid
            break
    if last_user_turn_id is None:
        # Fallback: scan backward.
        for j in range(last_user_idx - 1, -1, -1):
            p = (msgs[j].get("payload") or {})
            if p.get("turn_id"):
                last_user_turn_id = p["turn_id"]
                break
    if last_user_turn_id is None:
        last_user_turn_id = f"codex-idx{last_user_idx}"

    text_parts: list[str] = []
    tool_parts: list[str] = []
    last_a_turn_id = None
    for j in range(last_user_idx + 1, len(msgs)):
        m = msgs[j]
        if m.get("type") != "response_item":
            continue
        p = m.get("payload") or {}
        payload_type = p.get("type")
        if payload_type == "message" and p.get("role") == "assistant":
            t, tools = _codex_text(p.get("content", ""))
            if t:
                text_parts.append(t)
            if tools:
                tool_parts.append(tools)
            last_a_turn_id = p.get("turn_id") or last_user_turn_id
            continue

        patch_text = _codex_apply_patch_text(p)
        if patch_text:
            tool_parts.append(patch_text)
            last_a_turn_id = p.get("turn_id") or last_user_turn_id

    if last_a_turn_id is None:
        return None, None, None, None, False

    had_prose = bool(text_parts)
    asst_parts = text_parts + tool_parts if had_prose else tool_parts
    asst_text = "\n\n".join(asst_parts)
    # Use `<turn_id>:user` / `<turn_id>:assistant` so both halves disambiguate
    # if two turns share an id (defensive — shouldn't happen).
    user_uuid = f"{last_user_turn_id}:user"
    asst_uuid = f"{last_a_turn_id}:assistant"
    return user_text, user_uuid, asst_text, asst_uuid, had_prose


def find_last_turn(msgs: list) -> tuple:
    last_user_idx = None
    user_text = None
    user_uuid = None
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        msg = m.get("message") or {}
        if m.get("type") == "user" or msg.get("role") == "user":
            txt = extract_text(msg.get("content", ""))
            if txt and not txt.lstrip().startswith("[tool_result"):
                user_text = txt
                user_uuid = m.get("uuid") or f"idx{i}"
                last_user_idx = i
                break
    if last_user_idx is None:
        return None, None, None, None, False

    text_parts = []
    tool_parts = []
    last_a_uuid = None
    for j in range(last_user_idx + 1, len(msgs)):
        m = msgs[j]
        msg = m.get("message") or {}
        if m.get("type") == "assistant" or msg.get("role") == "assistant":
            t_only, tools = _extract_assistant(msg.get("content", ""))
            if t_only:
                text_parts.append(t_only)
            if tools:
                tool_parts.append(tools)
            last_a_uuid = m.get("uuid") or f"idx{j}"

    if last_a_uuid is None:
        return None, None, None, None, False

    had_prose = bool(text_parts)
    asst_parts = text_parts + tool_parts if had_prose else tool_parts
    asst_text = "\n\n".join(asst_parts)
    return user_text, user_uuid, asst_text, last_a_uuid, had_prose


def _load_state(state_path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return {}


def _save_state(state_path, d: dict) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(d))
    except Exception:
        pass


def persist_last_turn(
    transcript_path: str,
    session_id: str,
    cwd: str,
    require_prose: bool = False,
) -> str:
    if not transcript_path or not os.path.exists(transcript_path):
        return ""

    msgs = read_transcript(transcript_path)
    if _is_codex_transcript(transcript_path):
        user_text, user_uuid, asst_text, a_uuid, had_prose = find_last_turn_codex(msgs)
    else:
        user_text, user_uuid, asst_text, a_uuid, had_prose = find_last_turn(msgs)
    if not user_text or not asst_text or not user_uuid:
        return ""
    if require_prose and not had_prose:
        return ""

    data_dir = resolve_data_dir(cwd)
    load_env(data_dir)  # ensure sync config + API keys are present in this hook process
    state_path = paths_for(data_dir)["state_path"]

    # Fast path: if state file already records this assistant uuid as
    # persisted, skip opening the DB entirely. Both hooks (Stop and
    # on_prompt) end up here; the second call should be a no-op when the
    # first already wrote the turn.
    state = _load_state(state_path)
    if state.get(session_id) == a_uuid:
        return "skip"

    user_text = user_text[:8000]

    # Goal relevance gate: when this store has a goal, an LLM decides whether
    # the turn is worth keeping. Irrelevant turns never touch the DB. We record
    # the assistant uuid in the state file even on a drop so the sibling hook
    # (Stop vs UserPromptSubmit) doesn't re-judge the same turn. Fails open.
    from .goals import evaluate_turn_relevance

    if evaluate_turn_relevance(data_dir, user_text, asst_text) == "drop":
        state[session_id] = a_uuid
        _save_state(state_path, state)
        return "gated"

    mem = Memory(data_dir=data_dir)
    try:
        mem.ensure_session(session_id, cwd)
        turn_id, action = mem.upsert_turn(session_id, user_uuid, user_text, asst_text, cwd)
    finally:
        mem.close()

    if action in ("insert", "update"):
        try:
            from .summarizer import spawn_background

            spawn_background(str(data_dir), turn_id)
        except Exception:
            pass
        state[session_id] = a_uuid
        _save_state(state_path, state)
    return action
