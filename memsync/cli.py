from __future__ import annotations

import argparse
import dataclasses
import difflib
import logging
import platform
import shutil
import sys
from pathlib import Path

from memsync import __version__
from memsync.backups import backup, latest_backup, list_backups, prune
from memsync.claude_md import sync_many as sync_instruction_targets
from memsync.config import (
    DEFAULT_LLM_BACKENDS,
    Config,
    get_config_path,
    harvest_chunk_tokens_for_backend,
    instruction_targets,
    normalize_backend_name,
    normalize_backends,
)
from memsync.harvest import (
    HarvestIndexError,
    find_latest_session,
    find_project_dir,
    list_sessions,
    load_harvested_index,
    read_session_transcript,
    save_harvested_index,
)
from memsync.journal import list_prunable_journal, prune_journal
from memsync.llm import LLMError
from memsync.providers import all_providers, auto_detect, get_provider
from memsync.sync import (
    harvest_memory_content,
    harvest_sessions_batched,
    load_or_init_archive,
    load_or_init_memory,
    log_session_notes,
    refresh_memory_content,
)
from memsync.usage import append_usage, format_summary, load_usage, usage_log_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BACKEND_DISPLAY_NAMES = {
    "claude_code": "claude",
}


def _display_backend_name(name: str) -> str:
    canonical = normalize_backend_name(name)
    return _BACKEND_DISPLAY_NAMES.get(canonical, canonical)


def _configured_backends(config: Config) -> list[str]:
    backends = normalize_backends(config.llm_backends)
    if backends:
        return backends
    legacy_backends = normalize_backends([config.llm_backend, config.fallback_backend])
    if legacy_backends:
        return legacy_backends
    return list(DEFAULT_LLM_BACKENDS)


def _instruction_targets(config: Config) -> list[tuple[str, Path]]:
    return instruction_targets(config)


def _sync_instruction_targets(memory_path: Path, config: Config) -> None:
    """
    Point CLAUDE.md / AGENTS.md at whatever should be resident context.

    With projection enabled that is the generated core, not GLOBAL_MEMORY.md —
    the whole point being that per-project detail stops being loaded into every
    session. The projection is rebuilt here so the core can never go stale
    relative to the source it was derived from.
    """
    source = memory_path
    if config.projection_enabled:
        from memsync.projection import (
            build_projection,
            core_path,
            topics_path,
            write_projection,
        )
        projection = build_projection(
            memory_path.read_text(encoding="utf-8"), config, topics_path(config)
        )
        write_projection(projection, config)
        source = core_path(config)

    sync_instruction_targets(source, [path for _, path in _instruction_targets(config)])


def _print_instruction_targets(config: Config, prefix: str = "") -> None:
    for label, target in _instruction_targets(config):
        if target.is_symlink():
            print(f"{prefix}{label:<12} {target} → symlink ✓")
        elif target.exists():
            print(f"{prefix}{label:<12} {target} ✓ (copy)")
        else:
            print(f"{prefix}{label:<12} {target} ✗ (not synced — run memsync init)")


def _scheduled_harvest_config(config: Config) -> Config:
    """Return config for unattended harvest runs, avoiding local Ollama unless enabled."""
    if config.daemon.harvest_allow_ollama:
        return config

    backends = [backend for backend in _configured_backends(config) if backend != "ollama"]
    if backends == _configured_backends(config):
        return config

    return dataclasses.replace(
        config,
        llm_backends=backends,
        llm_backend=backends[0] if backends else "none",
        fallback_backend=backends[1] if len(backends) > 1 else "none",
    )


def _harvest_chunk_summary(config: Config) -> str:
    backends = _configured_backends(config)
    unique = []
    for backend in backends:
        if backend not in unique:
            unique.append(backend)
    if not unique:
        return str(config.harvest_chunk_tokens)
    return ", ".join(
        f"{_display_backend_name(backend)}={harvest_chunk_tokens_for_backend(config, backend)}"
        for backend in unique
    )


def _find_cli_path(command: str) -> str | None:
    import shutil
    import subprocess

    path = shutil.which(command)
    if path or sys.platform != "win32":
        return path

    result = subprocess.run(  # noqa: S603
        ["cmd.exe", "/c", "where", command],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,  # no console-window flash from background jobs
    )
    if result.returncode != 0:
        return None

    matches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return matches[0] if matches else None


def _check_backend_readiness(backend: str, config: Config) -> tuple[bool, str]:
    import os

    backend = normalize_backend_name(backend)

    if backend == "anthropic":
        if config.api_key:
            return True, "anthropic â€” API key set via config"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return (
                True,
                "anthropic â€” API key set via env var"
                " (consider: memsync config set api_key <key>)",
            )
        return False, "anthropic â€” api_key not set; refresh will fail"

    if backend == "codex":
        path = _find_cli_path("codex")
        if path:
            return True, f"codex CLI found at {path}"
        return False, "codex CLI not found â€” install with: npm install -g @openai/codex"

    if backend == "claude_code":
        path = _find_cli_path("claude")
        if path:
            return True, f"claude CLI found at {path}"
        return False, "claude CLI not found â€” install from https://claude.ai/code"

    if backend == "gemini":
        if config.gemini_api_key:
            return True, f"gemini ({config.gemini_model}) â€” API key configured"
        try:
            import google.auth

            google.auth.default(
                scopes=["https://www.googleapis.com/auth/generative-language"]
            )
            return True, f"gemini ({config.gemini_model}) â€” ADC (gcloud credentials)"
        except Exception as err:  # noqa: BLE001
            return False, f"gemini â€” no API key and ADC unavailable ({err})"

    if backend == "gemini_cli":
        path = _find_cli_path("gemini")
        if path:
            return True, f"gemini CLI ({config.gemini_model}) â€” found at {path}"
        return False, "gemini CLI not found â€” install with: npm install -g @google/gemini-cli"

    if backend == "ollama":
        path = _find_cli_path("ollama")
        if path:
            return True, f"ollama CLI found at {path}"
        return False, "ollama CLI not found â€” install from https://ollama.com"

    return False, f"unknown backend '{backend}'"


def _check_llm_waterfall(config: Config) -> tuple[bool, str]:
    details: list[str] = []
    for backend in _configured_backends(config):
        ok, detail = _check_backend_readiness(backend, config)
        details.append(f"{_display_backend_name(backend)}: {detail}")
        if ok:
            return True, detail if len(details) == 1 else "; ".join(details)
    return False, "; ".join(details) or "no backends configured"

def _resolve_memory_root(config: Config) -> Path | None:
    """
    Return the .claude-memory root directory for this machine.
    Uses config.sync_root if set, otherwise asks the configured provider to detect.
    """
    if config.sync_root:
        sync_root = config.sync_root
    else:
        try:
            provider = get_provider(config.provider)
        except KeyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return None
        sync_root = provider.detect()
        if sync_root is None:
            print(
                f"Error: provider '{config.provider}' could not find its sync folder.\n"
                "Run 'memsync init' or set a custom path with:\n"
                "  memsync config set sync_root /path/to/folder",
                file=sys.stderr,
            )
            return None
        provider_instance = provider
        return provider_instance.get_memory_root(sync_root)

    try:
        provider = get_provider(config.provider)
        return provider.get_memory_root(sync_root)
    except KeyError:
        # Custom path with unknown provider name — use default .claude-memory
        return sync_root / ".claude-memory"


