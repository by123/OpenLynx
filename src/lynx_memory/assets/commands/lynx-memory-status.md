---
description: Show the current lynx-memory scope (project / global) and stats
allowed-tools: Bash(lynx-memory:*)
---

Run:

```bash
lynx-memory status
```

Summarize the key points for the user:

- **Current scope**: `project` or `global`
- Path of the **active data directory**
- Number of sessions / turns / summaries
- Whether a project-level repo `./.lynx-memory/` exists in the current directory
- Whether the global repo `~/.openlynx/` exists

Do not perform any write or merge operations.
