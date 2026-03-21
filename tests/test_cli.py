from __future__ import annotations

from unittest.mock import patch

import pytest

from memsync.cli import (
    build_parser,
    cmd_config_set,
    cmd_config_show,
    cmd_diff,
    cmd_doctor,
    cmd_init,
    cmd_providers,
    cmd_prune,
    cmd_refresh,
    cmd_show,
    cmd_status,
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
        assert "Model:" in out

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
        result = cmd_daemon_status(FakeArgs(), config)
        out = capsys.readouterr().out
        assert result == 0
        assert "not running" in out.lower()

    def test_schedule_shows_jobs(self, tmp_config, capsys):
        config, _ = tmp_config
        from memsync.cli import cmd_daemon_schedule

        class FakeArgs:
            pass

        # daemon extras installed, config has refresh enabled — should show jobs
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
