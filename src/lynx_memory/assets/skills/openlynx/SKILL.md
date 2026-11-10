---
name: openlynx
description: Use OpenLynx memory commands to inspect, browse, merge, or delete persisted coding-agent memory, and to read the work journal (what was done per project and day, project cards, open loops).
---

# OpenLynx Memory

OpenLynx provides shared long-term memory across supported coding agents.

Use the installed slash commands or CLI:

- `lynx-memory status` to inspect active project/global scope and database stats.
- `lynx-memory web` to open the local history browser.
- `lynx-memory goal show` to view the active goal; `lynx-memory goal set "..."` to set one.
- `lynx-memory merge --from global --to project --dry-run` to preview memory moves.
- `lynx-memory delete --scope <project|global|both>` only after explicit user confirmation.
- `lynx-memory sync status` to show cloud-sync config; `lynx-memory sync init` to sync the current store to a Turso database for cross-machine recall (creates a remote DB — confirm with the user first).

**Work journal** — Claude-written records of what got done, per project and day:

- `lynx-memory worklog show [--day YYYY-MM-DD]` prints a day's log; `--project <name>` prints a project card (state, open loops, decisions in force, pitfalls, definitions, metrics).
- Files to read directly: `~/.openlynx/worklog/days/<date>.md`, `~/.openlynx/worklog/projects/<name>.md`, `~/.openlynx/worklog/open-loops.md`, `~/.openlynx/worklog/index.json`.
- `lynx-memory worklog run` refreshes the last 14 days (only changed units call the LLM); `lynx-memory worklog backfill` summarizes all history — it can make hundreds of `claude -p` calls, so run `--dry-run` first and confirm with the user.
- `lynx-memory schedule install|status|uninstall` manages the nightly job.

When the user asks "what did I do / how did I solve X / what's still open", check the journal before searching raw turns.

A **goal** is an optional per-scope statement of what the user is working toward.
When set, OpenLynx stores only turns an LLM judges relevant to the goal (strict by
default) and focuses summaries on it. With no goal, every turn is stored normally.

Global OpenLynx state lives under `~/.openlynx/` by default. Project-scoped
memory lives under `<project>/.lynx-memory/` when that marker exists.
