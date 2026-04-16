from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path


def backup(source: Path, backup_dir: Path) -> Path:
    """
    Copy source to backup_dir with a timestamp suffix.
    Returns the path of the new backup file.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"GLOBAL_MEMORY_{timestamp}.md"
    shutil.copy2(source, dest)
    return dest


def prune(backup_dir: Path, keep_days: int) -> list[Path]:
    """
    Delete backups older than keep_days. Returns list of deleted paths.
    """
    cutoff = datetime.now() - timedelta(days=keep_days)
    deleted: list[Path] = []

    for backup_file in backup_dir.glob("GLOBAL_MEMORY_*.md"):
        try:
            ts_str = backup_file.stem.replace("GLOBAL_MEMORY_", "")
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if ts < cutoff:
                backup_file.unlink()
                deleted.append(backup_file)
        except ValueError:
            pass  # skip files with unexpected names

    return deleted


def list_backups(backup_dir: Path) -> list[Path]:
    """Return all backups sorted newest-first."""
    backups = list(backup_dir.glob("GLOBAL_MEMORY_*.md"))
    return sorted(backups, reverse=True)


def latest_backup(backup_dir: Path) -> Path | None:
    """Return the most recent backup, or None if no backups exist."""
    backups = list_backups(backup_dir)
    return backups[0] if backups else None
