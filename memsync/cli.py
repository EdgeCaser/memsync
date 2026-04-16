from __future__ import annotations

import argparse
import dataclasses
import difflib
import platform
import sys
from pathlib import Path

from memsync import __version__
from memsync.backups import backup, latest_backup, list_backups, prune
from memsync.claude_md import sync as sync_claude_md
from memsync.config import Config, get_config_path
from memsync.harvest import (
    find_latest_session,
    find_project_dir,
    list_sessions,
    load_harvested_index,
    read_session_transcript,
    save_harvested_index,
)
from memsync.providers import all_providers, auto_detect, get_provider
from memsync.llm import LLMError
from memsync.sync import (
    harvest_memory_content,
    load_or_init_memory,
    log_session_notes,
    refresh_memory_content,
)
from memsync.usage import append_usage, format_summary, load_usage, usage_log_path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

    # Write config
    new_config = Config(
        provider=provider_name,
        sync_root=sync_root if provider_name == "custom" else None,
    )
    new_config.save()

    # Sync to CLAUDE.md
    sync_claude_md(global_memory, new_config.claude_md_target)

    print("memsync initialized.\n")
    print(f"  Provider:    {provider.display_name}")
    print(f"  Sync root:   {sync_root}")
    print(f"  Memory:      {global_memory}")
    target = new_config.claude_md_target
    if target.is_symlink():
        print(f"  CLAUDE.md:   {target} → (symlink)")
    else:
        print(f"  CLAUDE.md:   {target}")
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

    print("Refreshing global memory...", end=" ", flush=True)

    try:
        result = refresh_memory_content(notes, current_memory, config)
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
    )

    if args.dry_run:
        print("\n[DRY RUN] No files written.\n")
        if result["changed"]:
            old_lines = current_memory.strip().splitlines(keepends=True)
            new_lines = result["updated_content"].splitlines(keepends=True)
            diff = difflib.unified_diff(old_lines, new_lines, fromfile="current", tofile="updated")
            diff_text = "".join(diff)
            if diff_text:
                print("--- diff ---")
                print(diff_text)
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

    # Backup then write
    backup_path = backup(global_memory, memory_root / "backups")
    global_memory.write_text(result["updated_content"], encoding="utf-8")
    sync_claude_md(global_memory, config.claude_md_target)

    # Log the transaction for auditability
    from memsync.journal import log_transaction
    log_transaction(
        transaction_type="refresh",
        input_data={"notes": notes} if notes else {"file": str(args.file)},
        memory_before=current_memory,
        memory_after=result["updated_content"],
        llm_metadata=result,
        journal_dir=str(memory_root / "journal"),
    )

    log_session_notes(notes, memory_root / "sessions")

    print("done.")
    print(f"  Backup:    {backup_path}")
    print(f"  Memory:    {global_memory}")
    print("  CLAUDE.md synced ✓")
    return 0


