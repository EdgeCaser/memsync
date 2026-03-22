# memsync

Cross-platform global memory manager for Claude Code.

Claude Code has no memory between sessions. memsync fixes that: it maintains one canonical `GLOBAL_MEMORY.md` in your cloud sync folder, linked to `~/.claude/CLAUDE.md` so Claude Code reads it at every session start.

You can update memory manually with a single command, or install the daemon and let it run in the background — harvesting your session transcripts every night and keeping your memory current without any intervention.

---

## How it works

```
OneDrive/.claude-memory/
  GLOBAL_MEMORY.md          ← source of truth, synced across all machines
  backups/                  ← automatic backups before every write
  sessions/                 ← raw session notes, append-only audit trail
  harvested.json            ← index of Claude Code sessions already harvested
  usage.jsonl               ← API usage log, synced across machines

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
- An Anthropic API key — stored in config (`memsync config set api_key <key>`) or via `ANTHROPIC_API_KEY` env var
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

# 2. Store your API key
memsync config set api_key sk-ant-...

# 3. Edit your memory file — fill in who you are, active projects, preferences
# File is at: OneDrive/.claude-memory/GLOBAL_MEMORY.md

# 4. After a Claude Code session, let memsync read the transcript automatically
memsync harvest

# Or tell it explicitly what to remember
memsync refresh --notes "Decided to use JWT tokens instead of sessions."

# 5. Verify everything is wired up
memsync status
memsync doctor
```

> **Recommended: run the daemon.** The steps above work fine manually, but most users install the daemon so memory updates happen automatically every night — no commands needed after setup. See [Automation with the daemon](#automation-with-the-daemon).

---

## Commands

| Command | Description |
|---|---|
| `memsync init` | First-time setup: create directory structure, sync to CLAUDE.md |
| `memsync harvest` | Extract memories from a Claude Code session transcript |
| `memsync refresh --notes "..."` | Merge explicit notes into memory via Claude API |
| `memsync usage` | Show API usage and estimated cost across all machines |
| `memsync show` | Print current GLOBAL_MEMORY.md |
| `memsync diff` | Diff current memory vs last backup |
| `memsync status` | Show paths, provider, sync state |
| `memsync doctor` | Self-check: verify installation health without API calls |
| `memsync providers` | List all providers and detection status |
| `memsync config show` | Print current config |
| `memsync config set <key> <value>` | Update a config value |
| `memsync prune` | Remove old backups |

### `memsync harvest` options

```bash
memsync harvest                        # prompt to confirm, then harvest latest session
memsync harvest --all                  # sweep all projects (for nightly scheduled runs)
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
memsync config set api_key sk-ant-...             # store API key in config (recommended)
memsync config set provider icloud
memsync config set model claude-opus-4-20250514
memsync config set sync_root /path/to/custom/folder
memsync config set keep_days 60
memsync config set max_memory_lines 300
memsync config set claude_md_target ~/.claude/CLAUDE.md
```

### `memsync usage`

Tracks every API call made by memsync across all machines. The log is stored in your cloud sync folder so it accumulates across devices.

```
All time:
  Calls:           42
  Input tokens:    310,402
  Output tokens:   18,204
  Estimated cost:  $1.2045

This month (2026-03):
  Calls:           12
  ...

By machine:
  macbook-pro           28 call(s)  $0.8210
  desktop-win           14 call(s)  $0.3835

Recent (last 10):
  2026-03-21 14:02  harvest    4821 in /   312 out  $0.0192  [macbook-pro] changed
```

Pricing is estimated based on public Anthropic rates and is for reference only.

---

## Automation with the daemon

The recommended way to use memsync is to install the daemon and forget about it. Every night it reads your Claude Code session transcripts and automatically extracts what's worth keeping — no commands, no notes, no maintenance.

```bash
pip install 'memsync[daemon]'    # note: quotes required on Mac/zsh
memsync daemon start --detach    # start in background now
memsync daemon install           # register as a system service (auto-starts on boot)
```

What runs automatically:

| Job | Schedule | What it does |
|---|---|---|
| Harvest | 2:00am | Reads session transcripts, extracts memories |
| Refresh | 11:55pm | Merges notes captured via mobile endpoint |
| Drift check | Every 6h | Alerts if CLAUDE.md is out of sync |
| Backup mirror | Hourly | Local copy of `.claude-memory/` (opt-in) |
| Email digest | Monday 9am | Weekly summary of memory changes (opt-in) |

The daemon also runs:
- A **web UI** (port 5000) for viewing and editing `GLOBAL_MEMORY.md` in a browser
- A **mobile capture endpoint** (port 5001) for sending notes from iPhone Shortcuts or any HTTP client

### Daemon commands

```bash
memsync daemon start             # start in foreground (Ctrl+C to stop)
memsync daemon start --detach    # start as a background process
memsync daemon stop              # stop the background daemon
memsync daemon status            # show running status and job config
memsync daemon schedule          # show all scheduled jobs and next run times
memsync daemon install           # register as a system service (auto-starts on boot)
memsync daemon uninstall         # remove system service registration
memsync daemon web               # open the web UI in your browser
```

For platform-specific auto-start setup, see [`docs/DAEMON_SETUP.md`](docs/DAEMON_SETUP.md):
- **Mac** — launchd (auto-start at login)
- **Windows** — Task Scheduler (auto-start at login)
- **Linux** — systemd (auto-start at boot)
- **Raspberry Pi** — full 24/7 setup guide

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
api_key = "sk-ant-..."   # stored here, not in environment

[paths]
claude_md_target = "/Users/ian/.claude/CLAUDE.md"

[backups]
keep_days = 30
```

The API key is stored in this file (in `%APPDATA%` on Windows, `~/.config` on Mac/Linux), not in your shell environment or source control. Set it with:

```bash
memsync config set api_key sk-ant-...
```

The `ANTHROPIC_API_KEY` environment variable is still accepted as a fallback, but the config value takes precedence.

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

## Known limitations

- **Concurrent writes:** Running `memsync refresh` or `memsync harvest` on two machines simultaneously results in the last write winning. The losing write's backup is in `backups/`. Risk is low since writes are infrequent.
- **Max memory size:** The memory file is kept under ~400 lines. Very dense files may hit the 4096 token response limit — reduce the file size if you see truncation errors.
- **Harvest reads local sessions only:** `~/.claude/projects/` is machine-local and not synced. The nightly harvest job runs on whichever machine has the session files. On machines without Claude Code (e.g. a Pi), the harvest job skips silently.
- **API costs:** memsync uses the Anthropic API (pay-per-token), separate from a Claude Max subscription. Typical usage is ~$3–10/month. Track it with `memsync usage`.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To add a new cloud provider, see [docs/adding-a-provider.md](docs/adding-a-provider.md).

---

## License

MIT
