# Pi Hybrid Memory Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Memsync's shared memory off OneDrive onto a Pi-hosted hybrid — Syncthing for live cross-machine sync, Pi git snapshots plus an encrypted SSD backup for history — with zero fork of Memsync.

**Architecture:** Each machine keeps a local `memsync-shared` folder that Syncthing replicates over Tailscale, with the always-on Pi (`pi-gateway`) as hub/introducer. Memsync's existing `custom` provider points at that folder. On the Pi only, a git repo (metadata kept outside the synced tree) snapshots history every 15 minutes and `restic` writes encrypted backups to the external SSD.

**Tech Stack:** Syncthing, Tailscale, git, restic, Python 3.11+ (Memsync), PowerShell (Windows), bash/cron (Pi).

## Global Constraints

- No fork of Memsync. Only permitted code change is the `claude_md.py` patch in Task 1, contributed to the existing repo.
- Memory content is sensitive. All Syncthing traffic rides Tailscale only; global discovery and relaying are OFF. Offsite backups are encrypted at rest.
- `config.toml` (machine-local, in `%APPDATA%\memsync` / `~/.config/memsync`) contains live API keys and MUST NEVER enter the synced folder, the git repo, or any backup.
- Never reproduce API key values from `config.toml` in any file or output.
- Repositories under `OneDrive\Documents\GitHub\` are NOT moved or touched by this plan.
- Scheduled jobs run headless/hidden (no window, no focus steal).
- No em dashes in any user-facing copy produced by this work.
- Python: 3.11+, standard library only for any Memsync code change (no new deps).

## Environment values (discover once, before Task 1)

Run these and keep the results next to you. Commands below compute them as shell/PowerShell variables so steps stay literal.

| Value | How to get it |
|---|---|
| Pi Tailscale name | `pi-gateway` (known) |
| Pi user + home | on Pi: `whoami` and `echo $HOME` |
| Pi external SSD mount | on Pi: `lsblk -f` then `df -h` — identify the SSD mountpoint (e.g. `/mnt/ssd`) |
| Tailscale IP of each node | on each node: `tailscale ip -4` |
| Current OneDrive memory root | on Windows: `memsync status` (prints the resolved memory root; expected `C:\Users\ianfe\OneDrive\.claude-memory`) |

Canonical local folder name on every machine: **`memsync-shared`** (Memsync will manage `memsync-shared/.claude-memory` inside it).
- Windows: `C:\Users\ianfe\memsync-shared`
- macOS: `~/memsync-shared`
- Pi: `$HOME/memsync-shared`

---

### Task 1: Patch `claude_md.py` to prefer a symlink on Windows (copy fallback)

Without this, once `~/.claude/CLAUDE.md` is a symlink to `GLOBAL_MEMORY.md`, Memsync's Windows copy path calls `shutil.copy2(src, dst)` where both resolve to the same file, raising `SameFileError` and breaking `refresh`/`harvest`. The fix: use the symlink-preferred logic on all platforms, short-circuiting when the link is already correct, falling back to copy on `OSError` (covers non-admin Windows without Developer Mode).

**Files:**
- Modify: `memsync/claude_md.py:9-39` (the `sync()` function)
- Test: `tests/test_claude_md.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: unchanged public signatures — `sync(memory_path: Path, target_path: Path) -> None`, `is_synced(memory_path: Path, target_path: Path) -> bool`. Behavior change only: Windows now attempts a symlink first.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claude_md.py`:

```python
import platform
from pathlib import Path
import memsync.claude_md as claude_md