def _require_memory_root(config: Config) -> tuple[Path, int] | tuple[None, int]:
    """
    Resolve memory root and check it exists. Returns (path, 0) or (None, exit_code).
    """
    memory_root = _resolve_memory_root(config)
    if memory_root is None:
        return None, 4
    if not memory_root.exists():
        print(
            "Error: memory directory not found. Run 'memsync init' first.",
            file=sys.stderr,
        )
        return None, 2
    return memory_root, 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace, config: Config) -> int:
    """Set up memory structure for the first time."""
    # Check if already initialized (unless --force)
    if get_config_path().exists() and not args.force:
        print("memsync already initialized. Use --force to reinitialize.")
        return 0

    # Resolve provider
    if args.sync_root:
        sync_root = Path(args.sync_root).expanduser()
        if not sync_root.exists():
            print(f"Error: path does not exist: {sync_root}", file=sys.stderr)
            return 1
        provider_name = args.provider or "custom"
        try:
            provider = get_provider(provider_name)
        except KeyError:
            provider = get_provider("custom")
            provider_name = "custom"
        memory_root = provider.get_memory_root(sync_root)

    elif args.provider:
        try:
            provider = get_provider(args.provider)
        except KeyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        sync_root = provider.detect()
        if sync_root is None:
            print(
                f"Error: provider '{args.provider}' could not find its sync folder.\n"
                f"Try: memsync init --sync-root /path/to/folder",
                file=sys.stderr,
            )
            return 4
        memory_root = provider.get_memory_root(sync_root)
        provider_name = args.provider

    else:
        # Auto-detect
        detected = auto_detect()
        if not detected:
            print(
                "Error: no cloud sync folder detected.\n"
                "Run with --sync-root to specify a path manually:\n"
                "  memsync init --sync-root /path/to/sync/folder",
                file=sys.stderr,
            )
            return 4

        if len(detected) == 1:
            provider = detected[0]
            sync_root = provider.detect()
            memory_root = provider.get_memory_root(sync_root)
            provider_name = provider.name
        else:
            # Multiple detected — ask user to choose
            print("Multiple sync providers detected:")
            for i, p in enumerate(detected, 1):
                path = p.detect()
                print(f"  {i}. {p.display_name} ({path})")
            while True:
                choice = input(f"Choose [1-{len(detected)}]: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(detected):
                    provider = detected[int(choice) - 1]
                    sync_root = provider.detect()
                    memory_root = provider.get_memory_root(sync_root)
                    provider_name = provider.name
                    break
                print("Invalid choice.")

    # Create directory structure
    for subdir in (memory_root, memory_root / "backups", memory_root / "sessions"):
        subdir.mkdir(parents=True, exist_ok=True)

    # Write starter memory if not present (--force skips this check)
    global_memory = memory_root / "GLOBAL_MEMORY.md"
    if not global_memory.exists() or args.force:
        starter = load_or_init_memory(Path("/nonexistent/force-new"))
        global_memory.write_text(starter, encoding="utf-8")

    # Create empty archive if not present
    archive_path = memory_root / "MEMORY_ARCHIVE.md"
    if not archive_path.exists():
        archive_path.write_text(
            load_or_init_archive(Path("/nonexistent/force-new")), encoding="utf-8"
        )

    # Write config
    new_config = Config(
        provider=provider_name,
        sync_root=sync_root if provider_name == "custom" else None,
    )
    new_config.save()

    # Sync to instruction targets
    _sync_instruction_targets(global_memory, new_config)

    print("memsync initialized.\n")
    print(f"  Provider:    {provider.display_name}")
    print(f"  Sync root:   {sync_root}")
    print(f"  Memory:      {global_memory}")
    for label, target in _instruction_targets(new_config):
        if target.is_symlink():
            print(f"  {label:<10} {target} → (symlink)")
        else:
            print(f"  {label:<10} {target}")
    print()
    print("Next: edit your memory file, then run:")
    print('  memsync refresh --notes "initial setup complete"')
    return 0


def cmd_refresh(args: argparse.Namespace, config: Config) -> int:
    """Merge session notes into GLOBAL_MEMORY.md via the Claude API."""
    # Gather notes
    notes = ""
    if args.notes:
        notes = args.notes
    elif args.file:
        note_path = Path(args.file)
        if not note_path.exists():
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            return 1
        notes = note_path.read_text(encoding="utf-8")
    else:
        if not sys.stdin.isatty():
            notes = sys.stdin.read()
        else:
            print(
                "Error: provide --notes, --file, or pipe notes via stdin.",
                file=sys.stderr,
            )
            return 1

    if not notes.strip():
        print("Error: notes are empty.", file=sys.stderr)
        return 1

    # Allow one-off model override without touching config
    if args.model:
        config = dataclasses.replace(config, model=args.model)

    # Resolve paths
    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    global_memory = memory_root / "GLOBAL_MEMORY.md"
    if not global_memory.exists():
        print(
            "Error: GLOBAL_MEMORY.md not found. Run 'memsync init' first.",
            file=sys.stderr,
        )
        return 3

    current_memory = load_or_init_memory(global_memory)
    archive_path = memory_root / "MEMORY_ARCHIVE.md"
    current_cold = load_or_init_archive(archive_path)

    print("Refreshing global memory...", end=" ", flush=True)

    try:
        result = refresh_memory_content(notes, current_memory, config, current_cold)
    except LLMError as e:
        print(f"\nError: LLM request failed: {e}", file=sys.stderr)
        return 5

    append_usage(
        memory_root,
        command="refresh",
        model=config.model,
        input_tokens=result.get("input_tokens", 0),
        output_tokens=result.get("output_tokens", 0),
        changed=result.get("changed", False),
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print("\n[DRY RUN] No files written.\n")
        if result["changed"]:
            old_lines = current_memory.strip().splitlines(keepends=True)
            new_lines = result["updated_content"].splitlines(keepends=True)
            diff = difflib.unified_diff(
                old_lines, new_lines, fromfile="hot/current", tofile="hot/updated"
            )
            diff_text = "".join(diff)
            if diff_text:
                print("--- hot diff ---")
                print(diff_text)
            if result.get("changed_cold"):
                old_cold = current_cold.strip().splitlines(keepends=True)
                new_cold = result["updated_cold"].splitlines(keepends=True)
                cold_diff = "".join(difflib.unified_diff(
                    old_cold, new_cold, fromfile="cold/current", tofile="cold/updated"
                ))
                if cold_diff:
                    print("--- cold diff ---")
                    print(cold_diff)
        else:
            print("No changes detected.")
        return 0

    if result["truncated"]:
        print(
            "\nError: API response was truncated (hit max_tokens limit).\n"
            "Memory file was NOT updated. Try reducing your notes or memory file size.",
            file=sys.stderr,
        )
        return 5

    if result.get("malformed"):
        print(
            "\nError: API response does not look like a memory file (missing leading # or <!--).\n"
            "Memory file was NOT updated. The raw response has been printed below for"
            " inspection.\n",
            file=sys.stderr,
        )
        print(result["updated_content"], file=sys.stderr)
        return 6

    if not result["changed"]:
        print("no changes.")
        return 0

    # Backup then write hot layer
    backup_path = backup(global_memory, memory_root / "backups")
    global_memory.write_text(result["updated_content"], encoding="utf-8")
    _sync_instruction_targets(global_memory, config)

    # Write cold layer if changed — always back up first so cold has the same
    # safety net as hot (cf. 2026-05 archive-collapse incident).
    if result.get("changed_cold") and result.get("updated_cold"):
        if archive_path.exists():
            backup(archive_path, memory_root / "backups")
        archive_path.write_text(result["updated_cold"], encoding="utf-8")

    log_session_notes(notes, memory_root / "sessions")

    # Audit journal — see memsync/journal.py
    from memsync.journal import log_transaction
    log_transaction(
        transaction_type="refresh",
        input_data={"notes": notes} if args.notes or not args.file else {"file": str(args.file)},
        memory_before=current_memory,
        memory_after=result["updated_content"],
        llm_metadata={
            k: v for k, v in result.items() if k not in ("updated_content", "updated_cold")
        },
        journal_dir=str(memory_root / "journal"),
    )

    print("done.")
    print(f"  Backup:    {backup_path}")
    hot_lines = len(result["updated_content"].splitlines())
    cold_lines = len(result.get("updated_cold", "").splitlines())
    print(f"  Hot:       {global_memory} ({hot_lines} lines)")
    if result.get("changed_cold"):
        print(f"  Cold:      {archive_path} ({cold_lines} lines)")
    for label, _target in _instruction_targets(config):
        print(f"  {label} synced ✓")
    return 0


def _harvest_all(
    args: argparse.Namespace,
    config: Config,
    memory_root: Path,
    global_memory: Path,
) -> int:
    """Sweep all projects, holding the shared-store lock so two machines don't
    harvest into the same synced store at once (which produces Syncthing
    conflict copies). If another machine is mid-harvest, defer with a friendly
    message rather than racing it."""
    from memsync.lock import DEFAULT_STALE_SECONDS, LockHeld, store_lock

    # A dry run writes nothing, so it neither needs the lock nor should risk
    # leaving one behind. Taking it meant an interrupted preview stranded a lock
    # file that blocked real harvests until it aged out.
    if args.dry_run:
        return _harvest_all_locked(args, config, memory_root, global_memory)

    stale = getattr(
        getattr(config, "daemon", None), "harvest_lock_stale_seconds", DEFAULT_STALE_SECONDS
    )
    try:
        with store_lock(memory_root, stale):
            return _harvest_all_locked(args, config, memory_root, global_memory)
    except LockHeld as exc:
        msg = f"Another harvest is running on {exc.host} (pid {exc.pid}); skipping this run."
        if args.auto:
            logger.warning("harvest: %s", exc)
        else:
            print(msg)
        return 0


def _harvest_all_locked(
    args: argparse.Namespace,
    config: Config,
    memory_root: Path,
    global_memory: Path,
) -> int:
    """Sweep all projects under ~/.claude/projects/ and harvest unprocessed sessions."""
    import time

    started_at = time.monotonic()
    daemon = getattr(config, "daemon", None)
    roots = daemon.projects_roots() if daemon else [Path("~/.claude/projects").expanduser()]

    present = [root for root in roots if root.exists()]
    missing = [root for root in roots if not root.exists()]
    if not present:
        if not args.auto:
            where = ", ".join(str(root) for root in roots)
            print(f"No Claude Code projects directory found at {where}")
        return 0
    # A root that has gone away is reported, not fatal: these point at synced
    # copies of other machines' transcripts, and a peer that is offline or a
    # sync folder that has not appeared yet must not stop the harvest of the
    # roots that are present.
    for root in missing:
        print(f"  Skipping missing projects root {root}", file=sys.stderr)

    try:
        harvested = load_harvested_index(memory_root)
    except HarvestIndexError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(
            "Refusing to harvest: treating a damaged index as empty would "
            "re-harvest every session.",
            file=sys.stderr,
        )
        return 1
    new_sessions: list[Path] = []
    # Session stems are UUIDs, but the same session can appear under two roots
    # when a machine both writes locally and syncs a copy. Harvest it once.
    seen_stems: set[str] = set()
    for root in present:
        for project_dir in sorted(root.iterdir()):
            if not project_dir.is_dir():
                continue
            for session_path in list_sessions(project_dir):
                if session_path.stem in seen_stems:
                    continue
                seen_stems.add(session_path.stem)
                stored = harvested.get(session_path.stem)
                if stored is None:
                    new_sessions.append(session_path)
                elif stored >= 0:
                    # Re-harvest if the session has grown since last harvest
                    _, current_count = read_session_transcript(session_path)
                    if current_count > stored:
                        new_sessions.append(session_path)

    if not new_sessions:
        if not args.auto:
            print("No new sessions to harvest.")
        return 0

    if not args.auto:
        print(f"Found {len(new_sessions)} unprocessed session(s) across all projects.")

    if args.model:
        config = dataclasses.replace(config, model=args.model)

    if args.auto:
        max_sessions = config.daemon.harvest_max_sessions_per_run
        if max_sessions > 0 and len(new_sessions) > max_sessions:
            new_sessions = new_sessions[:max_sessions]
        config = _scheduled_harvest_config(config)

    if args.dry_run:
        # This path ignored --dry-run entirely: the flag parsed, nothing read
        # it, and the sweep ran for real — LLM calls, memory write, commit and
        # push. The most expensive command in the tool had an inert safety flag.
        #
        # Previewing stops here rather than extracting: extraction is the part
        # that costs money and time, and what a preview is actually asked is
        # "what would this touch", which is already known.
        cap = config.daemon.harvest_max_sessions_per_run
        print("\n[DRY RUN] Nothing written, no backend calls made.\n")
        print(f"Would harvest {len(new_sessions)} session(s):")
        for session_path in new_sessions[:20]:
            _, count = read_session_transcript(session_path)
            print(f"  {session_path.stem}  ({count} message(s))")
        if len(new_sessions) > 20:
            print(f"  ... and {len(new_sessions) - 20} more")
        if args.auto and cap > 0 and len(new_sessions) == cap:
            print(f"\n  Capped at harvest_max_sessions_per_run = {cap}.")
        runtime = config.daemon.harvest_max_runtime_seconds
        if runtime > 0:
            print(f"  Runtime budget {runtime}s may stop the run before the last session.")
        return 0

    current_memory = load_or_init_memory(global_memory)
    archive_path = memory_root / "MEMORY_ARCHIVE.md"
    current_cold = load_or_init_archive(archive_path)
    changed_any = False
    changed_cold_any = False
    errors = 0
    updated_count = 0
    error_sessions: list[tuple[str, str]] = []

    # Read transcripts up front — local file reads, cheap relative to any LLM call.
    sessions: list[tuple[str, str]] = []
    message_counts: dict[str, int] = {}
    for session_path in new_sessions:
        transcript, msg_count = read_session_transcript(session_path)
        message_counts[session_path.stem] = msg_count
        sessions.append((session_path.stem, transcript))

    # Batched harvest: extract from every session, then merge ONCE.
    #
    # This used to merge per session, and the merge regenerates the entire hot
    # layer regardless of what the session contained — so a 48-char transcript
    # cost the same ~167s as a 9,665-char one, and 25 sessions could not fit in
    # the 30-minute budget. Measured on the Pi 2026-07-28: extract 3.6s median,
    # merge 102-146s, the merge being 97% of a single-chunk session.
    #
    # The trade is that memory is no longer rebuilt between sessions within a
    # run, so later sessions in a batch cannot see facts extracted from earlier
    # ones. Candidates are merged together in one pass instead, which is where
    # cross-session duplicates get reconciled anyway.
    max_runtime = config.daemon.harvest_max_runtime_seconds if args.auto else 0
    deadline = (
        started_at + max_runtime - config.claude_code_timeout if max_runtime > 0 else None
    )

    def _on_session(sid: str, ext: dict | None, ms: int, status: str) -> None:
        nonlocal errors
        if status == "empty":
            return
        if status == "deferred":
            if not args.auto:
                print(f"  {sid}: deferred to next run (runtime budget).")
            return
        if status == "failed":
            errors += 1
            error_sessions.append((sid, "extract failed — will retry next run"))
            if not args.auto:
                print(f"  {sid}: extract failed — will retry next run.")
            return
        # status == "extracted"
        if not args.auto:
            found = len((ext or {}).get("candidates", "").strip())
            backend = (ext or {}).get("backend", "unknown")
            print(f"  {sid}: extracted {found} chars [{backend}, {ms / 1000:.1f}s]")
        try:
            append_usage(
                memory_root,
                command="harvest",
                model=config.model,
                input_tokens=(ext or {}).get("input_tokens", 0),
                output_tokens=(ext or {}).get("output_tokens", 0),
                session_id=sid,
                changed=False,  # the batch merge decides this; recorded on the run
                backend=(ext or {}).get("backend", ""),
                duration_ms=ms,
            )
        except OSError as e:
            logger.warning("Failed to write usage log: %s", e)

    if not args.auto:
        print(f"Extracting from {len(sessions)} session(s), then merging once...")

    try:
        result = harvest_sessions_batched(
            sessions,
            current_memory,
            config,
            current_cold,
            deadline=deadline,
            on_session=_on_session,
        )
    except LLMError as e:
        print(f"\nError: batched merge failed: {e}", file=sys.stderr)
        errors += 1
        error_sessions.append(("(merge)", f"all backends failed: {str(e)[:120]}"))
        result = None

    if result is not None:
        for sid, reason in result.get("failed_ids", []):
            if not any(existing == sid for existing, _ in error_sessions):
                error_sessions.append((sid, reason))

        if result.get("malformed") or result.get("truncated"):
            # Nothing is marked harvested — the write is skipped, so every
            # session in the batch retries next run rather than being silently
            # dropped after a response that could not be used.
            reason = "malformed" if result.get("malformed") else "truncated"
            errors += 1
            error_sessions.append(("(merge)", f"{reason} response — batch not written"))
            if not args.auto:
                print(f"\nMerge response {reason} — nothing written; batch will retry.")
        else:
            for sid in result.get("harvested_ids", []):
                harvested[sid] = message_counts.get(sid, 0)
            save_harvested_index(memory_root, harvested)
            updated_count = len(result.get("harvested_ids", []))

            try:
                append_usage(
                    memory_root,
                    command="harvest",
                    model=config.model,
                    input_tokens=result.get("input_tokens", 0),
                    output_tokens=result.get("output_tokens", 0),
                    session_id="(merge)",
                    changed=result.get("changed", False),
                    backend=result.get("backend", ""),
                    # The merge alone, timed inside harvest_sessions_batched.
                    # Timing the outer call here recorded extraction and pacing
                    # under the name "merge" — 738s of a 742s run, when the
                    # merge was ~190s of it.
                    duration_ms=result.get("merge_ms", 0),
                )
            except OSError as e:
                logger.warning("Failed to write usage log: %s", e)

            if result["changed"]:
                current_memory = result["updated_content"]
                changed_any = True
                if result.get("changed_cold") and result.get("updated_cold"):
                    current_cold = result["updated_cold"]
                    changed_cold_any = True
                if not args.auto:
                    backend = result.get("backend", "unknown")
                    tokens = result.get("input_tokens", 0) + result.get("output_tokens", 0)
                    print(
                        f"\nMerged {updated_count} session(s) "
                        f"[{backend}, {tokens} tokens]"
                    )

    if changed_any:
        backup_path = backup(global_memory, memory_root / "backups")
        global_memory.write_text(current_memory, encoding="utf-8")
        _sync_instruction_targets(global_memory, config)
        if changed_cold_any:
            if archive_path.exists():
                backup(archive_path, memory_root / "backups")
            archive_path.write_text(current_cold, encoding="utf-8")
        if not args.auto:
            hot_lines = len(current_memory.splitlines())
            print("\ndone.")
            print(f"  Backup:    {backup_path}")
            print(f"  Hot:       {global_memory} ({hot_lines} lines)")
            if changed_cold_any:
                cold_lines = len(current_cold.splitlines())
                print(f"  Cold:      {archive_path} ({cold_lines} lines)")
            for label, _target in _instruction_targets(config):
                print(f"  {label} synced ✓")
    else:
        if not args.auto:
            print("\nNo memory changes.")

    # Audit journal — one entry per --all sweep summarising the batch.
    from memsync.journal import log_transaction
    log_transaction(
        transaction_type="harvest_all",
        input_data={
            "session_count": len(new_sessions),
            "sessions": [p.stem for p in new_sessions],
        },
        memory_before="",  # batch — per-session before/after isn't meaningful here
        memory_after=current_memory,
        llm_metadata={"changed": changed_any, "errors": errors},
        journal_dir=str(memory_root / "journal"),
    )

    # In --auto mode the per-session prints above are suppressed, so emit one
    # summary line when anything notable happened. This gives the scheduled
    # wrapper (and its Slack notification) a non-empty body to report; a clean
    # no-change run still prints nothing so quiet nights stay quiet.
    if args.auto and (changed_any or errors):
        summary = f"harvest: {len(new_sessions)} session(s), {updated_count} updated"
        if errors:
            summary += f", {errors} error(s)"
        print(summary)
        for sid, reason in error_sessions:
            print(f"  - {sid}: {reason}")

    # One line per run, written whatever the outcome. A run that fails every
    # session writes no per-session records at all, which is precisely the shape
    # of a night that dies silently — so the run record is the only thing that
    # can say "it ran, and achieved nothing".
    from memsync.usage import append_run
    append_run(
        memory_root,
        "harvest",
        sessions=len(new_sessions),
        updated=updated_count,
        errors=errors,
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )

    # Exit non-zero only on total failure — a run that errored on some sessions
    # but still updated memory is a partial success, not a failure. One bad
    # session out of many should not raise a "harvest FAILED" alarm.
    return 1 if errors and not changed_any else 0


def cmd_harvest(args: argparse.Namespace, config: Config) -> int:
    """Extract memories from a Claude Code session transcript."""
    import datetime

    # Resolve memory root
    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    global_memory = memory_root / "GLOBAL_MEMORY.md"
    if not global_memory.exists():
        print(
            "Error: GLOBAL_MEMORY.md not found. Run 'memsync init' first.",
            file=sys.stderr,
        )
        return 3

    # --all: sweep every project under ~/.claude/projects/
    if getattr(args, "all", False):
        return _harvest_all(args, config, memory_root, global_memory)

    # Resolve project dir
    if args.project:
        project_dir = Path(args.project).expanduser()
        if not project_dir.exists():
            print(f"Error: project path does not exist: {args.project}", file=sys.stderr)
            return 1
    else:
        project_dir = find_project_dir(Path.cwd())
        if project_dir is None:
            print(
                "Error: no Claude Code session directory found for this project.\n"
                "Try specifying a path with: memsync harvest --project ~/.claude/projects/<key>",
                file=sys.stderr,
            )
            return 4

    # Load harvest index
    try:
        harvested = load_harvested_index(memory_root)
    except HarvestIndexError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Resolve session file
    if args.session:
        session_path = Path(args.session).expanduser()
        if not session_path.exists():
            print(f"Error: session file not found: {args.session}", file=sys.stderr)
            return 1
    else:
        # Always find the latest session — growth check happens below
        session_path = find_latest_session(project_dir, exclude=None)
        if session_path is None:
            if args.auto:
                return 0  # Silent success — nothing to do
            print("No sessions found in project directory.")
            return 0

    # Parse transcript
    transcript, message_count = read_session_transcript(session_path)
    if not transcript.strip():
        if args.auto:
            return 0
        print("Session transcript is empty — nothing to harvest.")
        return 0

    # Skip if already harvested and session hasn't grown since last harvest
    if not args.force and session_path.stem in harvested:
        stored_count = harvested[session_path.stem]
        # stored_count == -1: old index format, count unknown — treat as already done
        if stored_count < 0 or message_count <= stored_count:
            if args.auto:
                return 0
            print(
                f"No new messages since last harvest ({message_count} messages). "
                "Use --force to re-harvest."
            )
            return 0

    # Confirmation prompt (skipped in --auto mode)
    if not args.auto:
        mtime = datetime.datetime.fromtimestamp(session_path.stat().st_mtime)
        mtime_str = mtime.strftime("%Y-%m-%d %H:%M")
        print(f"Session: {session_path.stem}")
        print(f"Date:     {mtime_str}")
        print(f"Messages: {message_count}")
        answer = input("Harvest this session? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return 0

    # Allow one-off model override
    if args.model:
        config = dataclasses.replace(config, model=args.model)

    current_memory = load_or_init_memory(global_memory)
    archive_path = memory_root / "MEMORY_ARCHIVE.md"
    current_cold = load_or_init_archive(archive_path)

    if not args.auto:
        print("Harvesting session...", end=" ", flush=True)

    try:
        result = harvest_memory_content(transcript, current_memory, config, current_cold)
    except LLMError as e:
        print(f"\nError: LLM request failed: {e}", file=sys.stderr)
        return 5

    append_usage(
        memory_root,
        command="harvest",
        model=config.model,
        input_tokens=result.get("input_tokens", 0),
        output_tokens=result.get("output_tokens", 0),
        session_id=session_path.stem,
        changed=result.get("changed", False),
        dry_run=args.dry_run,
    )

    if args.dry_run:
        import difflib
        print("\n[DRY RUN] No files written.\n")
        if result["changed"]:
            old_lines = current_memory.strip().splitlines(keepends=True)
            new_lines = result["updated_content"].splitlines(keepends=True)
            diff = difflib.unified_diff(
                old_lines, new_lines, fromfile="hot/current", tofile="hot/harvested"
            )
            diff_text = "".join(diff)
            if diff_text:
                print("--- hot diff ---")
                print(diff_text)
            if result.get("changed_cold"):
                old_cold = current_cold.strip().splitlines(keepends=True)
                new_cold = result["updated_cold"].splitlines(keepends=True)
                cold_diff = "".join(difflib.unified_diff(
                    old_cold, new_cold, fromfile="cold/current", tofile="cold/harvested"
                ))
                if cold_diff:
                    print("--- cold diff ---")
                    print(cold_diff)
        else:
            print("No changes detected.")
        return 0

    if result["truncated"]:
        print(
            "\nError: API response was truncated (hit max_tokens limit).\n"
            "Memory file was NOT updated.",
            file=sys.stderr,
        )
        return 5

    if result.get("malformed"):
        print(
            "\nError: API response does not look like a memory file (missing leading # or <!--).\n"
            "Memory file was NOT updated. The raw response has been printed below for"
            " inspection.\n",
            file=sys.stderr,
        )
        print(result["updated_content"], file=sys.stderr)
        return 6

    # Mark session as harvested with current message count
    harvested[session_path.stem] = message_count
    save_harvested_index(memory_root, harvested)

    if not result["changed"]:
        if not args.auto:
            print("no changes.")
        return 0

    backup_path = backup(global_memory, memory_root / "backups")
    global_memory.write_text(result["updated_content"], encoding="utf-8")
    _sync_instruction_targets(global_memory, config)

    # Back up cold before overwrite — same safety net as hot.
    if result.get("changed_cold") and result.get("updated_cold"):
        if archive_path.exists():
            backup(archive_path, memory_root / "backups")
        archive_path.write_text(result["updated_cold"], encoding="utf-8")

    # Audit journal — see memsync/journal.py
    from memsync.journal import log_transaction
    log_transaction(
        transaction_type="harvest",
        input_data={"session_path": str(session_path)},
        memory_before=current_memory,
        memory_after=result["updated_content"],
        llm_metadata={
            k: v for k, v in result.items() if k not in ("updated_content", "updated_cold")
        },
        journal_dir=str(memory_root / "journal"),
    )

    if not args.auto:
        print("done.")
        print(f"  Backup:    {backup_path}")
        hot_lines = len(result["updated_content"].splitlines())
        print(f"  Hot:       {global_memory} ({hot_lines} lines)")
        if result.get("changed_cold"):
            cold_lines = len(result.get("updated_cold", "").splitlines())
            print(f"  Cold:      {archive_path} ({cold_lines} lines)")
        for label, _target in _instruction_targets(config):
            print(f"  {label} synced ✓")

    return 0


def cmd_usage(args: argparse.Namespace, config: Config) -> int:
    """Show API usage and estimated cost across all machines."""
    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    log_path = usage_log_path(memory_root)
    entries = load_usage(memory_root)
    print(f"Usage log: {log_path}")
    print(f"Entries:   {len(entries)}\n")
    print(format_summary(entries))
    return 0


def cmd_telemetry(args: argparse.Namespace, config: Config) -> int:
    """Show recent run outcomes and per-backend latencies."""
    from memsync.usage import format_telemetry

    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code
    print(format_telemetry(memory_root, limit=getattr(args, "limit", 10)))
    return 0


def probe_backends(config: Config) -> list[tuple[str, bool, str]]:
    """
    Ask every configured backend for one short answer, and time it.

    `doctor` otherwise checks only that a backend is *installed*, which is a
    different question from whether it *works*: on 2026-07-27 all four were
    installed and all four were dead — a model the provider had retired, two
    expired credentials, and a timeout too short to ever succeed. Readiness
    checks reported everything fine. Finding the truth took hours; this makes
    it one command, at the cost of a real call per backend.
    """
    import time as _time

    from memsync.llm import call_llm_with_backend

    results: list[tuple[str, bool, str]] = []
    for backend in _configured_backends(config):
        started = _time.monotonic()
        try:
            call_llm_with_backend(
                backend, "Reply with exactly: OK", "Reply with exactly: OK", "", config
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                (backend, False, " ".join(str(exc).split())[:110])
            )
        else:
            results.append(
                (backend, True, f"answered in {_time.monotonic() - started:.1f}s")
            )
    return results


def cmd_show(args: argparse.Namespace, config: Config) -> int:
    """Print current GLOBAL_MEMORY.md (or MEMORY_ARCHIVE.md with --archive) to stdout."""
    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    if getattr(args, "archive", False):
        archive_path = memory_root / "MEMORY_ARCHIVE.md"
        if not archive_path.exists():
            print("No archive yet. Run a harvest or refresh first.", file=sys.stderr)
            return 3
        print(archive_path.read_text(encoding="utf-8"))
        return 0

    global_memory = memory_root / "GLOBAL_MEMORY.md"
    if not global_memory.exists():
        print("No global memory file yet. Run: memsync init", file=sys.stderr)
        return 3

    print(global_memory.read_text(encoding="utf-8"))
    return 0


def cmd_diff(args: argparse.Namespace, config: Config) -> int:
    """Show unified diff between current memory and the most recent (or specified) backup."""
    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    global_memory = memory_root / "GLOBAL_MEMORY.md"
    if not global_memory.exists():
        print("No global memory file yet. Run: memsync init", file=sys.stderr)
        return 3

    backup_dir = memory_root / "backups"

    if args.backup:
        backup_path = backup_dir / args.backup
        if not backup_path.exists():
            print(f"Error: backup not found: {args.backup}", file=sys.stderr)
            return 1
    else:
        backup_path = latest_backup(backup_dir)
        if backup_path is None:
            print("No backups found.")
            return 0

    current = global_memory.read_text(encoding="utf-8").splitlines(keepends=True)
    previous = backup_path.read_text(encoding="utf-8").splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        previous, current,
        fromfile=f"backup ({backup_path.name})",
        tofile="current",
    ))

    if diff:
        print("".join(diff))
    else:
        print("No differences from last backup.")
    return 0


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    """Show paths, provider, and sync state."""
    system = platform.system()
    _os_names = {"Darwin": "macOS (Darwin)", "Windows": "Windows", "Linux": "Linux"}
    os_name = _os_names.get(system, system)
    print(f"Platform:      {os_name}")

    config_path = get_config_path()
    config_marker = "✓" if config_path.exists() else "✗ (not found — run memsync init)"
    print(f"Config:        {config_path} {config_marker}")
    print(f"Provider:      {config.provider}")
    backends = _configured_backends(config)
    print(f"LLM backend:   {_display_backend_name(backends[0])}")
    print(f"LLM waterfall: {' -> '.join(_display_backend_name(name) for name in backends)}")
    print(f"Harvesting:    chunks {_harvest_chunk_summary(config)}")

    targets: list[str] = []
    if "anthropic" in backends:
        targets.append(f"anthropic/{config.model}")
    if "gemini" in backends or "gemini_cli" in backends:
        targets.append(f"gemini/{config.gemini_model}")
    if "ollama" in backends:
        targets.append(f"ollama/{config.ollama_model}")
    if targets:
        print(f"LLM targets:   {', '.join(targets)}")

    memory_root = _resolve_memory_root(config)
    if memory_root is None:
        return 4

    if config.sync_root:
        print(f"Sync root:     {config.sync_root} {'✓' if config.sync_root.exists() else '✗'}")
    else:
        try:
            provider = get_provider(config.provider)
            sync_root = provider.detect()
            label = str(sync_root) if sync_root else "(not detected)"
            marker = "✓" if sync_root else "✗"
            print(f"Sync root:     {label} {marker}")
        except KeyError:
            print(f"Sync root:     (unknown provider '{config.provider}')")

    global_memory = memory_root / "GLOBAL_MEMORY.md"
    mem_marker = "✓" if global_memory.exists() else "✗ (run memsync init)"
    hot_lines = (
        len(global_memory.read_text(encoding="utf-8").splitlines())
        if global_memory.exists()
        else 0
    )
    print(f"Memory (hot):  {global_memory} {mem_marker} ({hot_lines} lines)")

    if config.projection_enabled and global_memory.exists():
        # Cheap: rebuilds the projection in memory, writes nothing.
        from memsync.projection import (
            build_projection,
            check_constraints_budget,
            topics_path,
        )
        try:
            _projection = build_projection(
                global_memory.read_text(encoding="utf-8"), config, topics_path(config)
            )
            _warning = check_constraints_budget(_projection, config)
        except Exception:  # noqa: BLE001 — status must report, never fail
            logger.debug("status: constraint budget check failed", exc_info=True)
            _warning = None
        if _warning:
            print(f"⚠ Constraints:  {_warning}")

    archive_path = memory_root / "MEMORY_ARCHIVE.md"
    if archive_path.exists():
        cold_lines = len(archive_path.read_text(encoding="utf-8").splitlines())
        print(f"Archive (cold):{archive_path} ✓ ({cold_lines} lines)")

    for label, target in _instruction_targets(config):
        if target.is_symlink():
            print(f"{label:<13}{target} → symlink ✓")
        elif target.exists():
            print(f"{label:<13}{target} ✓ (copy)")
        else:
            print(f"{label:<13}{target} ✗ (not synced — run memsync init)")

    backup_dir = memory_root / "backups"
    if backup_dir.exists():
        count = len(list_backups(backup_dir))
        print(f"Backups:       {count} file(s)")

    session_dir = memory_root / "sessions"
    if session_dir.exists():
        sessions = list(session_dir.glob("*.md"))
        print(f"Session logs:  {len(sessions)} day(s)")

    _print_harvest_health(config, memory_root)

    return 0


def _print_harvest_health(config: Config, memory_root: Path) -> None:
    """
    Say when a harvest last actually wrote something, and complain if that was
    a while ago.

    This exists because two consecutive nightly harvests died without anyone
    noticing. Every backend was down, the Slack alert meant to catch exactly
    that was itself failing silently, and nothing else in the tool distinguished
    "quiet because there was nothing to do" from "quiet because it is broken".
    A status command that cannot answer "is this thing still running" is not
    reporting status.
    """
    from datetime import UTC, datetime

    from memsync.usage import last_successful_harvest

    latest = last_successful_harvest(memory_root)
    if latest is None:
        print("Last harvest:  never — no harvest has written to this memory")
        return

    machine, ts = latest
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    hours = (datetime.now(UTC) - ts).total_seconds() / 3600
    when = f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f} days ago"

    stale_after = getattr(getattr(config, "daemon", None), "harvest_stale_hours", 36)
    mark = " ⚠ STALE" if stale_after and hours > stale_after else " ✓"
    print(f"Last harvest:  {ts:%Y-%m-%d %H:%M} on {machine} ({when}){mark}")
    if stale_after and hours > stale_after:
        print(f"               No successful harvest in {stale_after}h. Check the "
              f"harvest log and backend auth.")


