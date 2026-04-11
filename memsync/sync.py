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

# Harvest prompt: reads a full session transcript and extracts what's worth keeping.
# Deliberately separate from SYSTEM_PROMPT — different task, different tuning surface.
HARVEST_SYSTEM_PROMPT = """You are maintaining a persistent global memory file for an AI assistant user.
This file is loaded at the start of every Claude Code session, on every machine and project.
It is the user's identity layer — not project docs, not cold storage.

Read the conversation transcript below and extract facts worth adding to persistent memory:
- Decisions made, approaches chosen, or things agreed upon
- Work completed, milestones reached, or features shipped
- Problems solved and how they were resolved
- Preferences or constraints the user expressed
- Project or priority status changes
- Anything the user would want to know in a future session

Then merge those extractions into the existing memory file:
- Keep the file tight (under 400 lines)
- Update facts that have changed
- Demote completed items from "Current priorities" to a brief "Recent completions" section
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append
- If the conversation contained nothing worth persisting, return the file UNCHANGED

RETURN: Only the updated GLOBAL_MEMORY.md content. No explanation, no preamble."""


def harvest_memory_content(transcript: str, current_memory: str, config: Config) -> dict:
    """
    Call the Claude API to extract memories from a session transcript and merge
    them into current_memory.
    Returns a dict with keys: updated_content (str), changed (bool), truncated (bool).
    Does NOT write files — caller handles I/O.
    """
    client = anthropic.Anthropic(api_key=config.api_key or None)

    user_prompt = f"""\
CURRENT GLOBAL MEMORY:
{current_memory}

SESSION TRANSCRIPT:
{transcript}"""

    prefill = _build_prefill(current_memory)
    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=HARVEST_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": prefill},
        ],
    )

    updated_content = _strip_model_wrapper(prefill + response.content[0].text)

    if not _looks_like_memory_file(updated_content):
        return {
            "updated_content": updated_content,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    updated_content = enforce_hard_constraints(current_memory, updated_content)
    changed = updated_content != current_memory.strip()
    truncated = response.stop_reason == "max_tokens"

    return {
        "updated_content": updated_content,
        "changed": changed,
        "truncated": truncated,
        "malformed": False,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def _build_prefill(current_memory: str) -> str:
    """
    Build an assistant prefill string that forces the model to start outputting
    the memory file rather than a narrative summary.

    Uses the first line of the current memory if it looks like a valid start
    (heading or comment marker), otherwise falls back to the memsync comment.
    """
    first_line = current_memory.strip().splitlines()[0] if current_memory.strip() else ""
    if first_line.startswith("#") or first_line.startswith("<!--"):
        return first_line
    return "<!-- memsync v0.2 -->"


def _strip_model_wrapper(content: str) -> str:
    """
    Strip wrapper artifacts the model sometimes adds around the memory file:
    - Code fences (```markdown, ```md, plain ```)
    - Preamble lines before the first heading ("Here's the updated...", etc.)
    """
    stripped = content.strip()

    # Strip code fences first
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Remove opening fence line (e.g. ```markdown)
        lines = lines[1:]
        # Remove closing fence if present
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    # Strip preamble lines before the first heading or comment marker.
    # The model sometimes leads with "Here's the updated memory file:" or similar.
    lines = stripped.splitlines()
    while lines:
        first = lines[0].strip()
        if first.startswith("#") or first.startswith("<!--") or first == "":
            break
        # This line is preamble — drop it
        lines = lines[1:]
    stripped = "\n".join(lines).strip()

    return stripped


def _looks_like_memory_file(content: str) -> bool:
    """
    Sanity-check that the model returned a memory file, not a narrative response.
    A valid response starts with a markdown heading or the memsync comment marker.
    """
    first_line = content.lstrip().splitlines()[0] if content.strip() else ""
    return first_line.startswith("#") or first_line.startswith("<!--")


def refresh_memory_content(notes: str, current_memory: str, config: Config) -> dict:
    """
    Call the Claude API to merge notes into current_memory.
    Returns a dict with keys: updated_content (str), changed (bool), malformed (bool).
    Does NOT write files — caller handles I/O.
    """
    client = anthropic.Anthropic(api_key=config.api_key or None)

    user_prompt = f"""\
CURRENT GLOBAL MEMORY:
{current_memory}

SESSION NOTES:
{notes}"""

    prefill = _build_prefill(current_memory)
    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": prefill},
        ],
    )

    updated_content = _strip_model_wrapper(prefill + response.content[0].text)

    # Reject responses that look like narrative explanations rather than a memory file.
    # The model occasionally ignores "no preamble" and returns prose — writing that
    # verbatim would corrupt GLOBAL_MEMORY.md.
    if not _looks_like_memory_file(updated_content):
        return {
            "updated_content": updated_content,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    # Enforce hard constraints in code — model can silently drop them (PITFALLS #1)
    updated_content = enforce_hard_constraints(current_memory, updated_content)

    changed = updated_content != current_memory.strip()

    # Detect truncation via stop_reason — more reliable than content heuristics (PITFALLS #10)
    truncated = response.stop_reason == "max_tokens"

    return {
        "updated_content": updated_content,
        "changed": changed,
        "truncated": truncated,
        "malformed": False,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
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
