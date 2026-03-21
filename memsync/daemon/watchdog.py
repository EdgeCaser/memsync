"""
Drift watchdog for the memsync daemon.

Thin wrapper that exposes drift detection as a standalone callable.
The scheduler calls job_drift_check from scheduler.py directly;
this module exists for users who want to invoke drift checking outside
the scheduler (e.g. from a cron job or ad-hoc script).
"""
from __future__ import annotations

from memsync.config import Config
from memsync.daemon.scheduler import job_drift_check


def run_drift_check(config: Config) -> None:
    """Run a single drift check immediately, outside the scheduler."""
    job_drift_check(config)
