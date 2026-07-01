from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path


def sync(memory_path: Path, target_path: Path) -> None:
    """
    Keep target_path (CLAUDE.md) in sync with memory_path (GLOBAL_MEMORY.md).

    All platforms: prefer a symlink. If a non-memsync file already exists at
    the target, back it up first (.pre-memsync.bak) so user data is never lost.

    Windows: symlinks require admin/Developer Mode. If symlink creation fails
    (OSError), fall back to a copy, refreshed on every `memsync refresh`, so
    drift is acceptable in practice.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Prefer a symlink on every platform. On Windows this needs admin or
    # Developer Mode; if unavailable, symlink_to raises OSError and we copy.
    if target_path.is_symlink():
        if target_path.resolve() == memory_path.resolve():
            return  # already correct, no work needed, and avoids SameFileError
        target_path.unlink()
    elif target_path.exists():
        # Back up any existing real file before replacing it.
        target_path.rename(target_path.with_suffix(".pre-memsync.bak"))

    try:
        target_path.symlink_to(memory_path)
    except OSError:
        shutil.copy2(memory_path, target_path)


def is_synced(memory_path: Path, target_path: Path) -> bool:
    """
    Return True if target_path points at (or has the same content as) memory_path.
    """
    if not target_path.exists():
        return False

    if target_path.is_symlink():
        return target_path.resolve() == memory_path.resolve()

    # Windows copy path: compare content
    try:
        return target_path.read_bytes() == memory_path.read_bytes()
    except OSError:
        return False


def sync_many(memory_path: Path, target_paths: Iterable[Path]) -> None:
    """Sync one memory file into multiple instruction targets."""
    seen: set[str] = set()
    for target_path in target_paths:
        expanded = Path(target_path).expanduser()
        key = str(expanded).lower()
        if key in seen:
            continue
        seen.add(key)
        sync(memory_path, expanded)
