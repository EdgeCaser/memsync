# memsync

Cross-platform global memory manager for Claude Code.

Claude Code has no memory between sessions. memsync fixes that: it maintains one canonical `GLOBAL_MEMORY.md` in your cloud sync folder, linked to `~/.claude/CLAUDE.md` so Claude Code reads it at every session start.

After a meaningful session, run `memsync refresh --notes "..."` and the Claude API merges your notes into the memory file automatically.

---

## How it works

```
OneDrive/.claude-memory/
  GLOBAL_MEMORY.md          ← source of truth, synced across all machines
  backups/                  ← automatic backups before every write
  sessions/                 ← raw session notes, append-only audit trail
  harvested.json            ← index of Claude Code sessions already harvested

~/.claude/CLAUDE.md         ← symlink → GLOBAL_MEMORY.md (Mac/Linux)
                               copy of GLOBAL_MEMORY.md (Windows)

~/.claude/projects/<key>/   ← Claude Code session transcripts (machine-local)
  <uuid>.jsonl
```

Every Claude Code session starts by reading `~/.claude/CLAUDE.md`. memsync keeps it current.

Memory is updated two ways:
- **`memsync harvest`** — reads Claude Code's session transcript directly and extracts what's worth remembering. No notes required from you.
- **`memsync refresh --notes "..."`** — you tell it what to remember. Use this for deliberate captures or non-Claude-Code work.

---

## Requirements

- Python 3.11+
- An Anthropic API key (`ANTHROPIC_API_KEY` env var)
- One of: OneDrive, iCloud Drive, Google Drive — or any folder you specify

---

## Installation

```bash
pip install memsync
```

---

## Quick start

```bash
# 1. Initialize (auto-detects your cloud provider)
memsync init

# 2. Edit your memory file — fill in who you are, active projects, preferences
# File is at: OneDrive/.claude-memory/GLOBAL_MEMORY.md

# 3. After a Claude Code session, let memsync read the transcript automatically
memsync harvest

# Or tell it explicitly what to remember
memsync refresh --notes "Decided to use JWT tokens instead of sessions."

# 4. Check everything is wired up
memsync status
```

---

## Commands

| Command | Description |
|---|---|
| `memsync init` | First-time setup: create directory structure, sync to CLAUDE.md |
| `memsync harvest` | Extract memories from a Claude Code session transcript |
| `memsync refresh --notes "..."` | Merge explicit notes into memory via Claude API |
| `memsync show` | Print current GLOBAL_MEMORY.md |
| `memsync diff` | Diff current memory vs last backup |
| `memsync status` | Show paths, provider, sync state |
| `memsync providers` | List all providers and detection status |
| `memsync config show` | Print current config |
| `memsync config set <key> <value>` | Update a config value |
| `memsync prune` | Remove old backups |

### `memsync harvest` options

```bash
memsync harvest                        # prompt to confirm, then harvest latest session
memsync harvest --dry-run              # preview what would change, no write
memsync harvest --auto                 # skip prompt, silent (for daemon/hook use)
memsync harvest --force                # re-harvest even if already processed
memsync harvest --session path/to.jsonl  # harvest a specific session file
memsync harvest --project ~/.claude/projects/<key>  # harvest from a specific project dir
```

### `memsync refresh` options

```bash
memsync refresh --notes "inline notes"
memsync refresh --file notes.txt
echo "notes" | memsync refresh
memsync refresh --notes "..." --dry-run      # preview changes, no write
memsync refresh --notes "..." --model claude-opus-4-20250514  # one-off model override
```

### `memsync init` options

```bash
memsync init                          # auto-detect provider
memsync init --provider icloud        # use a specific provider
memsync init --sync-root /path/to/folder  # use a custom path
memsync init --force                  # reinitialize even if already set up
```

### `memsync config set` keys

```bash
memsync config set provider icloud
memsync config set model claude-opus-4-20250514
memsync config set sync_root /path/to/custom/folder
memsync config set keep_days 60
memsync config set max_memory_lines 300
memsync config set claude_md_target ~/.claude/CLAUDE.md
```

---

## Cloud providers

| Provider | macOS | Windows | Linux |
|---|---|---|---|
| OneDrive | ✓ | ✓ | ✓ (rclone) |
| iCloud Drive | ✓ | ✓ | ✗ |
| Google Drive | ✓ | ✓ | ✓ (rclone) |
| Custom path | ✓ | ✓ | ✓ |

Detection is automatic. If multiple providers are found during `memsync init`, you'll be prompted to choose.

**Windows note:** Symlinks require admin rights or Developer Mode on Windows. memsync copies `GLOBAL_MEMORY.md` to `~/.claude/CLAUDE.md` instead. The copy is refreshed on every `memsync refresh`.

**iCloud note:** iCloud Drive doesn't sync dot-folders on Mac. memsync stores data in `claude-memory/` (no leading dot) when using the iCloud provider.

---

## Configuration

Config file location:
- macOS/Linux: `~/.config/memsync/config.toml`
- Windows: `%APPDATA%\memsync\config.toml`

Config is machine-specific — two machines can use different providers pointing to the same cloud storage location.

Example config:

```toml
[core]
provider = "onedrive"
model = "claude-sonnet-4-20250514"
max_memory_lines = 400

[paths]
claude_md_target = "/Users/ian/.claude/CLAUDE.md"

[backups]
keep_days = 30
```

To update the model when Anthropic releases new ones:

```bash
memsync config set model claude-sonnet-4-20250514
```

---

## What belongs in GLOBAL_MEMORY.md

The memory file is your **identity layer** — not a knowledge base, not project docs.

Good things to include:
- Who you are, your roles, active projects
- Current priorities and focus
- Standing preferences (communication style, output format)
- Hard constraints (rules that must never be softened through compaction)

See `docs/global-memory-guide.md` for a complete guide.

---

## Automation with the daemon

The daemon runs two nightly jobs so you don't have to think about it:
- **2:00am — harvest**: reads your Claude Code sessions from today and extracts memories automatically.
- **11:55pm — refresh**: merges notes captured via the mobile endpoint.

```bash
pip3 install 'memsync[daemon]'   # note: quotes required on Mac/zsh
memsync daemon start --detach    # start now
```

For auto-start on boot and full setup instructions for each platform, see [`docs/DAEMON_SETUP.md`](docs/DAEMON_SETUP.md):
- **Mac** — launchd (auto-start at login)
- **Windows** — Task Scheduler (auto-start at login)
- **Linux** — systemd (auto-start at boot)
- **Raspberry Pi** — full 24/7 setup guide

---

## Known limitations

- **Concurrent writes:** Running `memsync refresh` or `memsync harvest` on two machines simultaneously results in the last write winning. The losing write's backup is in `backups/`. Risk is low since writes are infrequent.
- **Max memory size:** The memory file is kept under ~400 lines. Very dense files may hit the 4096 token response limit — reduce the file size if you see truncation errors.
- **Harvest reads local sessions only:** `~/.claude/projects/` is machine-local and not synced. The nightly harvest job runs on whichever machine has the session files. On machines without Claude Code (e.g. a Pi), the harvest job skips silently.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To add a new cloud provider, see [docs/adding-a-provider.md](docs/adding-a-provider.md).

---

## License

MIT