# Commands that write the shared store, and so are worth a commit. `project`
# is absent on purpose: its output is machine-local and regenerated, so
# committing it would version a derived artefact.
_STORE_WRITERS = frozenset({"init", "refresh", "harvest", "dedup", "prune"})


def _snapshot_store(config: Config, memory_root: Path, message: str) -> None:
    """
    Record a store write in git history, if the store is a repo and git is on.

    Best-effort by design: the memory write has already happened and succeeded,
    so a bookkeeping failure is reported, not raised.

    Commit, then pull, then push. The order is not cosmetic: this runs directly
    after a memory write, so the store is dirty every time, and `git pull
    --rebase` refuses to run over unstaged changes. Pulling first therefore
    never pulled — each machine only ever pushed, and the divergence surfaced
    days later as a rejected push nobody was watching for.
    """
    if not config.git_enabled:
        return
    from memsync import store

    commit = store.snapshot(memory_root, message)
    if commit:
        print(f"  store: committed {commit}")

    if not config.git_autosync:
        return

    try:
        store.pull(memory_root)
    except store.StoreError as exc:
        # A push over a divergence git could not rebase would be rejected
        # anyway. One clear failure beats two.
        print(f"  store: pull skipped — {exc}", file=sys.stderr)
        return

    # Not gated on `commit`: a push that failed earlier — network down, remote
    # unreachable — leaves this machine ahead with nothing new to commit, and
    # gating here stranded that work until some later run happened to write.
    if store.status(memory_root).ahead:
        try:
            print(f"  store: {store.push(memory_root)}")
        except store.StoreError as exc:
            print(f"  store: push failed — {exc}", file=sys.stderr)


