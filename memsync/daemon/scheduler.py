"""
APScheduler wrapper and job definitions for the memsync daemon.

Five jobs:
  nightly_refresh   — reads today's session log and calls the Claude API
  nightly_harvest   — sweeps ~/.claude/projects/ and extracts memories from session transcripts
  backup_mirror     — copies .claude-memory/ to a local mirror path hourly
  drift_check       — checks whether instruction targets are in sync with GLOBAL_MEMORY.md
  weekly_digest     — generates and emails a weekly summary

All jobs return early gracefully when filesystem state is missing rather than
raising. This is load-bearing — see DAEMON_PITFALLS.md #2.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from memsync.config import Config

logger = logging.getLogger("memsync.daemon")


def _daemon_llm_config(config: Config) -> Config:
    """Return daemon-safe LLM config, avoiding local Ollama unless explicitly enabled."""
    if config.daemon.harvest_allow_ollama:
        return config

    backends = [backend for backend in config.llm_backends if backend != "ollama"]
    if backends == config.llm_backends:
        return config

    return dataclasses.replace(
        config,
        llm_backends=backends,
        llm_backend=backends[0] if backends else "none",
        fallback_backend=backends[1] if len(backends) > 1 else "none",
    )


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
        for i, sched in enumerate(config.daemon.refresh_schedule):
            scheduler.add_job(
                func=job_nightly_refresh,
                trigger=CronTrigger.from_crontab(sched),
                args=[config],
                id=f"refresh_{i}",
                name="Memory refresh",
                misfire_grace_time=3600,
                coalesce=True,
                max_instances=1,
            )

    if config.daemon.harvest_enabled:
        scheduler.add_job(
            func=job_nightly_harvest,
            trigger=CronTrigger.from_crontab(config.daemon.harvest_schedule),
            args=[config],
            id="nightly_harvest",
            name="Nightly session harvest",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
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
            name="Instruction target drift check",
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
    from memsync.claude_md import sync_many
    from memsync.config import instruction_targets
    from memsync.providers import get_provider
    from memsync.sync import load_or_init_archive, refresh_memory_content

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

        archive_path = memory_root / "MEMORY_ARCHIVE.md"
        current_memory = memory_path.read_text(encoding="utf-8")
        current_cold = load_or_init_archive(archive_path)
        llm_config = _daemon_llm_config(config)
        result = refresh_memory_content(notes, current_memory, llm_config, current_cold)

        if result["changed"]:
            backup(memory_path, memory_root / "backups")
            memory_path.write_text(result["updated_content"], encoding="utf-8")
            sync_many(memory_path, [path for _, path in instruction_targets(config)])
            if result.get("changed_cold") and result.get("updated_cold"):
                if archive_path.exists():
                    backup(archive_path, memory_root / "backups")
                archive_path.write_text(result["updated_cold"], encoding="utf-8")
            logger.info("nightly_refresh: memory updated for %s", today)
        else:
            logger.info("nightly_refresh: no changes for %s", today)

        # Audit journal — log every refresh attempt, not just successful writes,
        # so the journal also captures no-op nights.
        from memsync.journal import log_transaction
        log_transaction(
            transaction_type="refresh",
            input_data={"session_log": str(session_log), "source": "scheduler"},
            memory_before=current_memory,
            memory_after=result.get("updated_content", current_memory),
            llm_metadata={
                k: v for k, v in result.items() if k not in ("updated_content", "updated_cold")
            },
            journal_dir=str(memory_root / "journal"),
        )

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
    from memsync.claude_md import sync_many
    from memsync.config import instruction_targets
    from memsync.harvest import (
        list_sessions,
        load_harvested_index,
        read_session_transcript,
        save_harvested_index,
    )
    from memsync.providers import get_provider
    from memsync.sync import harvest_sessions_batched, load_or_init_archive, load_or_init_memory

    lock_info = None
    lock_root = None
    try:
        started_at = time.monotonic()
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

        # Advisory cross-machine lock: if another machine is mid-harvest into the
        # shared store, defer this run rather than racing it into conflict copies.
        from memsync.lock import LockHeld, acquire

        try:
            lock_info = acquire(memory_root, config.daemon.harvest_lock_stale_seconds)
            lock_root = memory_root
        except LockHeld as exc:
            logger.warning("nightly_harvest: %s — skipping this run", exc)
            return

        # Resolve projects roots — configurable so users can point at mounted or
        # synced paths, and plural because the machine awake at harvest time is
        # often not the one that produced the transcripts.
        roots = config.daemon.projects_roots()
        present = [root for root in roots if root.exists()]
        if not present:
            logger.debug(
                "nightly_harvest: no projects dir found at %s, skipping",
                ", ".join(str(root) for root in roots),
            )
            return
        for root in roots:
            if not root.exists():
                logger.info("nightly_harvest: skipping missing projects root %s", root)

        # Collect all unharvested sessions across all project subdirectories
        harvested = load_harvested_index(memory_root)
        new_sessions: list[Path] = []
        seen_stems: set[str] = set()
        for root in present:
            for project_dir in sorted(root.iterdir()):
                if project_dir.is_dir():
                    for session_path in list_sessions(project_dir):
                        if session_path.stem in seen_stems:
                            continue
                        seen_stems.add(session_path.stem)
                        if session_path.stem not in harvested:
                            new_sessions.append(session_path)

        if not new_sessions:
            logger.debug("nightly_harvest: no new sessions to process")
            return

        max_sessions = config.daemon.harvest_max_sessions_per_run
        if max_sessions > 0 and len(new_sessions) > max_sessions:
            logger.warning(
                "nightly_harvest: limiting run to %d of %d new session(s)",
                max_sessions,
                len(new_sessions),
            )
            new_sessions = new_sessions[:max_sessions]

        logger.info("nightly_harvest: processing %d new session(s)", len(new_sessions))

        # Batched harvest: extract from every session, then merge ONCE. The merge
        # regenerates the full hot layer (~minutes), so a per-session merge blows
        # the runtime budget — batching keeps it to a single regeneration per run.
        archive_path = memory_root / "MEMORY_ARCHIVE.md"
        current_memory = load_or_init_memory(memory_path)
        current_cold = load_or_init_archive(archive_path)
        changed_any = False
        changed_cold_any = False
        llm_config = _daemon_llm_config(config)

        # Read transcripts (cheap, local) and track message counts for the index.
        sessions: list[tuple[str, str]] = []
        message_counts: dict[str, int] = {}
        for session_path in new_sessions:
            transcript, message_count = read_session_transcript(session_path)
            message_counts[session_path.stem] = message_count
            sessions.append((session_path.stem, transcript))

        # Reserve one merge's worth of time before the runtime budget runs out.
        max_runtime = config.daemon.harvest_max_runtime_seconds
        deadline = (
            started_at + max_runtime - config.claude_code_timeout
            if max_runtime > 0
            else None
        )

        try:
            result = harvest_sessions_batched(
                sessions, current_memory, llm_config, current_cold, deadline=deadline
            )
        except Exception:
            logger.warning(
                "nightly_harvest: batched merge failed — sessions will retry next run"
            )
            result = None

        if result is not None:
            if result.get("truncated") or result.get("malformed"):
                # Don't mark anything — the write is skipped, so let them retry.
                logger.warning(
                    "nightly_harvest: merge response %s — skipping write",
                    "truncated" if result.get("truncated") else "malformed",
                )
            else:
                for sid in result.get("harvested_ids", []):
                    harvested[sid] = message_counts.get(sid, 0)
                if result["changed"]:
                    current_memory = result["updated_content"]
                    changed_any = True
                    if result.get("changed_cold") and result.get("updated_cold"):
                        current_cold = result["updated_cold"]
                        changed_cold_any = True
                    tokens = result.get("input_tokens", 0) + result.get("output_tokens", 0)
                    logger.info(
                        "nightly_harvest: merged %d session(s) [%s, %d tokens]",
                        len(result.get("harvested_ids", [])),
                        result.get("backend", "unknown"),
                        tokens,
                    )

        # Persist index and write memory once after the batch
        save_harvested_index(memory_root, harvested)

        if changed_any:
            backup(memory_path, memory_root / "backups")
            memory_path.write_text(current_memory, encoding="utf-8")
            sync_many(memory_path, [path for _, path in instruction_targets(config)])
            if changed_cold_any:
                if archive_path.exists():
                    backup(archive_path, memory_root / "backups")
                archive_path.write_text(current_cold, encoding="utf-8")
            logger.info("nightly_harvest: memory updated from %d session(s)", len(new_sessions))
        else:
            logger.info("nightly_harvest: no changes from %d session(s)", len(new_sessions))

        # Audit journal — one entry per scheduled sweep summarising the batch.
        from memsync.journal import log_transaction
        log_transaction(
            transaction_type="harvest_all",
            input_data={
                "session_count": len(new_sessions),
                "sessions": [p.stem for p in new_sessions],
                "source": "scheduler",
            },
            memory_before="",
            memory_after=current_memory,
            llm_metadata={"changed": changed_any},
            journal_dir=str(memory_root / "journal"),
        )

    except Exception:
        logger.exception("nightly_harvest: unexpected error")
    finally:
        if lock_info is not None and lock_root is not None:
            from memsync.lock import release

            release(lock_root, lock_info)


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
    Check if any instruction target is stale relative to GLOBAL_MEMORY.md.
    Fires a notification via the configured channel if out of sync.
    Never raises.
    """
    from memsync.claude_md import is_synced
    from memsync.config import instruction_targets
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

        stale = [
            (label, path)
            for label, path in instruction_targets(config)
            if not is_synced(memory_path, path)
        ]
        if stale:
            lines = [
                f"{label} at {path} does not match GLOBAL_MEMORY.md at {memory_path}."
                for label, path in stale
            ]
            notify(
                config,
                subject="memsync: instruction target is out of sync",
                body="\n".join(lines) + "\nRun: memsync refresh to resync.",
            )
            logger.warning(
                "drift_check: out of sync targets: %s",
                ", ".join(label for label, _ in stale),
            )
        else:
            logger.debug("drift_check: all instruction targets are in sync")

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
