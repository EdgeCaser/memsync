from __future__ import annotations

import re
from pathlib import Path

from memsync.config import Config
from memsync.llm import call_llm

_HOT_DELIMITER = "<!-- memsync:hot -->"
_COLD_DELIMITER = "<!-- memsync:cold -->"

# The system prompts are load-bearing — see PITFALLS.md #8 before editing.
# Specific phrases matter; don't casually reword them.
SYSTEM_PROMPT = """You are maintaining a persistent two-layer global memory for an AI assistant user.
Both files are synced across machines. Only the hot layer is loaded into Claude Code sessions.

HOT layer (GLOBAL_MEMORY.md): always in context — keep under 100 lines.
  Contains: identity, active priorities, standing preferences, hard constraints.
COLD layer (MEMORY_ARCHIVE.md): never in context — reference only.
  Contains: completed work, historical decisions, resolved items.

YOUR JOB:
- Merge new session notes into the hot layer
- Keep the hot layer tight (under 100 lines) — demote completed or stale items to cold
- Update facts that have changed
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append, always keep them hot
- NEVER add a bullet that already exists verbatim or near-verbatim in the same section
- If nothing meaningful changed, return both layers UNCHANGED

RETURN: Begin your response with the first delimiter — no preamble before it:
<!-- memsync:hot -->
[updated GLOBAL_MEMORY.md content]
<!-- memsync:cold -->
[updated MEMORY_ARCHIVE.md content]"""

# Harvest prompt: reads a full session transcript and extracts what's worth keeping.
# Deliberately separate from SYSTEM_PROMPT — different task, different tuning surface.
# See PITFALLS.md #8 before editing — specific phrases matter.
HARVEST_SYSTEM_PROMPT = """You are maintaining a persistent two-layer global memory for an AI assistant user.
Both files are synced across machines. Only the hot layer is loaded into Claude Code sessions.

HOT layer (GLOBAL_MEMORY.md): always in context — keep under 100 lines.
  Contains: identity, active priorities, standing preferences, hard constraints.
COLD layer (MEMORY_ARCHIVE.md): never in context — reference only.
  Contains: completed work, historical decisions, resolved items.

Read the conversation transcript below and extract facts worth adding to persistent memory:
- Decisions made, approaches chosen, or things agreed upon
- Work completed, milestones reached, or features shipped
- Problems solved and how they were resolved
- Preferences or constraints the user expressed
- Project or priority status changes
- Anything the user would want to know in a future session

Then merge those extractions into the appropriate layer:
- Keep the hot layer tight (under 100 lines) — demote completed or stale items to cold
- Update facts that have changed
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append, always keep them hot
- NEVER add a bullet that already exists verbatim or near-verbatim in the same section
- If the conversation contained nothing worth persisting, return both layers UNCHANGED

RETURN: Begin your response with the first delimiter — no preamble before it:
<!-- memsync:hot -->
[updated GLOBAL_MEMORY.md content]
<!-- memsync:cold -->
[updated MEMORY_ARCHIVE.md content]"""

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

MERGE_SYSTEM_PROMPT = """You are maintaining a persistent two-layer global memory for an AI assistant user.
Both files are synced across machines. Only the hot layer is loaded into Claude Code sessions.

HOT layer (GLOBAL_MEMORY.md): always in context — keep under 100 lines.
  Contains: identity, active priorities, standing preferences, hard constraints.
COLD layer (MEMORY_ARCHIVE.md): never in context — reference only.
  Contains: completed work, historical decisions, resolved items.

You will receive candidate facts extracted from a recent session. Merge them into the appropriate layer:
- Keep the hot layer tight (under 100 lines) — demote completed or stale items to cold
- Update facts that have changed
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append, always keep them hot
- NEVER add a bullet that already exists verbatim or near-verbatim in the same section
- If none of the candidates add meaningful new information, return both layers UNCHANGED

RETURN: Begin your response with the first delimiter — no preamble before it:
<!-- memsync:hot -->
[updated GLOBAL_MEMORY.md content]
<!-- memsync:cold -->
[updated MEMORY_ARCHIVE.md content]"""


