# EXISTING_CODE.md

This is the working prototype built before the architecture was formalized.
Use this as the foundation — refactor it to fit the target architecture
described in ARCHITECTURE.md. Do not rewrite from scratch.

The prototype works and has been designed with the final architecture in mind.
The main gaps are: no provider abstraction, no config system, hardcoded model string.

---

## memsync/paths.py (prototype)

This becomes the provider system. Replace with `memsync/providers/`.
The detection logic here is correct and tested — migrate it into
`OneDriveProvider.detect()`.

```python
"""
Path resolution for memsync.
Handles Mac, Windows, and OneDrive sync layer.
"""

import os
import platform
from pathlib import Path


def get_platform() -> str:
    system = platform.system()
    if system == "Darwin":
        return "mac"
    elif system == "Windows":
        return "windows"
    else:
        return "linux"


def get_onedrive_root() -> Path:
    """
    Resolve the OneDrive root directory cross-platform.
    Checks env vars first (most reliable), then common default paths.
    """
    if get_platform() == "windows":
        onedrive = os.environ.get("OneDrive") or os.environ.get("ONEDRIVE")
        if onedrive:
            return Path(onedrive)
        candidates = [
            Path.home() / "OneDrive",
            Path("C:/Users") / os.environ.get("USERNAME", "") / "OneDrive",
        ]
    else:
        candidates = [
            Path.home() / "OneDrive",
            Path.home() / "Library" / "CloudStorage" / "OneDrive-Personal",
        ]
        cloud_storage = Path.home() / "Library" / "CloudStorage"
        if cloud_storage.exists():
            for d in cloud_storage.iterdir():
                if d.name.startswith("OneDrive"):
                    candidates.insert(0, d)

    for path in candidates:
        if path.exists():
            return path

    raise FileNotFoundError(
        "OneDrive directory not found. "
        "Set MEMSYNC_ONEDRIVE env var to your OneDrive path."
    )


def get_memory_paths() -> dict[str, Path]:
    """
    Returns all relevant paths for memsync.
    MEMSYNC_ONEDRIVE env var overrides auto-detection.
    """
    onedrive_override = os.environ.get("MEMSYNC_ONEDRIVE")
    onedrive_root = Path(onedrive_override) if onedrive_override else get_onedrive_root()

    memory_root = onedrive_root / ".claude-memory"

    if get_platform() == "windows":
        claude_config = Path.home() / ".claude"
    else:
        claude_config = Path.home() / ".claude"

    return {
        "onedrive_root": onedrive_root,
        "memory_root": memory_root,
        "global_memory": memory_root / "GLOBAL_MEMORY.md",
        "backups": memory_root / "backups",
        "session_log": memory_root / "sessions",
        "claude_config": claude_config,
        "claude_md": claude_config / "CLAUDE.md",
    }


def ensure_directories(paths: dict[str, Path]) -> None:
    for key in ("memory_root", "backups", "session_log"):
        paths[key].mkdir(parents=True, exist_ok=True)
```

---

## memsync/sync.py (prototype)

The core API call and compaction logic. Migrate this into the new `sync.py`
but pull `model` from config instead of hardcoding it.
The system prompt here is the result of iteration — don't change it lightly.
See PITFALLS.md for why specific lines are the way they are.

