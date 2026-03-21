from __future__ import annotations

import platform

import pytest

from memsync.claude_md import is_synced, sync


@pytest.fixture
def memory_and_target(tmp_path):
    memory = tmp_path / "sync" / ".claude-memory" / "GLOBAL_MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("# Global Memory\n- test content", encoding="utf-8")
    target = tmp_path / ".claude" / "CLAUDE.md"
    return memory, target


class TestSyncWindows:
    def test_creates_copy_on_windows(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Windows")

        sync(memory, target)

        assert target.exists()
        assert not target.is_symlink()
        assert target.read_bytes() == memory.read_bytes()

    def test_copy_is_idempotent(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Windows")

        sync(memory, target)
        sync(memory, target)  # should not raise

        assert target.read_bytes() == memory.read_bytes()

    def test_creates_parent_dirs(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        # Target parent doesn't exist yet
        assert not target.parent.exists()

        sync(memory, target)
        assert target.parent.exists()


class TestSyncUnix:
    @pytest.mark.skipif(platform.system() == "Windows", reason="symlinks require Unix")
    def test_creates_symlink(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        sync(memory, target)

        assert target.is_symlink()
        assert target.resolve() == memory.resolve()

    @pytest.mark.skipif(platform.system() == "Windows", reason="symlinks require Unix")
    def test_symlink_is_idempotent(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        sync(memory, target)
        sync(memory, target)  # already correct — should not raise

        assert target.is_symlink()

    @pytest.mark.skipif(platform.system() == "Windows", reason="symlinks require Unix")
    def test_backs_up_existing_file_before_linking(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old content", encoding="utf-8")

        sync(memory, target)

        bak = target.with_suffix(".pre-memsync.bak")
        assert bak.exists()
        assert bak.read_text(encoding="utf-8") == "old content"
        assert target.is_symlink()


class TestIsSynced:
    def test_false_when_target_missing(self, memory_and_target):
        memory, target = memory_and_target
        assert is_synced(memory, target) is False

    def test_true_after_sync_on_windows(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        sync(memory, target)
        assert is_synced(memory, target) is True

    def test_false_when_content_differs(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        sync(memory, target)
        memory.write_text("updated content", encoding="utf-8")
        assert is_synced(memory, target) is False

    @pytest.mark.skipif(platform.system() == "Windows", reason="symlinks require Unix")
    def test_true_after_symlink(self, memory_and_target, monkeypatch):
        memory, target = memory_and_target
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        sync(memory, target)
        assert is_synced(memory, target) is True