def _strip_label_prefix(text: str) -> str:
    """Strip leading label lines (e.g. 'HOT MEMORY...') before the actual markdown content."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("<!--"):
            return "".join(lines[i:])
    return text


def _parse_tiered_response(text: str, current_cold: str) -> tuple[str, str]:
    """
    Split LLM response into (hot, cold) using delimiters.
    Falls back to (text, current_cold) when delimiters are absent — safe degradation
    that preserves backward compat with tests that mock single-file responses.
    """
    hot_idx = text.find(_HOT_DELIMITER)
    cold_idx = text.find(_COLD_DELIMITER)

    if hot_idx == -1 or cold_idx == -1 or cold_idx <= hot_idx:
        return text, current_cold

    hot = _strip_label_prefix(text[hot_idx + len(_HOT_DELIMITER):cold_idx].strip())
    cold = _strip_label_prefix(text[cold_idx + len(_COLD_DELIMITER):].strip())
    return hot, cold


def _truncate_archive_for_prompt(archive: str, max_lines: int) -> str:
    """
    Truncate archive to max_lines preserving complete sections.
    Keeps the most recent sections (bottom of file), adds a truncation notice.
    """
    lines = archive.splitlines()
    if len(lines) <= max_lines:
        return archive

    section_starts = [i for i, line in enumerate(lines) if re.match(r"^#{1,6}\s+", line)]

    if not section_starts:
        return "[ARCHIVE TRUNCATED]\n" + "\n".join(lines[-max_lines:])

    kept: list[str] = []
    lines_used = 0
    for i in range(len(section_starts) - 1, -1, -1):
        start = section_starts[i]
        end = section_starts[i + 1] if i + 1 < len(section_starts) else len(lines)
        section = lines[start:end]
        if lines_used + len(section) <= max_lines:
            kept.insert(0, "\n".join(section))
            lines_used += len(section)
        else:
            break

    if not kept:
        return "[ARCHIVE TRUNCATED]\n" + "\n".join(lines[-max_lines:])

    return "[ARCHIVE TRUNCATED — showing most recent sections only]\n" + "\n".join(kept)


def load_or_init_archive(path: Path) -> str:
    """Read archive file, or return an empty starter if it doesn't exist yet."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "<!-- memsync archive -->\n# Memory Archive\n\n## Recent completions\n"


def harvest_memory_content(transcript: str, current_memory: str, config: Config, current_cold: str = "") -> dict:
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
        return _harvest_chunked(transcript, current_memory, config, current_cold)
    return _harvest_one_shot(transcript, current_memory, config, current_cold)


