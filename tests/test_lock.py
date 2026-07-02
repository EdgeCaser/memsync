from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime, timedelta

import pytest

from memsync.lock import (
    LOCK_FILENAME,
    LockHeld,
    acquire,
    release,
    store_lock,
)


@pytest.fixture
def store(tmp_path):
    """A memory-root directory standing in for the shared store."""
    return tmp_path


def _lock_file(store):
    return store / LOCK_FILENAME


def _write_raw_lock(store, *, host="other-host", pid=999, ts=None):
    """Drop a lock file as if another machine created it."""
    if ts is None:
        ts = datetime.now(UTC).isoformat()
    _lock_file(store).write_text(
        json.dumps({"host": host, "pid": pid, "ts": ts}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# acquire / release
# ---------------------------------------------------------------------------

def test_acquire_creates_lock_file_with_identity(store):
    info = acquire(store)
    assert _lock_file(store).exists()
    on_disk = json.loads(_lock_file(store).read_text(encoding="utf-8"))
    assert on_disk["host"] == socket.gethostname()
    assert on_disk["pid"] == os.getpid()
    assert on_disk["ts"] == info["ts"]


def test_acquire_raises_when_held_by_live_run(store):
    _write_raw_lock(store, host="mac", pid=123)  # fresh -> live
    with pytest.raises(LockHeld) as exc:
        acquire(store)
    assert exc.value.host == "mac"
    assert exc.value.pid == 123


def test_release_removes_our_lock(store):
    info = acquire(store)
    release(store, info)
    assert not _lock_file(store).exists()


def test_release_leaves_foreign_lock_untouched(store):
    _write_raw_lock(store, host="pi", pid=555)
    # Pretend we think we own it but with different identity — must not delete.
    release(store, {"host": socket.gethostname(), "pid": os.getpid()})
    assert _lock_file(store).exists()
    on_disk = json.loads(_lock_file(store).read_text(encoding="utf-8"))
    assert on_disk["host"] == "pi"


def test_release_is_noop_when_no_lock(store):
    release(store, {"host": "x", "pid": 1})  # must not raise
    assert not _lock_file(store).exists()


# ---------------------------------------------------------------------------
# staleness / stealing
# ---------------------------------------------------------------------------

def test_stale_lock_is_stolen(store):
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    _write_raw_lock(store, host="crashed-host", pid=42, ts=old)
    info = acquire(store, stale_seconds=3600)
    # We now own it.
    on_disk = json.loads(_lock_file(store).read_text(encoding="utf-8"))
    assert on_disk["host"] == socket.gethostname()
    assert on_disk["pid"] == info["pid"]


def test_unreadable_lock_is_stolen(store):
    _lock_file(store).write_text("}{ not json", encoding="utf-8")
    info = acquire(store)
    on_disk = json.loads(_lock_file(store).read_text(encoding="utf-8"))
    assert on_disk["pid"] == info["pid"]


def test_lock_just_under_stale_threshold_is_respected(store):
    recent = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    _write_raw_lock(store, host="busy", pid=7, ts=recent)
    with pytest.raises(LockHeld):
        acquire(store, stale_seconds=3600)


# ---------------------------------------------------------------------------
# store_lock context manager
# ---------------------------------------------------------------------------

def test_store_lock_acquires_and_releases(store):
    with store_lock(store):
        assert _lock_file(store).exists()
    assert not _lock_file(store).exists()


def test_store_lock_releases_on_exception(store):
    with pytest.raises(RuntimeError):
        with store_lock(store):
            assert _lock_file(store).exists()
            raise RuntimeError("boom")
    assert not _lock_file(store).exists()


def test_store_lock_raises_when_held(store):
    _write_raw_lock(store, host="mac", pid=321)
    with pytest.raises(LockHeld):
        with store_lock(store):
            pytest.fail("body should not run when lock is held")


def test_store_lock_serial_reuse(store):
    with store_lock(store):
        pass
    # Lock released -> a second run can take it.
    with store_lock(store):
        assert _lock_file(store).exists()
    assert not _lock_file(store).exists()
