"""
Tests for memsync.daemon.scheduler

All jobs are tested in isolation by calling them directly with mocked filesystem
and mocked API. No real APScheduler scheduling occurs in these tests.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from memsync.config import Config, DaemonConfig
from memsync.daemon.scheduler import (
    build_scheduler,
    job_backup_mirror,
    job_drift_check,
    job_nightly_refresh,
    job_weekly_digest,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def daemon_config(tmp_path: Path) -> Config:
    """Config with daemon enabled, sync_root pointing to tmp_path."""
    sync_root = tmp_path / "sync"
    memory_root = sync_root / ".claude-memory"
    (memory_root / "sessions").mkdir(parents=True)
    (memory_root / "backups").mkdir(parents=True)
    (memory_root / "GLOBAL_MEMORY.md").write_text(
        "# Global Memory\n\n## Identity\n- Test user\n\n## Hard constraints\n- Always test\n",
        encoding="utf-8",
    )
    claude_md = tmp_path / "claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# Global Memory\n", encoding="utf-8")

    return Config(
        provider="custom",
        sync_root=sync_root,
        claude_md_target=claude_md,
        daemon=DaemonConfig(
            enabled=True,
            refresh_enabled=True,
            backup_mirror_path="",
            drift_check_enabled=True,
            digest_enabled=False,
        ),
    )


# ---------------------------------------------------------------------------
# build_scheduler
# ---------------------------------------------------------------------------

class TestBuildScheduler:
    def test_returns_background_scheduler_by_default(self, daemon_config: Config) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = build_scheduler(daemon_config, blocking=False)
        assert isinstance(scheduler, BackgroundScheduler)

    def test_returns_blocking_scheduler_when_requested(self, daemon_config: Config) -> None:
        from apscheduler.schedulers.blocking import BlockingScheduler

        scheduler = build_scheduler(daemon_config, blocking=True)
        assert isinstance(scheduler, BlockingScheduler)

    def test_refresh_job_added_when_enabled(self, daemon_config: Config) -> None:
        scheduler = build_scheduler(daemon_config)
        job_ids = [j.id for j in scheduler.get_jobs()]
        assert "nightly_refresh" in job_ids

    def test_refresh_job_not_added_when_disabled(self, daemon_config: Config) -> None:
        import dataclasses

        cfg = dataclasses.replace(
            daemon_config,
            daemon=dataclasses.replace(daemon_config.daemon, refresh_enabled=False),
        )
        scheduler = build_scheduler(cfg)
        job_ids = [j.id for j in scheduler.get_jobs()]
        assert "nightly_refresh" not in job_ids

    def test_backup_mirror_job_added_when_path_set(
        self, daemon_config: Config, tmp_path: Path
    ) -> None:
        import dataclasses

        mirror = tmp_path / "mirror"
        cfg = dataclasses.replace(
            daemon_config,
            daemon=dataclasses.replace(daemon_config.daemon, backup_mirror_path=str(mirror)),
        )
        scheduler = build_scheduler(cfg)
        job_ids = [j.id for j in scheduler.get_jobs()]
        assert "backup_mirror" in job_ids

    def test_backup_mirror_job_not_added_when_path_empty(self, daemon_config: Config) -> None:
        scheduler = build_scheduler(daemon_config)
        job_ids = [j.id for j in scheduler.get_jobs()]
        assert "backup_mirror" not in job_ids

    def test_drift_check_job_added_when_enabled(self, daemon_config: Config) -> None:
        scheduler = build_scheduler(daemon_config)
        job_ids = [j.id for j in scheduler.get_jobs()]
        assert "drift_check" in job_ids

    def test_digest_job_not_added_when_disabled(self, daemon_config: Config) -> None:
        scheduler = build_scheduler(daemon_config)
        job_ids = [j.id for j in scheduler.get_jobs()]
        assert "weekly_digest" not in job_ids

    def test_digest_job_added_when_enabled(self, daemon_config: Config) -> None:
        import dataclasses

        cfg = dataclasses.replace(
            daemon_config,
            daemon=dataclasses.replace(daemon_config.daemon, digest_enabled=True),
        )
        scheduler = build_scheduler(cfg)
        job_ids = [j.id for j in scheduler.get_jobs()]
        assert "weekly_digest" in job_ids


# ---------------------------------------------------------------------------
# job_nightly_refresh
# ---------------------------------------------------------------------------

class TestJobNightlyRefresh:
    def test_skips_when_no_session_log(self, daemon_config: Config) -> None:
        """No session log for today → early return, no API call."""
        with patch("memsync.sync.refresh_memory_content") as mock_refresh:
            job_nightly_refresh(daemon_config)
            mock_refresh.assert_not_called()

    def test_skips_when_session_log_empty(self, daemon_config: Config) -> None:
        memory_root = daemon_config.sync_root / ".claude-memory"
        today = date.today().strftime("%Y-%m-%d")
        (memory_root / "sessions" / f"{today}.md").write_text("   \n", encoding="utf-8")

        with patch("memsync.sync.refresh_memory_content") as mock_refresh:
            job_nightly_refresh(daemon_config)
            mock_refresh.assert_not_called()

    def test_calls_api_when_notes_exist(self, daemon_config: Config) -> None:
        memory_root = daemon_config.sync_root / ".claude-memory"
        today = date.today().strftime("%Y-%m-%d")
        (memory_root / "sessions" / f"{today}.md").write_text(
            "Today I worked on testing.", encoding="utf-8"
        )

        mock_result = {"changed": False, "updated_content": "# Global Memory\n", "truncated": False}

        with patch("memsync.sync.refresh_memory_content", return_value=mock_result) as mock_refresh:
            job_nightly_refresh(daemon_config)
            mock_refresh.assert_called_once()

    def test_writes_updated_memory_when_changed(self, daemon_config: Config) -> None:
        memory_root = daemon_config.sync_root / ".claude-memory"
        today = date.today().strftime("%Y-%m-%d")
        (memory_root / "sessions" / f"{today}.md").write_text(
            "Worked on something new.", encoding="utf-8"
        )

        new_content = "# Global Memory\n\n## Identity\n- Updated user\n"
        mock_result = {"changed": True, "updated_content": new_content, "truncated": False}

        with patch("memsync.sync.refresh_memory_content", return_value=mock_result):
            with patch("memsync.claude_md.sync"):
                job_nightly_refresh(daemon_config)

        written = (memory_root / "GLOBAL_MEMORY.md").read_text(encoding="utf-8")
        assert written == new_content

    def test_does_not_raise_on_exception(self, daemon_config: Config) -> None:
        """Job must never propagate exceptions — daemon would crash."""
        memory_root = daemon_config.sync_root / ".claude-memory"
        today = date.today().strftime("%Y-%m-%d")
        (memory_root / "sessions" / f"{today}.md").write_text("notes", encoding="utf-8")

        with patch(
            "memsync.sync.refresh_memory_content",
            side_effect=RuntimeError("boom"),
        ):
            job_nightly_refresh(daemon_config)  # must not raise

    def test_skips_when_sync_root_missing(self, tmp_path: Path) -> None:
        """No sync root → early return, no crash."""
        config = Config(provider="custom", sync_root=None)
        job_nightly_refresh(config)  # must not raise


# ---------------------------------------------------------------------------
# job_backup_mirror
# ---------------------------------------------------------------------------

class TestJobBackupMirror:
    def test_copies_files_to_mirror(self, daemon_config: Config, tmp_path: Path) -> None:
        import dataclasses

        mirror = tmp_path / "mirror"
        cfg = dataclasses.replace(
            daemon_config,
            daemon=dataclasses.replace(daemon_config.daemon, backup_mirror_path=str(mirror)),
        )
        job_backup_mirror(cfg)

        assert (mirror / "GLOBAL_MEMORY.md").exists()

    def test_creates_mirror_directory(self, daemon_config: Config, tmp_path: Path) -> None:
        import dataclasses

        mirror = tmp_path / "deep" / "mirror"
        cfg = dataclasses.replace(
            daemon_config,
            daemon=dataclasses.replace(daemon_config.daemon, backup_mirror_path=str(mirror)),
        )
        assert not mirror.exists()
        job_backup_mirror(cfg)
        assert mirror.exists()

    def test_does_not_raise_on_exception(self, daemon_config: Config) -> None:
        import dataclasses

        cfg = dataclasses.replace(
            daemon_config,
            daemon=dataclasses.replace(
                daemon_config.daemon, backup_mirror_path="/nonexistent/\x00bad"
            ),
        )
        # Should log the error, not raise
        job_backup_mirror(cfg)  # must not raise


# ---------------------------------------------------------------------------
# job_drift_check
# ---------------------------------------------------------------------------

class TestJobDriftCheck:
    def test_sends_notification_when_out_of_sync(self, daemon_config: Config) -> None:
        with patch("memsync.claude_md.is_synced", return_value=False):
            with patch("memsync.daemon.notify.notify") as mock_notify:
                job_drift_check(daemon_config)
                mock_notify.assert_called_once()
                call_kwargs = mock_notify.call_args
                assert "out of sync" in call_kwargs[1]["subject"].lower()

    def test_no_notification_when_in_sync(self, daemon_config: Config) -> None:
        with patch("memsync.claude_md.is_synced", return_value=True):
            with patch("memsync.daemon.notify.notify") as mock_notify:
                job_drift_check(daemon_config)
                mock_notify.assert_not_called()

    def test_skips_when_memory_missing(self, daemon_config: Config) -> None:
        memory_path = daemon_config.sync_root / ".claude-memory" / "GLOBAL_MEMORY.md"
        memory_path.unlink()

        with patch("memsync.daemon.notify.notify") as mock_notify:
            job_drift_check(daemon_config)
            mock_notify.assert_not_called()

    def test_does_not_raise_on_exception(self, daemon_config: Config) -> None:
        with patch("memsync.claude_md.is_synced", side_effect=RuntimeError("boom")):
            job_drift_check(daemon_config)  # must not raise


# ---------------------------------------------------------------------------
# job_weekly_digest
# ---------------------------------------------------------------------------

class TestJobWeeklyDigest:
    def test_calls_generate_and_send(self, daemon_config: Config) -> None:
        with patch("memsync.daemon.digest.generate_and_send") as mock_send:
            import dataclasses

            cfg = dataclasses.replace(
                daemon_config,
                daemon=dataclasses.replace(daemon_config.daemon, digest_enabled=True),
            )
            job_weekly_digest(cfg)
            mock_send.assert_called_once_with(cfg)

    def test_does_not_raise_on_exception(self, daemon_config: Config) -> None:
        with patch(
            "memsync.daemon.digest.generate_and_send",
            side_effect=RuntimeError("smtp error"),
        ):
            job_weekly_digest(daemon_config)  # must not raise
