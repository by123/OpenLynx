---
name: openlynx
description: Use OpenLynx memory commands to inspect, browse, merge, or delete persisted coding-agent memory.
---

# OpenLynx Memory

OpenLynx provides shared long-term memory across supported coding agents.

Use the installed slash commands or CLI:

- `lynx-memory status` to inspect active project/global scope and database stats.
- `lynx-memory web` to open the local history browser.
- `lynx-memory merge --from global --to project --dry-run` to preview memory moves.
- `lynx-memory delete --scope <project|global|both>` only after explicit user confirmation.

Global OpenLynx state lives under `~/.openlynx/` by default. Project-scoped
memory lives under `<project>/.lynx-memory/` when that marker exists.