def test_sync_is_noop_when_symlink_already_correct(tmp_path, monkeypatch):
    # Simulate Windows so we exercise the previously copy-only branch.
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    memory = tmp_path / "GLOBAL_MEMORY.md"
    memory.write_text("hello", encoding="utf-8")
    target = tmp_path / "CLAUDE.md"

    # First sync creates the link (or copy fallback).
    claude_md.sync(memory, target)
    # Second sync must NOT raise SameFileError and must leave content intact.
    claude_md.sync(memory, target)

    assert target.read_text(encoding="utf-8") == "hello"
    assert claude_md.is_synced(memory, target)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_claude_md.py::test_sync_is_noop_when_symlink_already_correct -v`
Expected: FAIL — on the second `sync()` call, `shutil.SameFileError` is raised (or on a symlink-capable host the first call already errors), because the current Windows branch always copies.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `sync()` (lines 19-39) with a single platform-agnostic path:

```python
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Prefer a symlink on every platform. On Windows this needs admin or
    # Developer Mode; if unavailable, symlink_to raises OSError and we copy.
    if target_path.is_symlink():
        if target_path.resolve() == memory_path.resolve():
            return  # already correct — no work, and avoids SameFileError
        target_path.unlink()
    elif target_path.exists():
        # Back up any existing real file before replacing it.
        target_path.rename(target_path.with_suffix(".pre-memsync.bak"))

    try:
        target_path.symlink_to(memory_path)
    except OSError:
        shutil.copy2(memory_path, target_path)
```

- [ ] **Step 4: Run the full claude_md test module to verify pass + no regressions**

Run: `python -m pytest tests/test_claude_md.py -v`
Expected: PASS for all tests, including the new one.

- [ ] **Step 5: Commit**

```bash
git add memsync/claude_md.py tests/test_claude_md.py
git commit -m "fix(claude_md): prefer symlink on Windows, avoid SameFileError when target already linked"
```

---

### Task 2: Install and connect Syncthing on all nodes over Tailscale

**Files:** none (system setup). Deliverable: Syncthing running on Pi, Windows, Mac; each admin UI reachable; devices mutually added; discovery and relaying OFF.

- [ ] **Step 1: Install Syncthing**

- Pi: `sudo apt-get update && sudo apt-get install -y syncthing`
- Windows (this box): `winget install -e --id Syncthing.Syncthing`
- macOS: `brew install syncthing`

- [ ] **Step 2: Start Syncthing once on each node to generate its device ID**

- Pi: `syncthing --version` then run as service in Step 6 of Task 7; for now `systemctl --user start syncthing` (or `syncthing` in a terminal).
- Windows: launch "Syncthing" (opens `http://127.0.0.1:8384`).
- macOS: `brew services start syncthing` (opens `http://127.0.0.1:8384`).

Expected: each node's web UI at `http://127.0.0.1:8384` loads.

- [ ] **Step 3: Record each node's Device ID**

In each web UI: Actions → Show ID. Copy all three IDs.

- [ ] **Step 4: Add the other two devices on each node, pinning Tailscale addresses**

On each node: Add Remote Device → paste the other node's Device ID → under Advanced → Addresses, set `tcp://<that node's Tailscale IP>:22000` (from the environment table) instead of `dynamic`. On the Pi's device entries, tick **Introducer**.

- [ ] **Step 5: Disable global discovery and relaying (privacy)**

On each node: Settings → Connections → uncheck **Global Discovery**, **Enable Relaying**, and **NAT Traversal**. Leave **Local Discovery** on. Save.

- [ ] **Step 6: Verify direct Tailscale connections**

In each web UI, confirm the other devices show **Connected** with a `tcp://100.x.x.x` address (a Tailscale IP), not a relay.
Expected: all three nodes show each other Connected over Tailscale IPs.

---

### Task 3: Create and verify the shared `memsync-shared` folder

**Files:** creates the `memsync-shared` folder on each node (no repo files). Deliverable: an empty shared folder that round-trips a test file across all three nodes.

- [ ] **Step 1: Create the folder on each node**

- Windows: `New-Item -ItemType Directory -Force "$env:USERPROFILE\memsync-shared" | Out-Null`
- macOS: `mkdir -p ~/memsync-shared`
- Pi: `mkdir -p "$HOME/memsync-shared"`

