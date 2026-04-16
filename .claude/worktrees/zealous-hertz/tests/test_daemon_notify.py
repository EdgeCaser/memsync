"""
Tests for memsync.daemon.notify

Tests the three notification channels: log, email, file.
Email is always mocked — no real SMTP connections made.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memsync.config import Config, DaemonConfig
from memsync.daemon.notify import _write_flag_file, notify


@pytest.fixture
def log_config() -> Config:
    return Config(daemon=DaemonConfig(drift_notify="log"))


@pytest.fixture
def email_config() -> Config:
    return Config(
        daemon=DaemonConfig(
            drift_notify="email",
            digest_email_from="from@example.com",
            digest_email_to="to@example.com",
            digest_smtp_host="smtp.example.com",
            digest_smtp_port=587,
            digest_smtp_user="user",
            digest_smtp_password="pass",
        )
    )


@pytest.fixture
def file_config() -> Config:
    return Config(daemon=DaemonConfig(drift_notify="file"))


class TestNotifyLog:
    def test_log_channel_does_not_raise(self, log_config: Config) -> None:
        notify(log_config, "test subject", "test body")

    def test_unknown_channel_falls_back_to_log(self) -> None:
        config = Config(daemon=DaemonConfig(drift_notify="unknown_channel"))
        notify(config, "subject", "body")  # must not raise


class TestNotifyEmail:
    def test_email_channel_calls_smtp(self, email_config: Config) -> None:
        with patch("smtplib.SMTP") as mock_smtp_class:
            mock_smtp = MagicMock()
            mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)
            notify(email_config, "subject", "body")
            mock_smtp.send_message.assert_called_once()

    def test_email_failure_does_not_raise(self, email_config: Config) -> None:
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("no server")):
            notify(email_config, "subject", "body")  # must not raise

    def test_uses_env_var_password_over_config(self, email_config: Config, monkeypatch) -> None:
        """MEMSYNC_SMTP_PASSWORD env var takes precedence over plaintext config."""
        monkeypatch.setenv("MEMSYNC_SMTP_PASSWORD", "env_secret")
        with patch("smtplib.SMTP") as mock_smtp_class:
            mock_smtp = MagicMock()
            mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)
            from memsync.daemon.notify import _send_email
            _send_email(email_config, "subject", "body")
            mock_smtp.login.assert_called_once_with("user", "env_secret")


class TestNotifyFile:
    def test_file_channel_writes_alert(
        self, file_config: Config, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _write_flag_file("alert subject", "alert body")
        alerts_dir = tmp_path / ".config" / "memsync" / "alerts"
        files = list(alerts_dir.glob("*_alert.txt"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "alert subject" in content
        assert "alert body" in content

    def test_file_channel_notify_does_not_raise(
        self, file_config: Config, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        notify(file_config, "subject", "body")  # must not raise
