# ARCHITECTURE.md

## System overview

```
User runs: memsync harvest              User runs: memsync refresh --notes "..."
                │                                           │
                ▼                                           ▼
         memsync/cli.py          ← argument parsing, routes to commands
                │
                ▼
         memsync/config.py       ← loads ~/.config/memsync/config.toml
                │
          ┌─────┴─────┐
          ▼           ▼
memsync/harvest.py   memsync/providers/<x>.py  ← resolves sync root path
  (reads session               │
   transcripts)                ▼
          │           memsync/sync.py    ← calls Claude API (harvest or refresh)
          └─────┬─────┘
                ▼
       memsync/backups.py        ← backs up before writing
                │
                ▼
       memsync/claude_md.py      ← syncs GLOBAL_MEMORY.md → ~/.claude/CLAUDE.md
```

---

## Module responsibilities

### `memsync/cli.py`
- Entry point. Parses args, loads config, routes to command functions.
- Does NOT contain business logic — only wiring.
- Every command function signature: `def cmd_<name>(args, config) -> int`
- Returns exit code. Print errors to stderr, output to stdout.

### `memsync/config.py`
- Loads and saves `~/.config/memsync/config.toml` (Mac/Linux)
  or `%APPDATA%\memsync\config.toml` (Windows).
- Exposes a `Config` dataclass — no raw dicts passed around the codebase.
- Handles missing keys with sensible defaults.
- See `CONFIG.md` for full schema.

### `memsync/providers/`
- `__init__.py` — defines `BaseProvider` ABC and `get_provider(name)` registry function.
- One file per provider: `onedrive.py`, `icloud.py`, `gdrive.py`, `custom.py`.
- Each provider implements `detect() -> Path | None` and `is_available() -> bool`.
- See `PROVIDERS.md` for full spec and all three implementations.

### `memsync/harvest.py`
- Reads Claude Code session JSONL files from `~/.claude/projects/<key>/`.
- `cwd_to_project_key(cwd)` — maps a working directory path to the Claude Code project key.
- `find_project_dir(cwd)` — finds `~/.claude/projects/<key>` for the given directory.
- `read_session_transcript(path)` — parses JSONL, extracts human messages + assistant text, skips tool calls/results/thinking. Returns `(transcript, message_count)`.
- `load_harvested_index(memory_root)` / `save_harvested_index(...)` — tracks which session UUIDs have been processed. Index stored in `harvested.json` inside the memory root (synced via cloud).
- Does NOT call the API. Caller (cli.py or scheduler) passes transcript to sync.py.

### `memsync/sync.py`
- The only module that calls the Anthropic API.
- Two entry points:
  - `refresh_memory_content(notes, current_memory, config)` — merges explicit user notes.
  - `harvest_memory_content(transcript, current_memory, config)` — extracts memories from a session transcript.
- Both return `{updated_content, changed, truncated}`. Neither writes files — caller handles I/O.
- `enforce_hard_constraints(old, new)` — re-appends any hard constraint lines the model dropped. Called by both functions.
- See `PITFALLS.md` — this module has the most trust/safety concerns.

### `memsync/backups.py`
- `backup(source: Path, backup_dir: Path) -> Path` — copies with timestamp.
- `prune(backup_dir: Path, keep_days: int) -> list[Path]` — removes old backups.
- `list_backups(backup_dir: Path) -> list[Path]` — sorted newest-first.
- `latest_backup(backup_dir: Path) -> Path | None`

### `memsync/claude_md.py`
- `sync(memory_path: Path, target_path: Path) -> None`
  - `target_path` comes from `config.claude_md_target` — never hardcoded.
  - Mac/Linux: create symlink if not already correct, backup any existing file first.
  - Windows: copy (symlinks require admin rights on Windows).
- `is_synced(memory_path: Path, target_path: Path) -> bool`

---

## Data flow: `memsync init`

```
1. cli.py        — parse args
2. config.py     — check if config already exists (warn if --force not set)
3. providers/    — run detect() on each registered provider in priority order
4. cli.py        — if multiple detected, prompt user to choose
5. config.py     — write config with chosen provider + detected path
6. providers/    — call get_memory_root() to get the .claude-memory path
7. (filesystem)  — create .claude-memory/, backups/, sessions/ dirs
8. (filesystem)  — write starter GLOBAL_MEMORY.md if not exists
9. claude_md.py  — sync to ~/.claude/CLAUDE.md
10. cli.py       — print summary of what was created
```

## Data flow: `memsync harvest`

