from __future__ import annotations

import re
from pathlib import Path

from memsync.config import Config
from memsync.llm import call_llm

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
# See PITFALLS.md #8 before editing — specific phrases matter.
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

# Two-phase chunked harvest prompts. See PITFALLS.md #8 — load-bearing phrases preserved.
EXTRACT_SYSTEM_PROMPT = """You are scanning a segment of a conversation transcript for facts worth adding to a persistent memory file.

Extract only facts a person would want recalled in a future AI session:
- Decisions made or approaches chosen
- Work completed or milestones reached
- Preferences or constraints the user expressed
- Problems solved and how they were resolved
- Project or priority status changes

Return a bullet list (one fact per line, starting with "- ").
If nothing in this segment is worth persisting, return exactly: NONE

RETURN: Only the bullet list or NONE. No explanation, no preamble."""

MERGE_SYSTEM_PROMPT = """You are maintaining a persistent global memory file for an AI assistant user.
This file is loaded at the start of every Claude Code session, on every machine and project.
It is the user's identity layer — not project docs, not cold storage.

You will receive a list of candidate facts extracted from a recent session. Merge them into the memory file:
- Keep the file tight (under 400 lines)
- Update facts that have changed
- Demote completed items from "Current priorities" to a brief "Recent completions" section
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append
- If none of the candidates add meaningful new information, return the file UNCHANGED

RETURN: Only the updated GLOBAL_MEMORY.md content. No explanation, no preamble."""


def harvest_memory_content(transcript: str, current_memory: str, config: Config) -> dict:
    """
    Extract memories from a session transcript and merge them into current_memory.

    When config.harvest_chunk_tokens > 0 (default 6000), uses a two-phase approach:
      1. Split transcript into chunks, extract candidate facts from each via LLM.
      2. Merge all candidates into current_memory in a single LLM call.
    This keeps every LLM call under the token limit, avoiding rate-limit fallback
    to local Ollama with oversized prompts.

    When harvest_chunk_tokens == 0, falls back to the original single-shot path
    (full transcript in one call).

    Returns a dict with keys: updated_content, changed, truncated, malformed,
    input_tokens, output_tokens, backend, chunks_processed.
    Does NOT write files — caller handles I/O.
    """
    if config.harvest_chunk_tokens > 0:
        return _harvest_chunked(transcript, current_memory, config)
    return _harvest_one_shot(transcript, current_memory, config)


def _harvest_one_shot(transcript: str, current_memory: str, config: Config) -> dict:
    """Original single-shot path: sends full transcript in one LLM call."""
    user_prompt = f"""\
CURRENT GLOBAL MEMORY:
{current_memory}

SESSION TRANSCRIPT:
{transcript}"""

    prefill = _build_prefill(current_memory)
    llm_result = call_llm(HARVEST_SYSTEM_PROMPT, user_prompt, prefill, config)

    updated_content = _strip_model_wrapper(llm_result["text"])

    if not _looks_like_memory_file(updated_content):
        return {
            "updated_content": updated_content,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
            "backend": llm_result.get("backend", "unknown"),
            "chunks_processed": 1,
        }

    updated_content = enforce_hard_constraints(current_memory, updated_content)
    changed = updated_content != current_memory.strip()

    return {
        "updated_content": updated_content,
        "changed": changed,
        "truncated": llm_result["truncated"],
        "malformed": False,
        "input_tokens": llm_result["input_tokens"],
        "output_tokens": llm_result["output_tokens"],
        "backend": llm_result.get("backend", "unknown"),
        "chunks_processed": 1,
    }


def extract_candidates_from_chunk(chunk: str, config: Config) -> dict:
    """
    Call LLM to extract memory-worthy facts from one transcript chunk.

    Returns {"candidates": str, "truncated": bool, "input_tokens": int, "output_tokens": int}.
    candidates is "" if the model found nothing worth persisting.
    """
    user_prompt = f"TRANSCRIPT SEGMENT:\n{chunk}"
    llm_result = call_llm(EXTRACT_SYSTEM_PROMPT, user_prompt, "", config)
    text = llm_result["text"].strip()
    candidates = "" if not text or text.upper() == "NONE" else text
    return {
        "candidates": candidates,
        "truncated": llm_result["truncated"],
        "input_tokens": llm_result["input_tokens"],
        "output_tokens": llm_result["output_tokens"],
        "backend": llm_result.get("backend", "unknown"),
    }


