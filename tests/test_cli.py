from __future__ import annotations

import dataclasses
import platform
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memsync.llm import LLMError

from memsync.cli import (
    _harvest_all,
    build_parser,
    cmd_config_set,
    cmd_config_show,
    cmd_diff,
    cmd_doctor,
    cmd_harvest,
    cmd_init,
    cmd_providers,
    cmd_prune,
    cmd_refresh,
    cmd_show,
    cmd_status,
    cmd_usage,
)
from memsync.config import Config

SAMPLE_MEMORY = """\
<!-- memsync v0.2 -->
# Global Memory

## Identity & context
- Test user

## Hard constraints
- Always backup before writing
"""


def _args(**kwargs):
    """Build a minimal args namespace."""
    defaults = {
        "notes": None, "file": None, "dry_run": False, "model": None,
        "backup": None, "keep_days": None,
    }
    defaults.update(kwargs)

    class Namespace:
        pass

    ns = Namespace()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


class TestCmdShow:
    def test_prints_memory_content(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        result = cmd_show(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "Global Memory" in out

    def test_returns_3_when_no_memory_file(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_show(_args(), config)
        assert result == 3

    def test_returns_2_when_memory_root_missing(self, tmp_path, capsys):
        config = Config(provider="custom", sync_root=tmp_path / "sync")
        result = cmd_show(_args(), config)
        assert result == 2


class TestCmdStatus:
    def test_shows_platform_info(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        result = cmd_status(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "Platform:" in out
        assert "LLM backend:" in out

    def test_shows_memory_path(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        cmd_status(_args(), config)
        out = capsys.readouterr().out
        assert str(global_memory) in out


class TestCmdPrune:
    def test_prunes_old_backups(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        backup_dir = config.sync_root / ".claude-memory" / "backups"

        # Create a backup manually by copying
        from memsync.backups import backup
        backup(global_memory, backup_dir)

        result = cmd_prune(_args(keep_days=0), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "Pruned" in out

    def test_reports_nothing_to_prune(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        result = cmd_prune(_args(keep_days=30), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "No backups" in out

    def test_dry_run_does_not_delete(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        backup_dir = config.sync_root / ".claude-memory" / "backups"

        from memsync.backups import backup
        b = backup(global_memory, backup_dir)

        result = cmd_prune(_args(keep_days=0, dry_run=True), config)
        assert result == 0
        assert b.exists()  # not deleted


class TestCmdProviders:
    def test_lists_all_providers(self, tmp_config, capsys):
        config, _ = tmp_config
        result = cmd_providers(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "onedrive" in out
        assert "icloud" in out
        assert "gdrive" in out
        assert "custom" in out

    def test_shows_active_provider(self, tmp_config, capsys):
        config, _ = tmp_config
        cmd_providers(_args(), config)
        out = capsys.readouterr().out
        assert "Active provider:" in out


class TestCmdRefresh:
    def _mock_refresh_result(self, changed=True, truncated=False, content=SAMPLE_MEMORY):
        return {"updated_content": content, "changed": changed, "truncated": truncated}

    def test_returns_1_on_empty_notes(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        result = cmd_refresh(_args(notes="   "), config)
        assert result == 1

    def test_dry_run_does_not_write(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        original = global_memory.read_text(encoding="utf-8")

        mock_result = self._mock_refresh_result(changed=True)
        with patch("memsync.cli.refresh_memory_content", return_value=mock_result):
            result = cmd_refresh(_args(notes="some notes", dry_run=True), config)

        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == original  # unchanged

    def test_no_change_prints_message(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        mock_result = self._mock_refresh_result(changed=False)

        with patch("memsync.cli.refresh_memory_content", return_value=mock_result):
            result = cmd_refresh(_args(notes="some notes"), config)

        out = capsys.readouterr().out
        assert result == 0
        assert "no changes" in out.lower()

    def test_truncation_returns_5(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        mock_result = self._mock_refresh_result(changed=True, truncated=True)

        with patch("memsync.cli.refresh_memory_content", return_value=mock_result):
            result = cmd_refresh(_args(notes="some notes"), config)

        assert result == 5

    def test_successful_refresh_writes_backup_and_memory(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        updated = SAMPLE_MEMORY + "\n- new item added"
        mock_result = self._mock_refresh_result(changed=True, content=updated)

        with patch("memsync.cli.refresh_memory_content", return_value=mock_result):
            result = cmd_refresh(_args(notes="some notes"), config)

        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == updated

        backup_dir = config.sync_root / ".claude-memory" / "backups"
        from memsync.backups import list_backups
        assert len(list_backups(backup_dir)) == 1

    def test_model_override_passed_to_refresh(self, memory_file):
        config, tmp_path, _ = memory_file
        mock_result = self._mock_refresh_result(changed=False)

        with patch("memsync.cli.refresh_memory_content", return_value=mock_result) as mock_fn:
            cmd_refresh(_args(notes="notes", model="claude-haiku-4-5-20251001"), config)

        called_config = mock_fn.call_args.args[2]
        assert called_config.model == "claude-haiku-4-5-20251001"


@pytest.mark.smoke
class TestParser:
    def test_refresh_requires_notes_or_file(self):
        parser = build_parser()
        args = parser.parse_args(["refresh", "--notes", "hello"])
        assert args.notes == "hello"

    def test_prune_default_keep_days_is_none(self):
        parser = build_parser()
        args = parser.parse_args(["prune"])
        assert args.keep_days is None  # falls back to config.keep_days

    def test_config_set_parses_key_value(self):
        parser = build_parser()
        args = parser.parse_args(["config", "set", "model", "claude-opus-4-20250514"])
        assert args.key == "model"
        assert args.value == "claude-opus-4-20250514"

    def test_doctor_is_registered(self):
        parser = build_parser()
        args = parser.parse_args(["doctor"])
        assert args.func is cmd_doctor


# ---------------------------------------------------------------------------
# cmd_init
# ---------------------------------------------------------------------------

class TestCmdInit:
    def _init_args(self, **kwargs):
        defaults = {"force": False, "provider": None, "sync_root": None}
        defaults.update(kwargs)

        class Namespace:
            pass

        ns = Namespace()
        for k, v in defaults.items():
            setattr(ns, k, v)
        return ns

    def test_init_with_sync_root(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        sync_dir = tmp_path / "my-sync"
        sync_dir.mkdir()

        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "config.toml")
        monkeypatch.setattr("memsync.cli.sync_claude_md", lambda src, dst: None)

        result = cmd_init(self._init_args(sync_root=str(sync_dir)), config)
        assert result == 0

        memory = sync_dir / ".claude-memory" / "GLOBAL_MEMORY.md"
        assert memory.exists()
        assert "<!-- memsync v0.2 -->" in memory.read_text(encoding="utf-8")

    def test_init_with_sync_root_creates_dirs(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        sync_dir = tmp_path / "sync-root"
        sync_dir.mkdir()

        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "config.toml")
        monkeypatch.setattr("memsync.cli.sync_claude_md", lambda src, dst: None)
        cmd_init(self._init_args(sync_root=str(sync_dir)), config)

        assert (sync_dir / ".claude-memory" / "backups").exists()
        assert (sync_dir / ".claude-memory" / "sessions").exists()

    def test_init_with_explicit_provider(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        fake_root = tmp_path / "onedrive"
        fake_root.mkdir()

        from memsync.providers.onedrive import OneDriveProvider
        monkeypatch.setattr(OneDriveProvider, "detect", lambda self: fake_root)
        monkeypatch.setattr("memsync.cli.sync_claude_md", lambda src, dst: None)

        result = cmd_init(self._init_args(provider="onedrive"), config)
        assert result == 0

    def test_init_returns_4_when_provider_not_found(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_init(self._init_args(provider="onedrive"), config)
        # OneDrive not present in tmp_path → 4 (detection failed)
        # OR 0 if OneDrive is detected on this machine; just check it ran
        assert result in (0, 4)

    def test_init_sync_root_nonexistent_returns_1(self, tmp_config, monkeypatch, capsys):
        config, tmp_path = tmp_config
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "config.toml")
        result = cmd_init(self._init_args(sync_root="/nonexistent/path/xyz"), config)
        assert result == 1

    def test_init_already_initialized_without_force(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        config_path = tmp_path / "config.toml"
        config_path.write_text("[core]\nprovider = 'onedrive'\n", encoding="utf-8")
        monkeypatch.setattr("memsync.config.get_config_path", lambda: config_path)
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: config_path)

        result = cmd_init(self._init_args(), config)
        assert result == 0  # exits gracefully

    def test_init_force_overwrites_existing_memory(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        sync_dir = tmp_path / "sync-force"
        sync_dir.mkdir()
        memory_dir = sync_dir / ".claude-memory"
        memory_dir.mkdir()
        existing = memory_dir / "GLOBAL_MEMORY.md"
        existing.write_text("# Old content", encoding="utf-8")

        monkeypatch.setattr("memsync.cli.sync_claude_md", lambda src, dst: None)
        cmd_init(self._init_args(sync_root=str(sync_dir), force=True), config)

        new_content = existing.read_text(encoding="utf-8")
        assert "<!-- memsync v0.2 -->" in new_content

    def test_init_writes_config_file(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        sync_dir = tmp_path / "sync-cfg"
        sync_dir.mkdir()

        saved_configs = []

        def capture_save(self):
            saved_configs.append(self)
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "config.toml")
        monkeypatch.setattr(Config, "save", capture_save)
        monkeypatch.setattr("memsync.cli.sync_claude_md", lambda src, dst: None)

        cmd_init(self._init_args(sync_root=str(sync_dir)), config)
        assert len(saved_configs) == 1


# ---------------------------------------------------------------------------
# cmd_diff
# ---------------------------------------------------------------------------

class TestCmdDiff:
    def test_returns_3_when_no_memory_file(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_diff(_args(), config)
        assert result == 3

    def test_prints_no_backups_message(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        result = cmd_diff(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "No backups found" in out

    def test_shows_diff_against_latest_backup(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        backup_dir = config.sync_root / ".claude-memory" / "backups"

        # Create a backup of the original
        from memsync.backups import backup
        backup(global_memory, backup_dir)

        # Modify the current memory
        global_memory.write_text(
            global_memory.read_text(encoding="utf-8") + "\n- New item added",
            encoding="utf-8",
        )

        result = cmd_diff(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "New item added" in out

    def test_no_diff_when_identical(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        backup_dir = config.sync_root / ".claude-memory" / "backups"

        from memsync.backups import backup
        backup(global_memory, backup_dir)

        result = cmd_diff(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "No differences" in out

    def test_specific_backup_flag(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        backup_dir = config.sync_root / ".claude-memory" / "backups"

        from memsync.backups import backup
        b = backup(global_memory, backup_dir)

        result = cmd_diff(_args(backup=b.name), config)
        assert result == 0

    def test_nonexistent_backup_returns_1(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        result = cmd_diff(_args(backup="GLOBAL_MEMORY_19991231_235959.md"), config)
        assert result == 1


# ---------------------------------------------------------------------------
# cmd_config_show
# ---------------------------------------------------------------------------

class TestCmdConfigShow:
    def test_returns_2_when_no_config(self, tmp_config, monkeypatch, capsys):
        config, tmp_path = tmp_config
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "config.toml")
        result = cmd_config_show(_args(), config)
        assert result == 2

    def test_prints_config_contents(self, tmp_config, monkeypatch, capsys):
        config, tmp_path = tmp_config
        config_path = tmp_path / "config.toml"
        config_path.write_text("[core]\nprovider = \"onedrive\"\n", encoding="utf-8")
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: config_path)

        result = cmd_config_show(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "onedrive" in out


# ---------------------------------------------------------------------------
# cmd_config_set
# ---------------------------------------------------------------------------

class TestCmdConfigSet:
    def _set_args(self, key, value):
        class Namespace:
            pass
        ns = Namespace()
        ns.key = key
        ns.value = value
        return ns

    def test_set_model(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))

        result = cmd_config_set(self._set_args("model", "claude-opus-4-20250514"), config)
        assert result == 0
        assert saved[0].model == "claude-opus-4-20250514"

    def test_set_provider(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))

        result = cmd_config_set(self._set_args("provider", "icloud"), config)
        assert result == 0
        assert saved[0].provider == "icloud"

    def test_set_invalid_provider_returns_1(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_config_set(self._set_args("provider", "dropbox"), config)
        err = capsys.readouterr().err
        assert result == 1
        assert "dropbox" in err

    def test_set_keep_days(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))

        result = cmd_config_set(self._set_args("keep_days", "60"), config)
        assert result == 0
        assert saved[0].keep_days == 60

    def test_set_keep_days_non_integer_returns_1(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_config_set(self._set_args("keep_days", "thirty"), config)
        assert result == 1

    def test_set_max_memory_lines(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))

        result = cmd_config_set(self._set_args("max_memory_lines", "300"), config)
        assert result == 0
        assert saved[0].max_memory_lines == 300

    def test_set_sync_root(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        sync_dir = tmp_path / "new-sync"
        sync_dir.mkdir()
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))

        result = cmd_config_set(self._set_args("sync_root", str(sync_dir)), config)
        assert result == 0
        assert saved[0].sync_root == sync_dir
        assert saved[0].provider == "custom"  # auto-set when sync_root configured

    def test_set_sync_root_nonexistent_returns_1(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_config_set(self._set_args("sync_root", "/nonexistent/xyz"), config)
        assert result == 1

    def test_set_unknown_key_returns_1(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_config_set(self._set_args("unknown_key", "value"), config)
        err = capsys.readouterr().err
        assert result == 1
        assert "unknown_key" in err


# ---------------------------------------------------------------------------
# cmd_doctor
# ---------------------------------------------------------------------------

class TestCmdDoctor:
    def test_all_checks_pass_returns_0(self, memory_file, monkeypatch):
        config, tmp_path, global_memory = memory_file

        # Sync CLAUDE.md first
        from memsync.claude_md import sync as sync_claude_md
        config.claude_md_target.parent.mkdir(parents=True, exist_ok=True)
        sync_claude_md(global_memory, config.claude_md_target)

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-abc")
        monkeypatch.setattr("memsync.cli.get_config_path",
                            lambda: tmp_path / "config.toml")
        (tmp_path / "config.toml").write_text("[core]\n", encoding="utf-8")

        result = cmd_doctor(_args(), config)
        assert result == 0

    def test_missing_api_key_fails(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        result = cmd_doctor(_args(), config)
        out = capsys.readouterr().out
        assert result == 1
        assert "API key" in out

    def test_missing_memory_file_fails(self, tmp_config, monkeypatch, capsys):
        config, tmp_path = tmp_config
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        # Memory root exists but no GLOBAL_MEMORY.md

        result = cmd_doctor(_args(), config)
        capsys.readouterr()
        assert result == 1

    def test_output_includes_all_check_labels(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        cmd_doctor(_args(), config)
        out = capsys.readouterr().out
        assert "Config file" in out
        assert "API key" in out
        assert "Provider" in out


# ---------------------------------------------------------------------------
# Daemon CLI commands
# ---------------------------------------------------------------------------

class TestDaemonCLIGuard:
    """When daemon extras are not installed, all commands print a hint."""

    def test_guard_fails_gracefully_when_no_extras(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_start

        class FakeArgs:
            detach = False

        with patch("memsync.cli._daemon_import_guard", return_value=False):
            result = cmd_daemon_start(FakeArgs(), config)
        assert result == 1

    def test_stop_without_pid_file_returns_1(self, tmp_config, capsys, tmp_path, monkeypatch):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_stop

        class FakeArgs:
            pass

        monkeypatch.setattr("memsync.cli._PID_FILE", tmp_path / "nonexistent.pid")
        result = cmd_daemon_stop(FakeArgs(), config)
        assert result == 1

    def test_status_no_pid_file(self, tmp_config, capsys, tmp_path, monkeypatch):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_status

        class FakeArgs:
            pass

        monkeypatch.setattr("memsync.cli._PID_FILE", tmp_path / "nonexistent.pid")
        with patch("memsync.cli._daemon_import_guard", return_value=True):
            result = cmd_daemon_status(FakeArgs(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "not running" in out.lower()

    def test_schedule_shows_jobs(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_schedule

        class FakeArgs:
            pass

        fake_job = MagicMock()
        fake_job.name = "Nightly refresh"
        fake_job.id = "nightly_refresh"
        fake_job.next_run_time = None
        fake_scheduler = MagicMock()
        fake_scheduler.get_jobs.return_value = [fake_job]
        fake_module = MagicMock()
        fake_module.build_scheduler.return_value = fake_scheduler
        with patch.dict(sys.modules, {"memsync.daemon.scheduler": fake_module}):
            with patch("memsync.cli._daemon_import_guard", return_value=True):
                result = cmd_daemon_schedule(FakeArgs(), config)
        capsys.readouterr()
        assert result == 0

    def test_install_raises_not_implemented_on_windows(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_install

        class FakeArgs:
            pass

        with patch("memsync.daemon.service.install_service",
                   side_effect=NotImplementedError("Windows not supported")):
            result = cmd_daemon_install(FakeArgs(), config)
        assert result == 1

    def test_uninstall_raises_not_implemented_on_windows(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_uninstall

        class FakeArgs:
            pass

        with patch("memsync.daemon.service.uninstall_service",
                   side_effect=NotImplementedError("Windows not supported")):
            result = cmd_daemon_uninstall(FakeArgs(), config)
        assert result == 1

    def test_web_opens_browser(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_web

        class FakeArgs:
            pass

        with patch("memsync.cli._daemon_import_guard", return_value=True):
            with patch("webbrowser.open") as mock_open:
                result = cmd_daemon_web(FakeArgs(), config)
        assert result == 0
        mock_open.assert_called_once()

    def test_parser_has_daemon_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["daemon", "stop"])
        from memsync.cli import cmd_daemon_stop
        assert args.func is cmd_daemon_stop

    def test_parser_daemon_start_has_detach_flag(self):
        parser = build_parser()
        args = parser.parse_args(["daemon", "start", "--detach"])
        assert args.detach is True


# ---------------------------------------------------------------------------
# cmd_harvest
# ---------------------------------------------------------------------------

def _harvest_args(**kwargs):
    """Build a minimal args namespace for harvest commands."""
    defaults = {
        "project": None, "session": None, "all": False,
        "auto": False, "force": False, "dry_run": False, "model": None,
    }
    defaults.update(kwargs)

    class Namespace:
        pass

    ns = Namespace()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


class TestCmdHarvest:
    def _mock_harvest_result(self, changed=True, truncated=False, malformed=False,
                             content=SAMPLE_MEMORY):
        return {
            "updated_content": content,
            "changed": changed,
            "truncated": truncated,
            "malformed": malformed,
            "input_tokens": 100,
            "output_tokens": 50,
        }

    def test_returns_code_when_memory_root_missing(self, tmp_path, capsys):
        config = Config(provider="custom", sync_root=tmp_path / "nonexistent")
        result = cmd_harvest(_harvest_args(), config)
        assert result == 2

    def test_returns_3_when_no_global_memory(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_harvest(_harvest_args(), config)
        assert result == 3

    def test_all_flag_delegates(self, memory_file, monkeypatch):
        config, tmp_path, global_memory = memory_file
        with patch("memsync.cli._harvest_all", return_value=0) as mock_all:
            result = cmd_harvest(_harvest_args(all=True), config)
        assert result == 0
        mock_all.assert_called_once()

    def test_explicit_project_path(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # No sessions in project dir
        result = cmd_harvest(_harvest_args(project=str(project_dir), auto=True), config)
        assert result == 0

    def test_project_not_found_returns_1(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        result = cmd_harvest(_harvest_args(project="/nonexistent/path"), config)
        assert result == 1

    def test_auto_detect_project_dir_not_found_returns_4(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: None)
        result = cmd_harvest(_harvest_args(), config)
        assert result == 4

    def test_no_new_sessions_returns_0(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session", lambda pd, exclude=None: None)
        result = cmd_harvest(_harvest_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "No sessions found" in out

    def test_empty_transcript_returns_0(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "abc.jsonl"
        session.write_text("", encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript", lambda p: ("", 0))

        result = cmd_harvest(_harvest_args(auto=True), config)
        assert result == 0

    def test_successful_update(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user","message":{"content":"hello"}}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript text", 1))

        updated = SAMPLE_MEMORY + "\n- harvested item"
        mock_result = self._mock_harvest_result(changed=True, content=updated)

        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            result = cmd_harvest(_harvest_args(auto=True), config)

        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == updated

    def test_no_changes(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user"}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript", 1))

        mock_result = self._mock_harvest_result(changed=False)
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            result = cmd_harvest(_harvest_args(auto=True), config)

        assert result == 0

    def test_dry_run_does_not_write(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        original = global_memory.read_text(encoding="utf-8")
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user"}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript", 1))

        updated = SAMPLE_MEMORY + "\n- new item"
        mock_result = self._mock_harvest_result(changed=True, content=updated)
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            result = cmd_harvest(_harvest_args(auto=True, dry_run=True), config)

        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == original

    def test_truncated_returns_5(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user"}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript", 1))

        mock_result = self._mock_harvest_result(changed=True, truncated=True)
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            result = cmd_harvest(_harvest_args(auto=True), config)

        assert result == 5

    def test_malformed_returns_6(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user"}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript", 1))

        mock_result = self._mock_harvest_result(changed=True, malformed=True)
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            result = cmd_harvest(_harvest_args(auto=True), config)

        assert result == 6

    def test_llm_error_returns_5(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user"}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript", 1))

        with patch("memsync.cli.harvest_memory_content", side_effect=LLMError("all backends failed")):
            result = cmd_harvest(_harvest_args(auto=True), config)

        assert result == 5

    def test_model_override(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user"}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript", 1))

        mock_result = self._mock_harvest_result(changed=False)
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result) as mock_fn:
            cmd_harvest(_harvest_args(auto=True, model="claude-haiku-4-5-20251001"), config)

        called_config = mock_fn.call_args.args[2]
        assert called_config.model == "claude-haiku-4-5-20251001"

    def test_session_marked_as_harvested(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        memory_root = config.sync_root / ".claude-memory"
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-xyz.jsonl"
        session.write_text('{"type":"user"}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session",
                            lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript", 1))

        mock_result = self._mock_harvest_result(changed=False)
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            cmd_harvest(_harvest_args(auto=True), config)

        import json
        index = json.loads((memory_root / "harvested.json").read_text(encoding="utf-8"))
        assert "session-xyz" in index

    def test_explicit_session_path(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = tmp_path / "custom-session.jsonl"
        session.write_text('{"type":"user","message":{"content":"hi"}}', encoding="utf-8")

        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.read_session_transcript",
                            lambda p: ("transcript", 1))

        mock_result = self._mock_harvest_result(changed=False)
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            result = cmd_harvest(
                _harvest_args(project=str(project_dir), session=str(session), auto=True),
                config,
            )

        assert result == 0

    def test_session_not_found_returns_1(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        result = cmd_harvest(
            _harvest_args(project=str(project_dir), session="/nonexistent/session.jsonl"),
            config,
        )
        assert result == 1


# ---------------------------------------------------------------------------
# _harvest_all
# ---------------------------------------------------------------------------

def _redirect_projects_dir(monkeypatch, target_dir):
    """Monkeypatch Path.expanduser so ~/.claude/projects resolves to target_dir."""
    _orig = Path.expanduser

    def _expanduser(self):
        # Handle both Unix (/) and Windows (\) separators
        s = str(self).replace("\\", "/")
        if ".claude/projects" in s:
            return target_dir
        return _orig(self)

    monkeypatch.setattr(Path, "expanduser", _expanduser)


class TestHarvestAll:
    def _mock_harvest_result(self, changed=True, truncated=False, malformed=False,
                             content=SAMPLE_MEMORY):
        return {
            "updated_content": content,
            "changed": changed,
            "truncated": truncated,
            "malformed": malformed,
            "input_tokens": 100,
            "output_tokens": 50,
        }

    def test_no_projects_dir(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        _redirect_projects_dir(monkeypatch, tmp_path / "nonexistent-projects")

        result = _harvest_all(
            _harvest_args(auto=True), config, memory_root, global_memory,
        )
        assert result == 0

    def test_no_new_sessions(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "my-project"
        proj.mkdir(parents=True)
        _redirect_projects_dir(monkeypatch, projects_dir)

        # No JSONL files in project dir
        result = _harvest_all(_harvest_args(auto=True), config, memory_root, global_memory)
        assert result == 0

    def test_processes_sessions_and_writes(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"

        projects_dir = tmp_path / "claude-projects"
        proj = projects_dir / "my-project"
        proj.mkdir(parents=True)
        s1 = proj / "session-001.jsonl"
        s1.write_text('{"type":"user","message":{"content":"hi"}}', encoding="utf-8")
        _redirect_projects_dir(monkeypatch, projects_dir)

        updated = SAMPLE_MEMORY + "\n- harvested from all"
        mock_result = self._mock_harvest_result(changed=True, content=updated)

        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    result = _harvest_all(
                        _harvest_args(auto=True), config, memory_root, global_memory,
                    )

        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == updated

    def test_api_error_increments_errors(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"

        projects_dir = tmp_path / "claude-projects"
        proj = projects_dir / "my-project"
        proj.mkdir(parents=True)
        s1 = proj / "session-001.jsonl"
        s1.write_text('{"type":"user"}', encoding="utf-8")
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_memory_content", side_effect=LLMError("all backends failed")):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    result = _harvest_all(
                        _harvest_args(auto=True), config, memory_root, global_memory,
                    )

        assert result == 1  # errors > 0

    def test_skips_truncated(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"

        projects_dir = tmp_path / "claude-projects"
        proj = projects_dir / "my-project"
        proj.mkdir(parents=True)
        s1 = proj / "session-001.jsonl"
        s1.write_text('{"type":"user"}', encoding="utf-8")
        _redirect_projects_dir(monkeypatch, projects_dir)

        mock_result = self._mock_harvest_result(changed=True, truncated=True)
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    _harvest_all(
                        _harvest_args(auto=True), config, memory_root, global_memory,
                    )

        # No error, but truncated session is skipped — memory unchanged
        original = (
            "<!-- memsync v0.2 -->\n"
            "# Global Memory\n\n"
            "## Identity & context\n"
            "- Test user, software engineer\n\n"
            "## Hard constraints\n"
            "- Always backup before writing\n"
            "- Never skip tests\n"
        )
        assert global_memory.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# cmd_refresh — additional error paths
# ---------------------------------------------------------------------------

class TestCmdRefreshErrors:
    def _mock_refresh_result(self, changed=True, truncated=False, malformed=False,
                             content=SAMPLE_MEMORY):
        return {
            "updated_content": content,
            "changed": changed,
            "truncated": truncated,
            "malformed": malformed,
            "input_tokens": 100,
            "output_tokens": 50,
        }

    def test_reads_from_file(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        note_file = tmp_path / "notes.txt"
        note_file.write_text("notes from file", encoding="utf-8")

        mock_result = self._mock_refresh_result(changed=False)
        with patch("memsync.cli.refresh_memory_content", return_value=mock_result):
            result = cmd_refresh(_args(file=str(note_file)), config)
        assert result == 0

    def test_file_not_found_returns_1(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        result = cmd_refresh(_args(file="/nonexistent/notes.txt"), config)
        assert result == 1

    def test_stdin_read(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("notes from stdin"))
        monkeypatch.setattr("memsync.cli.sys.stdin",
                            type("FakeStdin", (), {"isatty": lambda self: False,
                                                   "read": lambda self: "notes from stdin"})())

        mock_result = self._mock_refresh_result(changed=False)
        with patch("memsync.cli.refresh_memory_content", return_value=mock_result):
            result = cmd_refresh(_args(), config)
        assert result == 0

    def test_no_notes_no_stdin_returns_1(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        # Simulate a tty (no piped input)
        monkeypatch.setattr("memsync.cli.sys.stdin",
                            type("FakeStdin", (), {"isatty": lambda self: True})())
        result = cmd_refresh(_args(), config)
        assert result == 1

    def test_llm_error_returns_5(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        with patch("memsync.cli.refresh_memory_content",
                   side_effect=LLMError("all backends failed")):
            result = cmd_refresh(_args(notes="some notes"), config)
        assert result == 5

    def test_malformed_response_returns_6(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        mock_result = self._mock_refresh_result(changed=True, malformed=True)
        with patch("memsync.cli.refresh_memory_content", return_value=mock_result):
            result = cmd_refresh(_args(notes="some notes"), config)
        assert result == 6

    def test_dry_run_no_changes(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        mock_result = self._mock_refresh_result(changed=False)
        with patch("memsync.cli.refresh_memory_content", return_value=mock_result):
            result = cmd_refresh(_args(notes="some notes", dry_run=True), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "No changes" in out


# ---------------------------------------------------------------------------
# cmd_usage
# ---------------------------------------------------------------------------

class TestCmdUsage:
    def test_prints_summary(self, memory_file, capsys):
        config, tmp_path, _ = memory_file
        result = cmd_usage(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "Usage log:" in out
        assert "No usage recorded yet." in out

    def test_returns_code_when_memory_root_missing(self, tmp_path, capsys):
        config = Config(provider="custom", sync_root=tmp_path / "nonexistent")
        result = cmd_usage(_args(), config)
        assert result == 2


# ---------------------------------------------------------------------------
# cmd_status — additional paths
# ---------------------------------------------------------------------------

class TestCmdStatusExtras:
    def test_sync_root_not_set_uses_provider(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        # Config without sync_root set — forces provider detection path
        config_no_root = Config(
            provider="custom",
            sync_root=None,
            claude_md_target=config.claude_md_target,
        )
        # Custom provider with sync_root=None will fail detection
        result = cmd_status(_args(), config_no_root)
        # Returns 4 because custom provider can't detect without config
        assert result == 4

    def test_target_is_copy(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        # Create a copy (not symlink) of CLAUDE.md
        target = config.claude_md_target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(global_memory.read_text(encoding="utf-8"), encoding="utf-8")

        result = cmd_status(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "copy" in out


# ---------------------------------------------------------------------------
# cmd_config_set — additional paths
# ---------------------------------------------------------------------------

class TestCmdConfigSetExtras:
    def _set_args(self, key, value):
        class Namespace:
            pass
        ns = Namespace()
        ns.key = key
        ns.value = value
        return ns

    def test_set_claude_md_target(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))
        result = cmd_config_set(self._set_args("claude_md_target", "/custom/path"), config)
        assert result == 0
        assert saved[0].claude_md_target == Path("/custom/path")

    def test_set_api_key(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))
        result = cmd_config_set(self._set_args("api_key", "sk-ant-test-key"), config)
        assert result == 0
        assert saved[0].api_key == "sk-ant-test-key"

    def test_set_max_memory_lines_non_integer(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_config_set(self._set_args("max_memory_lines", "abc"), config)
        assert result == 1


# ---------------------------------------------------------------------------
# cmd_doctor — additional paths
# ---------------------------------------------------------------------------

class TestCmdDoctorExtras:
    def test_api_key_from_config(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        # Set API key via config using the Anthropic legacy backend
        import dataclasses
        config_with_key = dataclasses.replace(config, api_key="sk-ant-test", llm_backend="anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr("memsync.cli.get_config_path",
                            lambda: tmp_path / "config.toml")
        (tmp_path / "config.toml").write_text("[core]\n", encoding="utf-8")

        from memsync.claude_md import sync as sync_claude_md
        config_with_key.claude_md_target.parent.mkdir(parents=True, exist_ok=True)
        sync_claude_md(global_memory, config_with_key.claude_md_target)

        result = cmd_doctor(_args(), config_with_key)
        out = capsys.readouterr().out
        assert result == 0
        assert "set via config" in out

    def test_api_key_from_env(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key")
        monkeypatch.setattr("memsync.cli.get_config_path",
                            lambda: tmp_path / "config.toml")
        (tmp_path / "config.toml").write_text("[core]\n", encoding="utf-8")
        import dataclasses
        config = dataclasses.replace(config, llm_backend="anthropic")

        from memsync.claude_md import sync as sync_claude_md
        config.claude_md_target.parent.mkdir(parents=True, exist_ok=True)
        sync_claude_md(global_memory, config.claude_md_target)

        cmd_doctor(_args(), config)
        out = capsys.readouterr().out
        assert "env var" in out


# ---------------------------------------------------------------------------
# _resolve_memory_root / _require_memory_root error paths
# ---------------------------------------------------------------------------

class TestResolveMemoryRoot:
    def test_unknown_provider_no_sync_root_returns_4(self, tmp_path, capsys):
        """Unknown provider, no sync_root → KeyError → _require_memory_root returns 4."""
        config = Config(
            provider="unknown_provider_xyz",
            sync_root=None,
            claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
        )
        result = cmd_show(_args(), config)
        assert result == 4

    def test_sync_root_set_unknown_provider_falls_back_to_claude_memory(self, tmp_path, capsys):
        """sync_root set but unknown provider → fallback to .claude-memory subdir."""
        sync_root = tmp_path / "sync"
        sync_root.mkdir()
        config = Config(
            provider="unknown_xyz",
            sync_root=sync_root,
            claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
        )
        # .claude-memory doesn't exist → returns 2
        result = cmd_show(_args(), config)
        assert result == 2

    def test_provider_detect_succeeds_no_sync_root(self, tmp_path, capsys):
        """Provider detect() returns a path → uses provider.get_memory_root()."""
        fake_provider = MagicMock()
        fake_provider.detect.return_value = tmp_path / "cloud"
        fake_provider.get_memory_root.return_value = tmp_path / "cloud" / ".claude-memory"
        config = Config(
            provider="onedrive",
            sync_root=None,
            claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
        )
        with patch("memsync.cli.get_provider", return_value=fake_provider):
            result = cmd_show(_args(), config)
        # memory_root path doesn't exist → returns 2
        assert result == 2
        fake_provider.get_memory_root.assert_called_once_with(tmp_path / "cloud")


# ---------------------------------------------------------------------------
# cmd_refresh — missing memory file paths
# ---------------------------------------------------------------------------

class TestCmdRefreshMemoryPaths:
    def test_memory_root_none_returns_4(self, tmp_path, capsys):
        config = Config(
            provider="unknown_xyz",
            sync_root=None,
            claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
        )
        result = cmd_refresh(_args(notes="notes"), config)
        assert result == 4

    def test_global_memory_missing_returns_3(self, tmp_config, capsys):
        config, _ = tmp_config
        # tmp_config has memory_root dir but no GLOBAL_MEMORY.md
        result = cmd_refresh(_args(notes="notes"), config)
        assert result == 3


# ---------------------------------------------------------------------------
# cmd_diff — missing memory root
# ---------------------------------------------------------------------------

class TestCmdDiffMemoryPath:
    def test_memory_root_missing_returns_2(self, tmp_path, capsys):
        config = Config(
            provider="custom",
            sync_root=tmp_path / "nonexistent",
            claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
        )
        result = cmd_diff(_args(), config)
        assert result == 2


# ---------------------------------------------------------------------------
# cmd_prune — additional paths
# ---------------------------------------------------------------------------

class TestCmdPruneExtras:
    def test_memory_root_missing_returns_2(self, tmp_path, capsys):
        config = Config(
            provider="custom",
            sync_root=tmp_path / "nonexistent",
            claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
        )
        result = cmd_prune(_args(), config)
        assert result == 2

    def test_dry_run_no_backups(self, memory_file, capsys):
        config, _, _ = memory_file
        result = cmd_prune(_args(keep_days=30, dry_run=True), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "No backups older" in out

    def test_dry_run_would_delete(self, memory_file, capsys):
        config, _, _ = memory_file
        backup_dir = config.sync_root / ".claude-memory" / "backups"
        old = backup_dir / "GLOBAL_MEMORY_20200101_000000.md"
        old.write_text("old content", encoding="utf-8")
        result = cmd_prune(_args(keep_days=1, dry_run=True), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "Would prune" in out


# ---------------------------------------------------------------------------
# _harvest_all — non-auto mode (print paths, model override, write path)
# ---------------------------------------------------------------------------

class TestHarvestAllNonAuto:
    def _mock_result(self, changed=True, truncated=False, malformed=False, content=SAMPLE_MEMORY):
        return {
            "updated_content": content,
            "changed": changed,
            "truncated": truncated,
            "malformed": malformed,
            "input_tokens": 100,
            "output_tokens": 50,
        }

    def _setup_project(self, tmp_path, n_sessions=1):
        projects_dir = tmp_path / "claude-projects"
        proj = projects_dir / "my-project"
        proj.mkdir(parents=True)
        sessions = []
        for i in range(n_sessions):
            s = proj / f"session-{i:03d}.jsonl"
            s.write_text('{"type":"user","message":{"content":"hi"}}', encoding="utf-8")
            sessions.append(s)
        return projects_dir, sessions

    def test_no_new_sessions_prints_message(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir = tmp_path / "projects"
        (projects_dir / "my-project").mkdir(parents=True)
        _redirect_projects_dir(monkeypatch, projects_dir)

        result = _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)
        out = capsys.readouterr().out
        assert result == 0
        assert "No new sessions" in out

    def test_found_sessions_prints_count(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_memory_content", return_value=self._mock_result(changed=False)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    result = _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)

        out = capsys.readouterr().out
        assert result == 0
        assert "unprocessed session" in out
        assert "no changes" in out

    def test_model_override_applies(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path)
        _redirect_projects_dir(monkeypatch, projects_dir)

        captured = []

        def mock_harvest(transcript, memory, cfg):
            captured.append(cfg)
            return self._mock_result(changed=False)

        with patch("memsync.cli.harvest_memory_content", side_effect=mock_harvest):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    _harvest_all(
                        _harvest_args(auto=True, model="claude-haiku-4-5-20251001"),
                        config, memory_root, global_memory,
                    )

        assert captured[0].model == "claude-haiku-4-5-20251001"

    def test_changed_non_auto_prints_done(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path)
        _redirect_projects_dir(monkeypatch, projects_dir)

        updated = SAMPLE_MEMORY + "\n- new item"
        with patch("memsync.cli.harvest_memory_content", return_value=self._mock_result(changed=True, content=updated)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    with patch("memsync.cli.sync_claude_md"):
                        result = _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)

        out = capsys.readouterr().out
        assert result == 0
        assert "updated" in out
        assert "done" in out

    def test_no_changes_non_auto_prints_message(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_memory_content", return_value=self._mock_result(changed=False)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    result = _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)

        out = capsys.readouterr().out
        assert result == 0
        assert "No memory changes" in out

    def test_truncated_non_auto_prints(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_memory_content", return_value=self._mock_result(truncated=True)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)

        out = capsys.readouterr().out
        assert "truncated" in out

    def test_malformed_non_auto_prints(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_memory_content", return_value=self._mock_result(malformed=True)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)

        out = capsys.readouterr().out
        assert "malformed" in out

    def test_multiple_sessions_triggers_sleep(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path, n_sessions=2)
        _redirect_projects_dir(monkeypatch, projects_dir)

        sleep_calls = []
        with patch("memsync.cli.harvest_memory_content", return_value=self._mock_result(changed=False)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                    _harvest_all(_harvest_args(auto=True), config, memory_root, global_memory)

        assert len(sleep_calls) == 1  # sleep only between 2nd+ sessions


# ---------------------------------------------------------------------------
# cmd_harvest — interactive confirmation (non-auto mode)
# ---------------------------------------------------------------------------

class TestCmdHarvestInteractive:
    def _setup(self, memory_file, monkeypatch):
        config, tmp_path, global_memory = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user","message":{"content":"hi"}}', encoding="utf-8")
        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session", lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript", lambda p: ("transcript", 10))
        return config, global_memory

    def test_user_confirms_y_proceeds(self, memory_file, monkeypatch, capsys):
        config, global_memory = self._setup(memory_file, monkeypatch)
        updated = SAMPLE_MEMORY + "\n- new item"
        mock_result = {
            "updated_content": updated, "changed": True,
            "truncated": False, "malformed": False,
            "input_tokens": 100, "output_tokens": 50,
        }
        with patch("builtins.input", return_value="y"):
            with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
                result = cmd_harvest(_harvest_args(auto=False), config)
        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == updated

    def test_user_confirms_n_aborts(self, memory_file, monkeypatch, capsys):
        config, global_memory = self._setup(memory_file, monkeypatch)
        original = global_memory.read_text(encoding="utf-8")
        with patch("builtins.input", return_value="n"):
            result = cmd_harvest(_harvest_args(auto=False), config)
        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == original

    def test_user_presses_enter_aborts(self, memory_file, monkeypatch, capsys):
        config, global_memory = self._setup(memory_file, monkeypatch)
        original = global_memory.read_text(encoding="utf-8")
        with patch("builtins.input", return_value=""):
            result = cmd_harvest(_harvest_args(auto=False), config)
        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == original

    def test_non_auto_success_prints_done(self, memory_file, monkeypatch, capsys):
        config, _ = self._setup(memory_file, monkeypatch)
        updated = SAMPLE_MEMORY + "\n- new item"
        mock_result = {
            "updated_content": updated, "changed": True,
            "truncated": False, "malformed": False,
            "input_tokens": 100, "output_tokens": 50,
        }
        with patch("builtins.input", return_value="y"):
            with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
                result = cmd_harvest(_harvest_args(auto=False), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "done" in out

    def test_non_auto_no_changes_prints_message(self, memory_file, monkeypatch, capsys):
        config, _ = self._setup(memory_file, monkeypatch)
        mock_result = {
            "updated_content": SAMPLE_MEMORY, "changed": False,
            "truncated": False, "malformed": False,
            "input_tokens": 100, "output_tokens": 50,
        }
        with patch("builtins.input", return_value="y"):
            with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
                result = cmd_harvest(_harvest_args(auto=False), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "no changes" in out


# ---------------------------------------------------------------------------
# cmd_harvest — session growth detection
# ---------------------------------------------------------------------------

class TestCmdHarvestGrowthDetection:
    def _setup(self, memory_file, monkeypatch, stored_count):
        import json
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-xyz.jsonl"
        session.write_text('{"type":"user","message":{"content":"hi"}}', encoding="utf-8")
        (memory_root / "harvested.json").write_text(
            json.dumps({"session-xyz": stored_count}), encoding="utf-8"
        )
        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session", lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript", lambda p: ("transcript", 10))
        return config, global_memory

    def test_same_count_skips_silently(self, memory_file, monkeypatch, capsys):
        config, _ = self._setup(memory_file, monkeypatch, stored_count=10)
        result = cmd_harvest(_harvest_args(auto=True), config)
        assert result == 0

    def test_old_format_minus_one_skips(self, memory_file, monkeypatch, capsys):
        config, _ = self._setup(memory_file, monkeypatch, stored_count=-1)
        result = cmd_harvest(_harvest_args(auto=True), config)
        assert result == 0

    def test_grown_session_proceeds(self, memory_file, monkeypatch, capsys):
        config, global_memory = self._setup(memory_file, monkeypatch, stored_count=5)
        updated = SAMPLE_MEMORY + "\n- grown item"
        mock_result = {
            "updated_content": updated, "changed": True,
            "truncated": False, "malformed": False,
            "input_tokens": 100, "output_tokens": 50,
        }
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            result = cmd_harvest(_harvest_args(auto=True), config)
        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == updated

    def test_force_bypasses_count_check(self, memory_file, monkeypatch, capsys):
        config, global_memory = self._setup(memory_file, monkeypatch, stored_count=10)
        updated = SAMPLE_MEMORY + "\n- forced item"
        mock_result = {
            "updated_content": updated, "changed": True,
            "truncated": False, "malformed": False,
            "input_tokens": 100, "output_tokens": 50,
        }
        with patch("memsync.cli.harvest_memory_content", return_value=mock_result):
            result = cmd_harvest(_harvest_args(auto=True, force=True), config)
        assert result == 0
        assert global_memory.read_text(encoding="utf-8") == updated

    def test_no_growth_non_auto_prints_message(self, memory_file, monkeypatch, capsys):
        config, _ = self._setup(memory_file, monkeypatch, stored_count=10)
        result = cmd_harvest(_harvest_args(auto=False), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "No new messages" in out


# ---------------------------------------------------------------------------
# cmd_harvest — BadRequestError non-model re-raise
# ---------------------------------------------------------------------------

class TestCmdHarvestBadRequestNonModel:
    def test_non_model_bad_request_reraises(self, memory_file, monkeypatch):
        config, tmp_path, _ = memory_file
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        session = project_dir / "session-abc.jsonl"
        session.write_text('{"type":"user"}', encoding="utf-8")
        monkeypatch.setattr("memsync.cli.find_project_dir", lambda cwd: project_dir)
        monkeypatch.setattr("memsync.cli.find_latest_session", lambda pd, exclude=None: session)
        monkeypatch.setattr("memsync.cli.read_session_transcript", lambda p: ("transcript", 1))

        with patch("memsync.cli.harvest_memory_content",
                   side_effect=LLMError("all backends failed")):
            result = cmd_harvest(_harvest_args(auto=True), config)
        assert result == 5


# ---------------------------------------------------------------------------
# cmd_init — provider branch and fallback paths
# ---------------------------------------------------------------------------

def _init_args(**kwargs):
    defaults = {"sync_root": None, "provider": None, "force": False}
    defaults.update(kwargs)

    class Namespace:
        pass

    ns = Namespace()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


class TestCmdInitProviderBranch:
    def test_unknown_provider_returns_1(self, tmp_config, capsys, monkeypatch):
        config, tmp_path = tmp_config
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "no-config.toml")
        result = cmd_init(_init_args(provider="nosuchprovider"), config)
        assert result == 1

    def test_provider_detect_fails_returns_4(self, tmp_config, capsys, monkeypatch):
        config, tmp_path = tmp_config
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "no-config.toml")
        fake = MagicMock()
        fake.detect.return_value = None
        fake.name = "onedrive"
        monkeypatch.setattr("memsync.cli.get_provider", lambda name: fake)
        result = cmd_init(_init_args(provider="onedrive"), config)
        assert result == 4

    def test_sync_root_unknown_provider_falls_back_to_custom(self, tmp_config, capsys, monkeypatch):
        config, tmp_path = tmp_config
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "no-config.toml")
        sync_root = tmp_path / "cloud"
        sync_root.mkdir()
        monkeypatch.setattr("memsync.cli.sync_claude_md", lambda src, tgt: None)
        result = cmd_init(_init_args(sync_root=str(sync_root), provider="nosuchprovider"), config)
        assert result == 0


# ---------------------------------------------------------------------------
# Daemon import guard + per-command no-guard paths
# ---------------------------------------------------------------------------

class TestDaemonImportGuardExtras:
    def test_guard_returns_false_when_import_fails(self, capsys):
        from memsync.cli import _daemon_import_guard
        with patch.dict(sys.modules, {"apscheduler": None}):
            result = _daemon_import_guard()
        assert result is False

    def test_daemon_status_guard_false_returns_1(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_status
        with patch("memsync.cli._daemon_import_guard", return_value=False):
            result = cmd_daemon_status(object(), config)
        assert result == 1

    def test_daemon_schedule_guard_false_returns_1(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_schedule
        with patch("memsync.cli._daemon_import_guard", return_value=False):
            result = cmd_daemon_schedule(object(), config)
        assert result == 1

    def test_daemon_schedule_no_jobs_prints_message(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_schedule
        fake_scheduler = MagicMock()
        fake_scheduler.get_jobs.return_value = []
        fake_module = MagicMock()
        fake_module.build_scheduler.return_value = fake_scheduler
        with patch.dict(sys.modules, {"memsync.daemon.scheduler": fake_module}):
            with patch("memsync.cli._daemon_import_guard", return_value=True):
                result = cmd_daemon_schedule(object(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "No jobs" in out

    def test_daemon_install_guard_false_returns_1(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_install
        with patch("memsync.cli._daemon_import_guard", return_value=False):
            result = cmd_daemon_install(object(), config)
        assert result == 1

    def test_daemon_install_permission_error_returns_1(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_install
        with patch("memsync.daemon.service.install_service", side_effect=PermissionError):
            result = cmd_daemon_install(object(), config)
        assert result == 1

    def test_daemon_uninstall_guard_false_returns_1(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_uninstall
        with patch("memsync.cli._daemon_import_guard", return_value=False):
            result = cmd_daemon_uninstall(object(), config)
        assert result == 1

    def test_daemon_uninstall_not_implemented_returns_1(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_uninstall
        with patch("memsync.daemon.service.uninstall_service",
                   side_effect=NotImplementedError("not supported")):
            result = cmd_daemon_uninstall(object(), config)
        assert result == 1

    def test_daemon_web_guard_false_returns_1(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_web
        with patch("memsync.cli._daemon_import_guard", return_value=False):
            result = cmd_daemon_web(object(), config)
        assert result == 1


# ---------------------------------------------------------------------------
# cmd_daemon_stop — with PID file
# ---------------------------------------------------------------------------

class TestCmdDaemonStopWithPid:
    def test_invalid_pid_text_returns_1(self, tmp_config, capsys, tmp_path, monkeypatch):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_stop
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("not-a-number", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", pid_file)
        result = cmd_daemon_stop(object(), config)
        assert result == 1

    def test_kills_process_and_removes_pid_file(self, tmp_config, capsys, tmp_path, monkeypatch):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_stop
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("99999", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", pid_file)

        if platform.system() == "Windows":
            with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                result = cmd_daemon_stop(object(), config)
        else:
            with patch("os.kill", return_value=None):
                result = cmd_daemon_stop(object(), config)

        assert result == 0
        assert not pid_file.exists()

    def test_stale_pid_removes_file(self, tmp_config, capsys, tmp_path, monkeypatch):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_stop
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("99999", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", pid_file)

        # Both Windows (via OSError) and Unix (via ProcessLookupError) are caught
        if platform.system() == "Windows":
            with patch("subprocess.run", side_effect=OSError("process not found")):
                result = cmd_daemon_stop(object(), config)
        else:
            with patch("os.kill", side_effect=ProcessLookupError()):
                result = cmd_daemon_stop(object(), config)

        assert result == 0
        assert not pid_file.exists()


# ---------------------------------------------------------------------------
# cmd_daemon_status — with PID file
# ---------------------------------------------------------------------------

class TestCmdDaemonStatusWithPid:
    def test_invalid_pid_text_returns_1(self, tmp_config, capsys, tmp_path, monkeypatch):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_status
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("not-a-number", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", pid_file)
        result = cmd_daemon_status(object(), config)
        assert result == 1

    def test_running_process_reports_running(self, tmp_config, capsys, tmp_path, monkeypatch):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_status
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", pid_file)

        with patch("memsync.cli._daemon_import_guard", return_value=True):
            if platform.system() == "Windows":
                mock_result = MagicMock()
                mock_result.stdout = "12345 python.exe"
                with patch("subprocess.run", return_value=mock_result):
                    result = cmd_daemon_status(object(), config)
            else:
                with patch("os.kill", return_value=None):
                    result = cmd_daemon_status(object(), config)

        out = capsys.readouterr().out
        assert result == 0
        assert "running" in out.lower()

    def test_stale_pid_reports_not_running(self, tmp_config, capsys, tmp_path, monkeypatch):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_status
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", pid_file)

        with patch("memsync.cli._daemon_import_guard", return_value=True):
            if platform.system() == "Windows":
                mock_result = MagicMock()
                mock_result.stdout = "no match here"
                with patch("subprocess.run", return_value=mock_result):
                    result = cmd_daemon_status(object(), config)
            else:
                with patch("os.kill", side_effect=ProcessLookupError()):
                    result = cmd_daemon_status(object(), config)

        out = capsys.readouterr().out
        assert result == 0
        assert "not running" in out.lower()


# ---------------------------------------------------------------------------
# cmd_doctor — additional paths
# ---------------------------------------------------------------------------

class TestCmdDoctorAdditional:
    def test_unknown_provider_no_sync_root(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        config_no_root = dataclasses.replace(config, provider="unknown_xyz", sync_root=None)
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "config.toml")
        (tmp_path / "config.toml").write_text("[core]\n", encoding="utf-8")
        result = cmd_doctor(_args(), config_no_root)
        out = capsys.readouterr().out
        assert "unknown provider" in out

    def test_memory_root_none_shows_cannot_resolve(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        config_bad = dataclasses.replace(config, provider="unknown_xyz", sync_root=None)
        monkeypatch.setattr("memsync.cli.get_config_path", lambda: tmp_path / "config.toml")
        (tmp_path / "config.toml").write_text("[core]\n", encoding="utf-8")
        result = cmd_doctor(_args(), config_bad)
        out = capsys.readouterr().out
        assert "cannot resolve" in out


# ---------------------------------------------------------------------------
# cmd_status — symlink CLAUDE.md
# ---------------------------------------------------------------------------

class TestCmdStatusSymlink:
    def test_symlink_claude_md_shows_symlink(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        target = config.claude_md_target
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.symlink_to(global_memory)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")

        result = cmd_status(_args(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "symlink" in out


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_exits_0_for_providers(self, tmp_config, monkeypatch):
        from memsync.cli import main
        config, _ = tmp_config
        monkeypatch.setattr("sys.argv", ["memsync", "providers"])
        monkeypatch.setattr("memsync.config.Config.load", lambda: config)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