def _harvest_all(
    args: argparse.Namespace,
    config: Config,
    memory_root: Path,
    global_memory: Path,
) -> int:
    """Sweep all projects under ~/.claude/projects/ and harvest unprocessed sessions."""
    projects_dir_str = getattr(config, "daemon", None) and config.daemon.harvest_projects_dir
    projects_dir = (
        Path(projects_dir_str).expanduser()
        if projects_dir_str
        else Path("~/.claude/projects").expanduser()
    )

    if not projects_dir.exists():
        if not args.auto:
            print(f"No Claude Code projects directory found at {projects_dir}")
        return 0

    harvested = load_harvested_index(memory_root)
    new_sessions: list[Path] = []
    for project_dir in sorted(projects_dir.iterdir()):
        if project_dir.is_dir():
            for session_path in list_sessions(project_dir):
                if session_path.stem not in harvested:
                    new_sessions.append(session_path)

    if not new_sessions:
        if not args.auto:
            print("No new sessions to harvest.")
        return 0

    if not args.auto:
        print(f"Found {len(new_sessions)} unprocessed session(s) across all projects.")

    if args.model:
        config = dataclasses.replace(config, model=args.model)

    current_memory = load_or_init_memory(global_memory)
    changed_any = False
    errors = 0
    _first_call = True

    for session_path in new_sessions:
        transcript, msg_count = read_session_transcript(session_path)
        harvested[session_path.stem] = msg_count  # mark regardless of outcome

        if not transcript.strip():
            continue

        if not args.auto:
            print(f"  Harvesting {session_path.stem}...", end=" ", flush=True)

        if not _first_call:
            import time
            time.sleep(20)
        _first_call = False

        try:
            result = harvest_memory_content(transcript, current_memory, config)
        except LLMError as e:
            print(f"\nError processing {session_path.stem}: {e}", file=sys.stderr)
            errors += 1
            continue

        try:
            append_usage(
                memory_root,
                command="harvest",
                model=config.model,
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                session_id=session_path.stem,
                changed=result.get("changed", False),
            )
        except OSError as e:
            print(f"Warning: failed to write usage log: {e}", file=sys.stderr)

        if result["truncated"]:
            if not args.auto:
                print("truncated — skipped.")
            continue

        if result.get("malformed"):
            if not args.auto:
                print("malformed response — skipped.")
            errors += 1
            continue

        if result["changed"]:
            current_memory = result["updated_content"]
            changed_any = True
            if not args.auto:
                print("updated.")
        else:
            if not args.auto:
                print("no changes.")

    # Log the transaction for auditability
    from memsync.journal import log_transaction
    log_transaction(
        transaction_type="harvest",
        input_data={"session_path": str(session_path)} if session_path else {},
        memory_before=current_memory,
        memory_after=result["updated_content"],
        llm_metadata=result,
        journal_dir=str(memory_root / "journal"),
    )

    # Persist index and write memory once after all sessions processed
    save_harvested_index(memory_root, harvested)

    if changed_any:
        backup_path = backup(global_memory, memory_root / "backups")
        global_memory.write_text(current_memory, encoding="utf-8")
        sync_claude_md(global_memory, config.claude_md_target)
        if not args.auto:
            print("\ndone.")
            print(f"  Backup:    {backup_path}")
            print(f"  Memory:    {global_memory}")
            print("  CLAUDE.md synced ✓")
    else:
        if not args.auto:
            print("\nNo memory changes.")

    return 1 if errors else 0


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
    harvested = load_harvested_index(memory_root)

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

    if not args.auto:
        print("Harvesting session...", end=" ", flush=True)

    try:
        result = harvest_memory_content(transcript, current_memory, config)
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
    )

    if args.dry_run:
        import difflib
        print("\n[DRY RUN] No files written.\n")
        if result["changed"]:
            old_lines = current_memory.strip().splitlines(keepends=True)
            new_lines = result["updated_content"].splitlines(keepends=True)
            diff = difflib.unified_diff(
                old_lines, new_lines, fromfile="current", tofile="harvested"
            )
            diff_text = "".join(diff)
            if diff_text:
                print("--- diff ---")
                print(diff_text)
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
    sync_claude_md(global_memory, config.claude_md_target)

    if not args.auto:
        print("done.")
        print(f"  Backup:    {backup_path}")
        print(f"  Memory:    {global_memory}")
        print("  CLAUDE.md synced ✓")

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


def cmd_show(args: argparse.Namespace, config: Config) -> int:
    """Print current GLOBAL_MEMORY.md to stdout."""
    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

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
    print(f"LLM backend:   {config.llm_backend}")
    if config.llm_backend == "gemini":
        print(f"LLM model:     {config.gemini_model} (fallback: ollama/{config.ollama_model})")
    elif config.llm_backend == "gemini_cli":
        print(f"LLM model:     {config.gemini_model} via gemini CLI (fallback: ollama/{config.ollama_model})")
    elif config.llm_backend == "ollama":
        print(f"LLM model:     ollama/{config.ollama_model}")
    else:
        print(f"Model:         {config.model}")

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
    print(f"Memory:        {global_memory} {mem_marker}")

    target = config.claude_md_target
    if target.is_symlink():
        print(f"CLAUDE.md:     {target} → symlink ✓")
    elif target.exists():
        print(f"CLAUDE.md:     {target} ✓ (copy)")
    else:
        print(f"CLAUDE.md:     {target} ✗ (not synced — run memsync init)")

    backup_dir = memory_root / "backups"
    if backup_dir.exists():
        count = len(list_backups(backup_dir))
        print(f"Backups:       {count} file(s)")

    session_dir = memory_root / "sessions"
    if session_dir.exists():
        sessions = list(session_dir.glob("*.md"))
        print(f"Session logs:  {len(sessions)} day(s)")

    return 0