def cmd_store(args: argparse.Namespace, config: Config) -> int:
    """Inspect or initialise git-backed history for the memory store."""
    from memsync import store

    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    action = getattr(args, "store_command", "status")

    if action == "init":
        try:
            steps = store.init_repo(memory_root, allow_syncthing=args.allow_syncthing)
        except store.StoreError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Store at {memory_root}:")
        for step in steps:
            print(f"  {step}")
        print(
            "\nNo remote is configured, so history is local only. Add one with:\n"
            "  git -C <store> remote add origin <url>\n"
            "Then enable: memsync config set git_enabled true"
        )
        return 0

    if action == "sync":
        if not store.is_repo(memory_root):
            print("Error: store is not a git repository. Run 'memsync store init'.",
                  file=sys.stderr)
            return 1
        try:
            # Commit first for the same reason autosync does: this is the
            # command someone runs after hand-editing the memory, and a rebase
            # will not start over uncommitted changes.
            commit = store.snapshot(memory_root, "memsync: manual sync")
            if commit:
                print(f"  committed {commit}")
            print(f"  {store.pull(memory_root)}")
            print(f"  {store.push(memory_root)}")
        except store.StoreError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    st = store.status(memory_root)
    print(f"Store:        {memory_root}")
    print(f"Git:          {'repository' if st.is_repo else 'not a repository'}")
    if st.is_repo:
        print(f"Branch:       {st.branch}{' (uncommitted changes)' if st.dirty else ''}")
        print(f"Remote:       {st.remote or 'none — history is local only'}")
        if st.remote:
            print(f"Divergence:   {st.ahead} ahead, {st.behind} behind")
    print(f"git_enabled:  {str(config.git_enabled).lower()}")

    markers = store.syncthing_markers(memory_root)
    if markers:
        print("\n  ! This store is inside a Syncthing folder.")
        print(f"    {', '.join(markers)}")
        print("    Git and file-level sync must not both manage this directory:")
        print("    Syncthing replicating .git/ corrupts the repository.")
    if st.conflict_files:
        print(f"\n  ! {st.conflict_files} Syncthing conflict file(s) present.")
        print("    Run 'memsync store conflicts' to see what they contain.")
    return 0


