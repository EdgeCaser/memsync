from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Bump when the on-disk entry shape changes so readers can tell eras apart.
#   v1 (unversioned): stored the full memory_before + memory_after on every entry.
#   v2: stores a unified diff + SHA-256 integrity hashes instead. Full snapshots
#       already live in backups/, so duplicating them here only bloated the
#       journal (and every synced machine) with near-identical copies.
JOURNAL_SCHEMA_VERSION = 2


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log_transaction(
    transaction_type: str,
    input_data: dict,
    memory_before: str,
    memory_after: str,
    llm_metadata: dict,
    journal_dir: str = "journal",
) -> None:
    """
    Log a transaction to a structured JSON file in the journal directory.

    The entry records a unified diff of the memory change plus SHA-256 hashes of
    the before/after content, not the full snapshots. backups/ already keeps
    timestamped full copies of the memory, so the journal only needs to capture
    what changed (and enough to verify integrity against those backups).

    Args:
        transaction_type: The type of transaction (e.g. "refresh", "harvest",
            "harvest_all"). May contain underscores.
        input_data: The data that initiated the transaction (notes, transcript
            path, ...).
        memory_before: The content of the memory before the transaction.
        memory_after: The content of the memory after the transaction.
        llm_metadata: Metadata from the LLM call (token counts, model, success).
        journal_dir: The directory where journal entries will be stored.
    """
    try:
        os.makedirs(journal_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        transaction_id = f"{transaction_type}_{timestamp}"

        diff = "".join(
            difflib.unified_diff(
                memory_before.splitlines(keepends=True),
                memory_after.splitlines(keepends=True),
                fromfile="memory_before",
                tofile="memory_after",
            )
        )

        log_entry = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "timestamp": datetime.now().isoformat(),
            "transaction_type": transaction_type,
            "input_data": input_data,
            "diff": diff,
            "memory_before_sha256": _sha256(memory_before),
            "memory_after_sha256": _sha256(memory_after),
            "memory_before_lines": len(memory_before.splitlines()),
            "memory_after_lines": len(memory_after.splitlines()),
            "llm_metadata": llm_metadata,
        }

        file_path = os.path.join(journal_dir, f"{transaction_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(log_entry, f, indent=2, ensure_ascii=False)

        logger.debug("Transaction logged to: %s", file_path)
    except Exception:
        # Audit-log failure must never break a refresh/harvest. The daemon's
        # exception handler would mask the real outcome if we let this raise.
        logger.exception("Failed to write journal entry for %s", transaction_type)


def _entry_timestamp(path: Path) -> datetime | None:
    """Parse the datetime encoded in a journal filename, or None if it doesn't
    match the pattern.

    Names look like '{type}_{YYYYMMDD}_{HHMMSS}_{ffffff}.json'. The type itself
    may contain underscores (e.g. 'harvest_all'), so we split from the right and
    read the trailing three fields as the timestamp.
    """
    parts = path.stem.rsplit("_", 3)
    if len(parts) < 4:
        return None
    try:
        return datetime.strptime("_".join(parts[-3:]), "%Y%m%d_%H%M%S_%f")
    except ValueError:
        return None


def list_prunable_journal(journal_dir: Path, keep_days: int) -> list[Path]:
    """Return journal entries older than keep_days, newest-first, without
    deleting anything. Files whose names don't parse as a journal timestamp
    (user-dropped files) are ignored."""
    journal_dir = Path(journal_dir)
    if not journal_dir.is_dir():
        return []
    cutoff = datetime.now() - timedelta(days=keep_days)
    prunable = [
        entry
        for entry in journal_dir.glob("*.json")
        if (ts := _entry_timestamp(entry)) is not None and ts < cutoff
    ]
    return sorted(prunable, reverse=True)


def prune_journal(journal_dir: Path, keep_days: int) -> list[Path]:
    """Delete journal entries older than keep_days. Returns the deleted paths.
    Files with unrecognised names are left untouched."""
    deleted: list[Path] = []
    for entry in list_prunable_journal(journal_dir, keep_days):
        try:
            entry.unlink()
            deleted.append(entry)
        except OSError:
            pass  # racing prune / permission — skip, don't abort the sweep
    return deleted