def cmd_prune(args: argparse.Namespace, config: Config) -> int:
    """Remove old backups."""
    memory_root, code = _require_memory_root(config)
    if memory_root is None:
        return code

    backup_dir = memory_root / "backups"
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
        return 0

    deleted = prune(backup_dir, keep_days=keep_days)
    if deleted:
        print(f"Pruned {len(deleted)} backup(s) older than {keep_days} days.")
        for p in deleted:
            print(f"  removed: {p.name}")
    else:
        print(f"No backups older than {keep_days} days.")
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
    import os

    checks: list[tuple[str, bool, str]] = []  # (label, ok, detail)

    # 1. Config file
    config_path = get_config_path()
    checks.append(("Config file", config_path.exists(), str(config_path)))

    # 2. LLM backend / API key check
    backend = config.llm_backend
    if backend == "anthropic":
        api_key_set = bool(config.api_key) or bool(os.environ.get("ANTHROPIC_API_KEY"))
        if config.api_key:
            api_key_detail = "anthropic — key set via config"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            api_key_detail = "anthropic — key set via env var (consider: memsync config set api_key <key>)"
        else:
            api_key_detail = "anthropic — api_key not set; refresh will fail"
        checks.append(("LLM / API key", api_key_set, api_key_detail))
    elif backend == "gemini":
        if config.gemini_api_key:
            detail = f"gemini ({config.gemini_model}) — API key configured"
            checks.append(("LLM / API key", True, detail))
        else:
            # No API key — check if ADC credentials are available
            try:
                import google.auth
                google.auth.default(
                    scopes=["https://www.googleapis.com/auth/generative-language"]
                )
                detail = f"gemini ({config.gemini_model}) — ADC (gcloud credentials)"
                checks.append(("LLM / API key", True, detail))
            except Exception as _adc_err:  # noqa: BLE001
                has_fallback = config.fallback_backend and config.fallback_backend != "none"
                if has_fallback:
                    detail = (
                        f"gemini ADC unavailable; will fall back to {config.fallback_backend} "
                        f"({_adc_err})"
                    )
                    checks.append(("LLM / API key", True, detail))
                else:
                    detail = f"gemini — no API key, ADC failed, no fallback configured: {_adc_err}"
                    checks.append(("LLM / API key", False, detail))
    elif backend == "gemini_cli":
        import shutil
        import subprocess as _sp
        cli_path = shutil.which("gemini") or (
            # Windows: gemini is a .cmd script, use cmd.exe to locate it
            _sp.run(["cmd.exe", "/c", "where", "gemini"], capture_output=True, text=True).stdout.strip().splitlines()[0]  # noqa: S603
            if sys.platform == "win32" else None
        )
        cli_ok = bool(cli_path)
        detail = (
            f"gemini CLI ({config.gemini_model}) — found at {cli_path}"
            if cli_ok
            else "gemini CLI not found — install with: npm install -g @google/gemini-cli"
        )
        checks.append(("LLM / gemini CLI", cli_ok, detail))
    else:
        checks.append(("LLM / API key", True, f"{backend} — no API key required"))

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

        # 6. CLAUDE.md is synced
        target = config.claude_md_target
        from memsync.claude_md import is_synced
        synced = global_memory.exists() and is_synced(global_memory, target)
        detail = f"{target} → {'synced' if synced else 'not synced (run memsync init)'}"
        checks.append(("CLAUDE.md synced", synced, detail))
    else:
        checks.append(("Memory directory", False, "cannot resolve — fix provider first"))

    # Print results
    all_ok = all(ok for _, ok, _ in checks)
    print("memsync doctor\n")
    for label, ok, detail in checks:
        marker = "✓" if ok else "✗"
        print(f"  {marker}  {label:<25} {detail}")

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
        "provider", "model", "sync_root", "claude_md_target", "max_memory_lines", "keep_days",
        "api_key", "llm_backend", "fallback_backend", "gemini_api_key", "gemini_model",
        "ollama_base_url", "ollama_model",
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
        if value not in ("gemini", "gemini_cli", "ollama", "anthropic"):
            print(
                f"Error: unknown llm_backend '{value}'.\n"
                "Valid values: gemini, gemini_cli, ollama, anthropic",
                file=sys.stderr,
            )
            return 1
        config = dataclasses.replace(config, llm_backend=value)

    elif key == "fallback_backend":
        if value not in ("gemini", "gemini_cli", "ollama", "anthropic", "none"):
            print(
                f"Error: unknown fallback_backend '{value}'.\n"
                "Valid values: gemini, gemini_cli, ollama, anthropic, none",
                file=sys.stderr,
            )
            return 1
        config = dataclasses.replace(config, fallback_backend=value)

    elif key == "gemini_api_key":
        config = dataclasses.replace(config, gemini_api_key=value)

    elif key == "gemini_model":
        config = dataclasses.replace(config, gemini_model=value)

    elif key == "ollama_base_url":
        config = dataclasses.replace(config, ollama_base_url=value)

    elif key == "ollama_model":
        config = dataclasses.replace(config, ollama_model=value)

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

        script = [sys.executable, "-m", "memsync.cli", "daemon", "start"]
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if platform.system() == "Windows":
            _flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            kwargs["creationflags"] = _flags
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(script, **kwargs)  # noqa: S603
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        print(f"Daemon started (PID {proc.pid}).")
        print("Stop with: memsync daemon stop")
        return 0

    # Foreground mode — run everything in threads, block until interrupted
    import threading

    from memsync.daemon.scheduler import build_scheduler

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
                    ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
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

    print(f"\nWeb UI:   {'enabled' if config.daemon.web_ui_enabled else 'disabled'}"
          f"  (port {config.daemon.web_ui_port})")
    print(f"Capture:  {'enabled' if config.daemon.capture_enabled else 'disabled'}"
          f"  (port {config.daemon.capture_port})")
    print(f"Refresh:  {'enabled' if config.daemon.refresh_enabled else 'disabled'}"
          f"  (schedule: {config.daemon.refresh_schedule})")
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


