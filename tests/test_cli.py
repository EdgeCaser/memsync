from __future__ import annotations

import dataclasses
import json
import platform
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memsync.llm import LLMError

from memsync.cli import (
    _harvest_all,
    _scheduled_harvest_config,
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
        assert "LLM waterfall:" in out
        assert "Harvesting:" in out

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

    def test_prunes_old_journal_entries(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        journal_dir = config.sync_root / ".claude-memory" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        old = journal_dir / "refresh_20200101_000000_000000.json"
        old.write_text("{}", encoding="utf-8")

        result = cmd_prune(_args(keep_days=30), config)
        out = capsys.readouterr().out
        assert result == 0
        assert not old.exists()
        assert "journal" in out.lower()


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
        monkeypatch.setattr("memsync.cli._sync_instruction_targets", lambda src, dst: None)

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
        monkeypatch.setattr("memsync.cli._sync_instruction_targets", lambda src, dst: None)
        cmd_init(self._init_args(sync_root=str(sync_dir)), config)

        assert (sync_dir / ".claude-memory" / "backups").exists()
        assert (sync_dir / ".claude-memory" / "sessions").exists()

    def test_init_with_explicit_provider(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        fake_root = tmp_path / "onedrive"
        fake_root.mkdir()

        from memsync.providers.onedrive import OneDriveProvider
        monkeypatch.setattr(OneDriveProvider, "detect", lambda self: fake_root)
        monkeypatch.setattr("memsync.cli._sync_instruction_targets", lambda src, dst: None)

        result = cmd_init(self._init_args(provider="onedrive"), config)
        assert result == 0

    def test_init_returns_4_when_provider_not_found(self, tmp_config, capsys):
        config, tmp_path = tmp_config
        result = cmd_init(self._init_args(provider="onedrive"), config)
        # OneDrive not present in tmp_path â†’ 4 (detection failed)
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

        monkeypatch.setattr("memsync.cli._sync_instruction_targets", lambda src, dst: None)
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
        monkeypatch.setattr("memsync.cli._sync_instruction_targets", lambda src, dst: None)

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
        from memsync.claude_md import sync_many
        config.claude_md_target.parent.mkdir(parents=True, exist_ok=True)
        sync_many(global_memory, [config.claude_md_target, config.codex_agents_target])

        monkeypatch.setattr("memsync.cli.get_config_path",
                            lambda: tmp_path / "config.toml")
        (tmp_path / "config.toml").write_text("[core]\n", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", tmp_path / "nonexistent.pid")
        monkeypatch.setattr(
            "memsync.cli._check_backend_readiness",
            lambda backend, _config: (backend == "claude_code", f"{backend} ready"),
        )

        result = cmd_doctor(_args(), config)
        assert result == 0

    def test_missing_all_llm_backends_fails(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        monkeypatch.setattr("memsync.cli.get_config_path",
                            lambda: tmp_path / "config.toml")
        (tmp_path / "config.toml").write_text("[core]\n", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", tmp_path / "nonexistent.pid")
        monkeypatch.setattr(
            "memsync.cli._check_backend_readiness",
            lambda backend, _config: (False, f"{backend} unavailable"),
        )

        result = cmd_doctor(_args(), config)
        out = capsys.readouterr().out
        assert result == 1
        assert "LLM / waterfall" in out

    def test_missing_memory_file_fails(self, tmp_config, monkeypatch, capsys):
        config, tmp_path = tmp_config
        monkeypatch.setattr(
            "memsync.cli._check_backend_readiness",
            lambda backend, _config: (backend == "claude_code", f"{backend} ready"),
        )
        # Memory root exists but no GLOBAL_MEMORY.md

        result = cmd_doctor(_args(), config)
        capsys.readouterr()
        assert result == 1

    def test_output_includes_all_check_labels(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        monkeypatch.setattr(
            "memsync.cli._check_backend_readiness",
            lambda backend, _config: (False, f"{backend} unavailable"),
        )

        cmd_doctor(_args(), config)
        out = capsys.readouterr().out
        assert "Config file" in out
        assert "LLM / waterfall" in out
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

    def test_harvest_all_defers_when_store_locked(self, memory_file, capsys):
        """A live foreign lock in the store makes --all defer without writing."""
        import json
        from datetime import UTC, datetime

        from memsync.lock import LOCK_FILENAME

        config, tmp_path, global_memory = memory_file
        memory_root = global_memory.parent
        (memory_root / LOCK_FILENAME).write_text(
            json.dumps(
                {"host": "other-machine", "pid": 4242,
                 "ts": datetime.now(UTC).isoformat()}
            ),
            encoding="utf-8",
        )
        before = global_memory.read_text(encoding="utf-8")

        result = _harvest_all(
            _harvest_args(all=True, auto=True), config, memory_root, global_memory
        )

        assert result == 0
        # Memory untouched â€” we deferred instead of harvesting.
        assert global_memory.read_text(encoding="utf-8") == before
        # Foreign lock left intact â€” we deferred, we did not steal a live lock.
        on_disk = json.loads((memory_root / LOCK_FILENAME).read_text(encoding="utf-8"))
        assert on_disk["host"] == "other-machine"

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


def _mock_batched_result(changed=True, truncated=False, malformed=False,
                         content=SAMPLE_MEMORY, harvested_ids=None, failed_ids=None):
    """Return shape of sync.harvest_sessions_batched, which the --all path uses.

    The --all sweep extracts per session then merges once, so what it gets back
    is a single merge result carrying the ids folded into it — not one result
    per session.
    """
    return {
        "updated_content": content,
        "updated_cold": "",
        "changed": changed,
        "changed_hot": changed,
        "changed_cold": False,
        "truncated": truncated,
        "malformed": malformed,
        "input_tokens": 100,
        "output_tokens": 50,
        "backend": "claude_code",
        "harvested_ids": ["session-001"] if harvested_ids is None else harvested_ids,
        "failed_ids": failed_ids or [],
    }


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

        with patch("memsync.cli.harvest_sessions_batched", return_value=mock_result):
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

        with patch("memsync.cli.harvest_sessions_batched", side_effect=LLMError("all backends failed")):
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
        with patch("memsync.cli.harvest_sessions_batched", return_value=mock_result):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    _harvest_all(
                        _harvest_args(auto=True), config, memory_root, global_memory,
                    )

        # No error, but truncated session is skipped â€” memory unchanged
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

    def test_auto_removes_ollama_from_scheduled_config(self, memory_file, monkeypatch):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"

        projects_dir = tmp_path / "claude-projects"
        proj = projects_dir / "my-project"
        proj.mkdir(parents=True)
        (proj / "session-001.jsonl").write_text(
            '{"type":"user","message":{"content":"hi"}}',
            encoding="utf-8",
        )
        _redirect_projects_dir(monkeypatch, projects_dir)

        captured = []

        def mock_harvest(sessions, memory, cfg, cold="", **kw):
            captured.append(cfg)
            return _mock_batched_result(changed=False)

        with patch("memsync.cli.harvest_sessions_batched", side_effect=mock_harvest):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                _harvest_all(_harvest_args(auto=True), config, memory_root, global_memory)

        assert "ollama" not in captured[0].llm_backends

    def test_auto_limits_sessions_per_run(self, memory_file, monkeypatch):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"

        projects_dir = tmp_path / "claude-projects"
        proj = projects_dir / "my-project"
        proj.mkdir(parents=True)
        for i in range(3):
            (proj / f"session-{i:03d}.jsonl").write_text(
                '{"type":"user","message":{"content":"hi"}}',
                encoding="utf-8",
            )
        _redirect_projects_dir(monkeypatch, projects_dir)
        config = dataclasses.replace(
            config,
            daemon=dataclasses.replace(config.daemon, harvest_max_sessions_per_run=2),
        )

        with patch("memsync.cli.harvest_sessions_batched",
                   return_value=_mock_batched_result(changed=False)) as mock_harvest:
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                _harvest_all(_harvest_args(auto=True), config, memory_root, global_memory)

        # One batched call now, but the per-run cap still has to bite: 3 sessions
        # exist and the cap is 2, so the batch must contain exactly 2.
        assert mock_harvest.call_count == 1
        assert len(mock_harvest.call_args[0][0]) == 2


class TestHarvestAcrossMultipleRoots:
    """
    The machine awake at harvest time is often not the one that produced the
    transcripts. An always-on host receives peers' transcripts through file
    sync, so the harvest has to sweep more than its own ~/.claude/projects.
    """

    @staticmethod
    def _session(root, project, stem):
        proj = root / project
        proj.mkdir(parents=True, exist_ok=True)
        path = proj / f"{stem}.jsonl"
        path.write_text('{"type":"user","message":{"content":"hi"}}', encoding="utf-8")
        return path

    @staticmethod
    def _config_with_roots(config, roots):
        return dataclasses.replace(
            config,
            daemon=dataclasses.replace(
                config.daemon, harvest_projects_dir=[str(r) for r in roots]
            ),
        )

    def _run(self, config, memory_root, global_memory, seen):
        # The sweep now hands every session to one batched call, so what these
        # tests count is the ids in that batch rather than one call per session.
        def _record(sessions, *a, **kw):  # noqa: ARG001
            seen.extend(sid for sid, _ in sessions)
            return _mock_batched_result(
                changed=False, harvested_ids=[sid for sid, _ in sessions]
            )

        with patch("memsync.cli.harvest_sessions_batched", side_effect=_record):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    return _harvest_all(
                        _harvest_args(auto=True), config, memory_root, global_memory,
                    )

    def test_sweeps_every_configured_root(self, memory_file):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        local, synced = tmp_path / "local", tmp_path / "synced"
        self._session(local, "proj-a", "session-local")
        self._session(synced, "proj-b", "session-remote")

        seen: list[str] = []
        cfg = self._config_with_roots(config, [local, synced])
        assert self._run(cfg, memory_root, global_memory, seen) == 0
        assert len(seen) == 2

    def test_a_missing_root_does_not_stop_the_others(self, memory_file, capsys):
        # These roots point at peers. One being offline, or its sync folder not
        # yet created, must not cost the harvest the roots that are present.
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        local = tmp_path / "local"
        self._session(local, "proj-a", "session-local")

        seen: list[str] = []
        cfg = self._config_with_roots(config, [local, tmp_path / "peer-that-is-offline"])
        assert self._run(cfg, memory_root, global_memory, seen) == 0
        assert len(seen) == 1
        assert "Skipping missing projects root" in capsys.readouterr().err

    def test_the_same_session_under_two_roots_is_harvested_once(self, memory_file):
        # A machine that both writes locally and syncs a copy of itself would
        # otherwise pay for the same transcript twice.
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        first, second = tmp_path / "first", tmp_path / "second"
        self._session(first, "proj", "session-dup")
        self._session(second, "proj", "session-dup")

        seen: list[str] = []
        cfg = self._config_with_roots(config, [first, second])
        assert self._run(cfg, memory_root, global_memory, seen) == 0
        assert len(seen) == 1

    def test_reports_when_no_root_exists(self, memory_file, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        cfg = self._config_with_roots(config, [tmp_path / "nope-one", tmp_path / "nope-two"])

        assert _harvest_all(
            _harvest_args(auto=False), cfg, memory_root, global_memory,
        ) == 0
        out = capsys.readouterr().out
        assert "nope-one" in out and "nope-two" in out

class TestScheduledHarvestConfig:
    def test_keeps_ollama_when_allowed(self):
        config = Config()
        config = dataclasses.replace(
            config,
            daemon=dataclasses.replace(config.daemon, harvest_allow_ollama=True),
        )
        assert _scheduled_harvest_config(config).llm_backends == config.llm_backends


# ---------------------------------------------------------------------------
# cmd_refresh â€” additional error paths
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
# cmd_status â€” additional paths
# ---------------------------------------------------------------------------

class TestCmdStatusExtras:
    def test_sync_root_not_set_uses_provider(self, memory_file, monkeypatch, capsys):
        config, tmp_path, _ = memory_file
        # Config without sync_root set â€” forces provider detection path
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
# cmd_config_set â€” additional paths
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

    def test_set_codex_agents_target(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))
        result = cmd_config_set(self._set_args("codex_agents_target", "/custom/agents"), config)
        assert result == 0
        assert saved[0].codex_agents_target == Path("/custom/agents")

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

    def test_set_llm_backend_updates_waterfall(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))
        result = cmd_config_set(self._set_args("llm_backend", "claude"), config)
        assert result == 0
        assert saved[0].llm_backends[0] == "claude_code"

    def test_set_backend_specific_harvest_chunk_tokens(self, tmp_config, monkeypatch):
        config, tmp_path = tmp_config
        saved = []
        monkeypatch.setattr(Config, "save", lambda self: saved.append(self))
        result = cmd_config_set(self._set_args("harvest_chunk_tokens_ollama", "1800"), config)
        assert result == 0
        assert saved[0].harvest_chunk_tokens_ollama == 1800


# ---------------------------------------------------------------------------
# cmd_doctor â€” additional paths
# ---------------------------------------------------------------------------

class TestCmdDoctorExtras:
    def test_api_key_from_config(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        # Set API key via config using the Anthropic legacy backend
        import dataclasses
        config_with_key = dataclasses.replace(
            config,
            api_key="sk-ant-test",
            llm_backend="anthropic",
            fallback_backend="none",
            llm_backends=["anthropic"],
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr("memsync.cli.get_config_path",
                            lambda: tmp_path / "config.toml")
        (tmp_path / "config.toml").write_text("[core]\n", encoding="utf-8")
        monkeypatch.setattr("memsync.cli._PID_FILE", tmp_path / "nonexistent.pid")

        from memsync.claude_md import sync_many
        config_with_key.claude_md_target.parent.mkdir(parents=True, exist_ok=True)
        sync_many(global_memory, [config_with_key.claude_md_target, config_with_key.codex_agents_target])

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
        config = dataclasses.replace(
            config,
            llm_backend="anthropic",
            fallback_backend="none",
            llm_backends=["anthropic"],
        )

        from memsync.claude_md import sync_many
        config.claude_md_target.parent.mkdir(parents=True, exist_ok=True)
        sync_many(global_memory, [config.claude_md_target, config.codex_agents_target])

        cmd_doctor(_args(), config)
        out = capsys.readouterr().out
        assert "env var" in out


# ---------------------------------------------------------------------------
# _resolve_memory_root / _require_memory_root error paths
# ---------------------------------------------------------------------------

class TestResolveMemoryRoot:
    def test_unknown_provider_no_sync_root_returns_4(self, tmp_path, capsys):
        """Unknown provider, no sync_root â†’ KeyError â†’ _require_memory_root returns 4."""
        config = Config(
            provider="unknown_provider_xyz",
            sync_root=None,
            claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
        )
        result = cmd_show(_args(), config)
        assert result == 4

    def test_sync_root_set_unknown_provider_falls_back_to_claude_memory(self, tmp_path, capsys):
        """sync_root set but unknown provider â†’ fallback to .claude-memory subdir."""
        sync_root = tmp_path / "sync"
        sync_root.mkdir()
        config = Config(
            provider="unknown_xyz",
            sync_root=sync_root,
            claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
        )
        # .claude-memory doesn't exist â†’ returns 2
        result = cmd_show(_args(), config)
        assert result == 2

    def test_provider_detect_succeeds_no_sync_root(self, tmp_path, capsys):
        """Provider detect() returns a path â†’ uses provider.get_memory_root()."""
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
        # memory_root path doesn't exist â†’ returns 2
        assert result == 2
        fake_provider.get_memory_root.assert_called_once_with(tmp_path / "cloud")


# ---------------------------------------------------------------------------
# cmd_refresh â€” missing memory file paths
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
# cmd_diff â€” missing memory root
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
# cmd_prune â€” additional paths
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
# _harvest_all â€” non-auto mode (print paths, model override, write path)
# ---------------------------------------------------------------------------

class TestHarvestAllNonAuto:
    def _mock_result(self, changed=True, truncated=False, malformed=False,
                     content=SAMPLE_MEMORY, harvested_ids=None):
        return _mock_batched_result(
            changed=changed, truncated=truncated, malformed=malformed,
            content=content,
            harvested_ids=["session-000"] if harvested_ids is None else harvested_ids,
        )

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

        with patch("memsync.cli.harvest_sessions_batched", return_value=self._mock_result(changed=False)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    result = _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)

        out = capsys.readouterr().out
        assert result == 0
        assert "unprocessed session" in out
        assert "No memory changes" in out

    def test_model_override_applies(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path)
        _redirect_projects_dir(monkeypatch, projects_dir)

        captured = []

        def mock_harvest(sessions, memory, cfg, cold="", **kw):
            captured.append(cfg)
            return self._mock_result(changed=False)

        with patch("memsync.cli.harvest_sessions_batched", side_effect=mock_harvest):
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
        with patch("memsync.cli.harvest_sessions_batched", return_value=self._mock_result(changed=True, content=updated)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    with patch("memsync.cli._sync_instruction_targets"):
                        result = _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)

        out = capsys.readouterr().out
        assert result == 0
        assert "Merged" in out
        assert "done" in out

    def test_no_changes_non_auto_prints_message(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_sessions_batched", return_value=self._mock_result(changed=False)):
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

        with patch("memsync.cli.harvest_sessions_batched", return_value=self._mock_result(truncated=True)):
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

        with patch("memsync.cli.harvest_sessions_batched", return_value=self._mock_result(malformed=True)):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("time.sleep"):
                    _harvest_all(_harvest_args(auto=False), config, memory_root, global_memory)

        out = capsys.readouterr().out
        assert "malformed" in out

    def test_all_sessions_go_to_one_batched_call(self, memory_file, monkeypatch, capsys):
        # Replaces test_multiple_sessions_triggers_sleep. The sweep no longer
        # paces itself between sessions, because it no longer makes a call per
        # session — it makes one batched call. Backend pacing moved down into
        # extract_candidates_from_chunk's inter-chunk sleep, covered in
        # tests/test_harvest.py. What matters here is that every session lands
        # in a single call rather than one merge each, since the per-session
        # merge is what could not fit inside the runtime budget.
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path, n_sessions=2)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_sessions_batched",
                   return_value=self._mock_result(changed=False)) as batched:
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                _harvest_all(_harvest_args(auto=True), config, memory_root, global_memory)

        assert batched.call_count == 1
        assert len(batched.call_args[0][0]) == 2

    def test_malformed_merge_marks_nothing_harvested(self, memory_file, monkeypatch, capsys):
        # Batching raises the stakes on this: one unusable merge response covers
        # the whole run, so marking the batch done would lose every session in
        # it silently rather than retrying them.
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path, n_sessions=2)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_sessions_batched",
                   return_value=self._mock_result(
                       changed=True, malformed=True,
                       harvested_ids=["session-000", "session-001"])):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                result = _harvest_all(
                    _harvest_args(auto=True), config, memory_root, global_memory,
                )

        index = json.loads((memory_root / "harvested.json").read_text(encoding="utf-8")) \
            if (memory_root / "harvested.json").exists() else {}
        assert index == {}, "a malformed merge must not mark its batch harvested"
        assert result == 1

    def test_failed_write_marks_nothing_harvested(self, memory_file, monkeypatch, capsys):
        # The index used to be saved right after the merge, before the content
        # it describes was written. A missing backups/ dir (the case on a store
        # obtained by cloning) then crashed the write and left the batch marked
        # done, so those sessions were never retried and their facts were lost.
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path, n_sessions=2)
        _redirect_projects_dir(monkeypatch, projects_dir)

        before = global_memory.read_text(encoding="utf-8")

        with patch("memsync.cli.harvest_sessions_batched",
                   return_value=self._mock_result(
                       changed=True,
                       harvested_ids=["session-000", "session-001"])):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                with patch("memsync.cli.backup", side_effect=FileNotFoundError("no backups/")):
                    with pytest.raises(FileNotFoundError):
                        _harvest_all(
                            _harvest_args(auto=True), config, memory_root, global_memory,
                        )

        index_file = memory_root / "harvested.json"
        index = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
        assert index == {}, "a batch whose write failed must stay retryable"
        assert global_memory.read_text(encoding="utf-8") == before

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        """Every file under the memory root, by content. A dry run must not
        change this — that is the backlog's own definition of done."""
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()
        }

    def test_dry_run_makes_no_backend_calls(self, memory_file, monkeypatch, capsys):
        # The bug: --dry-run parsed but nothing read it, so the sweep ran for
        # real — LLM calls, memory write, commit and push.
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path, n_sessions=3)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_sessions_batched") as batched:
            result = _harvest_all(
                _harvest_args(auto=False, dry_run=True), config, memory_root, global_memory,
            )

        batched.assert_not_called()
        assert result == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "Would harvest 3 session(s)" in out

    def test_dry_run_leaves_the_memory_root_byte_identical(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path, n_sessions=2)
        _redirect_projects_dir(monkeypatch, projects_dir)

        before = self._snapshot(memory_root)
        with patch("memsync.cli.harvest_sessions_batched",
                   return_value=self._mock_result(changed=True)):
            _harvest_all(
                _harvest_args(auto=True, dry_run=True), config, memory_root, global_memory,
            )
        assert self._snapshot(memory_root) == before

    def test_dry_run_takes_no_lock(self, memory_file, monkeypatch, capsys):
        # An interrupted preview used to strand a lock file that blocked real
        # harvests until it aged out.
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path, n_sessions=1)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.lock.store_lock") as lock:
            _harvest_all(
                _harvest_args(auto=False, dry_run=True), config, memory_root, global_memory,
            )
        lock.assert_not_called()
        assert not (memory_root / ".harvest.lock").exists()

    def test_batch_merge_failure_is_reported_not_swallowed(self, memory_file, monkeypatch, capsys):
        config, tmp_path, global_memory = memory_file
        memory_root = config.sync_root / ".claude-memory"
        projects_dir, _ = self._setup_project(tmp_path, n_sessions=1)
        _redirect_projects_dir(monkeypatch, projects_dir)

        with patch("memsync.cli.harvest_sessions_batched",
                   side_effect=LLMError("all backends failed")):
            with patch("memsync.cli.read_session_transcript", return_value=("transcript", 1)):
                result = _harvest_all(
                    _harvest_args(auto=True), config, memory_root, global_memory,
                )

        assert result == 1
        assert "all backends failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_harvest â€” interactive confirmation (non-auto mode)
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
# cmd_harvest â€” session growth detection
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
# cmd_harvest â€” BadRequestError non-model re-raise
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
# cmd_init â€” provider branch and fallback paths
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
        monkeypatch.setattr("memsync.cli._sync_instruction_targets", lambda src, tgt: None)
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
# cmd_daemon_stop â€” with PID file
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
# cmd_daemon_status â€” with PID file
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
# cmd_doctor â€” additional paths
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
# cmd_status â€” symlink CLAUDE.md
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
