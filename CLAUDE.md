# memsync — Project Context for Claude Code

You are building **memsync**, a cross-platform CLI tool that maintains a global
identity-layer memory file for Claude Code users, synced across machines via
cloud storage they already have (OneDrive, iCloud Drive, Google Drive).

This document is your entry point. Read all linked documents before writing any code.

---

## Document map

| File | What it contains |
|---|---|
| `CLAUDE.md` | This file — start here |
| `ARCHITECTURE.md` | Full system design, module map, data flow |
| `PROVIDERS.md` | Provider plugin system — BaseProvider ABC, all three implementations |
| `CONFIG.md` | Config file design, schema, platform paths |
| `COMMANDS.md` | Every CLI command — args, behavior, edge cases |
| `EXISTING_CODE.md` | Working prototype code — use this as the base, not a reference |
| `PITFALLS.md` | Known issues, trust boundaries, things that have already gone wrong |
| `REPO.md` | Repository structure, CI, PyPI, GitHub conventions |
| `STYLE.md` | Code style, naming conventions, what good looks like here |

Read them in this order:
1. ARCHITECTURE.md — understand the shape of the system
2. EXISTING_CODE.md — understand what already works
3. PROVIDERS.md — the most important new piece
4. CONFIG.md — feeds into everything
5. PITFALLS.md — read before touching sync.py or providers
6. COMMANDS.md, REPO.md, STYLE.md — as needed

---

## What this project is

memsync solves a specific problem: Claude Code has no memory between sessions.
The standard fix is `~/.claude/CLAUDE.md`, but it drifts, bloats, and doesn't
sync across machines.

memsync maintains one canonical `GLOBAL_MEMORY.md` in your cloud sync folder.
At session start, Claude Code reads it via a symlink (Mac/Linux) or copy (Windows)
at `~/.claude/CLAUDE.md`. After meaningful sessions, the user runs
`memsync refresh --notes "..."` and the Claude API merges the notes in.

This is the **identity layer** — who the user is, what they're working on, standing
preferences. Not project docs. Not cold storage. Not a knowledge base.

---

## What already exists

A working prototype was built in a Claude.ai chat session. It covers:
- OneDrive path detection (Mac + Windows)
- Core refresh logic (Claude API call, backup, sync to CLAUDE.md)
- CLI with: init, refresh, status, show, diff, prune

All prototype code is in `EXISTING_CODE.md`. Use it as the foundation.
Do not rewrite from scratch — refactor to fit the target architecture.

---

## What needs to be built

1. Provider abstraction layer (`memsync/providers/`) — BaseProvider ABC + 3 implementations
2. Config system (`memsync/config.py`) — TOML, platform-aware paths
3. Refactor existing code to use config + providers
4. Tests (`tests/`) — mocked filesystem + mocked API
5. CI (`.github/workflows/`) — test matrix Mac/Windows/Linux × Python 3.11/3.12
6. Docs — README, CONTRIBUTING, adding-a-provider guide
7. GitHub issue templates

See `REPO.md` for full repository layout and build order.

---

## Hard rules

- Never hardcode the model string. Always read from config.
- Never hardcode any path. Always go through the provider or config system.
- Hard constraints in GLOBAL_MEMORY.md are append-only. Enforce this in code, not prompts.
- Backups before every write. No exceptions.
- See `PITFALLS.md` before touching anything related to the Claude API call or path resolution.

---

## Owner context

Built by Ian (product leader, writer, not a full-time engineer).
Maintenance appetite: active at launch, wants to go passive or hand off over time.
That means: clear contributor docs, plugin architecture that doesn't require
touching core to add a provider, and CI that catches regressions without manual effort.