def cmd_store_conflicts(args: argparse.Namespace, config: Config) -> int:
    """
    Report what Syncthing's conflict copies hold that the live files do not.

    The question worth answering before deleting them is not how many there are
    but whether anything was lost when Syncthing forked the file — a fork is
    silent, so nobody has checked.
    """
    from memsync import store
    from memsync.sync import _constraint_is_superseded, _normalize_bullet

    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    conflicts = store.conflict_files(memory_root)
    if not conflicts:
        print("No Syncthing conflict files found.")
        return 0

    # Compare against every live memory file, not just the forked one's
    # counterpart. Content migrates hot -> cold as a normal part of harvest, so
    # a line demoted to the archive is still present in the store even though
    # it has left GLOBAL_MEMORY.md. Checking only the counterpart reports those
    # demotions as losses.
    live_corpus: list[str] = []
    for candidate in sorted(memory_root.glob("*.md")):
        if ".sync-conflict-" in candidate.name:
            continue
        try:
            live_corpus.append(candidate.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    corpus_norm = [
        (n, set(n.split()))
        for n in (
            _normalize_bullet(ln.strip())
            for text in live_corpus
            for ln in text.splitlines()
            if ln.strip()[:1] in ("-", "*", "+")
        )
        if n
    ]

    print(f"{len(conflicts)} conflict file(s) in {memory_root}\n")
    total_unique = 0
    for path in conflicts:
        # "GLOBAL_MEMORY.sync-conflict-20260715-085323-ZJ5IFMP.md" -> "GLOBAL_MEMORY.md"
        stem = path.name.split(".sync-conflict-")[0]
        live = memory_root / f"{stem}{path.suffix}"
        if not live.exists():
            print(f"  {path.name}: no live counterpart at {live.name}")
            continue
        try:
            conflict_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  {path.name}: unreadable — {exc}")
            continue

        # Coverage, not exact match. Most of what a fork "loses" is a line the
        # store also has in reworded or extended form, and counting those as
        # missing turns a handful of real losses into hundreds of false ones —
        # worse than not checking, because nobody reads a report that cries wolf.
        unique = [
            ln.strip()
            for ln in conflict_text.splitlines()
            if ln.strip()[:1] in ("-", "*", "+")
            and not _constraint_is_superseded(ln.strip(), corpus_norm)
        ]
        total_unique += len(unique)
        print(f"  {path.name}")
        print(f"    {len(unique)} line(s) not found anywhere in the live store")
        for line in unique[:5]:
            print(f"      {line[:130]}")
        if len(unique) > 5:
            print(f"      ... and {len(unique) - 5} more")

    if total_unique == 0:
        print("\nNothing in these files is missing from the live store — safe to delete.")
    else:
        print(
            f"\n{total_unique} line(s) exist only in conflict copies. Review before "
            "deleting; memsync will not remove them for you."
        )
    return 0


def cmd_project(args: argparse.Namespace, config: Config) -> int:
    """Generate the always-resident core and on-demand topic files."""
    from memsync.projection import (
        CORE_DIRNAME,
        build_projection,
        build_skill_description,
        check_budget,
        check_constraints_budget,
        core_path,
        skill_root,
        topics_path,
        write_projection,
    )

    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    global_memory = memory_root / "GLOBAL_MEMORY.md"
    if not global_memory.exists():
        print("Error: GLOBAL_MEMORY.md not found. Run 'memsync init' first.", file=sys.stderr)
        return 3

    original = global_memory.read_text(encoding="utf-8")
    projection = build_projection(original, config, topics_path(config))

    orig_chars = len(original)
    print(
        f"Hot layer:  {orig_chars:,} chars, {len(original.splitlines())} lines\n"
        f"Core:       {projection.core_chars:,} chars, {projection.core_lines} lines\n"
        f"Topics:     {len(projection.topics)} file(s), "
        f"{sum(len(t.content) for t in projection.topics):,} chars moved out of context"
    )
    if config.skill_enabled:
        desc = build_skill_description(projection.topics)
        print(
            f"Skill:      {config.skill_name}, routing description "
            f"{len(desc):,}/1,536 chars (the only part resident at session start)"
        )
    if orig_chars:
        pct = 100 * (orig_chars - projection.core_chars) / orig_chars
        print(f"Resident context reduced by {pct:.0f}%.")

    constraints_warning = check_constraints_budget(projection, config)
    if constraints_warning:
        print(f"  ! {constraints_warning}", file=sys.stderr)

    problems = check_budget(projection, config)
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    if problems:
        # Naming the biggest sections is the actionable part — "over budget" on
        # its own leaves the user guessing at what to cut.
        print("\n  Largest content in the core:", file=sys.stderr)
        from memsync.projection import split_sections
        _, sections = split_sections(projection.core)
        for section in sorted(sections, key=lambda s: -len(s.text))[:3]:
            print(f"    {len(section.text):>7,} chars  {section.title}", file=sys.stderr)
        print(
            "\n  Try 'memsync dedup --subsumed' first; it retires constraint "
            "bullets whose rule a longer bullet already states.",
            file=sys.stderr,
        )

    if args.dry_run:
        print("\n[DRY RUN] Nothing written. Re-run without --dry-run to write.")
        return 1 if problems else 0

    if problems and not args.force:
        print(
            "\nRefusing to write an over-budget core. Use --force to write anyway.",
            file=sys.stderr,
        )
        return 1

    written = write_projection(projection, config)
    print(f"\nWrote {len(written)} file(s):")
    print(f"  {core_path(config)}")
    print(f"  {topics_path(config) / '*.md'}  ({len(projection.topics)} topics)")
    if config.skill_enabled:
        print(f"  {skill_root(config) / 'SKILL.md'}")

    # Generated output used to land in the synced store, where its absolute
    # paths were wrong on every other machine and each rewrite synced back as a
    # change. Clear the old location so the two copies can't diverge.
    legacy = memory_root / CORE_DIRNAME
    if legacy.is_dir():
        shutil.rmtree(legacy)
        print(f"  removed legacy synced copy at {legacy}")

    if config.projection_enabled:
        _sync_instruction_targets(global_memory, config)
        for label, _target in _instruction_targets(config):
            print(f"  {label} synced ✓")
    else:
        print(
            "\nprojection_enabled is false, so CLAUDE.md still syncs from "
            "GLOBAL_MEMORY.md.\nEnable with: memsync config set projection_enabled true"
        )
    return 0


def cmd_dedup(args: argparse.Namespace, config: Config) -> int:
    """Remove duplicate bullet lines from memory layers.

    Default: fuzzy Python pass (free, no LLM).
    --semantic: LLM pass that also catches same-policy bullets phrased differently.
                Implies --dry-run unless --apply is also given.
    """
    from memsync.sync import _deduplicate_memory

    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    global_memory = memory_root / "GLOBAL_MEMORY.md"
    if not global_memory.exists():
        print("Error: GLOBAL_MEMORY.md not found. Run 'memsync init' first.", file=sys.stderr)
        return 3

    # ── Semantic (LLM) pass ──────────────────────────────────────────────────
    if getattr(args, "semantic", False):
        from memsync.backups import backup
        from memsync.sync import semantic_dedupe_memory

        print("Running semantic dedupe (LLM pass) on hot layer…")
        original = global_memory.read_text(encoding="utf-8")
        try:
            cleaned = semantic_dedupe_memory(original, config)
        except Exception as e:  # noqa: BLE001
            print(f"Error: semantic dedupe failed — {e}", file=sys.stderr)
            return 1

        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            cleaned.splitlines(keepends=True),
            fromfile="hot/current",
            tofile="hot/semantic-deduped",
        )
        diff_text = "".join(diff)

        if not diff_text:
            print("No semantic duplicates found.")
            return 0

        print(diff_text)
        apply = getattr(args, "apply", False)
        if not apply:
            orig_lines = len(original.splitlines())
            clean_lines = len(cleaned.splitlines())
            print(
                f"\n[DRY RUN] {orig_lines} → {clean_lines} lines "
                f"({orig_lines - clean_lines} removed). "
                "Re-run with --semantic --apply to write."
            )
            return 0

        backup(global_memory, memory_root / "backups")
        global_memory.write_text(cleaned, encoding="utf-8")
        _sync_instruction_targets(global_memory, config)
        orig_lines = len(original.splitlines())
        clean_lines = len(cleaned.splitlines())
        print(
            f"\nApplied: {orig_lines} → {clean_lines} lines "
            f"({orig_lines - clean_lines} removed)."
        )
        for label, _target in _instruction_targets(config):
            print(f"  {label} synced ✓")
        return 0

    # ── Subsumption pass (deterministic, no LLM) ─────────────────────────────
    if getattr(args, "subsumed", False):
        from memsync.backups import backup
        from memsync.sync import retire_subsumed_bullets

        original = global_memory.read_text(encoding="utf-8")
        cleaned, retired = retire_subsumed_bullets(original)
        if not retired:
            print("No subsumed bullets found.")
            return 0

        print(f"{len(retired)} bullet(s) are fully covered by a longer bullet:\n")
        for bullet in retired:
            print(f"  - {bullet[:150]}")
        saved = len(original) - len(cleaned)
        print(
            f"\n{len(original):,} → {len(cleaned):,} chars ({saved:,} removed), "
            f"{len(original.splitlines())} → {len(cleaned.splitlines())} lines."
        )

        if args.dry_run:
            print("\n[DRY RUN] Nothing written. Re-run without --dry-run to write.")
            return 0

        backup(global_memory, memory_root / "backups")
        global_memory.write_text(cleaned, encoding="utf-8")
        _sync_instruction_targets(global_memory, config)
        print("\nApplied.")
        for label, _target in _instruction_targets(config):
            print(f"  {label} synced ✓")
        return 0

    # ── Fuzzy Python pass (default) ──────────────────────────────────────────
    targets = [(global_memory, "hot")]
    archive_path = memory_root / "MEMORY_ARCHIVE.md"
    if archive_path.exists():
        targets.append((archive_path, "cold"))

    total_removed = 0
    for file_path, label in targets:
        original = file_path.read_text(encoding="utf-8")
        deduped = _deduplicate_memory(original, fuzzy=True)
        orig_lines = len(original.splitlines())
        dedup_lines = len(deduped.splitlines())
        removed = orig_lines - dedup_lines

        if args.dry_run:
            print(
                f"[DRY RUN] {label}: would remove {removed} duplicate(s) "
                f"({orig_lines} → {dedup_lines} lines)."
            )
            if removed > 0:
                diff = difflib.unified_diff(
                    original.splitlines(keepends=True), deduped.splitlines(keepends=True),
                    fromfile=f"{label}/current", tofile=f"{label}/deduped",
                )
                diff_text = "".join(diff)
                if diff_text:
                    print(diff_text)
        else:
            total_removed += removed
            if removed > 0:
                file_path.write_text(deduped, encoding="utf-8")
                print(
                    f"  {label}: removed {removed} duplicate(s) "
                    f"({orig_lines} → {dedup_lines} lines)."
                )
            else:
                print(f"  {label}: no duplicates found.")

    if args.dry_run:
        return 0

    if total_removed > 0:
        _sync_instruction_targets(global_memory, config)
        for label, _target in _instruction_targets(config):
            print(f"  {label} synced ✓")
    return 0


