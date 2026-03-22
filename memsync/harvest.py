from __future__ import annotations

import json
import platform
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path("~/.claude/projects").expanduser()


# ---------------------------------------------------------------------------
# Project directory resolution
# ---------------------------------------------------------------------------

def cwd_to_project_key(cwd: Path) -> str:
    """
    Convert a filesystem path to the key Claude Code uses for its project directory.

    Claude Code stores sessions under ~/.claude/projects/<key>/ where <key>
    is the absolute path with separators replaced by '-'.

    Examples:
      /Users/ian/projects/foo  →  -Users-ian-projects-foo
      C:\\Users\\Ian\\foo      →  C--Users-Ian-foo
    """
    if platform.system() == "Windows":
        # Drive letter colon becomes a dash; backslashes become dashes
        # e.g. C:\Users\Ian\foo → C--Users-Ian-foo
        raw = str(cwd.resolve())
        return raw.replace(":", "-").replace("\\", "-").replace(" ", "-")
    return str(cwd.resolve()).replace("/", "-").replace(" ", "-")


def find_project_dir(
    cwd: Path,
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> Path | None:
    """Return ~/.claude/projects/<key> for cwd, or None if it doesn't exist."""
    key = cwd_to_project_key(cwd)
    candidate = claude_projects_dir / key
    return candidate if candidate.exists() else None


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def list_sessions(project_dir: Path) -> list[Path]:
    """Return all session JSONL files sorted by modification time, newest first."""
    files = list(project_dir.glob("*.jsonl"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def find_latest_session(
    project_dir: Path,
    exclude: set[str] | None = None,
) -> Path | None:
    """
    Return the most recent session JSONL, skipping any whose stem is in exclude.
    Returns None if all sessions have been harvested or the directory is empty.
    """
    for path in list_sessions(project_dir):
        if exclude and path.stem in exclude:
            continue
        return path
    return None


# ---------------------------------------------------------------------------
# JSONL transcript extraction
# ---------------------------------------------------------------------------

def read_session_transcript(path: Path) -> tuple[str, int]:
    """
    Parse a Claude Code session JSONL and return a (transcript, message_count) tuple.

    Includes:
      - User messages that are plain strings (human-typed input)
      - Assistant text blocks (responses, not tool calls or thinking)

    Skips:
      - tool_result user entries (automated, not human intent)
      - assistant thinking blocks
      - assistant tool_use blocks
      - progress / queue-operation / file-history-snapshot / system entries
    """
    turns: list[str] = []

    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            if entry_type not in ("user", "assistant"):
                continue

            message = entry.get("message", {})
            role = message.get("role", entry_type)
            content = message.get("content", "")

            if isinstance(content, str):
                # Plain string — always a human-typed message
                text = content.strip()
                if text:
                    turns.append(f"[{role.upper()}]\n{text}")

            elif isinstance(content, list):
                # Block list — filter to text blocks only, skip tool_use/tool_result/thinking
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        t = block.get("text", "").strip()
                        if t:
                            parts.append(t)
                if parts:
                    turns.append(f"[{role.upper()}]\n" + "\n".join(parts))

    return "\n\n---\n\n".join(turns), len(turns)


# ---------------------------------------------------------------------------
# Harvest index — tracks which sessions have already been processed
# ---------------------------------------------------------------------------

def load_harvested_index(memory_root: Path) -> dict[str, int]:
    """
    Load the harvest index: session stem → message count at harvest time.
    Returns {} if the index doesn't exist.

    Backward compatible with the old list format — those entries get count -1
    (meaning "harvested but message count unknown, treat as already done").
    """
    index_path = memory_root / "harvested.json"
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            # Migrate old list format — count unknown
            return {stem: -1 for stem in data if isinstance(stem, str)}
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}
        return {}
    except (json.JSONDecodeError, ValueError):
        return {}


def save_harvested_index(memory_root: Path, harvested: dict[str, int]) -> None:
    """Persist the harvest index (session stem → message count)."""
    index_path = memory_root / "harvested.json"
    index_path.write_text(
        json.dumps(harvested, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
