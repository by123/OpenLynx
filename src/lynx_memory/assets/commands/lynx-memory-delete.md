---
description: Permanently delete lynx-memory history (requires double confirmation)
allowed-tools: Bash(lynx-memory:*)
---

You are helping the user **permanently delete** lynx-memory history (sqlite + chroma vector store). This is irreversible. Follow the steps below strictly.

## Step 1: Check current status

```bash
lynx-memory status
```

Report to the user: whether the project repo `./.lynx-memory/` and the global repo `~/.openlynx/` exist, and the turn / summary counts of each.

## Step 2: Ask which scope to delete

Give the user three explicit options and ask them to pick one:

- `project` — delete only the current project's memory
- `global` — delete only the global memory
- `both` — delete both

## Step 3: First confirmation

Restate the directory paths and counts that will be deleted, then ask: "Confirm deletion? Reply `DELETE` to continue."

- The user must reply with the **exact English word `DELETE`** to pass the first confirmation.
- Any other reply means abort — stop immediately and tell the user it has been cancelled.

## Step 4: Second confirmation

After the first one passes, ask again: "Final confirmation: are you sure you want to permanently delete all memory for [scope]? (y/N)"

- The user must explicitly reply `y` or `yes`.
- Any other reply means abort — stop immediately and tell the user it has been cancelled.

## Step 5: Run the delete

Only after both confirmations pass:

```bash
lynx-memory delete --scope <project|global|both> --yes
```

(`--yes` is used because the double confirmation has already happened in chat; the CLI's own interactive prompt can be skipped.)

## Step 6: Check status after deletion

```bash
lynx-memory status
```

Report the change to the user.

$ARGUMENTS
