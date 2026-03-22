# Getting started with memsync

This guide walks you through installing and using memsync from scratch. It assumes you
have used a terminal before — you know how to open one and type commands — but no
Python experience or coding background is required.

---

## What memsync does

Claude Code has no memory between sessions. Every time you start a new session, Claude
starts fresh — it doesn't remember your name, your projects, your preferences, or what
you decided last time.

The standard fix is a file at `~/.claude/CLAUDE.md`. Claude Code reads that file at the
start of every session, so anything you put there gets loaded automatically. But keeping
that file up to date by hand is tedious, it gets bloated, and if you work on more than
one computer the files drift apart.

memsync solves this by:

1. Storing one canonical memory file in your cloud sync folder (OneDrive, iCloud, Google Drive)
2. Keeping `~/.claude/CLAUDE.md` in sync with that file automatically
3. Using the Claude API to update the memory file — either by reading your session transcript directly, or by merging notes you provide

After a session, run `memsync harvest` and memsync reads what happened and updates the memory file itself. Or install the daemon and it does this automatically every night — no commands needed at all.

---

## Before you start: what you'll need

- A computer running macOS, Windows, or Linux
- Python 3.11 or newer
- An Anthropic API key
- One of: OneDrive, iCloud Drive, or Google Drive (or any folder you can specify manually)

The sections below walk you through each prerequisite.

---

## Step 1 — Check if Python is installed

Open a terminal:
- **Mac:** press `Cmd + Space`, type `Terminal`, press Enter
- **Windows:** press `Win + R`, type `cmd`, press Enter (or search for "PowerShell")
- **Linux:** you already know how to do this

Type this and press Enter:

```
python --version
```

You should see something like `Python 3.12.1`. If you see `Python 3.11` or higher, you
are good. Skip to Step 2.

If you see `Python 3.9` or lower, or `command not found`, you need to install or update
Python.

### Installing Python

