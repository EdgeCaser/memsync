# Pi Hybrid Memory Sync — Design Spec

Date: 2026-06-30
Status: Draft for review
Owner: Ian

## Problem

Memsync unites global memory across machines by writing a small set of text
files into a local folder and letting a cloud service replicate that folder.
Today the transport is OneDrive. OneDrive is unreliable at scale on the primary
Windows box (it wedges on "looking for changes" under the weight of ~946K files,
69 git repos, and 86 node_modules trees that share the same OneDrive account),
and it is a paid/proprietary dependency for what is really a handful of small
text files.

We want the same "work on the same projects across machines without re-sharing
context" outcome, using hardware already owned (the always-on Pi, `pi-gateway`),
with real backups, and without renting a server.

## Goals

- Replicate Memsync's `.claude-memory` folder across all machines through the Pi,
  independent of OneDrive.
- Continuous, hands-off sync (no manual push/pull in the common case).
- Versioned, offsite-capable backup with nothing sensitive leaving the Pi in
  readable form.
- No fork of Memsync. Ideally zero core code changes to start.
- Decouple memory reliability from OneDrive's health.

## Non-goals (v1)

- Syncing raw Claude Code session transcripts (`~/.claude/projects/**/*.jsonl`).
  The distilled `GLOBAL_MEMORY.md` is what enables cross-machine resume; each
  machine harvests its own local sessions. Transcripts already reach the Pi via
  `tokmon-sync` for the token dashboard and stay on that separate path.
- Syncing per-project memory dirs (`~/.claude/projects/<key>/memory/`). Planned
  as v2 (see "Future work").
- Team / multi-user memory. Out of scope, same as core Memsync.

## Scope

v1 syncs exactly Memsync's current set inside `.claude-memory/`:
`GLOBAL_MEMORY.md`, `harvested.json`, `backups/`, `sessions/`.

### Explicitly untouched by this design

This design relocates ONLY the `.claude-memory` folder (a handful of small text
files) off OneDrive. It does NOT move, rename, or touch any git repository under
`C:\Users\ianfe\OneDrive\Documents\GitHub\`. Every repo stays at its current
path. No tool, script, scheduled task, or config that references a repo path
needs to change. The only paths that change are Memsync's own — the `custom`
provider `sync_root` in machine-local config, and the `~/.claude/CLAUDE.md`
symlink target — both of which we set deliberately as part of setup. Moving the
repo tree out of OneDrive is a separate, optional, future project (see Migration
note) and is NOT required or implied by this spec.

## Architecture

### Topology

```
              Tailscale mesh (no public relays)
  Windows box  <=> ┐
                    ├<=>  Pi (pi-gateway): always-on Syncthing hub + "introducer"
  MacBook Pro  <=> ┘            │
                                ├─ git snapshot every 15 min ──► local version history
                                └─ encrypted snapshot ──► Pi external SSD (offsite-of-machines)
```

- **Syncthing** runs on every machine and on the Pi. One shared folder holds
  `.claude-memory`. The Pi is the always-on node, so the two laptops converge
  even when they are never online simultaneously. The Pi is configured as an
  **introducer** so adding a machine later is a one-click accept.
- All Syncthing traffic rides **Tailscale**. Each device's address is pinned to
  its Tailscale IP; **global discovery and relaying are disabled** so the memory
  file never traverses a public Syncthing relay.
- **Git snapshots** live only on the Pi and provide point-in-time history.
- **Offsite backup** is an encrypted snapshot written to the Pi's external SSD.

### The key trick: git lives only on the Pi

To avoid recreating the exact failure that plagues OneDrive (a churning `.git`
directory generating endless tiny change events), Syncthing must never sync any
`.git` folder. We achieve this by keeping git metadata **outside** the synced
tree, with the synced folder used only as a work-tree:

```bash
# On the Pi. GIT_DIR is outside the Syncthing folder; work-tree is inside it.
GD=/home/pi/memsync-backup.git
WT=/home/pi/sync/claude-memory        # the Syncthing-shared folder

