from __future__ import annotations

import json
import logging
import os
import platform
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path("~/.claude/projects").expanduser()


def default_codex_sessions_dir() -> Path:
    """$CODEX_HOME/sessions, falling back to ~/.codex/sessions. Resolved per call."""
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "sessions"


# Separator used between turns in the in-memory transcript string.
# Uses a Unicode Private Use Area character so it cannot appear naturally in
# any message content (including markdown horizontal rules like "---").
_TURN_SEPARATOR = "\n\n\ue001\n\n"


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


def is_codex_rollout(path: Path) -> bool:
    """True for Codex rollout files (rollout-<timestamp>-<uuid>.jsonl)."""
    return path.name.startswith("rollout-") and path.suffix == ".jsonl"


def _is_interactive_codex_rollout(path: Path) -> bool:
    """
    Decide from line 1 alone whether a rollout is a human conversation.

    Guardian and thread_spawn subagents carry a dict `source`; `codex exec`
    runs carry originator "codex_exec". Neither holds the user's own turns.
    Anything unreadable is skipped, never raised.
    """
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
        meta = json.loads(first)
    except (OSError, ValueError):
        logger.debug("Skipping rollout with unreadable first line: %s", path)
        return False
    if not isinstance(meta, dict) or meta.get("type") != "session_meta":
        logger.debug("Skipping rollout without session_meta first line: %s", path)
        return False
    payload = meta.get("payload")
    if not isinstance(payload, dict):
        logger.debug("Skipping rollout with no session_meta payload: %s", path)
        return False
    if not isinstance(payload.get("source"), str):
        return False
    return payload.get("originator") != "codex_exec"