- [ ] **Step 2: Add and share the folder in Syncthing**

On the Pi web UI: Add Folder → Folder Label `memsync-shared`, Folder ID `memsync-shared`, Path `$HOME/memsync-shared` → Sharing tab: share with Windows and Mac → Save. On Windows and Mac, accept the incoming folder share and set its local path to the folder from Step 1.

- [ ] **Step 3: Round-trip test**

On the Pi: `echo "sync-check $(date -u +%FT%TZ)" > "$HOME/memsync-shared/PING.txt"`
Within ~10s, on Windows: `Get-Content "$env:USERPROFILE\memsync-shared\PING.txt"`
Expected: the same line appears. Repeat by editing on Windows and reading on Mac.

- [ ] **Step 4: Remove the test file and confirm deletion propagates**

On Windows: `Remove-Item "$env:USERPROFILE\memsync-shared\PING.txt"`
Expected: within ~10s the file is gone on Pi and Mac. All folders show **Up to Date**.

---

### Task 4: Migrate Memsync off OneDrive into the shared folder

**Files:** copies existing memory data; updates machine-local `config.toml` on each machine (via CLI); creates `~/.claude/CLAUDE.md` symlink. Deliverable: each machine's Memsync reads/writes the shared folder and `memsync status` is healthy; a `refresh` on one machine appears on another. Depends on Task 1 (symlink patch) and Task 3.

- [ ] **Step 1: Confirm the current memory root (source of truth)**

On Windows: `memsync status`
Expected: prints the memory root, e.g. `C:\Users\ianfe\OneDrive\.claude-memory`. Record it as the migration source.

- [ ] **Step 2: Copy the memory data into the shared folder (one machine only)**

On Windows (adjust source if Step 1 differed):

```powershell
$src = "C:\Users\ianfe\OneDrive\.claude-memory"
$dst = "$env:USERPROFILE\memsync-shared\.claude-memory"
Copy-Item $src $dst -Recurse -Force
```

- [ ] **Step 3: Guard — ensure no secrets rode along**

```powershell
Get-ChildItem "$env:USERPROFILE\memsync-shared" -Recurse -Force | Where-Object { $_.Name -match 'config\.toml|\.env$|secrets' }
```
Expected: no output. If anything lists, delete it from the shared folder before it replicates. `config.toml` must stay only in `%APPDATA%\memsync`.

- [ ] **Step 4: Let it replicate, then verify byte-for-byte on the Pi**

On the Pi:

```bash
diff -rq "$HOME/memsync-shared/.claude-memory" <(: ) 2>/dev/null; \
ls -la "$HOME/memsync-shared/.claude-memory" && \
sha256sum "$HOME/memsync-shared/.claude-memory/GLOBAL_MEMORY.md"
```
On Windows: `Get-FileHash "$env:USERPROFILE\memsync-shared\.claude-memory\GLOBAL_MEMORY.md" -Algorithm SHA256`
Expected: the SHA-256 of `GLOBAL_MEMORY.md` matches on both.

- [ ] **Step 5: Re-point Memsync on each machine (auto-switches provider to custom)**

- Windows: `memsync config set sync_root "C:\Users\ianfe\memsync-shared"`
- macOS: `memsync config set sync_root "$HOME/memsync-shared"`
Expected: each prints the updated value and provider becomes `custom`. Confirm with `memsync config show`.

- [ ] **Step 6: Back up and replace `~/.claude/CLAUDE.md` with a symlink**

- Windows (admin shell):

```powershell
$link = "$env:USERPROFILE\.claude\CLAUDE.md"
$target = "$env:USERPROFILE\memsync-shared\.claude-memory\GLOBAL_MEMORY.md"
if (Test-Path $link) { Move-Item $link "$link.pre-pisync.bak" -Force }
New-Item -ItemType SymbolicLink -Path $link -Target $target | Out-Null
```