git --git-dir="$GD" --work-tree="$WT" add -A
git --git-dir="$GD" --work-tree="$WT" commit -m "snapshot <UTC timestamp>"   # only if changes
```

The machines are pure Syncthing peers. They never see git, never churn objects,
and stay hands-off. The Pi alone owns history.

### Component: Syncthing

- **Folder**: shared folder id `claude-memory`. Local paths:
  - Windows: `C:\Users\ianfe\.claude-memory-sync`
  - macOS:   `~/.claude-memory-sync`
  - Pi:      `/home/pi/sync/claude-memory`
  - Memsync's memory root is `<folder>/.claude-memory` (Memsync's default layout
    under a `sync_root`). See "Memsync wiring" for exact `sync_root` values.
- **Devices**: Windows box, MacBook Pro, Pi. Pi = introducer.
- **Addressing**: static `tcp://<tailscale-ip>:22000` per device; `dynamic`
  removed; global discovery off; relaying off; NAT traversal off (Tailscale
  handles connectivity).
- **Versioning**: enable Syncthing "Staggered" file versioning on the Pi copy as
  a cheap secondary net (git snapshots are the primary history).
- **Ignore** (`.stignore`) inside the folder: optionally ignore `.claude-memory/backups/`
  since Pi git snapshots already provide history; this trims per-write churn. Keep
  `harvested.json` and `sessions/` synced.

### Component: Git snapshot layer (Pi)

- Bare-ish repo at `/home/pi/memsync-backup.git` (metadata only), work-tree =
  the Syncthing folder, as shown above.
- **Cron**: every 15 min, `add -A` then commit only when the tree changed
  (`git diff --cached --quiet || git commit ...`).
- Runs headless/hidden; no window, matching standing preference for scheduled jobs.

### Component: Offsite backup (Pi external SSD, encrypted)

- After each git commit (or on an hourly cron), write an **encrypted** snapshot to
  the external SSD. Two acceptable implementations, decide at plan time:
  - `git bundle` of the backup repo, then encrypt with `age` (or gpg) to
    `/mnt/ssd/memsync-backups/memsync-<UTC>.bundle.age`, keeping the last N.
  - or `restic` repo on the SSD (built-in encryption + dedup + retention).
- Nothing readable leaves the Pi. This satisfies the sensitivity of
  `GLOBAL_MEMORY.md` and the owner's identity-separation constraints
  recorded in that file.
- Backblaze B2 (encrypted) is a drop-in future add if an off-premises copy is
  wanted; not in v1.

### Component: Memsync wiring (no fork)

- On each machine, use the existing **`custom`** provider:
  - Windows: `sync_root = C:\Users\ianfe\.claude-memory-sync`
  - macOS:   `sync_root = ~/.claude-memory-sync`
  - Memsync then manages `<sync_root>/.claude-memory/` exactly as today.
- `config.toml` is machine-local and NOT synced, so each machine independently
  points at its own local Syncthing copy. No config conflicts.
- **No core code change is required for v1.** An optional ~15-line `pi`/`syncthing`
  provider for auto-detection ergonomics can be contributed later; it does not
  gate this design.

### Component: CLAUDE.md linkage per machine

- macOS: `~/.claude/CLAUDE.md` is a symlink into
  `<sync_root>/.claude-memory/GLOBAL_MEMORY.md`; Syncthing updates are picked up
  instantly (unchanged from Memsync default).
- Windows: **admin is granted**, so create `~/.claude/CLAUDE.md` as a real symlink
  (requires admin or Windows Developer Mode at creation time only). This makes
  Windows behave like macOS and removes the copy-refresh problem entirely.
  - Verify Memsync's `claude_md.sync()` treats an already-correct symlink as
    "in sync" (content matches through the link, so `is_synced()` should return
    true and skip re-copying). If it instead clobbers the symlink with a copy,
    add a small `claude_md.py` enhancement to detect and preserve a correct
    symlink on Windows. This is the one place a tiny upstream patch may be needed.
  - Fallback if symlink is ever undesirable: a hidden Task Scheduler job with a
    file-change trigger that re-runs the Memsync copy.

## Data layout on disk

```
# Synced folder (Syncthing), per machine — NOT in OneDrive:
<sync_root>/.claude-memory/
  GLOBAL_MEMORY.md          <- source of truth (symlinked to ~/.claude/CLAUDE.md)
  harvested.json            <- processed-session index (must stay in sync)
  backups/                  <- Memsync timestamped backups (optionally .stignore'd)
  sessions/                 <- append-only refresh notes

# Pi only (never synced):
/home/pi/memsync-backup.git         <- git metadata (GIT_DIR outside work-tree)
/mnt/ssd/memsync-backups/           <- encrypted offsite snapshots

# Each machine, machine-local (not synced):
~/.config/memsync/config.toml   (macOS)  /  %APPDATA%\memsync\config.toml (Windows)
~/.claude/CLAUDE.md             <- symlink -> GLOBAL_MEMORY.md
```

