"""Imported first by every test module: point OpenLynx at a throw-away home
so tests never read the developer's real ~/.openlynx/.env (API keys,
SUMMARY_BACKEND=claude …), never append to the real hook.log, and never shell
out to a real `claude -p`."""
import os
import tempfile

_HOME = tempfile.mkdtemp(prefix="openlynx-tests-")
os.environ["LYNX_MEMORY_DIR"] = _HOME
os.environ["OPENLYNX_HOME"] = _HOME
os.environ["CLAUDE_BIN"] = os.path.join(_HOME, "no-claude")  # claude_cli.available() → False
os.environ.pop("SUMMARY_BACKEND", None)
