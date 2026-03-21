# COMMANDS.md

## Command map

```
memsync
├── init              Set up memory structure for the first time
├── harvest           Extract memories from a Claude Code session transcript
├── refresh           Merge explicit session notes into global memory
├── show              Print current GLOBAL_MEMORY.md
├── diff              Diff current memory vs last backup
├── status            Show paths, provider, sync state
├── config
│   ├── show          Print current config.toml
│   └── set           Update a config value
├── providers         List all providers and their detection status
└── prune             Remove old backups
```

---

## `memsync init`

**Purpose:** First-time setup. Creates directory structure, writes starter memory,
syncs to CLAUDE.md.

**Args:**
- `--force` — reinitialize even if memory already exists (prompts confirmation)
- `--provider <name>` — skip auto-detection, use this provider
- `--sync-root <path>` — skip auto-detection, use this path directly

**Behavior:**
1. Check if config already exists → warn and exit unless `--force`
2. If no `--provider` given, run auto-detection across all providers
   - If 0 detected: print friendly error explaining how to set manually
   - If 1 detected: use it, confirm with user
   - If 2+ detected: prompt user to choose
3. Resolve memory root from provider
4. Create: `memory_root/`, `memory_root/backups/`, `memory_root/sessions/`
5. If `GLOBAL_MEMORY.md` doesn't exist, write starter template
6. Write config
7. Run `claude_md.sync()` to create the CLAUDE.md link
8. Print summary

**Output (success):**
```
memsync initialized.

  Provider:    OneDrive
  Sync root:   /Users/ian/OneDrive
  Memory:      /Users/ian/OneDrive/.claude-memory/GLOBAL_MEMORY.md
  CLAUDE.md:   /Users/ian/.claude/CLAUDE.md → (symlink)

Next: edit your memory file, then run:
  memsync refresh --notes "initial setup complete"
```

---

## `memsync harvest`

**Purpose:** Read a Claude Code session transcript and extract what's worth adding to
GLOBAL_MEMORY.md. No notes required — memsync reads the session directly.

