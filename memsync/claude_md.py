from __future__ import annotations

import platform
import shutil
from collections.abc import Iterable
from pathlib import Path


def sync(memory_path: Path, target_path: Path) -> None:
    """
    Keep target_path (CLAUDE.md) in sync with memory_path (GLOBAL_MEMORY.md).

    If target_path is already a symlink pointing at memory_path, it is left
    untouched. This supports setups where CLAUDE.md is intentionally symlinked
    to the synced memory file, and avoids a SameFileError on Windows where the
    copy path would otherwise copy a file onto itself.

    Mac/Linux: create a symlink. If a non-memsync file already exists at the
    target, back it up first (.pre-memsync.bak) so user data is never lost.

    Windows: copy. Symlinks require admin/Developer Mode; the copy is refreshed
    on every `memsync refresh`, so drift is acceptable in practice.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Already a correct symlink to the memory file: leave it as-is. Avoids the
    # SameFileError the Windows copy path would raise, and preserves an
    # intentionally symlinked CLAUDE.md so live updates flow through.
    if target_path.is_symlink() and target_path.resolve() == memory_path.resolve():
        return

    if platform.system() == "Windows":
        # A symlink pointing somewhere *else* must be cleared first. copy2 opens
        # the target for writing, which follows the link and overwrites whatever
        # it points at — so copying the projected core onto a CLAUDE.md still
        # symlinked to GLOBAL_MEMORY.md would destroy the canonical store.
        if target_path.is_symlink():
            target_path.unlink()
        shutil.copy2(memory_path, target_path)
        return

    # Mac / Linux: prefer a symlink.
    if target_path.is_symlink():
        target_path.unlink()
    elif target_path.exists():
        # Back up any existing real file before replacing it.
        target_path.rename(target_path.with_suffix(".pre-memsync.bak"))

    try:
        target_path.symlink_to(memory_path)
    except OSError:
        # Fallback to copy if symlink creation fails (e.g. cross-device).
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