```python
"""
Memory refresh logic.
Calls Claude API to merge session notes into GLOBAL_MEMORY.md.
"""

import shutil
from datetime import datetime
from pathlib import Path

import anthropic

from .paths import get_memory_paths, ensure_directories

SYSTEM_PROMPT = """You are maintaining a persistent global memory file for an AI assistant user.
This file is loaded at the start of every Claude Code session, on every machine and project.
It is the user's identity layer — not project docs, not cold storage.

YOUR JOB:
- Merge new session notes into the existing memory file
- Keep the file tight (under 400 lines)
- Update facts that have changed
- Demote completed items from "Current priorities" to a brief "Recent completions" section
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append
- If nothing meaningful changed, return the file UNCHANGED

RETURN: Only the updated GLOBAL_MEMORY.md content. No explanation, no preamble."""


def load_or_init_memory(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")

    return """\
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
"""


def backup_memory(memory_path: Path, backup_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"GLOBAL_MEMORY_{timestamp}.md"
    shutil.copy2(memory_path, backup_path)
    return backup_path


def refresh_memory(notes: str, dry_run: bool = False) -> dict:
    paths = get_memory_paths()
    ensure_directories(paths)

    current_memory = load_or_init_memory(paths["global_memory"])

    client = anthropic.Anthropic()

    user_prompt = f"""\
CURRENT GLOBAL MEMORY:
{current_memory}

SESSION NOTES:
{notes}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",  # ← HARDCODED: move to config in refactor
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    updated_content = response.content[0].text.strip()
    changed = updated_content != current_memory.strip()

    if dry_run:
        return {
            "updated_content": updated_content,
            "backup_path": None,
            "changed": changed,
            "dry_run": True,
        }

    backup_path = None
    if paths["global_memory"].exists() and changed:
        backup_path = backup_memory(paths["global_memory"], paths["backups"])

    paths["global_memory"].write_text(updated_content, encoding="utf-8")
    sync_to_claude_md(paths)
    log_session_notes(notes, paths["session_log"])

    return {
        "updated_content": updated_content,
        "backup_path": backup_path,
        "changed": changed,
        "dry_run": False,
    }


def sync_to_claude_md(paths: dict) -> None:
    """
    Keep ~/.claude/CLAUDE.md in sync with the OneDrive master.
    Mac/Linux: symlink. Windows: copy.
    """
    import platform

    source = paths["global_memory"]
    dest = paths["claude_md"]

    dest.parent.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Windows":
        shutil.copy2(source, dest)
        return

    if dest.is_symlink():
        if dest.resolve() == source.resolve():
            return
        dest.unlink()

    if dest.exists():
        dest.rename(dest.with_suffix(".pre-memsync.bak"))

    try:
        dest.symlink_to(source)
    except OSError:
        shutil.copy2(source, dest)


def log_session_notes(notes: str, session_dir: Path) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = session_dir / f"{today}.md"
    timestamp = datetime.now().strftime("%H:%M:%S")

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n---\n### {timestamp}\n{notes}\n")


def prune_backups(backup_dir: Path, keep_days: int = 30) -> list[Path]:
    from datetime import timedelta

    cutoff = datetime.now() - timedelta(days=keep_days)
    deleted = []

    for backup in backup_dir.glob("GLOBAL_MEMORY_*.md"):
        try:
            ts_str = backup.stem.replace("GLOBAL_MEMORY_", "")
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if ts < cutoff:
                backup.unlink()
                deleted.append(backup)
        except ValueError:
            pass

    return deleted
```

---

## memsync/cli.py (prototype)

The full CLI. Refactor to pass `config` into each command function
and replace direct path dict calls with provider + config resolution.