def list_codex_sessions(sessions_root: Path) -> list[Path]:
    """Interactive Codex rollouts under sessions_root, newest first."""
    if not sessions_root.is_dir():
        return []
    files = [
        p for p in sessions_root.rglob("rollout-*.jsonl")
        if "archived_sessions" not in p.relative_to(sessions_root).parts
        and _is_interactive_codex_rollout(p)
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def discover_sessions(
    claude_roots: list[Path],
    codex_roots: list[Path],
    harvested: dict[str, int],
    *,
    include_grown: bool,
) -> list[Path]:
    """
    Every session under the given roots that still needs harvesting.

    Claude sessions come first (root order, project dirs sorted, newest first
    within a project), then Codex rollouts. Missing roots are skipped; callers
    report them. A stem seen under two roots is returned once, since a machine
    can both write a transcript locally and receive a synced copy of it.

    include_grown re-queues a harvested session whose message count has grown
    since, at the price of re-reading every harvested transcript.
    """
    candidates: list[Path] = []
    for root in claude_roots:
        if not root.is_dir():
            continue
        for project_dir in sorted(root.iterdir()):
            if project_dir.is_dir():
                candidates.extend(list_sessions(project_dir))
    for root in codex_roots:
        candidates.extend(list_codex_sessions(root))

    new_sessions: list[Path] = []
    seen_stems: set[str] = set()
    for session_path in candidates:
        if session_path.stem in seen_stems:
            continue
        seen_stems.add(session_path.stem)
        stored = harvested.get(session_path.stem)
        if stored is None:
            new_sessions.append(session_path)
        elif include_grown and stored >= 0:
            _, current_count = read_session_transcript(session_path)
            if current_count > stored:
                new_sessions.append(session_path)
    return new_sessions


# ---------------------------------------------------------------------------
# JSONL transcript extraction
# ---------------------------------------------------------------------------

# A user part that is exactly one <tag>...</tag> block is injected context.
_WHOLE_TAG_RE = re.compile(r"^<([A-Za-z_][\w\-]*)>(.*)</\1>$", re.DOTALL)
_UNWRAP_TAG = "send_user_message_question_reply"
# Markdown headers Codex uses for injected context (our own memory, attached
# file bodies, role prompts).
_INJECTED_PREFIXES = ("# AGENTS.md", "# Files", "## Referenced", "# ROLE")


def _clean_codex_user_part(text: str) -> str:
    """Return the part's text if it is human content, else ''."""
    text = text.strip()
    m = _WHOLE_TAG_RE.match(text)
    if m:
        return m.group(2).strip() if m.group(1) == _UNWRAP_TAG else ""
    if text.startswith(_INJECTED_PREFIXES):
        return ""
    return text


def read_codex_transcript(path: Path) -> tuple[str, int]:
    """
    Parse a Codex rollout JSONL and return a (transcript, message_count) tuple,
    in the same shape as read_session_transcript.

    Keeps user input_text parts that survive the injected-context filters and
    assistant output_text parts (commentary and final answers). Skips developer
    messages, reasoning, tool calls and outputs, and every non-message line.
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
            if not isinstance(entry, dict) or entry.get("type") != "response_item":
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue

            role = payload.get("role")
            if role == "user":
                part_type = "input_text"
            elif role == "assistant":
                part_type = "output_text"
            else:
                continue

            content = payload.get("content")
            if not isinstance(content, list):
                continue
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") != part_type:
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                text = _clean_codex_user_part(text) if role == "user" else text.strip()
                if text:
                    parts.append(text)
            if parts:
                turns.append(f"[{role.upper()}]\n" + "\n".join(parts))

    return _TURN_SEPARATOR.join(turns), len(turns)


def read_session_transcript(path: Path) -> tuple[str, int]:
    """
    Parse a session JSONL and return a (transcript, message_count) tuple.

    Codex rollouts (rollout-*.jsonl) are routed to read_codex_transcript; the
    rest of this docstring describes the Claude Code format.

    Includes:
      - User messages that are plain strings (human-typed input)
      - Assistant text blocks (responses, not tool calls or thinking)

    Skips:
      - tool_result user entries (automated, not human intent)
      - assistant thinking blocks
      - assistant tool_use blocks
      - progress / queue-operation / file-history-snapshot / system entries
    """
    if is_codex_rollout(path):
        return read_codex_transcript(path)

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

    return _TURN_SEPARATOR.join(turns), len(turns)


# ---------------------------------------------------------------------------
# Harvest index — tracks which sessions have already been processed
# ---------------------------------------------------------------------------

class HarvestIndexError(Exception):
    """harvested.json exists but could not be read as an index.

    Deliberately *not* the same condition as "the index is absent". An absent
    index means nothing has been harvested yet and re-harvesting everything is
    correct. A present-but-unreadable index means the record of what was already
    done is damaged, and treating it as empty silently re-harvests the entire
    backlog — hundreds of sessions at roughly three minutes each.

    The store's .gitattributes deliberately omits harvested.json from union
    merge so that "a genuine disagreement about its contents should stop and be
    resolved". Conflict markers make the file invalid JSON, so this is the
    exception that delivers that intent instead of swallowing it.
    """


def load_harvested_index(memory_root: Path) -> dict[str, int]:
    """
    Load the harvest index: session stem → message count at harvest time.
    Returns {} if the index doesn't exist.

    Backward compatible with the old list format — those entries get count -1
    (meaning "harvested but message count unknown, treat as already done").

    Raises HarvestIndexError if the file exists but is not readable as an index.
    """
    index_path = memory_root / "harvested.json"
    if not index_path.exists():
        return {}

    raw = index_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        hint = ""
        if "<<<<<<<" in raw or ">>>>>>>" in raw:
            hint = (
                " The file contains git conflict markers — resolve the conflict"
                " in harvested.json, then re-run."
            )
        raise HarvestIndexError(f"{index_path} is not valid JSON: {e}.{hint}") from e

    if isinstance(data, list):
        # Migrate old list format — count unknown
        return {stem: -1 for stem in data if isinstance(stem, str)}

    if isinstance(data, dict):
        kept = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}
        dropped = len(data) - len(kept)
        if dropped:
            # Individually malformed entries only cost a re-harvest of those
            # sessions, so they stay non-fatal — but they are never silent.
            logger.warning(
                "%s: ignoring %d malformed entr%s; those sessions will be re-harvested",
                index_path, dropped, "y" if dropped == 1 else "ies",
            )
        return kept

    raise HarvestIndexError(
        f"{index_path} holds a JSON {type(data).__name__}, expected an object or array."
    )


def save_harvested_index(memory_root: Path, harvested: dict[str, int]) -> None:
    """Persist the harvest index (session stem → message count)."""
    index_path = memory_root / "harvested.json"
    index_path.write_text(
        json.dumps(harvested, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Transcript chunking
# ---------------------------------------------------------------------------

def chunk_transcript(transcript: str, max_tokens: int) -> list[str]:
    """
    Split a transcript (produced by read_session_transcript) into chunks where
    each chunk contains at most max_tokens estimated tokens.

    Uses a 4 chars/token heuristic — accurate enough for chunking boundaries
    without requiring a tokenizer dependency.

    Splits only on turn boundaries. Individual turns that exceed max_tokens are
    truncated to max_chars with a marker suffix — the tail of a 93K-token tool
    dump is noise for memory extraction purposes.

    Returns [] for empty/whitespace-only input, otherwise at least one element.
    """
    if not transcript.strip():
        return []

    SEPARATOR = _TURN_SEPARATOR
    max_chars = max_tokens * 4
    turns = transcript.split(SEPARATOR)

    chunks: list[str] = []
    current_turns: list[str] = []
    current_chars = 0

    for turn in turns:
        # Truncate turns that individually exceed the limit before any packing logic
        if len(turn) > max_chars:
            approx_tokens = len(turn) // 4
            marker = (
                f"\n[TURN TRUNCATED: was ~{approx_tokens} tokens,"
                f" showing first ~{max_tokens} tokens]"
            )
            turn = turn[:max_chars] + marker

        # Cost of appending this turn to the current chunk (separator + content)
        added_chars = (len(SEPARATOR) if current_turns else 0) + len(turn)
        if current_turns and current_chars + added_chars > max_chars:
            chunks.append(SEPARATOR.join(current_turns))
            current_turns = [turn]
            current_chars = len(turn)
        else:
            current_turns.append(turn)
            current_chars += added_chars

    if current_turns:
        chunks.append(SEPARATOR.join(current_turns))

    return chunks