```
1. cli.py        — parse args
2. config.py     — load config
3. providers/    — resolve memory root path
4. harvest.py    — locate ~/.claude/projects/<key>/ for current working directory
5. harvest.py    — load harvested.json (set of already-processed session UUIDs)
6. harvest.py    — find most recent session JSONL not in the harvested index
7. harvest.py    — parse JSONL: extract human messages + assistant text only
8. cli.py        — (interactive mode) show session info, prompt to confirm
9. (filesystem)  — read current GLOBAL_MEMORY.md
10. sync.py      — call Claude API with transcript + current memory
11. sync.py      — enforce hard constraints
12. backups.py   — backup current file before overwriting
13. (filesystem) — write updated GLOBAL_MEMORY.md
14. claude_md.py — sync to ~/.claude/CLAUDE.md
15. harvest.py   — save updated harvested.json (marks session as processed)
16. cli.py       — print summary
```

## Data flow: `memsync refresh`

```
1. cli.py        — parse args, read notes from --notes / --file / stdin
2. config.py     — load config
3. providers/    — resolve memory root path
4. (filesystem)  — read current GLOBAL_MEMORY.md
5. sync.py       — call Claude API with current memory + notes
6. sync.py       — enforce hard constraints (append-only diff)
7. backups.py    — backup current file before overwriting
8. (filesystem)  — write updated GLOBAL_MEMORY.md
9. claude_md.py  — sync to ~/.claude/CLAUDE.md
10. sessions/    — append notes to dated session log
11. cli.py       — print summary (changed/unchanged, backup path)
```

---

## File layout on disk

```
# In cloud sync folder (synced across machines):
OneDrive/.claude-memory/          ← or iCloud/.claude-memory/, etc.
  GLOBAL_MEMORY.md                ← source of truth
  harvested.json                  ← index of already-harvested session UUIDs
  backups/
    GLOBAL_MEMORY_20260321_143022.md
    GLOBAL_MEMORY_20260320_091145.md
    ...
  sessions/
    2026-03-21.md                 ← raw refresh notes, append-only, never deleted
    2026-03-20.md
    ...

# On each machine (not synced):
~/.config/memsync/config.toml     ← machine-specific config
~/.claude/CLAUDE.md               ← symlink → OneDrive/.claude-memory/GLOBAL_MEMORY.md
                                     (or copy on Windows)
~/.claude/projects/<key>/         ← Claude Code session transcripts (machine-local)
  <uuid>.jsonl                    ← one file per conversation
```

---

## What does NOT belong in this tool

- Project-specific memory (that belongs in each project's CLAUDE.md)
- Cold storage / knowledge bases (use Hipocampus or RAG for that)
- Multi-user or team memory (out of scope for v1)
- Anything that requires a server, database, or API key beyond Anthropic's

---

## Futureproofing decisions

These are low-effort now and expensive to retrofit later. All three are
already reflected in the code specs above — this section explains the *why*.

### 1. Version the memory file format

Write a version comment at the top of every `GLOBAL_MEMORY.md` when it's
first created:

```markdown
<!-- memsync v0.2 -->
# Global Memory
...
```

If the schema ever needs to change (section names, structure, anything),
the version comment lets migration code know what it's dealing with.
Without it, you can't distinguish an old file from a new one.

Implementation: write this comment in `load_or_init_memory()` when creating
the starter template. Check for it in `refresh_memory_content()` and warn
(don't fail) if it's missing.

### 2. Don't hardcode the CLAUDE.md target path

`~/.claude/CLAUDE.md` is where Claude Code reads its global config today.
That could change. The target path lives in `config.claude_md_target` and
is never hardcoded anywhere in the logic modules. `cli.py` reads it from
config and passes it to `claude_md.sync()`. This is already reflected in
the `claude_md.py` module spec above.

### 3. Keep the Anthropic SDK version loose

`pyproject.toml` already has `anthropic>=0.40.0` — keep it that way.
Never pin to an exact version. Users should get SDK updates automatically
when they upgrade their environment.

---

## Key constraints

- Python 3.11+ only. Use match statements, `Path` everywhere, `tomllib` (stdlib).
- No dependencies beyond `anthropic`. Everything else stdlib.
- `tomllib` is read-only (stdlib in 3.11+). Use `tomli_w` for writing, or write TOML
  manually for the simple schema we have. See `CONFIG.md`.
- Must work offline except for `memsync refresh` and `memsync harvest` (the only commands needing the API).
- `harvest.py` reads machine-local Claude Code session files. It must not be imported by the daemon module — daemon imports from core only, never the reverse.
