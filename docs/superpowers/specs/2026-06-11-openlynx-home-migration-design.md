# OpenLynx Home Migration Design

## Goal

Move OpenLynx-owned data and reusable client artifacts out of host-specific
directories and into a neutral global home:

```text
~/.openlynx/
```

Claude Code, Codex CLI, and future hosts should share the same memory store,
environment configuration, slash command files, and skill files. Host-specific
directories should only contain integration points required by that host, such
as hook configuration files and symlinks.

## Current State

The package currently treats `~/.claude/lynx-memory/` as the global data store.
It writes `.env`, SQLite, Chroma, hook logs, and transient state under that
directory. `lynx-memory init` also copies bundled slash commands directly into
`~/.claude/commands/`.

Codex support already exists, but it shares data by pointing at the Claude
directory. That works technically, but makes Claude Code the implicit owner of
global OpenLynx state.

## Target Layout

The default global home becomes:

```text
~/.openlynx/
  .env
  db/
    memory.db
    chroma/
    hook.log
    last_turn.json
  commands/
    lynx-memory-status.md
    lynx-memory-pull-global.md
    lynx-memory-push-global.md
    lynx-memory-delete.md
    lynx-memory-history.md
  skills/
    openlynx/
      SKILL.md
```

`LYNX_MEMORY_DIR` continues to override the data directory for users who need a
custom location. Without that environment variable, all global storage resolves
to `~/.openlynx/`.

Project-scoped memory remains unchanged:

```text
<project>/.lynx-memory/
```

Project scope is intentionally separate from global shared scope so unrelated
projects do not automatically share local histories.

## Host Integration

Claude Code keeps host-owned configuration in:

```text
~/.claude/settings.json
~/.claude/commands/
~/.claude/skills/
```

Codex keeps host-owned configuration in:

```text
~/.codex/config.toml
~/.codex/hooks.json
~/.codex/commands/
~/.codex/skills/
```

OpenLynx installs reusable artifacts once into `~/.openlynx/`, then links host
directories to those shared files:

```text
~/.claude/commands/lynx-memory-status.md -> ~/.openlynx/commands/lynx-memory-status.md
~/.codex/commands/lynx-memory-status.md  -> ~/.openlynx/commands/lynx-memory-status.md

~/.claude/skills/openlynx -> ~/.openlynx/skills/openlynx
~/.codex/skills/openlynx  -> ~/.openlynx/skills/openlynx
```

Hook registrations remain host-specific because each host loads hooks from its
own configuration format. The hook commands continue to be the package console
scripts, and those scripts resolve shared global state through the new default
home.

## Migration Behavior

On `lynx-memory init`, OpenLynx checks the legacy global store:

```text
~/.claude/lynx-memory/
```

If `~/.openlynx/` does not exist and the legacy directory exists, the installer
migrates the legacy directory to `~/.openlynx/`. If both directories exist, the
installer does not merge automatically. It keeps using `~/.openlynx/` and prints
a warning that the legacy directory still exists.

The installer must not delete the legacy directory automatically. Avoiding data
loss is more important than cleaning up old paths. Users can remove the legacy
directory manually after verifying `lynx-memory status`.

## Command And Skill Installation

Bundled commands are written to:

```text
~/.openlynx/commands/
```

Host command paths become symlinks to the shared files. If an existing host
command file already points to the desired shared file, installation is a no-op.
If an existing host command is a regular file with the same bundled content, the
installer may replace it with a symlink after backing it up. If it differs from
the bundled content, the installer backs it up before linking.

Bundled skills, once present in the package, are written to:

```text
~/.openlynx/skills/openlynx/
```

The host skill entries link to that shared directory. If this package version
does not yet ship an OpenLynx skill asset, the installer should create the
shared parent directories but skip host skill links unless there is a real
target to link.

## Uninstall Behavior

`lynx-memory uninstall` removes host hook registrations and OpenLynx-created
host symlinks. It must not delete:

```text
~/.openlynx/
```

Memory data remains user-owned state. Deleting memory continues to require
`lynx-memory delete` or an explicit manual removal.

If a host command or skill path is no longer an OpenLynx-managed symlink, the
uninstaller leaves it in place and reports that it was skipped.

## Status And Diagnostics

`lynx-memory status` should show:

```text
openlynx home : ~/.openlynx
legacy dir    : ~/.claude/lynx-memory (exists=true/false)
env file      : ~/.openlynx/.env
database      : ~/.openlynx/db/memory.db
Claude hooks  : ...
Codex hooks   : ...
```

For project scope, status should still show the active project data directory,
but it should also identify the global OpenLynx home for clarity.

## Error Handling

The installer should be idempotent. Running `lynx-memory init` repeatedly should
not duplicate hooks, rewrite unchanged shared files, or create nested symlinks.

When symlinks are unsupported or fail, the installer should fall back to copying
the shared artifact into the host directory and print a warning. This keeps the
install usable on restrictive filesystems while preserving shared storage as the
normal path.

When migration fails, the installer should stop before changing hook
registrations. Partial state in host configs should not point to a data
directory that was not prepared successfully.

## Tests

Add focused tests for:

- Default global path resolves to `~/.openlynx/`.
- `LYNX_MEMORY_DIR` still overrides the global path.
- Legacy `~/.claude/lynx-memory/` migrates only when the new home is absent.
- Existing new and legacy directories do not auto-merge.
- Command installation writes shared files and creates host symlinks.
- Re-running installation is idempotent.
- Uninstall removes OpenLynx-managed symlinks but preserves `~/.openlynx/`.
- Codex hook feature flag behavior still works.

## Documentation

Update README and README.zh-CN so installation no longer presents
`~/.claude/lynx-memory/` as the global store. The docs should describe
`~/.openlynx/` as the shared home and mention the one-time migration from the
legacy Claude path.
