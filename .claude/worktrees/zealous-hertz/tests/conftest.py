from __future__ import annotations

import sys

import pytest

from memsync.config import Config

# Ensure UTF-8 stdout/stderr for the entire test session on Windows.
# CLI commands print ✓/✗ which fail on cp1252 without this.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """
    Config pointing entirely to tmp_path — no real filesystem touched.
    Creates the expected directory structure under tmp_path/sync/.claude-memory/
    """
    sync_root = tmp_path / "sync"
    memory_root = sync_root / ".claude-memory"
    (memory_root / "backups").mkdir(parents=True)
    (memory_root / "sessions").mkdir(parents=True)

    config = Config(
        provider="custom",
        sync_root=sync_root,
        claude_md_target=tmp_path / ".claude" / "CLAUDE.md",
    )

    monkeypatch.setattr(
        "memsync.config.get_config_path",
        lambda: tmp_path / "config.toml",
    )

    return config, tmp_path


@pytest.fixture
def memory_file(tmp_config):
    """A tmp_config with a pre-written GLOBAL_MEMORY.md."""
    config, tmp_path = tmp_config
    memory_root = config.sync_root / ".claude-memory"
    global_memory = memory_root / "GLOBAL_MEMORY.md"
    global_memory.write_text(
        "<!-- memsync v0.2 -->\n"
        "# Global Memory\n\n"
        "## Identity & context\n"
        "- Test user, software engineer\n\n"
        "## Hard constraints\n"
        "- Always backup before writing\n"
        "- Never skip tests\n",
        encoding="utf-8",
    )
    return config, tmp_path, global_memory
