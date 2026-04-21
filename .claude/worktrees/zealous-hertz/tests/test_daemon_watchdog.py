"""Tests for memsync.daemon.watchdog"""
from __future__ import annotations

from unittest.mock import patch

from memsync.config import Config
from memsync.daemon.watchdog import run_drift_check


class TestRunDriftCheck:
    def test_delegates_to_job_drift_check(self) -> None:
        config = Config(provider="custom", sync_root=None)
        with patch("memsync.daemon.watchdog.job_drift_check") as mock_job:
            run_drift_check(config)
        mock_job.assert_called_once_with(config)

    def test_does_not_raise_on_missing_sync_root(self) -> None:
        config = Config(provider="custom", sync_root=None)
        run_drift_check(config)  # must not raise