def cmd_prune(args: argparse.Namespace, config: Config) -> int:
    """Remove old backups."""
    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    backup_dir = memory_root / "backups"
    journal_dir = memory_root / "journal"
    keep_days = args.keep_days if args.keep_days is not None else config.keep_days

    if args.dry_run:
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(days=keep_days)
        would_delete = [
            b for b in list_backups(backup_dir)
            if _backup_timestamp(b) and _backup_timestamp(b) < cutoff
        ]
        if would_delete:
            n = len(would_delete)
            print(f"[DRY RUN] Would prune {n} backup(s) older than {keep_days} days:")
            for p in would_delete:
                print(f"  {p.name}")
        else:
            print(f"[DRY RUN] No backups older than {keep_days} days.")
        would_delete_journal = list_prunable_journal(journal_dir, keep_days)
        if would_delete_journal:
            n = len(would_delete_journal)
            noun = "entry" if n == 1 else "entries"
            print(f"[DRY RUN] Would prune {n} journal {noun} older than {keep_days} days.")
        else:
            print(f"[DRY RUN] No journal entries older than {keep_days} days.")
        return 0

    deleted = prune(backup_dir, keep_days=keep_days)
    if deleted:
        print(f"Pruned {len(deleted)} backup(s) older than {keep_days} days.")
        for p in deleted:
            print(f"  removed: {p.name}")
    else:
        print(f"No backups older than {keep_days} days.")

    deleted_journal = prune_journal(journal_dir, keep_days=keep_days)
    if deleted_journal:
        n = len(deleted_journal)
        noun = "entry" if n == 1 else "entries"
        print(f"Pruned {n} journal {noun} older than {keep_days} days.")
    else:
        print(f"No journal entries older than {keep_days} days.")
    return 0


def _backup_timestamp(path: Path):
    """Parse timestamp from backup filename, or return None."""
    from datetime import datetime
    try:
        ts_str = path.stem.replace("GLOBAL_MEMORY_", "")
        return datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def cmd_providers(args: argparse.Namespace, config: Config) -> int:
    """List all registered providers and their detection status."""
    print("Available providers:\n")
    for provider in all_providers():
        detected_path = provider.detect()
        if detected_path:
            marker = f"✓ detected at {detected_path}"
        else:
            if provider.name == "custom":
                marker = "✗ no path configured"
            else:
                marker = "✗ not detected"
        print(f"  {provider.name:<10} {provider.display_name:<18} {marker}")

    print(f"\nActive provider: {config.provider}")
    return 0


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    """
    Self-check: verify the installation is healthy without making any API calls.
    Exits 0 if all checks pass, 1 if any check fails.
    """
    checks: list[tuple[str, bool, str]] = []  # (label, ok, detail)

    # 1. Config file
    config_path = get_config_path()
    checks.append(("Config file", config_path.exists(), str(config_path)))

    # 2. LLM waterfall availability
    llm_ok, llm_detail = _check_llm_waterfall(config)
    checks.append(("LLM / waterfall", llm_ok, llm_detail))

    # 3. Provider / sync root accessible
    if config.sync_root:
        # Custom or explicit path — just verify it exists
        provider_ok = config.sync_root.exists()
        provider_detail = str(config.sync_root)
    else:
        try:
            provider = get_provider(config.provider)
            sync_root = provider.detect()
            provider_ok = sync_root is not None
            provider_detail = (
                str(sync_root) if sync_root else f"'{config.provider}' not detected on this machine"
            )
        except KeyError:
            provider_ok = False
            provider_detail = f"unknown provider '{config.provider}'"
    checks.append((f"Provider ({config.provider})", provider_ok, provider_detail))

    # 4. Memory root exists
    memory_root = _resolve_memory_root(config)
    if memory_root:
        mem_ok = memory_root.exists()
        checks.append(("Memory directory", mem_ok, str(memory_root)))

        # 5. GLOBAL_MEMORY.md exists
        global_memory = memory_root / "GLOBAL_MEMORY.md"
        checks.append(("GLOBAL_MEMORY.md", global_memory.exists(), str(global_memory)))

        # Compare against whatever _sync_instruction_targets actually writes.
        # With projection on that is the generated core, not GLOBAL_MEMORY.md —
        # checking the source instead reported every projection-enabled install
        # as broken, and a health check that always fails is not one.
        from memsync.claude_md import is_synced
        expected_source = global_memory
        if config.projection_enabled:
            from memsync.projection import core_path
            expected_source = core_path(config)
        for label, target in _instruction_targets(config):
            synced = expected_source.exists() and is_synced(expected_source, target)
            detail = f"{target} → {'synced' if synced else 'not synced (run memsync init)'}"
            checks.append((f"{label} synced", synced, detail))
    else:
        checks.append(("Memory directory", False, "cannot resolve — fix provider first"))

    # 7. Daemon health (optional but warn if stale)
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text(encoding="utf-8").strip())
            if platform.system() == "Windows":
                import subprocess as _sp
                _r = _sp.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True, text=True,
                )
                daemon_alive = str(pid) in _r.stdout
            else:
                import os as _os
                try:
                    _os.kill(pid, 0)
                    daemon_alive = True
                except (ProcessLookupError, OSError):
                    daemon_alive = False
            if daemon_alive:
                checks.append(("Daemon", True, f"running (PID {pid})"))
            else:
                checks.append((
                    "Daemon", False,
                    f"not running — stale PID {pid}. Run: memsync daemon start --detach",
                ))
        except ValueError:
            checks.append(("Daemon", False, f"corrupt PID file: {_PID_FILE}"))
    else:
        checks.append((
            "Daemon", True,
            "not started (optional — run: memsync daemon start --detach)",
        ))

    # Print results
    all_ok = all(ok for _, ok, _ in checks)
    print("memsync doctor\n")
    for label, ok, detail in checks:
        marker = "✓" if ok else "✗"
        print(f"  {marker}  {label:<25} {detail}")

    # --probe turns "is it installed" into "does it answer". Off by default
    # because it spends a real call per backend; worth every one of them when a
    # harvest has gone quiet, since a readiness check cannot tell a live
    # backend from a retired model or an expired token.
    if getattr(args, "probe", False):
        print("\n  Probing backends (one real call each):")
        alive = 0
        for backend, ok, detail in probe_backends(config):
            print(f"  {'✓' if ok else '✗'}  {_display_backend_name(backend):<25} {detail}")
            alive += bool(ok)
        if not alive:
            checks.append(("Backends answering", False, "none — harvest cannot work"))
            all_ok = False
        else:
            print(f"\n  {alive} backend(s) answering.")

    # Harvest liveness. A memory that stopped being fed two nights ago looks
    # exactly like a healthy one from every other check here.
    memory_root_for_harvest = _resolve_memory_root(config)
    if memory_root_for_harvest is not None:
        _print_harvest_health(config, memory_root_for_harvest)

    print()
    if all_ok:
        print("All checks passed.")
    else:
        failed = [label for label, ok, _ in checks if not ok]
        print(f"{len(failed)} check(s) failed: {', '.join(failed)}")

    return 0 if all_ok else 1


def cmd_config_show(args: argparse.Namespace, config: Config) -> int:
    """Print current config.toml contents."""
    config_path = get_config_path()
    if not config_path.exists():
        print("No config file found. Run 'memsync init' first.", file=sys.stderr)
        return 2
    print(config_path.read_text(encoding="utf-8"))
    return 0


