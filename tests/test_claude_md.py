from __future__ import annotations

import platform

import pytest

from memsync.claude_md import (
    BLOCK_BEGIN,
    BLOCK_END,
    is_block_synced,
    is_synced,
    sync,
    sync_block,
    sync_targets,
    target_is_synced,
)


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


def test_sync_preserves_target_that_is_already_a_correct_symlink(tmp_path, monkeypatch):
    """Regression: when the target is already a symlink to the memory file, the
    old always-copy Windows path raised SameFileError. sync() must not raise and
    must leave the link correct. Skips where the host forbids symlink creation."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    memory = tmp_path / "GLOBAL_MEMORY.md"
    memory.write_text("hello", encoding="utf-8")
    target = tmp_path / "CLAUDE.md"
    try:
        target.symlink_to(memory)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")

    sync(memory, target)  # pre-patch code raised SameFileError here

    assert target.is_symlink()
    assert target.resolve() == memory.resolve()
    assert target.read_text(encoding="utf-8") == "hello"
    assert is_synced(memory, target)


# ---------------------------------------------------------------------------
# Marked-block sync for shared targets (AGENTS.md)
# ---------------------------------------------------------------------------

OTHER_TOOL_BLOCK = (
    "<!-- BEGIN widget-skill (managed by widget-installer) -->\n"
    "## Widget Factory voice\n"
    "- Say gizmo, never doohickey.\n"
    "<!-- END widget-skill -->\n"
)


@pytest.fixture
def shared_target(tmp_path):
    memory = tmp_path / "core.md"
    memory.write_text("## Hard constraints\n*   Widgets ship on Fridays.\n", encoding="utf-8")
    target = tmp_path / ".codex" / "AGENTS.md"
    return memory, target


def _blocks(text: str) -> int:
    return text.count(BLOCK_BEGIN)


class TestSyncBlock:
    def test_creates_file_with_block(self, shared_target):
        memory, target = shared_target
        sync_block(memory, target)

        text = target.read_text(encoding="utf-8")
        assert text.startswith(BLOCK_BEGIN)
        assert "Widgets ship on Fridays." in text
        assert text.rstrip().endswith(BLOCK_END)
        assert is_block_synced(memory, target)

    def test_preserves_other_tool_content_and_backs_it_up(self, shared_target):
        memory, target = shared_target
        target.parent.mkdir(parents=True)
        target.write_text(OTHER_TOOL_BLOCK, encoding="utf-8")

        sync_block(memory, target)

        text = target.read_text(encoding="utf-8")
        assert text.startswith(OTHER_TOOL_BLOCK)
        assert _blocks(text) == 1
        backup = target.with_suffix(".pre-memsync.bak")
        assert backup.read_text(encoding="utf-8") == OTHER_TOOL_BLOCK

    def test_replaces_block_in_place_on_resync(self, shared_target):
        memory, target = shared_target
        target.parent.mkdir(parents=True)
        target.write_text(OTHER_TOOL_BLOCK, encoding="utf-8")
        sync_block(memory, target)
        with open(target, "a", encoding="utf-8") as f:
            f.write("\n## Added by hand after the block\n")

        memory.write_text("## Hard constraints\n*   Widgets ship on Mondays.\n", encoding="utf-8")
        sync_block(memory, target)

        text = target.read_text(encoding="utf-8")
        assert _blocks(text) == 1
        assert "Mondays" in text and "Fridays" not in text
        assert text.startswith(OTHER_TOOL_BLOCK)
        assert text.rstrip().endswith("## Added by hand after the block")
        assert is_block_synced(memory, target)

    def test_resync_is_a_no_op_when_unchanged(self, shared_target):
        memory, target = shared_target
        sync_block(memory, target)
        first = target.read_bytes()
        sync_block(memory, target)
        assert target.read_bytes() == first

    def test_keeps_crlf_in_content_outside_the_block(self, shared_target):
        memory, target = shared_target
        target.parent.mkdir(parents=True)
        target.write_bytes(OTHER_TOOL_BLOCK.replace("\n", "\r\n").encode("utf-8"))

        sync_block(memory, target)
        sync_block(memory, target)

        raw = target.read_bytes()
        assert raw.startswith(OTHER_TOOL_BLOCK.replace("\n", "\r\n").encode("utf-8"))

    def test_backslashes_in_memory_survive(self, shared_target):
        memory, target = shared_target
        memory.write_text(r"Path is C:\Users\widget\1" + "\n", encoding="utf-8")
        sync_block(memory, target)
        sync_block(memory, target)  # second pass goes through the re.sub path
        assert r"C:\Users\widget\1" in target.read_text(encoding="utf-8")

    def test_replaces_legacy_whole_file_copy(self, shared_target):
        memory, target = shared_target
        target.parent.mkdir(parents=True)
        target.write_text(
            "<!-- Generated by memsync. Do not edit -->\n## Old copy\n", encoding="utf-8"
        )

        sync_block(memory, target)

        text = target.read_text(encoding="utf-8")
        assert "Old copy" not in text
        assert text.startswith(BLOCK_BEGIN)
        assert not target.with_suffix(".pre-memsync.bak").exists()

    def test_not_synced_without_block(self, shared_target):
        memory, target = shared_target
        target.parent.mkdir(parents=True)
        target.write_bytes(memory.read_bytes())
        assert not is_block_synced(memory, target)

    def test_not_synced_when_block_is_stale(self, shared_target):
        memory, target = shared_target
        sync_block(memory, target)
        memory.write_text("## Hard constraints\n*   Changed.\n", encoding="utf-8")
        assert not is_block_synced(memory, target)

    @pytest.mark.skipif(platform.system() == "Windows", reason="symlinks need admin on Windows")
    def test_replaces_symlink_to_memory_with_real_file(self, shared_target):
        memory, target = shared_target
        target.parent.mkdir(parents=True)
        target.symlink_to(memory)

        sync_block(memory, target)

        assert not target.is_symlink()
        assert memory.read_text(encoding="utf-8").startswith("## Hard constraints")
        assert is_block_synced(memory, target)


class TestSyncTargets:
    def test_agents_md_gets_block_and_claude_md_gets_copy(
        self, shared_target, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        memory, agents = shared_target
        claude = tmp_path / ".claude" / "CLAUDE.md"
        agents.parent.mkdir(parents=True)
        agents.write_text(OTHER_TOOL_BLOCK, encoding="utf-8")

        sync_targets(memory, [("CLAUDE.md", claude), ("AGENTS.md", agents)])

        assert claude.read_bytes() == memory.read_bytes()
        assert OTHER_TOOL_BLOCK in agents.read_text(encoding="utf-8")
        assert target_is_synced(memory, "CLAUDE.md", claude)
        assert target_is_synced(memory, "AGENTS.md", agents)