**Args:**
- `--project <path>` — path to a `~/.claude/projects/<key>` directory (default: current working directory's project)
- `--session <path>` — path to a specific `.jsonl` session file (default: most recent unprocessed)
- `--auto` — skip confirmation prompt, run silently (for daemon or hook use)
- `--force` — re-harvest even if this session is already in `harvested.json`
- `--dry-run` — show what would change without writing
- `--model <id>` — one-off model override

**Behavior:**
1. Resolve the project directory from cwd (or `--project`)
2. Load `harvested.json` from memory root — the index of already-processed sessions
3. Find the most recent session JSONL not yet in the index (or use `--session`)
4. Parse the JSONL: extract human-typed messages and assistant text; skip tool calls, tool results, thinking blocks, and internal records
5. If not `--auto`, show session info and prompt to confirm
6. Call Claude API with a transcript-extraction prompt and current memory
7. Enforce hard constraints (same as refresh)
8. If changed and not dry-run: backup → write → sync CLAUDE.md
9. Mark session as harvested in `harvested.json` regardless of whether memory changed

**Output (interactive):**
```
Session: 3824abec-0413-4e88-97c2-4c90544fa560
Date:     2026-03-21 21:10
Messages: 18
Harvest this session? [y/N] y
Harvesting session... done.
  Backup:    /Users/ian/OneDrive/.claude-memory/backups/GLOBAL_MEMORY_20260321_220000.md
  Memory:    /Users/ian/OneDrive/.claude-memory/GLOBAL_MEMORY.md
  CLAUDE.md synced ✓
```

**Output (--auto, no changes):** silent, exits 0.

**harvested.json:** Stored in the memory root (synced via cloud). Contains a sorted list
of session UUIDs that have already been processed. Prevents re-harvesting the same session
on the next run.

---

## `memsync refresh`

**Purpose:** Core command. Merge session notes into GLOBAL_MEMORY.md via Claude API.

**Args:**
- `--notes <str>` / `-n <str>` — notes as inline string
- `--file <path>` / `-f <path>` — read notes from file
- `--dry-run` — print what would change, don't write anything
- (stdin) — if no --notes or --file and stdin is not a tty, read from stdin

**Exactly one of --notes, --file, or stdin must be provided.**

**Behavior:**
1. Load config
2. Resolve memory path via provider
3. Read current GLOBAL_MEMORY.md
4. Call Claude API (see sync.py spec below and PITFALLS.md)
5. Enforce hard constraints (append-only diff)
6. If changed AND not dry-run:
   a. Backup current file
   b. Write updated memory
   c. Sync to CLAUDE.md
   d. Append notes to sessions/<date>.md
7. Print summary

**Output (changed):**
```
Memory updated.
  Backup:  /Users/ian/OneDrive/.claude-memory/backups/GLOBAL_MEMORY_20260321_143022.md
  Memory:  /Users/ian/OneDrive/.claude-memory/GLOBAL_MEMORY.md
  CLAUDE.md synced ✓
```

**Output (no change):**
```
No changes detected.
```

**Output (dry-run):**
```
[DRY RUN] No files written.

--- diff ---
- Old line
+ New line
...
```

---

## `memsync show`

**Purpose:** Print current GLOBAL_MEMORY.md to stdout.

**Args:** none

**Use case:** Pipe to less, copy to clipboard, quick review.

---

## `memsync diff`

**Purpose:** Show unified diff between current memory and the most recent backup.

**Args:**
- `--backup <filename>` — diff against a specific backup instead of latest

**Output:** Standard unified diff format. If no backups exist, print a message.

---

## `memsync status`

**Purpose:** Sanity check — what is memsync pointing at on this machine?

**Output:**
```
Platform:      macOS (Darwin)
Config:        /Users/ian/.config/memsync/config.toml ✓
Provider:      OneDrive
Sync root:     /Users/ian/Library/CloudStorage/OneDrive-Personal ✓
Memory:        /Users/ian/Library/CloudStorage/OneDrive-Personal/.claude-memory/GLOBAL_MEMORY.md ✓
CLAUDE.md:     /Users/ian/.claude/CLAUDE.md → symlink ✓
Backups:       14 file(s)
Session logs:  22 day(s)
Model:         claude-sonnet-4-20250514
```

---

## `memsync config show`

Print the contents of config.toml.

## `memsync config set <key> <value>`

Update a single config value and save.

```bash
memsync config set provider icloud
memsync config set model claude-opus-4-20250514
memsync config set keep_days 60
memsync config set sync_root "/Users/ian/Dropbox"
```

After `config set sync_root`, automatically set provider to "custom".
After any change, print the updated value to confirm.

---

## `memsync providers`

List all registered providers and their detection status on this machine.

**Output:**
```
Available providers:

  onedrive   OneDrive          ✓ detected at /Users/ian/Library/CloudStorage/OneDrive-Personal
  icloud     iCloud Drive      ✓ detected at /Users/ian/Library/Mobile Documents/com~apple~CloudDocs
  gdrive     Google Drive      ✗ not detected
  custom     Custom Path       ✗ no path configured

Active provider: onedrive
```

---

## `memsync prune`

**Args:**
- `--keep-days <int>` — default from config (30)
- `--dry-run` — list what would be deleted without deleting

**Output:**
```
Pruned 3 backup(s) older than 30 days.
  removed: GLOBAL_MEMORY_20260101_120000.md
  removed: GLOBAL_MEMORY_20260102_083000.md
  removed: GLOBAL_MEMORY_20260115_201500.md
```

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | General error (printed to stderr) |
| 2 | Config not found — run `memsync init` |
| 3 | Memory file not found |
| 4 | Provider detection failed |
| 5 | API error |

---

## Error message conventions

- Always print errors to stderr
- Always suggest a fix, not just a description of the problem
- Example: `Error: no provider detected. Run 'memsync init' or set a custom path with 'memsync config set sync_root /path/to/folder'`