def _harvest_one_shot(transcript: str, current_memory: str, config: Config, current_cold: str = "") -> dict:
    """Original single-shot path: sends full transcript in one LLM call."""
    archive_section = ""
    if config.archive_in_harvest and current_cold:
        truncated = _truncate_archive_for_prompt(current_cold, config.archive_max_lines_in_prompt)
        archive_section = f"\nCOLD ARCHIVE (reference only — never in context):\n{truncated}\n"

    user_prompt = f"""\
HOT MEMORY (always in context — keep under {config.max_hot_lines} lines):
{current_memory}{archive_section}
SESSION TRANSCRIPT:
{transcript}"""

    llm_result = call_llm(HARVEST_SYSTEM_PROMPT, user_prompt, _HOT_DELIMITER, config)

    raw = _strip_model_wrapper(llm_result["text"])
    updated_hot, updated_cold = _parse_tiered_response(raw, current_cold)

    if not _looks_like_memory_file(updated_hot):
        return {
            "updated_content": raw,
            "updated_cold": current_cold,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
            "backend": llm_result.get("backend", "unknown"),
            "chunks_processed": 1,
        }

    updated_hot = enforce_hard_constraints(current_memory, updated_hot)
    updated_hot = _deduplicate_memory(updated_hot)
    updated_cold = _deduplicate_memory(updated_cold)
    changed_hot = updated_hot != current_memory.strip()
    changed_cold = updated_cold != current_cold.strip()

    return {
        "updated_content": updated_hot,
        "updated_cold": updated_cold,
        "changed": changed_hot or changed_cold,
        "changed_hot": changed_hot,
        "changed_cold": changed_cold,
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


def merge_candidates_into_memory(candidates: str, current_memory: str, config: Config, current_cold: str = "") -> dict:
    """
    Merge a bullet list of extracted candidate facts into current_memory via LLM.
    Returns the same dict shape as harvest_memory_content (without token counts,
    which the caller accumulates across all extract calls).
    """
    archive_section = ""
    if config.archive_in_harvest and current_cold:
        truncated = _truncate_archive_for_prompt(current_cold, config.archive_max_lines_in_prompt)
        archive_section = f"\nCOLD ARCHIVE (reference only — never in context):\n{truncated}\n"

    user_prompt = f"""\
HOT MEMORY (always in context — keep under {config.max_hot_lines} lines):
{current_memory}{archive_section}
CANDIDATE FACTS:
{candidates}"""

    llm_result = call_llm(MERGE_SYSTEM_PROMPT, user_prompt, _HOT_DELIMITER, config)
    raw = _strip_model_wrapper(llm_result["text"])
    updated_hot, updated_cold = _parse_tiered_response(raw, current_cold)

    if not _looks_like_memory_file(updated_hot):
        return {
            "updated_content": raw,
            "updated_cold": current_cold,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
            "backend": llm_result.get("backend", "unknown"),
        }

    updated_hot = enforce_hard_constraints(current_memory, updated_hot)
    updated_hot = _deduplicate_memory(updated_hot)
    updated_cold = _deduplicate_memory(updated_cold)
    changed_hot = updated_hot != current_memory.strip()
    changed_cold = updated_cold != current_cold.strip()

    return {
        "updated_content": updated_hot,
        "updated_cold": updated_cold,
        "changed": changed_hot or changed_cold,
        "changed_hot": changed_hot,
        "changed_cold": changed_cold,
        "truncated": llm_result["truncated"],
        "malformed": False,
        "input_tokens": llm_result["input_tokens"],
        "output_tokens": llm_result["output_tokens"],
        "backend": llm_result.get("backend", "unknown"),
    }


def _harvest_chunked(transcript: str, current_memory: str, config: Config, current_cold: str = "") -> dict:
    """Two-phase chunked harvest: extract candidates per chunk, then one merge call."""
    from memsync.harvest import chunk_transcript

    chunks = chunk_transcript(transcript, config.harvest_chunk_tokens)
    n_chunks = len(chunks)

    if not chunks:
        return {
            "updated_content": current_memory.strip(),
            "updated_cold": current_cold,
            "changed": False,
            "changed_hot": False,
            "changed_cold": False,
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
            "updated_cold": current_cold,
            "changed": False,
            "changed_hot": False,
            "changed_cold": False,
            "truncated": any_truncated,
            "malformed": False,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "backend": last_backend,
            "chunks_processed": n_chunks,
        }

    combined_candidates = "\n".join(candidate_blocks)
    merge_result = merge_candidates_into_memory(combined_candidates, current_memory, config, current_cold)
    merge_result["input_tokens"] += total_input
    merge_result["output_tokens"] += total_output
    merge_result["chunks_processed"] = n_chunks
    merge_result["truncated"] = merge_result["truncated"] or any_truncated
    return merge_result


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


def refresh_memory_content(notes: str, current_memory: str, config: Config, current_cold: str = "") -> dict:
    """
    Call the configured LLM to merge notes into current_memory (hot layer).
    Optionally consults current_cold (archive) to avoid re-adding known facts.
    Returns a dict with keys: updated_content (hot), updated_cold, changed, malformed.
    Does NOT write files — caller handles I/O.
    """
    archive_section = ""
    if config.archive_in_harvest and current_cold:
        truncated = _truncate_archive_for_prompt(current_cold, config.archive_max_lines_in_prompt)
        archive_section = f"\nCOLD ARCHIVE (reference only — never in context):\n{truncated}\n"

    user_prompt = f"""\
HOT MEMORY (always in context — keep under {config.max_hot_lines} lines):
{current_memory}{archive_section}
SESSION NOTES:
{notes}"""

    llm_result = call_llm(SYSTEM_PROMPT, user_prompt, _HOT_DELIMITER, config)

    raw = _strip_model_wrapper(llm_result["text"])
    updated_hot, updated_cold = _parse_tiered_response(raw, current_cold)

    # Reject responses that look like narrative explanations rather than a memory file.
    if not _looks_like_memory_file(updated_hot):
        return {
            "updated_content": raw,
            "updated_cold": current_cold,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
        }

    # Enforce hard constraints in code — model can silently drop them (PITFALLS #1)
    updated_hot = enforce_hard_constraints(current_memory, updated_hot)
    updated_hot = _deduplicate_memory(updated_hot)
    updated_cold = _deduplicate_memory(updated_cold)

    changed_hot = updated_hot != current_memory.strip()
    changed_cold = updated_cold != current_cold.strip()

    return {
        "updated_content": updated_hot,
        "updated_cold": updated_cold,
        "changed": changed_hot or changed_cold,
        "changed_hot": changed_hot,
        "changed_cold": changed_cold,
        "truncated": llm_result["truncated"],
        "malformed": False,
        "input_tokens": llm_result["input_tokens"],
        "output_tokens": llm_result["output_tokens"],
    }


def _deduplicate_memory(content: str) -> str:
    """
    Remove duplicate bullet lines within each section. Keeps the first occurrence.
    Resets dedup scope at each heading so the same bullet can appear in different sections.
    """
    lines = content.splitlines()
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            seen = set()
            result.append(line)
            continue
        stripped = line.strip()
        if stripped and stripped[0] in "-*+":
            key = re.sub(r"^[-*+]\s+", "", stripped.lower()).strip()
            if key in seen:
                continue
            seen.add(key)
        result.append(line)
    return "\n".join(result)


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