def cmd_config_set(args: argparse.Namespace, config: Config) -> int:
    """Update a single config value and save."""
    key = args.key
    value = args.value

    valid_keys = {
        "provider", "model", "sync_root", "claude_md_target", "codex_agents_target",
        "max_memory_lines", "keep_days",
        "api_key", "llm_backend", "fallback_backend", "gemini_api_key", "gemini_model",
        "claude_code_model", "claude_code_effort",
        "ollama_base_url", "ollama_model", "ollama_timeout", "ollama_num_ctx",
        "harvest_chunk_tokens",
        "harvest_chunk_tokens_codex",
        "harvest_chunk_tokens_claude_code",
        "harvest_chunk_tokens_gemini",
        "harvest_chunk_tokens_gemini_cli",
        "harvest_chunk_tokens_ollama",
        "harvest_chunk_tokens_anthropic",
        "chunk_inter_call_sleep",
        "max_hot_lines", "archive_in_harvest", "archive_max_lines_in_prompt",
        "projection_enabled", "core_max_chars", "skill_enabled",
        "git_enabled", "git_autosync",
    }
    if key not in valid_keys:
        print(
            f"Error: unknown config key '{key}'.\n"
            f"Valid keys: {', '.join(sorted(valid_keys))}",
            file=sys.stderr,
        )
        return 1

    if key == "provider":
        all_names = {p.name for p in all_providers()}
        if value not in all_names:
            print(
                f"Error: unknown provider '{value}'.\n"
                f"Available: {', '.join(sorted(all_names))}",
                file=sys.stderr,
            )
            return 1
        config = dataclasses.replace(config, provider=value)

    elif key == "sync_root":
        path = Path(value).expanduser()
        if not path.exists():
            print(f"Error: path does not exist: {path}", file=sys.stderr)
            return 1
        config = dataclasses.replace(config, sync_root=path, provider="custom")

    elif key == "claude_md_target":
        config = dataclasses.replace(config, claude_md_target=Path(value).expanduser())

    elif key == "codex_agents_target":
        config = dataclasses.replace(config, codex_agents_target=Path(value).expanduser())

    elif key == "max_memory_lines":
        if not value.isdigit():
            print("Error: max_memory_lines must be an integer.", file=sys.stderr)
            return 1
        config = dataclasses.replace(config, max_memory_lines=int(value))

    elif key == "keep_days":
        if not value.isdigit():
            print("Error: keep_days must be an integer.", file=sys.stderr)
            return 1
        config = dataclasses.replace(config, keep_days=int(value))

    elif key == "model":
        config = dataclasses.replace(config, model=value)

    elif key == "api_key":
        config = dataclasses.replace(config, api_key=value)

    elif key == "llm_backend":
        value = normalize_backend_name(value)
        if value not in ("codex", "claude_code", "gemini", "gemini_cli", "ollama", "anthropic"):
            print(
                f"Error: unknown llm_backend '{value}'.\n"
                "Valid values: codex, claude, claude_code, gemini, gemini_cli, ollama, anthropic",
                file=sys.stderr,
            )
            return 1
        llm_backends = [value] + [
            backend for backend in _configured_backends(config)
            if backend != value
        ]
        config = dataclasses.replace(
            config,
            llm_backend=value,
            fallback_backend=llm_backends[1] if len(llm_backends) > 1 else "none",
            llm_backends=llm_backends,
        )

    elif key == "fallback_backend":
        value = normalize_backend_name(value)
        valid_fallback_backends = (
            "codex",
            "claude_code",
            "gemini",
            "gemini_cli",
            "ollama",
            "anthropic",
            "none",
        )
        if value not in valid_fallback_backends:
            print(
                f"Error: unknown fallback_backend '{value}'.\n"
                "Valid values: codex, claude, claude_code, gemini, "
                "gemini_cli, ollama, anthropic, none",
                file=sys.stderr,
            )
            return 1
        current = _configured_backends(config)
        primary = current[0] if current else DEFAULT_LLM_BACKENDS[0]
        llm_backends = [primary]
        if value != "none" and value != primary:
            llm_backends.append(value)
        llm_backends.extend(
            backend for backend in current[1:]
            if backend not in llm_backends
        )
        config = dataclasses.replace(
            config,
            fallback_backend=llm_backends[1] if len(llm_backends) > 1 else "none",
            llm_backends=llm_backends,
        )

    elif key == "gemini_api_key":
        config = dataclasses.replace(config, gemini_api_key=value)

    elif key == "gemini_model":
        config = dataclasses.replace(config, gemini_model=value)

    elif key == "claude_code_model":
        config = dataclasses.replace(config, claude_code_model=value)

    elif key == "claude_code_effort":
        config = dataclasses.replace(config, claude_code_effort=value)

    elif key == "ollama_base_url":
        config = dataclasses.replace(config, ollama_base_url=value)

    elif key == "ollama_model":
        config = dataclasses.replace(config, ollama_model=value)

    elif key in ("ollama_timeout", "ollama_num_ctx"):
        try:
            ivalue = int(value)
        except ValueError:
            print(f"Error: {key} must be an integer, got '{value}'.", file=sys.stderr)
            return 1
        if ivalue <= 0:
            print(f"Error: {key} must be positive, got {ivalue}.", file=sys.stderr)
            return 1
        config = dataclasses.replace(config, **{key: ivalue})

    elif key in (
        "harvest_chunk_tokens",
        "harvest_chunk_tokens_codex",
        "harvest_chunk_tokens_claude_code",
        "harvest_chunk_tokens_gemini",
        "harvest_chunk_tokens_gemini_cli",
        "harvest_chunk_tokens_ollama",
        "harvest_chunk_tokens_anthropic",
    ):
        try:
            ivalue = int(value)
        except ValueError:
            print(f"Error: {key} must be an integer, got {value!r}.", file=sys.stderr)
            return 1
        if ivalue < 0:
            print(
                f"Error: {key} must be >= 0 "
                f"(0 = inherit or one-shot for the global key), got {ivalue}.",
                file=sys.stderr,
            )
            return 1
        config = dataclasses.replace(config, **{key: ivalue})

    elif key == "chunk_inter_call_sleep":
        try:
            ivalue = int(value)
        except ValueError:
            print(f"Error: chunk_inter_call_sleep must be an integer, got {value!r}.",
                  file=sys.stderr)
            return 1
        if ivalue < 0:
            print(
                f"Error: chunk_inter_call_sleep must be >= 0 (0 = no sleep), got {ivalue}.",
                file=sys.stderr,
            )
            return 1
        config = dataclasses.replace(config, chunk_inter_call_sleep=ivalue)

    elif key == "max_hot_lines":
        try:
            ivalue = int(value)
        except ValueError:
            print(f"Error: max_hot_lines must be an integer, got {value!r}.", file=sys.stderr)
            return 1
        if ivalue < 10:
            print(f"Error: max_hot_lines must be >= 10, got {ivalue}.", file=sys.stderr)
            return 1
        config = dataclasses.replace(config, max_hot_lines=ivalue)

    elif key in ("projection_enabled", "skill_enabled", "git_enabled", "git_autosync"):
        if value.lower() not in ("true", "false"):
            print(f"Error: {key} must be true or false, got {value!r}.", file=sys.stderr)
            return 1
        config = dataclasses.replace(config, **{key: value.lower() == "true"})

    elif key == "core_max_chars":
        try:
            ivalue = int(value)
        except ValueError:
            print(f"Error: core_max_chars must be an integer, got {value!r}.", file=sys.stderr)
            return 1
        if ivalue < 1000:
            print(f"Error: core_max_chars must be >= 1000, got {ivalue}.", file=sys.stderr)
            return 1
        config = dataclasses.replace(config, core_max_chars=ivalue)

    elif key == "archive_in_harvest":
        if value.lower() not in ("true", "false"):
            print(
                f"Error: archive_in_harvest must be true or false, got {value!r}.",
                file=sys.stderr,
            )
            return 1
        config = dataclasses.replace(config, archive_in_harvest=value.lower() == "true")

    elif key == "archive_max_lines_in_prompt":
        try:
            ivalue = int(value)
        except ValueError:
            print(
                f"Error: archive_max_lines_in_prompt must be an integer, got {value!r}.",
                file=sys.stderr,
            )
            return 1
        if ivalue < 0:
            print(
                f"Error: archive_max_lines_in_prompt must be >= 0, got {ivalue}.",
                file=sys.stderr,
            )
            return 1
        config = dataclasses.replace(config, archive_max_lines_in_prompt=ivalue)

    config.save()
    print(f"Set {key} = {value}")
    return 0


# ---------------------------------------------------------------------------
# Daemon commands (optional install — memsync[daemon])
# ---------------------------------------------------------------------------

_DAEMON_INSTALL_HINT = (
    "The daemon module is not installed.\n"
    "Install it with: pip install memsync[daemon]"
)

_PID_FILE = Path("~/.config/memsync/daemon.pid").expanduser()
_LOG_FILE = Path("~/.config/memsync/daemon.log").expanduser()


def _daemon_import_guard() -> bool:
    """Return True if daemon extras are installed, False (with error) if not."""
    try:
        import apscheduler  # noqa: F401
        import flask  # noqa: F401
        return True
    except ImportError:
        print(_DAEMON_INSTALL_HINT, file=sys.stderr)
        return False


def cmd_daemon_start(args: argparse.Namespace, config: Config) -> int:
    """Start the daemon (foreground or detached)."""
    if not _daemon_import_guard():
        return 1

    if args.detach:
        import subprocess

        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(_LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115
        script = [sys.executable, "-m", "memsync.cli", "daemon", "start"]
        kwargs: dict = {"stdout": log_fh, "stderr": log_fh}
        if platform.system() == "Windows":
            _flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            kwargs["creationflags"] = _flags
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(script, **kwargs)  # noqa: S603
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        print(f"Daemon started (PID {proc.pid}).")
        print(f"Logs:       {_LOG_FILE}")
        print("Stop with: memsync daemon stop")
        return 0

    # Foreground mode — configure logging then run everything in threads
    import logging as _logging
    import threading

    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            _logging.FileHandler(_LOG_FILE, encoding="utf-8"),
            _logging.StreamHandler(),
        ],
    )

    import os as _os

    from memsync.daemon.scheduler import build_scheduler

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(_os.getpid()), encoding="utf-8")

    threads: list[threading.Thread] = []

    if config.daemon.web_ui_enabled:
        from memsync.daemon.web import run_web

        t = threading.Thread(target=run_web, args=[config], daemon=True, name="web-ui")
        t.start()
        threads.append(t)
        print(f"Web UI:     http://{config.daemon.web_ui_host}:{config.daemon.web_ui_port}/")

    if config.daemon.capture_enabled:
        from memsync.daemon.capture import run_capture

        t = threading.Thread(target=run_capture, args=[config], daemon=True, name="capture")
        t.start()
        threads.append(t)
        print(f"Capture:    http://0.0.0.0:{config.daemon.capture_port}/note")

    scheduler = build_scheduler(config, blocking=False)
    scheduler.start()

    job_count = len(scheduler.get_jobs())
    print(f"Scheduler:  {job_count} job(s) running. Press Ctrl+C to stop.")

    try:
        import time
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        _PID_FILE.unlink(missing_ok=True)
        print("\nDaemon stopped.")
    return 0


def cmd_daemon_stop(args: argparse.Namespace, config: Config) -> int:
    """Stop a detached daemon process."""
    if not _PID_FILE.exists():
        print("No running daemon found (PID file not present).", file=sys.stderr)
        return 1

    import signal

    pid_text = _PID_FILE.read_text(encoding="utf-8").strip()
    try:
        pid = int(pid_text)
    except ValueError:
        print(f"Invalid PID file: {_PID_FILE}", file=sys.stderr)
        return 1

    try:
        if platform.system() == "Windows":
            import subprocess
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=True)  # noqa: S603,S607
        else:
            import os
            os.kill(pid, signal.SIGTERM)
        _PID_FILE.unlink(missing_ok=True)
        print(f"Daemon stopped (PID {pid}).")
    except (ProcessLookupError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        print(f"Process {pid} not found (already stopped?). PID file removed.")
    return 0


def cmd_daemon_status(args: argparse.Namespace, config: Config) -> int:
    """Show daemon running status."""
    if not _daemon_import_guard():
        return 1

    if _PID_FILE.exists():
        pid_text = _PID_FILE.read_text(encoding="utf-8").strip()
        try:
            pid = int(pid_text)
            # Check if process is still running
            if platform.system() == "Windows":
                import subprocess
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,  # no console-window flash
                )
                running = str(pid) in result.stdout
            else:
                import os
                try:
                    os.kill(pid, 0)
                    running = True
                except (ProcessLookupError, OSError):
                    running = False

            if running:
                print(f"Daemon is running (PID {pid}).")
            else:
                print(f"Daemon is NOT running (stale PID file: {pid}).")
                _PID_FILE.unlink(missing_ok=True)
        except ValueError:
            print(f"Invalid PID file: {_PID_FILE}", file=sys.stderr)
            return 1
    else:
        print("Daemon is not running.")

    log_status = str(_LOG_FILE) if _LOG_FILE.exists() else f"{_LOG_FILE} (not yet created)"
    print(f"Log:      {log_status}")
    print(f"\nWeb UI:   {'enabled' if config.daemon.web_ui_enabled else 'disabled'}"
          f"  (port {config.daemon.web_ui_port})")
    print(f"Capture:  {'enabled' if config.daemon.capture_enabled else 'disabled'}"
          f"  (port {config.daemon.capture_port})")
    print(f"Refresh:  {'enabled' if config.daemon.refresh_enabled else 'disabled'}"
          f"  (schedule: {', '.join(config.daemon.refresh_schedule)})")
    return 0


