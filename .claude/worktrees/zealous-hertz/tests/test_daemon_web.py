"""
Tests for memsync.daemon.web

Uses Flask's built-in test client. No real filesystem needed for route tests
except for paths inside tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memsync.config import Config, DaemonConfig
from memsync.daemon.web import create_app


@pytest.fixture
def web_config(tmp_path: Path) -> tuple[Config, Path]:
    sync_root = tmp_path / "sync"
    memory_root = sync_root / ".claude-memory"
    (memory_root / "backups").mkdir(parents=True)
    memory_file = memory_root / "GLOBAL_MEMORY.md"
    memory_file.write_text("# Global Memory\n\n## Identity\n- Test\n", encoding="utf-8")

    claude_md = tmp_path / "claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)

    config = Config(
        provider="custom",
        sync_root=sync_root,
        claude_md_target=claude_md,
        daemon=DaemonConfig(web_ui_enabled=True, web_ui_port=5000, web_ui_host="127.0.0.1"),
    )
    return config, memory_file


class TestWebIndex:
    def test_get_index_returns_200(self, web_config: tuple) -> None:
        config, _ = web_config
        app = create_app(config)
        with app.test_client() as client:
            resp = client.get("/")
        assert resp.status_code == 200

    def test_index_contains_memory_content(self, web_config: tuple) -> None:
        config, _ = web_config
        app = create_app(config)
        with app.test_client() as client:
            resp = client.get("/")
        assert b"Global Memory" in resp.data

    def test_index_shows_never_when_file_missing(self, web_config: tuple) -> None:
        config, memory_file = web_config
        memory_file.unlink()
        app = create_app(config)
        with app.test_client() as client:
            resp = client.get("/")
        assert resp.status_code == 200
        assert b"never" in resp.data


class TestWebSave:
    def test_save_writes_content(self, web_config: tuple) -> None:
        config, memory_file = web_config
        app = create_app(config)
        new_content = "# Updated Memory\n\n- new item\n"
        with app.test_client() as client:
            resp = client.post("/save", data={"content": new_content})
        assert resp.status_code == 302  # redirect after save
        assert memory_file.read_text(encoding="utf-8") == new_content

    def test_save_creates_backup(self, web_config: tuple) -> None:
        config, memory_file = web_config
        backup_dir = memory_file.parent / "backups"
        assert len(list(backup_dir.glob("*.md"))) == 0

        app = create_app(config)
        with app.test_client() as client:
            client.post("/save", data={"content": "# New Content\n"})

        assert len(list(backup_dir.glob("*.md"))) == 1

    def test_save_redirect_contains_success_message(self, web_config: tuple) -> None:
        config, _ = web_config
        app = create_app(config)
        with app.test_client() as client:
            resp = client.post("/save", data={"content": "# Content\n"})
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "Saved" in location or "saved" in location.lower()

    def test_save_syncs_to_claude_md(self, web_config: tuple) -> None:
        config, _ = web_config
        app = create_app(config)
        new_content = "# Synced Memory\n"
        with app.test_client() as client:
            client.post("/save", data={"content": new_content})
        # CLAUDE.md should have been written (copy on first run)
        assert config.claude_md_target.exists()
