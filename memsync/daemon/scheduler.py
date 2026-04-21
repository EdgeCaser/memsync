"""
APScheduler wrapper and job definitions for the memsync daemon.

Five jobs:
  nightly_refresh   — reads today's session log and calls the Claude API
  nightly_harvest   — sweeps ~/.claude/projects/ and extracts memories from session transcripts
  backup_mirror     — copies .claude-memory/ to a local mirror path hourly
  drift_check       — checks whether CLAUDE.md is in sync with GLOBAL_MEMORY.md
  weekly_digest     — generates and emails a weekly summary

All jobs return early gracefully when filesystem state is missing rather than
raising. This is load-bearing — see DAEMON_PITFALLS.md #2.
"""
from __future__ import annotations

import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from memsync.config import Config

logger = logging.getLogger("memsync.daemon")


def build_scheduler(
    config: Config, blocking: bool = False
) -> BackgroundScheduler | BlockingScheduler:
    """
    Build and configure the APScheduler instance from config.

    blocking=True  → BlockingScheduler  (foreground / testing)
    blocking=False → BackgroundScheduler (daemon mode, runs in a thread)
    """
    scheduler: BackgroundScheduler | BlockingScheduler = (
        BlockingScheduler() if blocking else BackgroundScheduler()
    )

    if config.daemon.refresh_enabled:
        scheduler.add_job(
            func=job_nightly_refresh,
            trigger=CronTrigger.from_crontab(config.daemon.refresh_schedule),
            args=[config],
            id="nightly_refresh",
            name="Nightly memory refresh",
            misfire_grace_time=3600,  # run even if missed by up to 1 hour
        )

    if config.daemon.harvest_enabled:
        scheduler.add_job(
            func=job_nightly_harvest,
            trigger=CronTrigger.from_crontab(config.daemon.harvest_schedule),
            args=[config],
            id="nightly_harvest",
            name="Nightly session harvest",
            misfire_grace_time=3600,
        )

    if config.daemon.backup_mirror_path:
        scheduler.add_job(
            func=job_backup_mirror,
            trigger=CronTrigger.from_crontab(config.daemon.backup_mirror_schedule),
            args=[config],
            id="backup_mirror",
            name="Backup mirror sync",
            misfire_grace_time=3600,
        )

    if config.daemon.drift_check_enabled:
        scheduler.add_job(
            func=job_drift_check,
            trigger="interval",
            hours=config.daemon.drift_check_interval_hours,
            args=[config],
            id="drift_check",
            name="CLAUDE.md drift check",
        )

    if config.daemon.digest_enabled:
        scheduler.add_job(
            func=job_weekly_digest,
            trigger=CronTrigger.from_crontab(config.daemon.digest_schedule),
            args=[config],
            id="weekly_digest",
            name="Weekly digest email",
        )

    return scheduler


def job_nightly_refresh(config: Config) -> None:
    """
    Read today's session log and run a refresh if there are notes.
    Silently skips if no session log exists for today (normal — rest days happen).
    Never raises — a crash here would take down the whole scheduler.
    """
    from datetime import date

    from memsync.backups import backup
    from memsync.claude_md import sync as sync_claude_md
    from memsync.providers import get_provider
    from memsync.sync import refresh_memory_content

    try:
        provider = get_provider(config.provider)
        sync_root = config.sync_root or provider.detect()
        if not sync_root:
            logger.warning("nightly_refresh: sync_root not found, skipping")
            return

        memory_root = provider.get_memory_root(sync_root)
        today = date.today().strftime("%Y-%m-%d")
        session_log = memory_root / "sessions" / f"{today}.md"

        if not session_log.exists():
            logger.debug("nightly_refresh: no session log for %s, skipping", today)
            return

        notes = session_log.read_text(encoding="utf-8").strip()
        if not notes:
            logger.debug("nightly_refresh: session log empty for %s, skipping", today)
            return

        memory_path = memory_root / "GLOBAL_MEMORY.md"
        if not memory_path.exists():
            logger.warning("nightly_refresh: GLOBAL_MEMORY.md not found, skipping")
            return

        current_memory = memory_path.read_text(encoding="utf-8")
        result = refresh_memory_content(notes, current_memory, config)

        if result["changed"]:
            backup(memory_path, memory_root / "backups")
            memory_path.write_text(result["updated_content"], encoding="utf-8")
            sync_claude_md(memory_path, config.claude_md_target)
            logger.info("nightly_refresh: memory updated for %s", today)
        else:
            logger.info("nightly_refresh: no changes for %s", today)

    except Exception:
        logger.exception("nightly_refresh: unexpected error")


