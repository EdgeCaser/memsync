"""Advisory cross-machine lock for the shared memory store.

memsync harvests run on several machines (e.g. Mac, Pi, PC) that all write into
one Syncthing-synced store. Two harvests writing at once produce Syncthing
sync-conflict copies of ``harvested.json`` / ``usage.jsonl``. This module
provides a best-effort advisory lock: a machine drops a ``.harvest.lock`` file
in the store before it starts and removes it when done, and a second machine
that sees a *live* lock defers instead of racing.

This is advisory, not a hard mutex. The lock file itself syncs with latency, so
two runs started within a sync cycle of each other can still both acquire it.
What it buys you: any two runs more than a sync-cycle apart no longer collide,
which — combined with the nightly schedule stagger — leaves only
near-simultaneous manual runs as a residual.

A lock older than ``stale_seconds`` (a crashed run that never released) is
treated as abandoned and stolen, so a dead machine can never wedge the store.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".harvest.lock"
# A live harvest never runs longer than ~30 min (harvest_max_runtime_seconds
# defaults to 1800). An hour is comfortably past that, so any lock older than
# this is a crashed run's leftover and safe to steal.
DEFAULT_STALE_SECONDS = 3600


class LockHeld(Exception):
    """Raised when the store is locked by another *live* harvest run."""

    def __init__(self, host: str, pid: int, age_seconds: float | None) -> None:
        self.host = host
        self.pid = pid
        self.age_seconds = age_seconds
        when = f"{int(age_seconds)}s ago" if age_seconds is not None else "an unknown time"
        super().__init__(f"harvest lock held by {host} (pid {pid}), acquired {when}")


def _now() -> datetime:
    return datetime.now(UTC)


def _lock_path(memory_root: Path) -> Path:
    return memory_root / LOCK_FILENAME


def _read_lock(path: Path) -> dict | None:
    """Return the lock file's parsed contents, or None if absent/unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _lock_age_seconds(data: dict | None) -> float | None:
    """Age of a lock in seconds from its ``ts`` field, or None if unparseable."""
    if not data:
        return None
    ts = data.get("ts")
    if not ts:
        return None
    try:
        acquired = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=UTC)
    return (_now() - acquired).total_seconds()


def _create_exclusive(path: Path, info: dict) -> bool:
    """Atomically create the lock file. Returns False if it already exists."""
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, json.dumps(info).encode("utf-8"))
    finally:
        os.close(fd)
    return True


def acquire(memory_root: Path, stale_seconds: int = DEFAULT_STALE_SECONDS) -> dict:
    """Acquire the store lock, returning our lock-info dict on success.

    Raises :class:`LockHeld` if a live lock is held by another run. A lock older
    than ``stale_seconds`` — or one whose contents are unreadable — is treated as
    abandoned, stolen, and re-acquired.
    """
    path = _lock_path(memory_root)
    info = {
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "ts": _now().isoformat(),
    }
    # At most two passes: a plain create, then one retry after stealing a stale
    # lock. If someone re-takes it in the gap, we treat that as genuinely held.
    for _ in range(2):
        if _create_exclusive(path, info):
            return info
        existing = _read_lock(path)
        age = _lock_age_seconds(existing)
        if existing is not None and age is not None and age <= stale_seconds:
            raise LockHeld(existing.get("host", "?"), existing.get("pid", -1), age)
        logger.warning(
            "harvest lock at %s is stale or unreadable (host=%s, age=%s) — stealing",
            path,
            (existing or {}).get("host", "?"),
            f"{int(age)}s" if age is not None else "unknown",
        )
        try:
            path.unlink()
        except OSError:
            pass
    existing = _read_lock(path)
    raise LockHeld(
        (existing or {}).get("host", "?"),
        (existing or {}).get("pid", -1),
        _lock_age_seconds(existing),
    )


def release(memory_root: Path, info: dict) -> None:
    """Remove the lock file, but only if it is still the one we created.

    Guards against deleting a lock a *different* run acquired after ours expired
    or was stolen. Never raises — a failed release just leaves a lock that will
    later be treated as stale.
    """
    path = _lock_path(memory_root)
    existing = _read_lock(path)
    if existing is None:
        return
    if existing.get("host") == info.get("host") and existing.get("pid") == info.get("pid"):
        try:
            path.unlink()
        except OSError:
            pass


@contextmanager
def store_lock(memory_root: Path, stale_seconds: int = DEFAULT_STALE_SECONDS) -> Iterator[None]:
    """Hold the store lock for the duration of the ``with`` block.

    Raises :class:`LockHeld` if another live run holds it. Releases on exit,
    including when the body raises.
    """
    info = acquire(memory_root, stale_seconds)
    try:
        yield
    finally:
        release(memory_root, info)