- macOS: `ln -sf ~/memsync-shared/.claude-memory/GLOBAL_MEMORY.md ~/.claude/CLAUDE.md`

- [ ] **Step 7: Verify Memsync health and the symlink**

- `memsync status` on each machine — Expected: memory root is the shared folder; no errors.
- Windows: `(Get-Item "$env:USERPROFILE\.claude\CLAUDE.md").LinkType` — Expected: `SymbolicLink`.

- [ ] **Step 8: End-to-end cross-machine test**

On Windows: `memsync refresh --notes "pi-sync migration smoke test $(Get-Date -Format o)"`
Expected: within ~10s, on the Mac, `tail -n 5 ~/memsync-shared/.claude-memory/GLOBAL_MEMORY.md` shows the merged note, and `cat ~/.claude/CLAUDE.md` reflects it (symlink is live). Run `memsync refresh`/`harvest` once more on Windows to confirm no `SameFileError` (validates Task 1).

- [ ] **Step 9: Freeze the OneDrive copy (do not delete yet)**

Rename the old memory folder so nothing writes to it, keeping it as a one-week fallback:

```powershell
Rename-Item "C:\Users\ianfe\OneDrive\.claude-memory" ".claude-memory.RETIRED-2026-06-30"
```
Expected: `memsync status` still healthy (it now uses the shared folder). Final removal happens in Task 8.

---

### Task 5: Pi git snapshot layer (history, Pi-only)

**Files (on Pi):** `~/bin/memsync-git-snapshot.sh`; a user crontab entry; git repo at `~/memsync-backup.git`. Deliverable: git history accrues every 15 min; no `.git` ever appears inside the synced folder.

- [ ] **Step 1: Initialize the metadata-only repo (GIT_DIR outside the work-tree)**

```bash
git init --bare "$HOME/memsync-backup.git"
GD="$HOME/memsync-backup.git"; WT="$HOME/memsync-shared"
git --git-dir="$GD" --work-tree="$WT" config core.worktree "$WT"
git --git-dir="$GD" --work-tree="$WT" config user.email "pi@pi-gateway"
git --git-dir="$GD" --work-tree="$WT" config user.name "memsync snapshot"
```

- [ ] **Step 2: Write the snapshot script**

Create `~/bin/memsync-git-snapshot.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
GD="$HOME/memsync-backup.git"
WT="$HOME/memsync-shared"

# Guard: a .git inside the synced tree means the design was violated.
if [ -e "$WT/.git" ]; then
  echo "ERROR: .git found inside synced folder $WT — aborting" >&2
  exit 1
fi

git --git-dir="$GD" --work-tree="$WT" add -A
if git --git-dir="$GD" --work-tree="$WT" diff --cached --quiet; then
  exit 0   # nothing changed — no empty commit
fi
git --git-dir="$GD" --work-tree="$WT" commit -q -m "snapshot $(date -u +%FT%TZ)"
```

Then: `chmod +x ~/bin/memsync-git-snapshot.sh`

- [ ] **Step 3: Verify commit-on-change and no-empty-commit**

```bash
echo "test $(date -u +%FT%TZ)" >> "$HOME/memsync-shared/.claude-memory/sessions/$(date -u +%F).md"
~/bin/memsync-git-snapshot.sh
git --git-dir="$HOME/memsync-backup.git" log --oneline -1   # expect a new snapshot commit
~/bin/memsync-git-snapshot.sh
git --git-dir="$HOME/memsync-backup.git" log --oneline -1   # expect SAME commit (no empty commit)
```
Expected: first run creates a commit; second run leaves the log unchanged.

- [ ] **Step 4: Schedule it every 15 minutes (headless)**

```bash
( crontab -l 2>/dev/null; echo "*/15 * * * * /bin/bash $HOME/bin/memsync-git-snapshot.sh >> $HOME/memsync-snapshot.log 2>&1" ) | crontab -
crontab -l | grep memsync-git-snapshot
```
Expected: the cron line is present.

