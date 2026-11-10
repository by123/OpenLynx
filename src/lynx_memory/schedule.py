"""Install the nightly work-journal job (`lynx-memory worklog run`).

macOS: a LaunchAgent (~/Library/LaunchAgents/com.openlynx.worklog.plist) with
one StartCalendarInterval per time. launchd fires a missed interval once when
the Mac wakes; anything missed while powered off is caught up by the next run
anyway, because `worklog run` rebuilds every missing/stale day in its window.

Linux: tagged lines in the user's crontab.

Default times are 20:30 (builds today's partial journal before an evening
review) and 08:30 (finalizes yesterday before a morning review).
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

LABEL = "com.openlynx.worklog"
CRON_TAG = "# openlynx-worklog"
DEFAULT_TIMES = ("20:30", "08:30")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_dir() -> Path:
    from .config import GLOBAL_DATA_DIR

    return GLOBAL_DATA_DIR / "logs"


def parse_times(values: Optional[List[str]]) -> List[tuple]:
    out = []
    for v in values or DEFAULT_TIMES:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", v.strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f"invalid time {v!r} (use HH:MM)")
        out.append((int(m.group(1)), int(m.group(2))))
    return sorted(set(out))


def _command(extra: Optional[List[str]] = None) -> List[str]:
    return [sys.executable, "-m", "lynx_memory.cli", "worklog", "run", *(extra or [])]


def _path_env() -> str:
    """PATH for the job: launchd starts with a bare PATH, but `claude -p`
    needs to be found (and node, for npm-installed claude)."""
    dirs = []
    from .claude_cli import claude_bin

    cb = claude_bin()
    if cb:
        dirs.append(str(Path(cb).resolve().parent))
        dirs.append(str(Path(cb).parent))
    node = shutil.which("node")
    if node:
        dirs.append(str(Path(node).parent))
    dirs.append(str(Path(sys.executable).parent))
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return ":".join(out)


def build_plist(times: List[tuple], extra: Optional[List[str]] = None) -> bytes:
    from .claude_cli import claude_bin

    logs = log_dir()
    env = {"HOME": str(Path.home()), "PATH": _path_env(), "LYNX_MEMORY_NO_HOOK": "1"}
    cb = claude_bin()
    if cb:
        env["CLAUDE_BIN"] = cb
    data = {
        "Label": LABEL,
        # caffeinate keeps the Mac awake for the length of the run
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", *_command(extra)],
        "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in times],
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs / "worklog.log"),
        "StandardErrorPath": str(logs / "worklog.err.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    return plistlib.dumps(data)


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _cron_lines(times: List[tuple], extra: Optional[List[str]] = None) -> List[str]:
    from .config import GLOBAL_DATA_DIR

    cmd = " ".join(_command(extra))
    log = GLOBAL_DATA_DIR / "logs" / "worklog.log"
    return [f"{m} {h} * * * LYNX_MEMORY_NO_HOOK=1 {cmd} >> {log} 2>&1 {CRON_TAG}" for h, m in times]


def _read_crontab() -> str:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _write_crontab(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def install(times: Optional[List[str]] = None, extra: Optional[List[str]] = None) -> str:
    parsed = parse_times(times)
    log_dir().mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        p = plist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        _launchctl("bootout", f"gui/{uid}/{LABEL}")  # ignore "not loaded"
        p.write_bytes(build_plist(parsed, extra))
        r = _launchctl("bootstrap", f"gui/{uid}", str(p))
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
        return f"launchd job {LABEL} → {p}"
    cur = [l for l in _read_crontab().splitlines() if CRON_TAG not in l]
    _write_crontab("\n".join(cur + _cron_lines(parsed, extra)) + "\n")
    return "crontab entries installed"


def uninstall() -> str:
    if sys.platform == "darwin":
        p = plist_path()
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        if p.exists():
            p.unlink()
            return f"removed {p}"
        return "not installed"
    cur = _read_crontab().splitlines()
    kept = [l for l in cur if CRON_TAG not in l]
    if len(kept) == len(cur):
        return "not installed"
    _write_crontab("\n".join(kept) + ("\n" if kept else ""))
    return "crontab entries removed"


def status() -> dict:
    if sys.platform == "darwin":
        p = plist_path()
        info = {"installed": p.exists(), "path": str(p), "loaded": False, "times": []}
        if p.exists():
            try:
                d = plistlib.loads(p.read_bytes())
                info["times"] = [f"{x['Hour']:02d}:{x['Minute']:02d}" for x in d.get("StartCalendarInterval", [])]
                info["command"] = " ".join(d.get("ProgramArguments", []))
            except Exception:
                pass
            r = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
            info["loaded"] = r.returncode == 0
            m = re.search(r"last exit code = ([^\n]+)", r.stdout or "")
            if m:
                info["last_exit"] = m.group(1)
        return info
    lines = [l for l in _read_crontab().splitlines() if CRON_TAG in l]
    return {"installed": bool(lines), "entries": lines}


def run_now() -> str:
    if sys.platform == "darwin" and plist_path().exists():
        r = _launchctl("kickstart", f"gui/{os.getuid()}/{LABEL}")
        if r.returncode == 0:
            return f"started {LABEL} (logs: {log_dir() / 'worklog.log'})"
        raise RuntimeError(r.stderr.strip() or "kickstart failed")
    raise RuntimeError("schedule not installed (run `lynx-memory schedule install`)")