Go to [python.org/downloads](https://www.python.org/downloads/) and download the
latest version (3.12 or 3.13).

**Windows:** Run the installer. On the first screen, check **"Add Python to PATH"**
before clicking Install. This is easy to miss and important.

**Mac:** The python.org installer works fine. Alternatively, if you use Homebrew:
`brew install python`

**Linux:** `sudo apt install python3 python3-pip` (Debian/Ubuntu) or
`sudo dnf install python3` (Fedora).

After installing, close and reopen your terminal, then run `python --version` again to
confirm.

---

## Step 2 — Get an Anthropic API key

memsync uses the Claude API to update your memory file. You need an API key to
authenticate with Anthropic's servers.

1. Go to [console.anthropic.com](https://console.anthropic.com) and sign in or
   create an account.
2. In the left sidebar, click **API Keys**.
3. Click **Create Key**, give it a name like "memsync", and copy the key. It starts
   with `sk-ant-...`.

Keep this key somewhere safe. You won't be able to see it again on the Anthropic website.

### Storing the API key

The recommended way is to store it in memsync's config file. After you run `memsync init`
in Step 4, run this:

```bash
memsync config set api_key sk-ant-your-key-here
```

memsync saves the key in its config file (`~/.config/memsync/config.toml` on Mac/Linux,
`%APPDATA%\memsync\config.toml` on Windows). It's stored on your machine only, not in
your cloud sync folder or any code.

**Alternative: environment variable**

You can also set `ANTHROPIC_API_KEY` as a shell environment variable if you prefer.
memsync will use it as a fallback when no key is set in config.

**Mac / Linux** — add to `~/.zshrc` or `~/.bashrc`:
```
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
```

**Windows (PowerShell):**
```powershell
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-your-key-here", "User")
```

---

## Step 3 — Install memsync

With Python installed and your API key set, installing memsync is one command:

```bash
pip install memsync
```

This downloads memsync and its dependencies from the internet.

> **If `pip` is not found on Mac/Linux**, try `pip3 install memsync` instead.
>
> **If you see a permissions error on Mac/Linux**, try:
> `pip install memsync --user`
>
> **If you see a permissions error on Windows**, right-click PowerShell and choose
> "Run as administrator", then try again.

To confirm the installation worked:

```bash
memsync --version
```

You should see something like `memsync 0.2.0`.

---

## Step 4 — Set up your memory file

Run the setup command:

```bash
memsync init
```

memsync will look for your cloud sync folder automatically. What happens next depends
on what it finds:

- **One provider detected:** It confirms the path and sets up immediately.
- **Multiple providers detected:** It lists them and asks you to choose one.
- **Nothing detected:** It asks you to specify a path manually (see below).

### If auto-detection fails

If memsync can't find your cloud folder, you can tell it where to look:

```bash
memsync init --sync-root /path/to/your/cloud/folder
```

Some common paths:

| Service | Mac | Windows |
|---|---|---|
| OneDrive | `~/OneDrive` | `C:\Users\YourName\OneDrive` |
| iCloud | `~/Library/Mobile Documents/com~apple~CloudDocs` | `C:\Users\YourName\iCloudDrive` |
| Google Drive | `~/Google Drive` | `G:\My Drive` |
| Dropbox | `~/Dropbox` | `C:\Users\YourName\Dropbox` |

Example:

```bash
memsync init --sync-root ~/OneDrive
```

### What init creates

After a successful `init`, you'll see output like:

```
memsync initialized.

  Provider:    OneDrive
  Sync root:   /Users/ian/OneDrive
  Memory:      /Users/ian/OneDrive/.claude-memory/GLOBAL_MEMORY.md
  CLAUDE.md:   /Users/ian/.claude/CLAUDE.md → (symlink)
```

Two important things were created:

1. **`GLOBAL_MEMORY.md`** — your memory file, living in your cloud folder so it syncs
   across all your machines.
2. **`~/.claude/CLAUDE.md`** — a link (or copy on Windows) that points to your memory
   file. Claude Code reads this at the start of every session.

---

## Step 5 — Fill in your memory file

Your memory file starts empty with placeholder prompts. You need to fill it in.

Find the file at the path shown by `memsync init` — it ends in
`/.claude-memory/GLOBAL_MEMORY.md`. Open it in any text editor:

**Mac:** `open -a TextEdit ~/OneDrive/.claude-memory/GLOBAL_MEMORY.md`
**Windows:** Navigate to the file in File Explorer and open it in Notepad.

You'll see this starter template:

```markdown
# Global Memory

> Loaded by Claude Code at session start on all machines and projects.
> Edit directly or run: memsync refresh --notes "..."

## Identity & context
- (Fill this in — who you are, your roles, active projects)

## Current priorities
- (What you're working on right now)

## Standing preferences
- (How you like to work — communication style, output format, etc.)

## Hard constraints
- (Rules that must never be lost or softened through compaction)
```

Replace the placeholders with real content. Here's an example of a filled-in file:

```markdown
# Global Memory

## Identity & context
- Jamie, product manager at a fintech startup
- Side project: building a personal budgeting app in Python
- Work machine: Windows laptop. Home machine: MacBook Pro.
- Comfortable reading code, less comfortable writing it from scratch

## Current priorities
- Finish MVP of budgeting app by end of month
- Q2 roadmap presentation to leadership next Tuesday
- Onboarding new engineer starting Monday

## Standing preferences
- Keep explanations concise — I can ask for more if needed
- When writing code, explain what each part does
- Prefer simple solutions over clever ones
- Always ask before deleting or overwriting anything

## Hard constraints
- Never commit API keys or passwords to code
- Always confirm before making changes that can't be undone
```

A few tips:
- **Be specific.** "I'm a product manager" is less useful than "Jamie, PM at a fintech startup, working on the mobile app."
- **Keep it short.** This file has a soft limit of 400 lines. If it's getting long, you're putting too much in it.
- **Hard constraints are enforced.** Items in the `## Hard constraints` section are
  never removed during automatic updates — memsync checks for this in code.

For more guidance on what to include, see `docs/global-memory-guide.md`.

---

## Step 6 — Verify everything is connected

Run:

```bash
memsync status
```

You should see something like:

```
Platform:      macOS (Darwin)
Config:        /Users/jamie/.config/memsync/config.toml ✓
Provider:      onedrive
Model:         claude-sonnet-4-20250514
Sync root:     /Users/jamie/OneDrive ✓
Memory:        /Users/jamie/OneDrive/.claude-memory/GLOBAL_MEMORY.md ✓
CLAUDE.md:     /Users/jamie/.claude/CLAUDE.md → symlink ✓
Backups:       0 file(s)
Session logs:  0 day(s)
```

Every line should show a `✓`. If anything shows `✗`, see the Troubleshooting section
at the end of this guide.

You can also run the built-in health check:

```bash
memsync doctor
```

This checks each component and tells you exactly what's wrong if something isn't set up.

---

## Your daily workflow

Once set up, you have two ways to use memsync: **run commands manually** after each
session, or **install the daemon** and let it handle everything automatically.

> **The daemon is the recommended approach.** Once installed, it harvests your sessions
> every night at 2am without you doing anything. See
> [Running the daemon](#running-the-daemon-recommended) below.

If you prefer manual control, here's how:

### Option 1: Let memsync read the session

`memsync harvest` reads Claude Code's session transcript directly — the actual
conversation — and extracts what's worth remembering. You don't need to write notes.

```bash
memsync harvest
```

It will show you the session details and ask to confirm:

```
Session: 3824abec-0413-4e88-97c2-4c90544fa560
Date:     2026-03-21 21:10
Messages: 18
Harvest this session? [y/N] y
Harvesting session... done.
  Backup:    /Users/jamie/OneDrive/.claude-memory/backups/GLOBAL_MEMORY_20260321_220000.md
  Memory:    /Users/jamie/OneDrive/.claude-memory/GLOBAL_MEMORY.md
  CLAUDE.md synced ✓
```

memsync keeps track of which sessions it has already harvested, so running it again
won't duplicate anything. Use `--force` to re-harvest if needed.

### Option 2: Tell it what to remember

If you want to explicitly encode something — a decision made outside a Claude Code session,
a preference you've discovered, or something you want to make sure doesn't get missed:

```bash
memsync refresh --notes "What happened in this session"
```

Examples:

```bash
memsync refresh --notes "Decided to use JWT tokens instead of sessions — simpler for our use case."

memsync refresh --notes "Discovered that the CSV parser breaks on files with Windows line endings. Fixed with universal newlines mode."

memsync refresh --notes "Switched from Flask to FastAPI. Flask felt too verbose."
```

Both commands backup the current memory file before writing anything.

### When nothing important changed

If you had a routine session with nothing worth keeping, skip both commands. Harvest is
for when something happened — decisions, completions, problems solved.

### Preview before writing

Not sure what the update will do? Use `--dry-run` with either command:

```bash
memsync harvest --dry-run
memsync refresh --notes "your notes" --dry-run
```

This shows you a diff of what would change without writing anything.

---

## Useful commands to know

### See your current memory

```bash
memsync show
```

Prints the full contents of your memory file to the terminal.

### See what changed in the last refresh

```bash
memsync diff
```

Shows a side-by-side comparison of the current memory file vs the most recent backup.
Lines starting with `+` were added; lines starting with `-` were removed.

### Read notes from a file

If you've been writing session notes in a text file as you go:

```bash
memsync refresh --file my-notes.txt
```

### Pipe notes from another command

```bash
echo "Switched to the new deploy pipeline, everything works" | memsync refresh
```

### Track API usage and cost

memsync logs every API call it makes. To see a summary across all your machines:

```bash
memsync usage
```

This shows total calls, token counts, estimated cost, and a breakdown by machine. The
log lives in your cloud folder so it accumulates across devices. Typical usage is
around $3–10/month.

---

## Running the daemon (recommended)

The daemon is the hands-off way to use memsync. Instead of running commands after
each session, it runs in the background and keeps your memory current automatically.

Install the daemon extras:

```bash
pip install 'memsync[daemon]'   # quotes required on Mac/zsh
```

Start it in the background:

```bash
memsync daemon start --detach
```

Register it as a system service so it starts automatically on login/boot:

```bash
memsync daemon install
```

What runs automatically once the daemon is installed:

| Job | Time | What it does |
|---|---|---|
| Harvest | 2:00am | Reads today's session transcripts, extracts memories |
| Refresh | 11:55pm | Merges notes captured via mobile |
| Drift check | Every 6h | Alerts if CLAUDE.md is out of sync |

The daemon also starts a **web UI** at `http://localhost:5000` for viewing and editing
your memory file in a browser, and a **mobile capture endpoint** at port 5001 for
sending notes from iPhone Shortcuts or any HTTP client.

Other daemon commands:

```bash
memsync daemon status      # show whether it's running and what's configured
memsync daemon schedule    # show all jobs and next run times
memsync daemon stop        # stop the background process
memsync daemon web         # open the web UI in your browser
memsync daemon uninstall   # remove the system service registration
```

For platform-specific setup (launchd on Mac, Task Scheduler on Windows, systemd on
Linux, or a Raspberry Pi for 24/7 operation), see `docs/DAEMON_SETUP.md`.

---

## Setting up on a second computer

One of the main benefits of memsync is that your memory syncs across machines
through your cloud folder.

On each new machine, you just need to:

1. Install Python (Step 1)
2. Install memsync: `pip install memsync`
3. Run `memsync init` — it will find the same cloud folder, which already has
   `GLOBAL_MEMORY.md` in it
4. Store your API key: `memsync config set api_key sk-ant-your-key-here`

That's it. The memory file already exists; init just wires up the local link.

---

## Troubleshooting

### "memsync: command not found"

Python installed the memsync command somewhere your terminal can't find it.

**Mac/Linux fix:**
```bash
pip install memsync --user
```
Then add the user bin directory to your PATH. On Mac, add this to `~/.zshrc`:
```
export PATH="$HOME/.local/bin:$PATH"
```

**Windows fix:** Close and reopen PowerShell. If it still doesn't work, try running:
```
python -m memsync --version
```
If that works, Python's Scripts directory isn't in your PATH. Search online for
"add Python scripts to PATH Windows".

---

### "Error: provider 'onedrive' could not find its sync folder"

memsync can't find your cloud sync folder. Tell it where to look:

```bash
memsync config set sync_root /full/path/to/your/cloud/folder
```

Then run `memsync status` to confirm it's found.

---

### "API key" shows ✗ in memsync doctor

The API key isn't configured. The simplest fix:

```bash
memsync config set api_key sk-ant-your-key-here
```

Then run `memsync doctor` again to confirm. If you'd rather use an environment variable,
see Step 2 for platform-specific instructions.

---

### "Error: API request failed"

Usually means the API key is wrong or has been revoked. Check it at
[console.anthropic.com](https://console.anthropic.com) under API Keys.

If the key is correct, check your internet connection.

---

### "CLAUDE.md: ✗ (not synced)"

The link between your cloud memory file and Claude Code's config file is broken.
Re-run init to fix it:

```bash
memsync init --force
```

---

### "Error: API response was truncated"

Your memory file or session notes are very long, and the Claude API hit its response
limit before finishing. The file was NOT updated.

Fix: edit your memory file (`memsync show`, then open the file in a text editor) and
remove anything that isn't pulling its weight. Aim for under 300 lines if you're
hitting this regularly.

---

### On Windows: CLAUDE.md is a copy, not a link

This is expected, not a bug. Windows requires administrator rights to create symlinks,
which memsync doesn't ask for. Instead it copies the file. The copy is updated every
time you run `memsync refresh`, so it stays current.

---

### Something else went wrong

Run the health check:

```bash
memsync doctor
```

It checks every component and prints exactly what's wrong with a ✗. Fix the flagged
items and run it again.

If you're still stuck, you can see more detail by running memsync with verbose Python
error output:

```bash
python -m memsync status
```

---

## Keeping your memory file healthy

A few habits that make the memory file more useful over time:

**Update it after real changes, not routine sessions.** If you spent two hours debugging
but nothing changed about your goals or preferences, you don't need to refresh.

**Keep the Hard constraints section intentional.** Only put things there that are
genuinely non-negotiable — rules you've been burned by before or preferences so
strong that "sometimes" isn't acceptable. This section is enforced in code; everything
in it persists forever.

**Edit the file directly when needed.** The refresh command is for session notes, but
you can open the file in any text editor and change it by hand. If you do, sync the
copy afterward:

```bash
memsync refresh --notes "Edited memory file directly — removed outdated project."
```

**Clean up old backups occasionally:**

```bash
memsync prune --keep-days 30
```

This removes backups older than 30 days. The default is already 30 days, so you can
also just run `memsync prune`.

---

## What the memory file looks like from Claude's perspective

Every time you open Claude Code in any project, it reads `~/.claude/CLAUDE.md` first.
Your memory file is loaded before any project-specific instructions. Claude sees
your identity, current priorities, and preferences before it reads a single line of
your project.

This is why specific, personal content in the memory file works better than generic
descriptions. Claude doesn't need "I prefer clear code" — that's assumed. It does
benefit from "I'm building a budgeting app in Python, currently debugging the CSV
import, and I prefer not to have tests suggested unless I ask."

The shorter and more specific the file, the more it helps.
