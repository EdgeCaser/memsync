from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

# Prefixes we recognise as memsync-managed backups. Files outside this list are
# ignored by prune/list_backups so user-dropped files in the dir aren't touched.
_BACKUP_PREFIXES = ("GLOBAL_MEMORY_", "MEMORY_ARCHIVE_")


def backup(source: Path, backup_dir: Path) -> Path:
    """
    Copy source to backup_dir with a timestamp suffix.
    The destination name is `{source.stem}_{timestamp}.md`, so both hot
    (GLOBAL_MEMORY.md) and cold (MEMORY_ARCHIVE.md) files keep their identity.
    Returns the path of the new backup file.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"{source.stem}_{timestamp}.md"
    shutil.copy2(source, dest)
    return dest


def prune(backup_dir: Path, keep_days: int) -> list[Path]:
    """
    Delete backups older than keep_days. Returns list of deleted paths.
    Covers both GLOBAL_MEMORY_* (hot) and MEMORY_ARCHIVE_* (cold) backups.
    """
    cutoff = datetime.now() - timedelta(days=keep_days)
    deleted: list[Path] = []

    for prefix in _BACKUP_PREFIXES:
        for backup_file in backup_dir.glob(f"{prefix}*.md"):
            try:
                ts_str = backup_file.stem.removeprefix(prefix)
                ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                if ts < cutoff:
                    backup_file.unlink()
                    deleted.append(backup_file)
            except ValueError:
                pass  # skip files with unexpected names

    return deleted


def list_backups(backup_dir: Path) -> list[Path]:
    """Return all hot-layer backups sorted newest-first.

    Hot-only on purpose: callers (cmd_diff, status counts) reason about the
    hot layer's history. Use list_archive_backups() for cold-layer history.
    """
    backups = list(backup_dir.glob("GLOBAL_MEMORY_*.md"))
    return sorted(backups, reverse=True)


def list_archive_backups(backup_dir: Path) -> list[Path]:
    """Return all cold-layer backups sorted newest-first."""
    backups = list(backup_dir.glob("MEMORY_ARCHIVE_*.md"))
    return sorted(backups, reverse=True)


def latest_backup(backup_dir: Path) -> Path | None:
    """Return the most recent hot-layer backup, or None if no backups exist."""
    backups = list_backups(backup_dir)
    return backups[0] if backups else None
