from __future__ import annotations

import platform
from pathlib import Path

import pytest

from memsync.config import Config, get_config_path


@pytest.mark.smoke
class TestConfigDefaults:
    def test_default_provider(self):
        c = Config()
        assert c.provider == "onedrive"

    def test_default_model(self):
        c = Config()
        assert c.model == "claude-sonnet-4-20250514"

    def test_default_max_memory_lines(self):
        c = Config()
        assert c.max_memory_lines == 400

    def test_default_keep_days(self):
        c = Config()
        assert c.keep_days == 30

    def test_default_sync_root_is_none(self):
        c = Config()
        assert c.sync_root is None

    def test_default_claude_md_target_is_set(self):
        c = Config()
        assert c.claude_md_target is not None
        assert c.claude_md_target == Path("~/.claude/CLAUDE.md").expanduser()

    def test_default_project_cwd_is_none(self):
        c = Config()
        assert c.project_cwd is None

    def test_default_llm_waterfall(self):
        c = Config()
        assert c.llm_backends == ["codex", "claude_code", "gemini", "ollama"]
        assert c.llm_backend == "codex"
        assert c.fallback_backend == "claude_code"
        assert c.harvest_chunk_tokens_codex == 8000
        assert c.harvest_chunk_tokens_claude_code == 8000
        assert c.harvest_chunk_tokens_ollama == 2500

    def test_default_daemon_harvest_limits(self):
        c = Config()
        assert c.daemon.harvest_allow_ollama is False
        assert c.daemon.harvest_max_runtime_seconds == 1800
        assert c.daemon.harvest_max_sessions_per_run == 25


class TestConfigPath:
    def test_windows_path_uses_appdata(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setenv("APPDATA", "C:/Users/test/AppData/Roaming")
        path = get_config_path()
        assert "memsync" in str(path)
        assert path.suffix == ".toml"

    def test_linux_path_uses_xdg(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/home/test/.config")
        path = get_config_path()
        assert path.as_posix().endswith("memsync/config.toml")

    def test_mac_path_uses_dotconfig(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        path = get_config_path()
        assert "memsync" in str(path)
        assert path.suffix == ".toml"


class TestConfigRoundTrip:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "memsync.config.get_config_path",
            lambda: tmp_path / "config.toml",
        )
        c = Config(
            provider="icloud",
            model="claude-haiku-4-5-20251001",
            keep_days=60,
            sync_root=tmp_path / "sync",
        )
        c.save()

        loaded = Config.load()
        assert loaded.provider == "icloud"
        assert loaded.model == "claude-haiku-4-5-20251001"
        assert loaded.keep_days == 60
        assert loaded.sync_root == tmp_path / "sync"

    def test_project_cwd_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "memsync.config.get_config_path",
            lambda: tmp_path / "config.toml",
        )
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        c = Config(
            provider="icloud",
            project_cwd=project_dir,
        )
        c.save()

        loaded = Config.load()
        assert loaded.project_cwd == project_dir

    def test_load_defaults_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "memsync.config.get_config_path",
            lambda: tmp_path / "nonexistent.toml",
        )
        c = Config.load()
        assert c.provider == "onedrive"

    def test_load_without_llm_section_uses_default_waterfall(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.toml"
        config_path.write_text("[core]\nprovider = \"onedrive\"\n", encoding="utf-8")
        monkeypatch.setattr(
            "memsync.config.get_config_path",
            lambda: config_path,
        )
        loaded = Config.load()
        assert loaded.llm_backends == ["codex", "claude_code", "gemini", "ollama"]

    def test_load_normalizes_claude_alias_in_backends(self):
        loaded = Config._from_dict({
            "llm": {
                "backends": ["codex", "claude", "gemini", "ollama"],
            }
        })
        assert loaded.llm_backends == ["codex", "claude_code", "gemini", "ollama"]

    def test_load_legacy_llm_keys_preserves_explicit_order(self):
        loaded = Config._from_dict({
            "llm": {
                "backend": "gemini",
                "fallback_backend": "ollama",
            }
        })
        assert loaded.llm_backends == ["gemini", "ollama"]

    def test_load_backend_chunk_overrides(self):
        loaded = Config._from_dict({
            "llm": {
                "harvest_chunk_tokens": 6000,
                "harvest_chunk_tokens_codex": 12000,
                "harvest_chunk_tokens_ollama": 1800,
            }
        })
        assert loaded.harvest_chunk_tokens == 6000
        assert loaded.harvest_chunk_tokens_codex == 12000
        assert loaded.harvest_chunk_tokens_ollama == 1800

    def test_load_daemon_harvest_limits(self):
        loaded = Config._from_dict({
            "daemon": {
                "harvest_allow_ollama": True,
                "harvest_max_runtime_seconds": 900,
                "harvest_max_sessions_per_run": 3,
            }
        })
        assert loaded.daemon.harvest_allow_ollama is True
        assert loaded.daemon.harvest_max_runtime_seconds == 900
        assert loaded.daemon.harvest_max_sessions_per_run == 3

    def test_toml_output_is_valid(self):
        import tomllib
        c = Config(
            provider="gdrive",
            keep_days=14,
            project_cwd=Path("/usr/local/projects/my_code"),
        )
        toml_text = c._to_toml()
        parsed = tomllib.loads(toml_text)
        assert parsed["core"]["provider"] == "gdrive"
        assert parsed["backups"]["keep_days"] == 14
        assert parsed["paths"]["project_cwd"] == "/usr/local/projects/my_code"
        assert parsed["llm"]["harvest_chunk_tokens_ollama"] == c.harvest_chunk_tokens_ollama
        assert parsed["daemon"]["harvest_allow_ollama"] is False
        assert parsed["daemon"]["harvest_max_runtime_seconds"] == 1800

    def test_sync_root_serialized_with_forward_slashes(self, tmp_path):
        c = Config(sync_root=tmp_path / "my sync" / "folder")
        toml_text = c._to_toml()
        # Forward slashes in path (TOML-safe)
        assert "\\" not in toml_text.split("sync_root")[1].split("\n")[0]

    def test_project_cwd_serialized_with_forward_slashes(self, tmp_path):
        c = Config(project_cwd=tmp_path / "another project" / "path")
        toml_text = c._to_toml()
        # Forward slashes in path (TOML-safe)
        assert "\\" not in toml_text.split("project_cwd")[1].split("\n")[0]