---

### Task 6: Encrypted offsite backup to the Pi external SSD (restic)

**Files (on Pi):** restic repo on the SSD; password file `~/.config/memsync-restic.pass` (chmod 600); `~/bin/memsync-restic-backup.sh`; crontab entry. Deliverable: encrypted snapshots on the SSD with retention, and a proven restore. Depends on Task 5 (backs up both the work-tree and the git repo).

- [ ] **Step 1: Install restic and create the password file**

```bash
sudo apt-get install -y restic
umask 077
head -c 32 /dev/urandom | base64 > "$HOME/.config/memsync-restic.pass"
chmod 600 "$HOME/.config/memsync-restic.pass"
```
Note: this password protects all backups. Record it once in your password manager; if lost, backups are unrecoverable.

- [ ] **Step 2: Initialize the restic repo on the SSD**

Use the SSD mountpoint from the environment table (example `/mnt/ssd`):

```bash
export RESTIC_REPOSITORY="/mnt/ssd/memsync-backups"
export RESTIC_PASSWORD_FILE="$HOME/.config/memsync-restic.pass"
restic init
```
Expected: `created restic repository ... at /mnt/ssd/memsync-backups`.

- [ ] **Step 3: Write the backup script (encrypted, with retention)**

Create `~/bin/memsync-restic-backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
export RESTIC_REPOSITORY="/mnt/ssd/memsync-backups"
export RESTIC_PASSWORD_FILE="$HOME/.config/memsync-restic.pass"

# Back up both the live files and the git history repo.
restic backup "$HOME/memsync-shared" "$HOME/memsync-backup.git" --tag memsync
restic forget --tag memsync --keep-hourly 24 --keep-daily 14 --keep-weekly 8 --prune
```

Then: `chmod +x ~/bin/memsync-restic-backup.sh`

- [ ] **Step 4: Run once and confirm an encrypted snapshot exists**

```bash
~/bin/memsync-restic-backup.sh
RESTIC_REPOSITORY=/mnt/ssd/memsync-backups RESTIC_PASSWORD_FILE=$HOME/.config/memsync-restic.pass restic snapshots
```
Expected: at least one snapshot listed, tagged `memsync`.

- [ ] **Step 5: Restore drill (prove recoverability)**

```bash
export RESTIC_REPOSITORY=/mnt/ssd/memsync-backups RESTIC_PASSWORD_FILE=$HOME/.config/memsync-restic.pass
restic restore latest --target /tmp/memsync-restore
diff "$HOME/memsync-shared/.claude-memory/GLOBAL_MEMORY.md" \
     "/tmp/memsync-restore$HOME/memsync-shared/.claude-memory/GLOBAL_MEMORY.md"
```
Expected: `diff` prints nothing (identical). Then `rm -rf /tmp/memsync-restore`.

- [ ] **Step 6: Schedule hourly (headless), offset from the git snapshot**

```bash
( crontab -l 2>/dev/null; echo "7 * * * * /bin/bash $HOME/bin/memsync-restic-backup.sh >> $HOME/memsync-restic.log 2>&1" ) | crontab -
crontab -l | grep memsync-restic
```
Expected: the cron line is present.

---

### Task 7: Run-as-service and staleness/conflict monitoring

**Files (on Pi):** `~/bin/memsync-health.sh`; crontab entry. Plus service enablement on each machine. Deliverable: Syncthing survives reboots on every node; stale sync or conflict files are surfaced.

- [ ] **Step 1: Enable Syncthing as a service on each node**