def job_nightly_harvest(config: Config) -> None:
    """
    Sweep all Claude Code project session directories and extract memories from
    any unprocessed session transcripts into GLOBAL_MEMORY.md.

    Silently skips when ~/.claude/projects/ doesn't exist — normal on Pi and CI.
    Processes sessions sequentially so each one builds on the updated memory.
    Never raises — a crash here would take down the whole scheduler.
    """
    from memsync.backups import backup
    from memsync.claude_md import sync as sync_claude_md
    from memsync.harvest import (
        list_sessions,
        load_harvested_index,
        read_session_transcript,
        save_harvested_index,
    )
    from memsync.providers import get_provider
    from memsync.sync import harvest_memory_content, load_or_init_memory

    try:
        provider = get_provider(config.provider)
        sync_root = config.sync_root or provider.detect()
        if not sync_root:
            logger.warning("nightly_harvest: sync_root not found, skipping")
            return

        memory_root = provider.get_memory_root(sync_root)
        memory_path = memory_root / "GLOBAL_MEMORY.md"
        if not memory_path.exists():
            logger.warning("nightly_harvest: GLOBAL_MEMORY.md not found, skipping")
            return

        # Resolve projects dir — configurable so users can point at a mounted/synced path
        projects_dir_str = config.daemon.harvest_projects_dir
        projects_dir = (
            Path(projects_dir_str).expanduser()
            if projects_dir_str
            else Path("~/.claude/projects").expanduser()
        )

        if not projects_dir.exists():
            logger.debug("nightly_harvest: projects dir not found at %s, skipping", projects_dir)
            return

        # Collect all unharvested sessions across all project subdirectories
        harvested = load_harvested_index(memory_root)
        new_sessions: list[Path] = []
        for project_dir in sorted(projects_dir.iterdir()):
            if project_dir.is_dir():
                for session_path in list_sessions(project_dir):
                    if session_path.stem not in harvested:
                        new_sessions.append(session_path)

        if not new_sessions:
            logger.debug("nightly_harvest: no new sessions to process")
            return

        logger.info("nightly_harvest: processing %d new session(s)", len(new_sessions))

        # Process sessions sequentially — each one builds on the updated memory
        current_memory = load_or_init_memory(memory_path)
        changed_any = False

        for session_path in new_sessions:
            transcript, message_count = read_session_transcript(session_path)

            if not transcript.strip():
                harvested[session_path.stem] = message_count  # empty transcripts won't improve on retry
                logger.debug("nightly_harvest: empty transcript in %s, skipping", session_path.stem)
                continue

            try:
                result = harvest_memory_content(transcript, current_memory, config)
            except Exception:
                logger.warning("nightly_harvest: all backends failed for %s — will retry next run", session_path.stem)
                continue  # not marked — will retry on next run

            harvested[session_path.stem] = message_count

            if result["truncated"]:
                logger.warning(
                    "nightly_harvest: response truncated for session %s — skipping write",
                    session_path.stem,
                )
                continue

            if result["changed"]:
                current_memory = result["updated_content"]
                changed_any = True
                backend = result.get("backend", "unknown")
                chunks = result.get("chunks_processed", 1)
                tokens = result.get("input_tokens", 0) + result.get("output_tokens", 0)
                logger.info(
                    "nightly_harvest: updated from %s [%s, %d chunk(s), %d tokens]",
                    session_path.stem, backend, chunks, tokens,
                )

        # Persist index and write memory once after all sessions processed
        save_harvested_index(memory_root, harvested)

        if changed_any:
            backup(memory_path, memory_root / "backups")
            memory_path.write_text(current_memory, encoding="utf-8")
            sync_claude_md(memory_path, config.claude_md_target)
            logger.info("nightly_harvest: memory updated from %d session(s)", len(new_sessions))
        else:
            logger.info("nightly_harvest: no changes from %d session(s)", len(new_sessions))

    except Exception:
        logger.exception("nightly_harvest: unexpected error")


def job_backup_mirror(config: Config) -> None:
    """
    Copy all files from .claude-memory/ to the configured local mirror path.
    Preserves timestamps. Creates the mirror directory if missing.
    Never raises.
    """
    import shutil

    from memsync.providers import get_provider

    try:
        provider = get_provider(config.provider)
        sync_root = config.sync_root or provider.detect()
        if not sync_root:
            logger.warning("backup_mirror: sync_root not found, skipping")
            return

        memory_root = provider.get_memory_root(sync_root)
        mirror = Path(config.daemon.backup_mirror_path).expanduser()
        mirror.mkdir(parents=True, exist_ok=True)

        copied = 0
        for src in memory_root.rglob("*"):
            if src.is_file():
                rel = src.relative_to(memory_root)
                dst = mirror / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1

        logger.info("backup_mirror: copied %d file(s) to %s", copied, mirror)

    except Exception:
        logger.exception("backup_mirror: unexpected error")


def job_drift_check(config: Config) -> None:
    """
    Check if CLAUDE.md is stale relative to GLOBAL_MEMORY.md.
    Fires a notification via the configured channel if out of sync.
    Never raises.
    """
    from memsync.claude_md import is_synced
    from memsync.daemon.notify import notify
    from memsync.providers import get_provider

    try:
        provider = get_provider(config.provider)
        sync_root = config.sync_root or provider.detect()
        if not sync_root:
            return

        memory_root = provider.get_memory_root(sync_root)
        memory_path = memory_root / "GLOBAL_MEMORY.md"

        if not memory_path.exists():
            return

        if not is_synced(memory_path, config.claude_md_target):
            notify(
                config,
                subject="memsync: CLAUDE.md is out of sync",
                body=(
                    f"CLAUDE.md at {config.claude_md_target} does not match "
                    f"GLOBAL_MEMORY.md at {memory_path}.\n"
                    "Run: memsync refresh to resync."
                ),
            )
            logger.warning("drift_check: CLAUDE.md is out of sync")
        else:
            logger.debug("drift_check: CLAUDE.md is in sync")

    except Exception:
        logger.exception("drift_check: unexpected error")


def job_weekly_digest(config: Config) -> None:
    """
    Generate and send a weekly digest of session logs.
    Delegates to memsync.daemon.digest. Never raises.
    """
    from memsync.daemon.digest import generate_and_send

    try:
        generate_and_send(config)
        logger.info("weekly_digest: digest sent")
    except Exception:
        logger.exception("weekly_digest: unexpected error")