def cmd_daemon_logs(args: argparse.Namespace, config: Config) -> int:  # noqa: ARG001
    """Show recent daemon log entries."""
    if not _LOG_FILE.exists():
        print(f"No log file found at {_LOG_FILE}.")
        print("Start the daemon with: memsync daemon start --detach")
        return 1
    lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    n = args.lines
    for line in lines[-n:]:
        print(line)
    return 0


def cmd_daemon_schedule(args: argparse.Namespace, config: Config) -> int:
    """Show all scheduled jobs and their next run times."""
    if not _daemon_import_guard():
        return 1

    from memsync.daemon.scheduler import build_scheduler

    scheduler = build_scheduler(config, blocking=False)
    jobs = scheduler.get_jobs()

    if not jobs:
        print("No jobs scheduled (check daemon config — all jobs may be disabled).")
        return 0

    print("Scheduled jobs:\n")
    for job in jobs:
        try:
            next_run = job.next_run_time
        except AttributeError:
            next_run = None
        next_str = (
            next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "(pending — start daemon)"
        )
        print(f"  {job.name}")
        print(f"    ID:       {job.id}")
        print(f"    Next run: {next_str}")
        print()
    return 0


def cmd_daemon_install(args: argparse.Namespace, config: Config) -> int:
    """Register the daemon as a system service (auto-starts on boot)."""
    if not _daemon_import_guard():
        return 1

    from memsync.daemon.service import install_service

    try:
        install_service()
    except NotImplementedError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except PermissionError:
        print(
            "Error: permission denied. Try: sudo memsync daemon install",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_daemon_uninstall(args: argparse.Namespace, config: Config) -> int:
    """Remove the daemon system service registration."""
    if not _daemon_import_guard():
        return 1

    from memsync.daemon.service import uninstall_service

    try:
        uninstall_service()
    except NotImplementedError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_daemon_web(args: argparse.Namespace, config: Config) -> int:
    """Open the web UI in the default browser."""
    if not _daemon_import_guard():
        return 1

    import webbrowser

    host = config.daemon.web_ui_host
    # 0.0.0.0 means listening on all interfaces — open localhost for browser
    browser_host = "localhost" if host in ("0.0.0.0", "") else host  # noqa: S104
    url = f"http://{browser_host}:{config.daemon.web_ui_port}/"
    print(f"Opening {url}")
    webbrowser.open(url)
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memsync",
        description="Cross-platform global memory manager for Claude Code.",
    )
    parser.add_argument("--version", action="version", version=f"memsync {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser("init", help="Set up memory structure for the first time")
    p_init.add_argument("--force", action="store_true", help="Reinitialize even if already set up")
    p_init.add_argument("--provider", help="Skip auto-detection, use this provider")
    p_init.add_argument("--sync-root", help="Skip auto-detection, use this path directly")
    p_init.set_defaults(func=cmd_init)

    # refresh
    p_refresh = subparsers.add_parser("refresh", help="Merge session notes into global memory")
    p_refresh.add_argument("--notes", "-n", help="Session notes as a string")
    p_refresh.add_argument("--file", "-f", help="Path to a file containing session notes")
    p_refresh.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    p_refresh.add_argument("--model", help="One-off model override (doesn't change config)")
    p_refresh.set_defaults(func=cmd_refresh)

    # harvest
    p_harvest = subparsers.add_parser(
        "harvest",
        help="Extract memories from a Claude Code session transcript",
    )
    p_harvest.add_argument(
        "--all", action="store_true",
        help="Sweep all projects under ~/.claude/projects/ (for scheduled runs)",
    )
    p_harvest.add_argument(
        "--project",
        help="Path to the ~/.claude/projects/<key> directory (default: current project)",
    )
    p_harvest.add_argument(
        "--session", help="Path to a specific session JSONL file (default: most recent unprocessed)"
    )
    p_harvest.add_argument(
        "--auto", action="store_true",
        help="Skip confirmation prompt and run silently (for hook use)",
    )
    p_harvest.add_argument(
        "--force", action="store_true",
        help="Re-harvest even if this session has already been processed",
    )
    p_harvest.add_argument(
        "--dry-run", action="store_true", help="Preview changes without writing"
    )
    p_harvest.add_argument("--model", help="One-off model override (doesn't change config)")
    p_harvest.set_defaults(func=cmd_harvest)

    # usage
    p_usage = subparsers.add_parser("usage", help="Show API usage and estimated cost")
    p_usage.set_defaults(func=cmd_usage)

    p_telemetry = subparsers.add_parser(
        "telemetry", help="Recent run outcomes and per-backend latencies"
    )
    p_telemetry.add_argument(
        "--limit", type=int, default=10, help="How many recent runs to show (default 10)"
    )
    p_telemetry.set_defaults(func=cmd_telemetry)


    # show
    p_show = subparsers.add_parser("show", help="Print current global memory")
    p_show.add_argument(
        "--archive", action="store_true", help="Show cold archive instead of hot layer"
    )
    p_show.set_defaults(func=cmd_show)

    # diff
    p_diff = subparsers.add_parser("diff", help="Diff current memory vs last backup")
    p_diff.add_argument("--backup", help="Diff against a specific backup filename")
    p_diff.set_defaults(func=cmd_diff)

    # status
    p_status = subparsers.add_parser("status", help="Show paths, provider, and sync state")
    p_status.set_defaults(func=cmd_status)

    # dedup
    p_dedup = subparsers.add_parser(
        "dedup",
        help=(
            "Remove duplicate bullet lines (fuzzy Python pass by default; "
            "--semantic for LLM pass)"
        ),
    )
    p_dedup.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    p_dedup.add_argument(
        "--semantic",
        action="store_true",
        help=(
            "LLM pass: also catches same-policy bullets phrased differently. "
            "Shows diff; use --apply to write."
        ),
    )
    p_dedup.add_argument(
        "--apply",
        action="store_true",
        help="With --semantic: write the changes after showing the diff (default is dry-run).",
    )
    p_dedup.add_argument(
        "--subsumed",
        action="store_true",
        help=(
            "Deterministic pass (no LLM): retire bullets whose every word a longer "
            "bullet in the same section already carries. Always keeps the longest."
        ),
    )
    p_dedup.set_defaults(func=cmd_dedup)

    # store
    p_store = subparsers.add_parser(
        "store", help="Git-backed history for the shared memory store"
    )
    store_sub = p_store.add_subparsers(dest="store_command", required=True)

    p_store_status = store_sub.add_parser("status", help="Show store and git state")
    p_store_status.set_defaults(func=cmd_store)

    p_store_init = store_sub.add_parser("init", help="Make the store a git repository")
    p_store_init.add_argument(
        "--allow-syncthing",
        action="store_true",
        help="Proceed even though Syncthing markers are present (see the docs first)",
    )
    p_store_init.set_defaults(func=cmd_store)

    p_store_sync = store_sub.add_parser("sync", help="Pull, commit, push")
    p_store_sync.set_defaults(func=cmd_store)

    p_store_conflicts = store_sub.add_parser(
        "conflicts", help="Report what Syncthing conflict copies hold that the live files do not"
    )
    p_store_conflicts.set_defaults(func=cmd_store_conflicts)

    # project
    p_project = subparsers.add_parser(
        "project",
        help="Generate the always-resident core plus on-demand topic files",
    )
    p_project.add_argument(
        "--dry-run", action="store_true", help="Report sizes without writing"
    )
    p_project.add_argument(
        "--force", action="store_true", help="Write even if the core is over budget"
    )
    p_project.set_defaults(func=cmd_project)

    # prune
    p_prune = subparsers.add_parser("prune", help="Remove old backups")
    p_prune.add_argument("--keep-days", type=int, dest="keep_days", default=None,
                         help="Keep backups newer than this many days (default: from config)")
    p_prune.add_argument("--dry-run", action="store_true", help="List what would be deleted")
    p_prune.set_defaults(func=cmd_prune)

    # providers
    p_providers = subparsers.add_parser("providers", help="List providers and detection status")
    p_providers.set_defaults(func=cmd_providers)

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Self-check: verify installation health")
    p_doctor.add_argument(
        "--probe",
        action="store_true",
        help=(
            "Also make one real call per backend. Checks that they answer, not "
            "merely that they are installed — the difference between a healthy "
            "waterfall and four dead ones."
        ),
    )
    p_doctor.set_defaults(func=cmd_doctor)

    # config
    p_config = subparsers.add_parser("config", help="View or update config")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)

    p_config_show = config_sub.add_parser("show", help="Print current config.toml")
    p_config_show.set_defaults(func=cmd_config_show)

    p_config_set = config_sub.add_parser("set", help="Update a config value")
    p_config_set.add_argument("key", help="Config key to update")
    p_config_set.add_argument("value", help="New value")
    p_config_set.set_defaults(func=cmd_config_set)

    # daemon (requires memsync[daemon])
    p_daemon = subparsers.add_parser("daemon", help="Manage the optional daemon process")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_command", required=True)

    p_daemon_start = daemon_sub.add_parser("start", help="Start the daemon")
    p_daemon_start.add_argument(
        "--detach", action="store_true", help="Start as a background process"
    )
    p_daemon_start.set_defaults(func=cmd_daemon_start)

    p_daemon_stop = daemon_sub.add_parser("stop", help="Stop the detached daemon")
    p_daemon_stop.set_defaults(func=cmd_daemon_stop)

    p_daemon_status = daemon_sub.add_parser("status", help="Show daemon running status")
    p_daemon_status.set_defaults(func=cmd_daemon_status)

    p_daemon_schedule = daemon_sub.add_parser(
        "schedule", help="Show scheduled jobs and next run times"
    )
    p_daemon_schedule.set_defaults(func=cmd_daemon_schedule)

    p_daemon_logs = daemon_sub.add_parser("logs", help="Show recent daemon log entries")
    p_daemon_logs.add_argument(
        "-n", "--lines", type=int, default=50, help="Number of lines to show (default: 50)"
    )
    p_daemon_logs.set_defaults(func=cmd_daemon_logs)

    p_daemon_install = daemon_sub.add_parser(
        "install", help="Register as a system service (auto-starts on boot)"
    )
    p_daemon_install.set_defaults(func=cmd_daemon_install)

    p_daemon_uninstall = daemon_sub.add_parser(
        "uninstall", help="Remove system service registration"
    )
    p_daemon_uninstall.set_defaults(func=cmd_daemon_uninstall)

    p_daemon_web = daemon_sub.add_parser("web", help="Open web UI in browser")
    p_daemon_web.set_defaults(func=cmd_daemon_web)

    return parser


def main() -> None:
    # Ensure UTF-8 output on Windows (needed for ✓/✗ status indicators)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()
    config = Config.load()
    code = args.func(args, config)

    # Snapshot after the command rather than at each write site: the store is
    # written from several paths inside refresh, harvest and dedup, and hooking
    # the dispatcher covers all of them — including ones added later — without
    # scattering bookkeeping through the write logic. Only on success, and only
    # for commands that touch the store.
    if code == 0 and config.git_enabled and getattr(args, "command", None) in _STORE_WRITERS:
        memory_root, _ = _require_memory_root(config)
        if memory_root is not None:
            _snapshot_store(config, memory_root, f"memsync: {args.command}")

    sys.exit(code)


if __name__ == "__main__":
    main()