- Pi: `sudo systemctl enable --now syncthing@$(whoami)`
- Windows: install the Syncthing service wrapper so it runs hidden at logon (SyncTrayzor's service mode, or `syncthing` via Task Scheduler at logon with "Run whether user is logged on or not", hidden). Verify it restarts after reboot.
- macOS: `brew services start syncthing` (already a launch agent).

- [ ] **Step 2: Write a health check on the Pi**

Create `~/bin/memsync-health.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
WT="$HOME/memsync-shared"
# Flag any Syncthing conflict files.
conflicts=$(find "$WT" -name '*.sync-conflict-*' 2>/dev/null || true)
# Flag stale memory: GLOBAL_MEMORY.md not modified in > 8 days (harvest is daily).
mem="$WT/.claude-memory/GLOBAL_MEMORY.md"
stale=""
if [ -f "$mem" ] && [ "$(find "$mem" -mtime +8 2>/dev/null)" ]; then stale="$mem"; fi
if [ -n "$conflicts" ] || [ -n "$stale" ]; then
  echo "MEMSYNC HEALTH ALERT $(date -u +%FT%TZ)"
  [ -n "$conflicts" ] && echo "conflict files:" && echo "$conflicts"
  [ -n "$stale" ] && echo "stale memory file: $stale"
fi
```

Then: `chmod +x ~/bin/memsync-health.sh`

- [ ] **Step 3: Verify the conflict path fires**

```bash
touch "$HOME/memsync-shared/.claude-memory/GLOBAL_MEMORY.sync-conflict-TEST"
~/bin/memsync-health.sh   # expect an ALERT listing the conflict file
rm "$HOME/memsync-shared/.claude-memory/GLOBAL_MEMORY.sync-conflict-TEST"
~/bin/memsync-health.sh   # expect no output
```
Expected: alert on the first run, silence on the second.

- [ ] **Step 4: Schedule the health check and route output to the existing Pi dashboard/log**

```bash
( crontab -l 2>/dev/null; echo "20 * * * * /bin/bash $HOME/bin/memsync-health.sh >> $HOME/memsync-health.log 2>&1" ) | crontab -
crontab -l | grep memsync-health
```
Expected: the cron line is present. (Optionally surface `memsync-health.log` on the `pi-gateway:8765` dashboard.)

- [ ] **Step 5: Reboot resilience test**

Reboot the Windows box; after logon, confirm Syncthing is running (service, no window) and the folder shows **Up to Date** without manual start.

---

### Task 8: Cutover — retire the OneDrive memory copy

**Files:** none. Deliverable: OneDrive no longer holds Memsync data; one-week fallback observed first. Do this only after Tasks 4-7 have run cleanly for about a week.

- [ ] **Step 1: Confirm a week of clean operation**

On the Pi: `git --git-dir="$HOME/memsync-backup.git" log --oneline | head` shows regular snapshots; `restic snapshots` shows daily backups; `memsync-health.log` shows no unresolved alerts.

- [ ] **Step 2: Delete the retired OneDrive copy**

```powershell
Remove-Item "C:\Users\ianfe\OneDrive\.claude-memory.RETIRED-2026-06-30" -Recurse -Force
```
Expected: `memsync status` on every machine remains healthy (using the shared folder).

- [ ] **Step 3: Record the outcome**

Note in the project that Memsync no longer depends on OneDrive. (Repos remain in OneDrive; moving them is a separate, optional future project — see `docs/pi-memory-sync-design-2026-06-30.md`.)

---

## Self-review notes

- Spec coverage: Topology (Tasks 2-3), git-only-on-Pi trick (Task 5), Syncthing config with discovery/relay off (Task 2), Memsync `custom` wiring (Task 4), Windows symlink via patch (Tasks 1, 4), encrypted SSD backup + restore drill (Task 6), migration + one-week fallback (Tasks 4, 8), conflict/staleness handling (Task 7), security guard on secrets (Task 4 Step 3, Global Constraints). All spec sections map to a task.
- The only Memsync code change is Task 1 (`claude_md.py`), consistent with the no-fork constraint.
- Per-project memory (spec v2) is intentionally out of scope here.
```
