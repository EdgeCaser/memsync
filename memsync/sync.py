from __future__ import annotations

import re
from pathlib import Path

import anthropic

from memsync.config import Config

# The system prompt is load-bearing — see PITFALLS.md #8 before editing.
# Specific phrases matter; don't casually reword them.
SYSTEM_PROMPT = """You are maintaining a persistent global memory file for an AI assistant user.
This file is loaded at the start of every Claude Code session, on every machine and project.
It is the user's identity layer — not project docs, not cold storage.

YOUR JOB:
- Merge new session notes into the existing memory file
- Keep the file tight (under 400 lines)
- Update facts that have changed
- Demote completed items from "Current priorities" to a brief "Recent completions" section
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append
- If nothing meaningful changed, return the file UNCHANGED

RETURN: Only the updated GLOBAL_MEMORY.md content. No explanation, no preamble."""


def refresh_memory_content(notes: str, current_memory: str, config: Config) -> dict:
    """
    Call the Claude API to merge notes into current_memory.
    Returns a dict with keys: updated_content (str), changed (bool).
    Does NOT write files — caller handles I/O.
    """
    client = anthropic.Anthropic()

    user_prompt = f"""\
CURRENT GLOBAL MEMORY:
{current_memory}

SESSION NOTES:
{notes}"""

    response = client.messages.create(
        model=config.model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    updated_content = response.content[0].text.strip()

    # Enforce hard constraints in code — model can silently drop them (PITFALLS #1)
    updated_content = enforce_hard_constraints(current_memory, updated_content)

    changed = updated_content != current_memory.strip()

    # Detect truncation via stop_reason — more reliable than content heuristics (PITFALLS #10)
    truncated = response.stop_reason == "max_tokens"

    return {
        "updated_content": updated_content,
        "changed": changed,
        "truncated": truncated,
    }


def enforce_hard_constraints(old: str, new: str) -> str:
    """
    Re-append any hard constraint lines the model dropped.
    Hard constraints are append-only by design — they must never be lost
    through compaction. This is enforced in Python, not by prompt alone.
    """
    old_constraints = _extract_constraints(old)
    new_constraints = _extract_constraints(new)

    dropped = [line for line in old_constraints if line not in new_constraints]
    if not dropped:
        return new

    return _reinsert_constraints(new, dropped)


def _extract_constraints(text: str) -> list[str]:
    """
    Extract bullet lines from the Hard constraints / Constraints section.
    Returns list of non-empty stripped lines within the section.
    """
    lines = text.splitlines()
    in_section = False
    constraints: list[str] = []

    for line in lines:
        if re.match(r"^##\s+(Hard constraints|Constraints)\s*$", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section:
            # Another heading ends the section
            if re.match(r"^#{1,6}\s+", line) and not re.match(
                r"^##\s+(Hard constraints|Constraints)\s*$", line, re.IGNORECASE
            ):
                break
            stripped = line.strip()
            if stripped:
                constraints.append(stripped)

    return constraints


def _reinsert_constraints(text: str, dropped: list[str]) -> str:
    """
    Find the Hard constraints section in text and append the dropped lines to it.
    If the section doesn't exist, append it at the end.
    """
    lines = text.splitlines()
    insert_idx: int | None = None

    in_section = False
    for i, line in enumerate(lines):
        if re.match(r"^##\s+(Hard constraints|Constraints)\s*$", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section:
            if re.match(r"^#{1,6}\s+", line):
                # Insert before the next heading
                insert_idx = i
                break
            insert_idx = i + 1  # keep updating to end of section

    if insert_idx is not None:
        for item in dropped:
            lines.insert(insert_idx, item)
            insert_idx += 1
        return "\n".join(lines)

    # Section not found — append it
    appended = "\n".join(lines)
    appended += "\n\n## Hard constraints\n"
    appended += "\n".join(dropped)
    return appended



def load_or_init_memory(path: Path) -> str:
    """
    Read memory file, or return the starter template if it doesn't exist yet.
    """
    if path.exists():
        return path.read_text(encoding="utf-8")

    return """\
<!-- memsync v0.2 -->
# Global Memory

> Loaded by Claude Code at session start on all machines and projects.
> Edit directly or run: memsync refresh --notes "..."

## Identity & context
- (Fill this in — who you are, your roles, active projects)

## Current priorities
- (What you're working on right now)

## Standing preferences
- (How you like to work — communication style, output format, etc.)

## Hard constraints
- (Rules that must never be lost or softened through compaction)
"""


def log_session_notes(notes: str, session_dir: Path) -> None:
    """Append session notes to today's dated log file. Append-only, never pruned."""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_path = session_dir / f"{today}.md"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n---\n### {timestamp}\n{notes}\n")