def merge_candidates_into_memory(candidates: str, current_memory: str, config: Config) -> dict:
    """
    Merge a bullet list of extracted candidate facts into current_memory via LLM.
    Returns the same dict shape as harvest_memory_content (without token counts,
    which the caller accumulates across all extract calls).
    """
    user_prompt = f"""\
CURRENT GLOBAL MEMORY:
{current_memory}

CANDIDATE FACTS:
{candidates}"""

    prefill = _build_prefill(current_memory)
    llm_result = call_llm(MERGE_SYSTEM_PROMPT, user_prompt, prefill, config)
    updated_content = _strip_model_wrapper(llm_result["text"])

    if not _looks_like_memory_file(updated_content):
        return {
            "updated_content": updated_content,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
            "backend": llm_result.get("backend", "unknown"),
        }

    updated_content = enforce_hard_constraints(current_memory, updated_content)
    changed = updated_content != current_memory.strip()

    return {
        "updated_content": updated_content,
        "changed": changed,
        "truncated": llm_result["truncated"],
        "malformed": False,
        "input_tokens": llm_result["input_tokens"],
        "output_tokens": llm_result["output_tokens"],
        "backend": llm_result.get("backend", "unknown"),
    }


def _harvest_chunked(transcript: str, current_memory: str, config: Config) -> dict:
    """Two-phase chunked harvest: extract candidates per chunk, then one merge call."""
    from memsync.harvest import chunk_transcript

    chunks = chunk_transcript(transcript, config.harvest_chunk_tokens)
    n_chunks = len(chunks)

    if not chunks:
        return {
            "updated_content": current_memory.strip(),
            "changed": False,
            "truncated": False,
            "malformed": False,
            "input_tokens": 0,
            "output_tokens": 0,
            "backend": "none",
            "chunks_processed": 0,
        }

    total_input = 0
    total_output = 0
    any_truncated = False
    candidate_blocks: list[str] = []
    last_backend = "unknown"

    for i, chunk in enumerate(chunks):
        if i > 0 and config.chunk_inter_call_sleep > 0:
            import time
            time.sleep(config.chunk_inter_call_sleep)
        result = extract_candidates_from_chunk(chunk, config)
        total_input += result["input_tokens"]
        total_output += result["output_tokens"]
        any_truncated = any_truncated or result["truncated"]
        last_backend = result.get("backend", last_backend)
        if result["candidates"]:
            candidate_blocks.append(result["candidates"])

    if not candidate_blocks:
        return {
            "updated_content": current_memory.strip(),
            "changed": False,
            "truncated": any_truncated,
            "malformed": False,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "backend": last_backend,
            "chunks_processed": n_chunks,
        }

    combined_candidates = "\n".join(candidate_blocks)
    merge_result = merge_candidates_into_memory(combined_candidates, current_memory, config)
    merge_result["input_tokens"] += total_input
    merge_result["output_tokens"] += total_output
    merge_result["chunks_processed"] = n_chunks
    # OR with extract-phase truncation — if any chunk was cut short, surface it
    merge_result["truncated"] = merge_result["truncated"] or any_truncated
    # backend from merge call wins (most representative — if it fell back, this shows it)
    return merge_result


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
    Call the configured LLM to merge notes into current_memory.
    Returns a dict with keys: updated_content (str), changed (bool), malformed (bool).
    Does NOT write files — caller handles I/O.
    """
    user_prompt = f"""\
CURRENT GLOBAL MEMORY:
{current_memory}

SESSION NOTES:
{notes}"""

    prefill = _build_prefill(current_memory)
    llm_result = call_llm(SYSTEM_PROMPT, user_prompt, prefill, config)

    updated_content = _strip_model_wrapper(llm_result["text"])

    # Reject responses that look like narrative explanations rather than a memory file.
    # The model occasionally ignores "no preamble" and returns prose — writing that
    # verbatim would corrupt GLOBAL_MEMORY.md.
    if not _looks_like_memory_file(updated_content):
        return {
            "updated_content": updated_content,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
        }

    # Enforce hard constraints in code — model can silently drop them (PITFALLS #1)
    updated_content = enforce_hard_constraints(current_memory, updated_content)

    changed = updated_content != current_memory.strip()

    return {
        "updated_content": updated_content,
        "changed": changed,
        "truncated": llm_result["truncated"],
        "malformed": False,
        "input_tokens": llm_result["input_tokens"],
        "output_tokens": llm_result["output_tokens"],
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