## Migration off OneDrive

1. Stand up Syncthing on Pi + Windows + Mac; establish the shared folder empty.
2. Copy current `OneDrive/.claude-memory` into the new folder on ONE machine;
   let it replicate; verify byte-for-byte on the others and the Pi.
3. Re-point each machine's `custom` provider `sync_root` to the new folder.
4. Recreate `~/.claude/CLAUDE.md` symlink to the new location on each machine.
5. Run `memsync status` on each machine to confirm it reads/writes correctly.
6. Stand up the Pi git repo + snapshot cron + encrypted SSD backup; confirm a
   commit and an encrypted snapshot appear.
7. Freeze (stop editing) the OneDrive copy but keep it ~1 week as a fallback,
   then remove it from OneDrive.

Note: repositories are NOT part of this migration and do not move. This step
relocates only the Memsync `.claude-memory` folder. As a side effect Memsync no
longer depends on OneDrive at all, so IF Ian ever chooses to tackle the OneDrive
repo-tree wedge, that becomes an independent decision with zero memory
implications. That is a distinct future project, not part of this spec.

## Backup & recovery runbook

- **Lose a laptop**: install Syncthing, accept the share, re-create the symlink.
  Full memory returns from the mesh.
- **Lose the Pi**: restore the newest encrypted snapshot from the SSD (or a
  freshly attached copy of it), re-create `memsync-backup.git`, set the Syncthing
  work-tree, resume. If the SSD is also gone, the live Syncthing copies on the
  laptops still hold current memory; only deep history is lost.
- **Bad merge / corruption**: `git --git-dir=... log` on the Pi, check out or
  restore any prior `GLOBAL_MEMORY.md`; Syncthing propagates the fix.

## Conflict handling

Memsync writes only on explicit `refresh`/`harvest`, which are infrequent and
usually one machine at a time, so true concurrent edits are rare. When they do
happen, Syncthing creates `*.sync-conflict-*` files rather than losing data, and
Memsync has already backed up the prior `GLOBAL_MEMORY.md`. The Pi git history is
the authoritative recovery point. Add a `memsync`-adjacent check (or a Pi cron
alert) that flags any `*.sync-conflict-*` file so it is reconciled promptly.

## Security & privacy

- All replication is Tailscale-only; Syncthing global discovery and relaying are
  disabled. The identity file never touches a public relay.
- Offsite backups are encrypted at rest on the Pi SSD (`age`/gpg or `restic`).
- No credentials are stored in any repo or synced file. `GLOBAL_MEMORY.md`
  content is never published anywhere public, and the identity-separation
  constraints it records are never breached by any exposed artifact.

## Testing / verification

- Sync: edit `GLOBAL_MEMORY.md` on machine A via `memsync refresh --notes`,
  confirm it appears on machine B and the Pi within seconds; confirm
  `~/.claude/CLAUDE.md` reflects it on both.
- Snapshot: force a change, wait for the cron window, confirm a new git commit and
  a new encrypted snapshot on the SSD.
- Restore drill: from the newest encrypted snapshot, reconstruct the repo in a
  scratch dir and diff against live; expect identical `GLOBAL_MEMORY.md`.
- Offline: disconnect a laptop, edit memory locally, reconnect, confirm clean
  convergence with no conflict file.

## Future work (v2+)

- **Per-project memory**: extend scope to `~/.claude/projects/<key>/memory/`
  dirs so project-specific notes travel across machines too. Larger conflict
  surface; design separately.
- **Optional `pi` provider**: ~15 lines for auto-detection; contributed upstream
  the normal way (no fork).
- **Backblaze B2** encrypted off-premises copy, layered on the same snapshot job.

## Risks & mitigations

- *Syncthing daemon not running on a machine* -> memory goes stale silently.
  Mitigate: run as a service (login item on Mac, hidden scheduled task/service on
  Windows) and add a simple "last-synced age" check to `memsync status` output or
  the Pi dashboard.
- *Windows symlink edge cases* -> covered by the admin grant + the `claude_md.py`
  verification step above.
- *`.git` accidentally inside the synced folder* -> forbidden by design (GIT_DIR
  is external); add a guard in the snapshot script that refuses to run if a
  `.git` appears inside the work-tree.
```