def cmd_orchestrate(args: argparse.Namespace, config: Config) -> int:
    """Run the orchestrator with specified scenario class."""
    import subprocess
    import shutil

    node_path = shutil.which("node")
    if not node_path:
        print("Error: Node.js is not found in your PATH. Please install Node.js to run the orchestrator.", file=sys.stderr)
        return 1

    script_path = Path(__file__).parent.parent / "scripts" / "run-orchestrated.mjs"
    if not script_path.exists():
        print(f"Error: Orchestrator script not found at {script_path}", file=sys.stderr)
        return 1

    command = [node_path, str(script_path)]
    if args.scenario_class:
        command.append(f"--scenario-class={args.scenario_class}")
    if args.dry_run:
        command.append("--dry-run")
    if args.yes:
        command.append("--yes")

    print(f"Running orchestrator: {' '.join(command)}")
    try:
        # Pass through stdin, stdout, stderr
        process = subprocess.run(command, check=False, text=True, capture_output=False) # noqa: S603
        return process.returncode
    except Exception as e:
        print(f"Error running orchestrator: {e}", file=sys.stderr)
        return 1


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

    # show
    p_show = subparsers.add_parser("show", help="Print current global memory")
    p_show.set_defaults(func=cmd_show)

    # diff
    p_diff = subparsers.add_parser("diff", help="Diff current memory vs last backup")
    p_diff.add_argument("--backup", help="Diff against a specific backup filename")
    p_diff.set_defaults(func=cmd_diff)

    # status
    p_status = subparsers.add_parser("status", help="Show paths, provider, and sync state")
    p_status.set_defaults(func=cmd_status)

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

    # orchestrate
    p_orchestrate = subparsers.add_parser("orchestrate", help="Run the orchestrator with specified scenario class")
    p_orchestrate.add_argument("scenario_class", help="The scenario class to run (e.g., governance, pricing)")
    p_orchestrate.add_argument("--dry-run", action="store_true", help="Perform a dry run without actual execution")
    p_orchestrate.add_argument("--yes", action="store_true", help="Auto-confirm any escalation prompts")
    p_orchestrate.set_defaults(func=cmd_orchestrate)

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
    sys.exit(args.func(args, config))


if __name__ == "__main__":
    main()
