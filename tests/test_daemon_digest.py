"""
Tests for memsync.daemon.digest

API calls are always mocked — no real Claude API calls in tests.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memsync.config import Config, DaemonConfig
from memsync.daemon.digest import generate_and_send, generate_digest


@pytest.fixture
def digest_memory_root(tmp_path: Path) -> Path:
    memory_root = tmp_path / ".claude-memory"
    (memory_root / "sessions").mkdir(parents=True)
    return memory_root


def _write_session(memory_root: Path, day: date, content: str) -> None:
    log = memory_root / "sessions" / f"{day.strftime('%Y-%m-%d')}.md"
    log.write_text(content, encoding="utf-8")


@pytest.fixture
def digest_config(tmp_path: Path) -> Config:
    sync_root = tmp_path / "sync"
    (sync_root / ".claude-memory" / "sessions").mkdir(parents=True)
    return Config(
        provider="custom",
        sync_root=sync_root,
        daemon=DaemonConfig(
            digest_enabled=True,
            digest_email_to="to@example.com",
            digest_email_from="from@example.com",
            digest_smtp_host="smtp.example.com",
        ),
    )


class TestGenerateDigest:
    def test_returns_empty_string_when_no_logs(self, digest_memory_root: Path) -> None:
        config = Config()
        result = generate_digest(digest_memory_root, config)
        assert result == ""

    def test_collects_past_7_days(self, digest_memory_root: Path) -> None:
        today = date.today()
        for i in range(1, 6):
            day = today - timedelta(days=i)
            _write_session(digest_memory_root, day, f"Notes for day -{i}")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Weekly summary text")]

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            result = generate_digest(digest_memory_root, Config())

        assert result == "Weekly summary text"

    def test_includes_today_in_window(self, digest_memory_root: Path) -> None:
        today = date.today()
        _write_session(digest_memory_root, today, "Today's notes")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="summary")]

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            result = generate_digest(digest_memory_root, Config())

        # Today is in the 7-day window (week_ago + 7 days = today)
        assert result == "summary"

    def test_passes_model_from_config(self, digest_memory_root: Path) -> None:
        today = date.today()
        _write_session(digest_memory_root, today - timedelta(days=1), "Yesterday's notes")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="summary")]
        config = Config(model="claude-haiku-4-5-20251001")

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            generate_digest(digest_memory_root, config)

        call_kwargs = mock_client.return_value.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


class TestGenerateAndSend:
    def test_sends_email_when_digest_available(self, digest_config: Config) -> None:
        memory_root = digest_config.sync_root / ".claude-memory"
        yesterday = date.today() - timedelta(days=1)
        _write_session(memory_root, yesterday, "Worked on testing")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Weekly digest text")]

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            with patch("memsync.daemon.notify._send_email") as mock_email:
                generate_and_send(digest_config)

        mock_email.assert_called_once()
        call_kwargs = mock_email.call_args
        assert "weekly digest" in call_kwargs[1]["subject"].lower()

    def test_skips_send_when_no_logs(self, digest_config: Config) -> None:
        with patch("memsync.daemon.notify._send_email") as mock_email:
            generate_and_send(digest_config)
        mock_email.assert_not_called()

    def test_skips_when_sync_root_missing(self) -> None:
        config = Config(provider="custom", sync_root=None)
        # Should not raise
        generate_and_send(config)