```python
"""
memsync CLI — see COMMANDS.md for full spec.
"""

import sys
import argparse
from pathlib import Path

from .paths import get_memory_paths, ensure_directories, get_platform
from .sync import refresh_memory, prune_backups, load_or_init_memory


def cmd_refresh(args: argparse.Namespace) -> int:
    notes = ""

    if args.notes:
        notes = args.notes
    elif args.file:
        note_path = Path(args.file)
        if not note_path.exists():
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            return 1
        notes = note_path.read_text(encoding="utf-8")
    else:
        if not sys.stdin.isatty():
            notes = sys.stdin.read()
        else:
            print("Error: provide --notes, --file, or pipe notes via stdin.", file=sys.stderr)
            return 1

    if not notes.strip():
        print("Error: notes are empty.", file=sys.stderr)
        return 1

    print("Refreshing global memory...", end=" ", flush=True)
    result = refresh_memory(notes, dry_run=args.dry_run)

    if args.dry_run:
        print("\n[DRY RUN] No files written.\n")
        if result["changed"]:
            print("Changes detected. Updated content:")
            print("─" * 60)
            print(result["updated_content"])
        else:
            print("No changes detected.")
        return 0

    if result["changed"]:
        print("done.")
        if result["backup_path"]:
            print(f"  Backup:  {result['backup_path']}")
        paths = get_memory_paths()
        print(f"  Memory:  {paths['global_memory']}")
        print(f"  CLAUDE.md: {paths['claude_md']}")
    else:
        print("no changes.")

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        paths = get_memory_paths()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Platform:      {get_platform()}")
    print(f"OneDrive root: {paths['onedrive_root']}")
    print(f"Memory file:   {paths['global_memory']} ", end="")
    print("✓" if paths["global_memory"].exists() else "✗ (not created yet)")
    print(f"CLAUDE.md:     {paths['claude_md']} ", end="")

    claude_md = paths["claude_md"]
    if claude_md.is_symlink():
        print(f"→ symlink to {claude_md.resolve()}")
    elif claude_md.exists():
        print("✓ (copy)")
    else:
        print("✗ (not synced)")

    backup_dir = paths["backups"]
    if backup_dir.exists():
        backups = list(backup_dir.glob("GLOBAL_MEMORY_*.md"))
        print(f"Backups:       {len(backups)} file(s) in {backup_dir}")

    session_dir = paths["session_log"]
    if session_dir.exists():
        sessions = list(session_dir.glob("*.md"))
        print(f"Session logs:  {len(sessions)} day(s) logged in {session_dir}")

    return 0


def cmd_show(args: argparse.Namespace) -> int:
    paths = get_memory_paths()
    if not paths["global_memory"].exists():
        print("No global memory file yet. Run: memsync init")
        return 1
    print(paths["global_memory"].read_text(encoding="utf-8"))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    import difflib

    paths = get_memory_paths()
    backup_dir = paths["backups"]

    if not paths["global_memory"].exists():
        print("No global memory file yet.")
        return 1

    backups = sorted(backup_dir.glob("GLOBAL_MEMORY_*.md"))
    if not backups:
        print("No backups found.")
        return 0

    latest_backup = backups[-1]
    current = paths["global_memory"].read_text(encoding="utf-8").splitlines(keepends=True)
    previous = latest_backup.read_text(encoding="utf-8").splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        previous, current,
        fromfile=f"backup ({latest_backup.name})",
        tofile="current",
    ))

    if diff:
        print("".join(diff))
    else:
        print("No differences from last backup.")

    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    paths = get_memory_paths()
    deleted = prune_backups(paths["backups"], keep_days=args.keep_days)
    if deleted:
        print(f"Pruned {len(deleted)} backup(s) older than {args.keep_days} days.")
        for p in deleted:
            print(f"  removed: {p.name}")
    else:
        print(f"No backups older than {args.keep_days} days.")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    try:
        paths = get_memory_paths()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Set MEMSYNC_ONEDRIVE env var to your OneDrive path and retry.")
        return 1

    ensure_directories(paths)

    if paths["global_memory"].exists() and not args.force:
        print(f"Memory file already exists: {paths['global_memory']}")
        print("Use --force to reinitialize.")
        return 0

    starter = load_or_init_memory(Path("/dev/null"))
    paths["global_memory"].write_text(starter, encoding="utf-8")

    from .sync import sync_to_claude_md
    sync_to_claude_md(paths)

    print("memsync initialized.")
    print(f"  Memory:    {paths['global_memory']}")
    print(f"  CLAUDE.md: {paths['claude_md']}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="memsync",
        description="Cross-platform global memory manager for Claude Code.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_refresh = subparsers.add_parser("refresh", help="Merge session notes into global memory")
    p_refresh.add_argument("--notes", "-n", help="Session notes as a string")
    p_refresh.add_argument("--file", "-f", help="Path to a file containing session notes")
    p_refresh.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    p_refresh.set_defaults(func=cmd_refresh)

    p_status = subparsers.add_parser("status", help="Show paths and sync status")
    p_status.set_defaults(func=cmd_status)

    p_show = subparsers.add_parser("show", help="Print current global memory")
    p_show.set_defaults(func=cmd_show)

    p_diff = subparsers.add_parser("diff", help="Diff current memory against last backup")
    p_diff.set_defaults(func=cmd_diff)

    p_prune = subparsers.add_parser("prune", help="Remove old backups")
    p_prune.add_argument("--keep-days", type=int, default=30)
    p_prune.set_defaults(func=cmd_prune)

    p_init = subparsers.add_parser("init", help="Initialize memory structure")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
```

---

## pyproject.toml (prototype)

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "memsync"
version = "0.1.0"
description = "Cross-platform global memory manager for Claude Code"
requires-python = ">=3.11"
dependencies = [
    "anthropic>=0.40.0",
]

[project.scripts]
memsync = "memsync.cli:main"

[tool.setuptools.packages.find]
where = ["."]
include = ["memsync*"]
```
