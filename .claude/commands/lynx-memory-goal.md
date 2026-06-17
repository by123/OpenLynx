---
description: View or set the lynx-memory goal that gates storage and focuses summaries
allowed-tools: Bash(lynx-memory:*)
---

A goal is an optional, per-scope (project or global) statement of what the user
is working toward. When a goal is set, OpenLynx:

- stores only turns an LLM judges relevant to the goal (strict by default);
- focuses per-turn and per-session summaries on the goal.

When no goal is set, memory behaves normally (every turn stored and summarized).

## Show the current goal(s)

```bash
lynx-memory goal show
```

Report back to the user:

- The **project** goal (if a `./.lynx-memory/` store exists) and the **global** goal.
- Whether gating is **on/off** and its **strictness** (`loose` / `balanced` / `strict`).
- Any warning that a summarization LLM key is missing (gating then stays inactive).

## Set or change the goal

Only run this when the user explicitly asks to set a goal. Use their exact wording.

```bash
# active scope (project if inside one, else global)
lynx-memory goal set "Ship the v2 billing API and migrate existing customers"

# force a scope
lynx-memory goal set --scope global "Keep my Rust learning notes"
lynx-memory goal set --scope project "Refactor the auth module to passkeys"
```

## Clear the goal

```bash
lynx-memory goal clear            # active scope, asks for confirmation
lynx-memory goal clear --scope global -y
```

Note: changing the goal does not delete already-stored turns; it only affects
which future turns are kept and how future summaries are written.
