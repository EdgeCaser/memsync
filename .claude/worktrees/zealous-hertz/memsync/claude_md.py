from __future__ import annotations

import platform
import shutil
from pathlib import Path


def sync(memory_path: Path, target_path: Path) -> None:
    """
    Keep target_path (CLAUDE.md) in sync with memory_path (GLOBAL_MEMORY.md).

    Mac/Linux: create a symlink. If a non-memsync file already exists at the
    target, back it up first (.pre-memsync.bak) so user data is never lost.

    Windows: always copy — symlinks require admin/Developer Mode. The copy is
    refreshed on every `memsync refresh`, so drift is acceptable in practice.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Windows":
        shutil.copy2(memory_path, target_path)
        return

    # Mac / Linux — prefer symlink
    if target_path.is_symlink():
        if target_path.resolve() == memory_path.resolve():
            return  # already correct
        target_path.unlink()

    if target_path.exists():
        # Back up any existing file before replacing it
        target_path.rename(target_path.with_suffix(".pre-memsync.bak"))

    try:
        target_path.symlink_to(memory_path)
    except OSError:
        # Fallback to copy if symlink creation fails (e.g. cross-device)
        shutil.copy2(memory_path, target_path)


def is_synced(memory_path: Path, target_path: Path) -> bool:
    """
    Return True if target_path points at (or has the same content as) memory_path.
    """
    if not target_path.exists():
        return False

    if target_path.is_symlink():
        return target_path.resolve() == memory_path.resolve()

    # Windows copy path — compare content
    try:
        return target_path.read_bytes() == memory_path.read_bytes()
    except OSError:
        return False
