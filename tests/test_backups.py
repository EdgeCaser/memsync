from __future__ import annotations

import time
from pathlib import Path

import pytest

from memsync.backups import backup, latest_backup, list_backups, prune


@pytest.fixture
def backup_env(tmp_path):
    """A source file and backup directory in tmp_path."""
    source = tmp_path / "GLOBAL_MEMORY.md"
    source.write_text("# memory content", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    return source, backup_dir


class TestBackup:
    def test_creates_file_with_timestamp_name(self, backup_env):
        source, backup_dir = backup_env
        result = backup(source, backup_dir)
        assert result.exists()
        assert result.name.startswith("GLOBAL_MEMORY_")
        assert result.suffix == ".md"

    def test_backup_content_matches_source(self, backup_env):
        source, backup_dir = backup_env
        result = backup(source, backup_dir)
        assert result.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    def test_successive_backups_have_unique_names(self, backup_env):
        source, backup_dir = backup_env
        b1 = backup(source, backup_dir)
        time.sleep(1)
        b2 = backup(source, backup_dir)
        assert b1.name != b2.name


class TestListBackups:
    def test_returns_newest_first(self, backup_env):
        source, backup_dir = backup_env
        b1 = backup(source, backup_dir)
        time.sleep(1)
        b2 = backup(source, backup_dir)
        listed = list_backups(backup_dir)
        assert listed[0] == b2
        assert listed[1] == b1

    def test_empty_dir_returns_empty_list(self, tmp_path):
        d = tmp_path / "backups"
        d.mkdir()
        assert list_backups(d) == []


class TestLatestBackup:
    def test_returns_most_recent(self, backup_env):
        source, backup_dir = backup_env
        backup(source, backup_dir)
        time.sleep(1)
        b2 = backup(source, backup_dir)
        assert latest_backup(backup_dir) == b2

    def test_returns_none_when_no_backups(self, tmp_path):
        d = tmp_path / "backups"
        d.mkdir()
        assert latest_backup(d) is None


class TestPrune:
    def test_removes_old_backups(self, backup_env):
        source, backup_dir = backup_env
        b = backup(source, backup_dir)
        deleted = prune(backup_dir, keep_days=0)
        assert b in deleted
        assert not b.exists()

    def test_keeps_recent_backups(self, backup_env):
        source, backup_dir = backup_env
        b = backup(source, backup_dir)
        deleted = prune(backup_dir, keep_days=30)
        assert b not in deleted
        assert b.exists()

    def test_skips_files_with_unexpected_names(self, backup_env):
        _, backup_dir = backup_env
        stray = backup_dir / "not-a-backup.md"
        stray.write_text("stray", encoding="utf-8")
        # Should not raise, should not delete stray file
        prune(backup_dir, keep_days=0)
        assert stray.exists()

    def test_returns_list_of_deleted_paths(self, backup_env):
        source, backup_dir = backup_env
        backup(source, backup_dir)
        time.sleep(1)
        backup(source, backup_dir)
        deleted = prune(backup_dir, keep_days=0)
        assert len(deleted) == 2
        assert all(isinstance(p, Path) for p in deleted)
