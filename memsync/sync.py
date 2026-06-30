from __future__ import annotations

import logging
import re
from pathlib import Path

from memsync.config import Config, harvest_chunk_tokens_for_backend
from memsync.llm import LLMError, call_llm, call_llm_with_backend, resolve_backends

logger = logging.getLogger(__name__)

_HOT_DELIMITER = "<!-- memsync:hot -->"
_COLD_DELIMITER = "<!-- memsync:cold -->"

# Tolerant matchers used by _parse_tiered_response. The LLM occasionally emits
# tab/extra-space variations inside the delimiter (e.g. "<!--\tmemsync:cold -->")
# and exact-match find() misses them, with destructive fallback behavior.
# See: 2026-05 incident where stray tab-delimited tags routed the entire LLM
# response into the hot layer.
_HOT_DELIM_RE = re.compile(r"<!--\s*memsync:hot\s*-->")
_COLD_DELIM_RE = re.compile(r"<!--\s*memsync:cold\s*-->")

# The system prompts are load-bearing — see PITFALLS.md #8 before editing.
# Specific phrases matter; don't casually reword them.
SYSTEM_PROMPT = """You are maintaining a persistent two-layer global memory for an AI assistant user.
Both files are synced across machines. Only the hot layer is loaded into Claude Code sessions.

HOT layer (GLOBAL_MEMORY.md): always in context — keep under 100 lines AND under 38,000 characters.
  Contains: identity, active priorities, standing preferences, hard constraints.
COLD layer (MEMORY_ARCHIVE.md): never in context — reference only.
  Contains: completed work, historical decisions, resolved items.

YOUR JOB:
- Merge new session notes into the hot layer
- Keep the hot layer tight (under 100 lines, under 38,000 chars) — demote completed or stale items to cold
- Update facts that have changed
- When candidates conflict or describe a state that changed across the session(s) — a config value, a "primary" choice, a status — record ONLY the most current version. An explicit change ("reverted to X", "demoted Y", "now uses Z", "switched from A to B") supersedes the earlier statement; do not keep the superseded value as if it were still true.
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append, always keep them hot
- NEVER add a bullet that already exists verbatim or near-verbatim in the same section
- NEVER add job search status, specific job roles, or application pipeline state — that belongs in the Rolehunt project, not global memory
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

HOT layer (GLOBAL_MEMORY.md): always in context — keep under 100 lines AND under 38,000 characters.
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
- Keep the hot layer tight (under 100 lines, under 38,000 chars) — demote completed or stale items to cold
- Update facts that have changed
- When candidates conflict or describe a state that changed across the session(s) — a config value, a "primary" choice, a status — record ONLY the most current version. An explicit change ("reverted to X", "demoted Y", "now uses Z", "switched from A to B") supersedes the earlier statement; do not keep the superseded value as if it were still true.
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append, always keep them hot
- NEVER add a bullet that already exists verbatim or near-verbatim in the same section
- NEVER add job search status, specific job roles, or application pipeline state — that belongs in the Rolehunt project, not global memory
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

HOT layer (GLOBAL_MEMORY.md): always in context — keep under 100 lines AND under 38,000 characters.
  Contains: identity, active priorities, standing preferences, hard constraints.
COLD layer (MEMORY_ARCHIVE.md): never in context — reference only.
  Contains: completed work, historical decisions, resolved items.

You will receive candidate facts extracted from a recent session. Merge them into the appropriate layer:
- Keep the hot layer tight (under 100 lines, under 38,000 chars) — demote completed or stale items to cold
- Update facts that have changed
- When candidates conflict or describe a state that changed across the session(s) — a config value, a "primary" choice, a status — record ONLY the most current version. An explicit change ("reverted to X", "demoted Y", "now uses Z", "switched from A to B") supersedes the earlier statement; do not keep the superseded value as if it were still true.
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append, always keep them hot
- NEVER add a bullet that already exists verbatim or near-verbatim in the same section
- NEVER add job search status, specific job roles, or application pipeline state — that belongs in the Rolehunt project, not global memory
- If none of the candidates add meaningful new information, return both layers UNCHANGED

RETURN: Begin your response with the first delimiter — no preamble before it:
<!-- memsync:hot -->
[updated GLOBAL_MEMORY.md content]
<!-- memsync:cold -->
[updated MEMORY_ARCHIVE.md content]"""

# Append-only variant: the model never sees or regenerates the full cold archive.
# It returns the updated hot layer plus ONLY the new entries to append to cold.
# This keeps the merge output small (hot only + a small delta) and makes it
# impossible to clobber the archive by echoing back a truncated view of it.
MERGE_SYSTEM_PROMPT_APPEND = """You are maintaining a persistent two-layer global memory for an AI assistant user.
Both files are synced across machines. Only the hot layer is loaded into Claude Code sessions.

HOT layer (GLOBAL_MEMORY.md): always in context — keep under 100 lines AND under 38,000 characters.
  Contains: identity, active priorities, standing preferences, hard constraints.
COLD layer (MEMORY_ARCHIVE.md): never in context — an append-only archive of completed/historical items.

You will receive the current HOT layer and candidate facts extracted from a recent session.
Merge the candidates into the HOT layer:
- Keep the hot layer tight (under 100 lines, under 38,000 chars)
- Update facts that have changed
- When candidates conflict or describe a state that changed across the session(s) — a config value, a "primary" choice, a status — record ONLY the most current version. An explicit change ("reverted to X", "demoted Y", "now uses Z", "switched from A to B") supersedes the earlier statement; do not keep the superseded value as if it were still true.
- Preserve the user's exact voice, formatting, and section structure
- NEVER remove entries under any "Hard constraints" or "Constraints" section — only append, always keep them hot
- NEVER add a bullet that already exists verbatim or near-verbatim in the same section
- NEVER add job search status, specific job roles, or application pipeline state — that belongs in the Rolehunt project, not global memory

When a hot item is completed or stale, move it OUT of the hot layer and place it in the cold section below.
The cold section is APPEND-ONLY: return ONLY the NEW entries to add to the archive (the demoted or
newly-historical items). You are NOT shown the existing archive — do not reproduce or invent it. If nothing
should be archived this round, leave the cold section empty.

RETURN: Begin your response with the first delimiter — no preamble before it:
<!-- memsync:hot -->
[updated GLOBAL_MEMORY.md content]
<!-- memsync:cold -->
[ONLY new entries to append to the archive, or empty]"""


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

    Three branches:
    - both delimiters absent → (text, current_cold). Single-file backward compat
      (tests mock responses without delimiters; treating those as malformed
      would be too aggressive).
    - both delimiters present and in order → parse normally.
    - one delimiter present but the other missing / out of order → ("", current_cold)
      to signal malformed. Downstream _looks_like_memory_file rejects empty
      content and the caller surfaces malformed=True without overwriting hot.
      Previously this branch fell back to (text, current_cold), which silently
      dumped the entire LLM response — analysis prose included — into the hot
      layer when the LLM emitted only a cold delimiter or used a stray tab in
      one of them. See: 2026-05 incident, quarantine_20260510_2116/.

    Whitespace inside the delimiter tags is tolerated via regex matching so
    "<!--\\tmemsync:cold -->" is recognised the same as "<!-- memsync:cold -->".
    """
    hot_match = _HOT_DELIM_RE.search(text)
    cold_match = _COLD_DELIM_RE.search(text)

    if hot_match is None and cold_match is None:
        return text, current_cold

    if (
        hot_match is None
        or cold_match is None
        or cold_match.start() <= hot_match.start()
    ):
        return "", current_cold

    hot = _strip_label_prefix(text[hot_match.end():cold_match.start()].strip())
    cold_raw = text[cold_match.end():].strip()

    # Defensive: if the LLM echoed back the truncation marker we injected for the
    # prompt-only view (see _truncate_archive_for_prompt), the "cold" it returned
    # is just the truncated *view* it saw, not the full archive. Writing it would
    # clobber on-disk data. Reject and keep current_cold as the source of truth.
    # Check the raw segment because _strip_label_prefix below would drop the
    # marker line (it isn't a markdown heading), hiding the evidence.
    # See: 2026-05 incident where MEMORY_ARCHIVE.md collapsed to the marker string.
    if "[ARCHIVE TRUNCATED" in cold_raw:
        return hot, current_cold

    cold = _strip_label_prefix(cold_raw)
    return hot, cold


def _append_cold_delta(raw: str, current_cold: str) -> str:
    """
    Append-only cold: extract ONLY the text after the cold delimiter (the new
    archive entries the model produced) and append it to the existing archive.

    Returns current_cold unchanged when there is no delta. Defends against the
    model echoing the whole archive back (or our truncation marker) — in
    append-only mode the model is never shown the archive, so anything that
    looks like the full archive is treated as no-op rather than appended.
    """
    cold_match = _COLD_DELIM_RE.search(raw)
    if cold_match is None:
        return current_cold

    delta = _strip_label_prefix(raw[cold_match.end():].strip()).strip()
    if not delta or "[ARCHIVE TRUNCATED" in delta:
        return current_cold
    if current_cold.strip() and delta == current_cold.strip():
        return current_cold  # model echoed the archive — ignore

    if not current_cold.strip():
        return _deduplicate_memory(delta)
    combined = current_cold.rstrip() + "\n\n" + delta
    return _deduplicate_memory(combined)


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


SEMANTIC_DEDUPE_PROMPT = """You are cleaning a persistent memory file for an AI assistant. Find and remove semantic duplicates — bullets that express the same policy or fact in different words.

RULES:
- When two bullets say the same thing, keep the more specific or informative version and remove the other
- Never remove bullets from a "Hard constraints" section unless they are semantically identical to another bullet in that same section
- Never change the meaning or wording of any bullet you keep
- Never merge bullets that express related but distinct policies
- Preserve all section headings, blank lines, and non-bullet content exactly
- If you find no semantic duplicates, return the file UNCHANGED

Return ONLY the cleaned file content. No preamble, no explanation."""


def semantic_dedupe_memory(content: str, config: Config) -> str:
    """
    Run an LLM pass over content to find and remove semantic duplicates.
    Returns the cleaned content string. Raises LLMError on failure.
    Uses the standard backend waterfall (no Anthropic API spend if codex is primary).
    """
    from memsync.llm import call_llm
    result = call_llm(
        system=SEMANTIC_DEDUPE_PROMPT,
        user=content,
        prefill="",
        config=config,
    )
    cleaned = result["text"].strip()
    # Guard: if the LLM returned something suspiciously short, reject it
    if len(cleaned) < len(content) * 0.5:
        raise ValueError(
            f"Semantic dedupe response too short ({len(cleaned)} chars vs "
            f"{len(content)} original) — rejecting to avoid data loss."
        )
    return cleaned


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
    from memsync.harvest import chunk_transcript

    errors: list[str] = []
    for backend, _fn in resolve_backends(config):
        backend_chunks = chunk_transcript(
            chunk,
            harvest_chunk_tokens_for_backend(config, backend),
        )
        total_input = 0
        total_output = 0
        any_truncated = False
        candidate_blocks: list[str] = []

        try:
            for i, backend_chunk in enumerate(backend_chunks):
                if i > 0 and config.chunk_inter_call_sleep > 0:
                    import time
                    time.sleep(config.chunk_inter_call_sleep)

                user_prompt = f"TRANSCRIPT SEGMENT:\n{backend_chunk}"
                llm_result = call_llm_with_backend(
                    backend,
                    EXTRACT_SYSTEM_PROMPT,
                    user_prompt,
                    "",
                    config,
                )
                total_input += llm_result["input_tokens"]
                total_output += llm_result["output_tokens"]
                any_truncated = any_truncated or llm_result["truncated"]

                text = llm_result["text"].strip()
                if text and text.upper() != "NONE":
                    candidate_blocks.append(text)

            return {
                "candidates": "\n".join(candidate_blocks),
                "truncated": any_truncated,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "backend": backend,
                "chunks_processed": len(backend_chunks),
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM backend '%s' failed during extract: %s", backend, e)
            errors.append(f"{backend}: {e}")

    raise LLMError("All LLM backends failed:\n" + "\n".join(errors))


def harvest_sessions_batched(
    sessions: list[tuple[str, str]],
    current_memory: str,
    config: Config,
    current_cold: str = "",
    deadline: float | None = None,
) -> dict:
    """
    Batched harvest: extract candidate facts from every session, then merge ALL
    of them into memory in a single pass.

    The merge regenerates the full hot layer, which is the slow step (~minutes),
    so doing it once per run instead of once per session is what keeps the run
    inside its runtime budget. Extraction is many small, fast calls.

    sessions: list of (session_id, transcript) tuples.
    deadline: optional time.monotonic() value; stop extracting once reached and
      merge what was collected so far (the rest retry next run). Reserve enough
      headroom before the deadline for one merge — the caller does this.
    Returns the usual merge result dict plus "harvested_ids" — the session ids
    whose candidates were successfully extracted and folded into the merge.
    A session that fails extraction is left out (not in harvested_ids) so it
    retries next run. If the final merge raises, the caller marks nothing.
    """
    import time

    all_candidates: list[str] = []
    harvested_ids: list[str] = []
    extract_input = 0
    extract_output = 0

    for session_id, transcript in sessions:
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                "batched harvest: extraction budget reached; merging %d collected session(s), "
                "%d deferred to next run",
                len(harvested_ids), len(sessions) - len(harvested_ids),
            )
            break
        if not transcript.strip():
            harvested_ids.append(session_id)  # empty transcript — nothing to extract, won't improve
            continue
        try:
            ext = extract_candidates_from_chunk(transcript, config)
        except Exception:  # noqa: BLE001
            logger.warning("batched harvest: extract failed for %s — will retry next run", session_id)
            continue  # not marked harvested
        extract_input += ext["input_tokens"]
        extract_output += ext["output_tokens"]
        if ext["candidates"].strip():
            all_candidates.append(ext["candidates"].strip())
        harvested_ids.append(session_id)

    combined = "\n".join(all_candidates).strip()
    if not combined:
        return {
            "updated_content": current_memory.strip(),
            "updated_cold": current_cold,
            "changed": False,
            "changed_hot": False,
            "changed_cold": False,
            "truncated": False,
            "malformed": False,
            "input_tokens": extract_input,
            "output_tokens": extract_output,
            "backend": "none",
            "chunks_processed": 0,
            "harvested_ids": harvested_ids,
        }

    result = merge_candidates_into_memory(combined, current_memory, config, current_cold)
    result["input_tokens"] = result.get("input_tokens", 0) + extract_input
    result["output_tokens"] = result.get("output_tokens", 0) + extract_output
    result["harvested_ids"] = harvested_ids
    return result


def _chunk_candidate_facts(candidates: str, max_tokens: int) -> list[str]:
    """Split candidate facts into line-preserving batches for smaller backends."""
    if not candidates.strip():
        return []
    if max_tokens <= 0:
        return [candidates]

    max_chars = max_tokens * 4
    lines = candidates.splitlines()
    batches: list[str] = []
    current: list[str] = []
    current_chars = 0

    for line in lines:
        added_chars = len(line) + (1 if current else 0)
        if current and current_chars + added_chars > max_chars:
            batches.append("\n".join(current).strip())
            current = [line]
            current_chars = len(line)
        else:
            current.append(line)
            current_chars += added_chars

    if current:
        batches.append("\n".join(current).strip())

    return [batch for batch in batches if batch]


def _merge_candidates_batch_with_backend(
    backend: str,
    candidates: str,
    current_memory: str,
    config: Config,
    current_cold: str = "",
) -> dict:
    """Merge one candidate batch using one specific backend."""
    append_only = config.harvest_append_only_cold

    if append_only:
        # Hot only — the model never sees the archive, so it can't clobber it,
        # and the merge output stays small (hot + a tiny cold delta).
        system_prompt = MERGE_SYSTEM_PROMPT_APPEND
        user_prompt = f"""\
HOT MEMORY (always in context — keep under {config.max_hot_lines} lines):
{current_memory}

CANDIDATE FACTS:
{candidates}"""
    else:
        archive_section = ""
        if config.archive_in_harvest and current_cold:
            truncated = _truncate_archive_for_prompt(current_cold, config.archive_max_lines_in_prompt)
            archive_section = f"\nCOLD ARCHIVE (reference only — never in context):\n{truncated}\n"
        system_prompt = MERGE_SYSTEM_PROMPT
        user_prompt = f"""\
HOT MEMORY (always in context — keep under {config.max_hot_lines} lines):
{current_memory}{archive_section}
CANDIDATE FACTS:
{candidates}"""

    llm_result = call_llm_with_backend(
        backend,
        system_prompt,
        user_prompt,
        _HOT_DELIMITER,
        config,
    )
    raw = _strip_model_wrapper(llm_result["text"])
    updated_hot, parsed_cold = _parse_tiered_response(raw, current_cold)

    if not _looks_like_memory_file(updated_hot):
        return {
            "updated_content": raw,
            "updated_cold": current_cold,
            "changed": False,
            "truncated": False,
            "malformed": True,
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
            "backend": llm_result.get("backend", backend),
        }

    updated_hot = enforce_hard_constraints(current_memory, updated_hot)
    updated_hot = _deduplicate_memory(updated_hot)

    if append_only:
        updated_cold = _append_cold_delta(raw, current_cold)
    else:
        updated_cold = _deduplicate_memory(parsed_cold)

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
        "backend": llm_result.get("backend", backend),
    }


def merge_candidates_into_memory(candidates: str, current_memory: str, config: Config, current_cold: str = "") -> dict:
    """
    Merge a bullet list of extracted candidate facts into current_memory via LLM.
    Returns the same dict shape as harvest_memory_content (without token counts,
    which the caller accumulates across all extract calls).
    """
    errors: list[str] = []
    original_hot = current_memory
    original_cold = current_cold

    for backend, _fn in resolve_backends(config):
        candidate_batches = _chunk_candidate_facts(
            candidates,
            harvest_chunk_tokens_for_backend(config, backend),
        )
        next_hot = current_memory
        next_cold = current_cold
        total_input = 0
        total_output = 0
        any_truncated = False

        try:
            for i, batch in enumerate(candidate_batches):
                if i > 0 and config.chunk_inter_call_sleep > 0:
                    import time
                    time.sleep(config.chunk_inter_call_sleep)

                result = _merge_candidates_batch_with_backend(
                    backend,
                    batch,
                    next_hot,
                    config,
                    next_cold,
                )
                total_input += result["input_tokens"]
                total_output += result["output_tokens"]
                any_truncated = any_truncated or result["truncated"]

                if result.get("malformed"):
                    result["input_tokens"] = total_input
                    result["output_tokens"] = total_output
                    result["truncated"] = any_truncated
                    return result

                next_hot = result["updated_content"]
                next_cold = result.get("updated_cold", next_cold)

            updated_hot = next_hot.strip()
            updated_cold = next_cold.strip()
            changed_hot = updated_hot != original_hot.strip()
            changed_cold = updated_cold != original_cold.strip()
            return {
                "updated_content": updated_hot,
                "updated_cold": updated_cold,
                "changed": changed_hot or changed_cold,
                "changed_hot": changed_hot,
                "changed_cold": changed_cold,
                "truncated": any_truncated,
                "malformed": False,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "backend": backend,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM backend '%s' failed during merge: %s", backend, e)
            errors.append(f"{backend}: {e}")

    raise LLMError("All LLM backends failed:\n" + "\n".join(errors))


def _harvest_chunked(transcript: str, current_memory: str, config: Config, current_cold: str = "") -> dict:
    """Two-phase chunked harvest: extract candidates per chunk, then one merge call."""
    from memsync.harvest import chunk_transcript

    primary_backend = resolve_backends(config)[0][0]
    chunks = chunk_transcript(
        transcript,
        harvest_chunk_tokens_for_backend(config, primary_backend),
    )

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
    total_chunks_processed = 0

    for i, chunk in enumerate(chunks):
        if i > 0 and config.chunk_inter_call_sleep > 0:
            import time
            time.sleep(config.chunk_inter_call_sleep)
        result = extract_candidates_from_chunk(chunk, config)
        total_input += result["input_tokens"]
        total_output += result["output_tokens"]
        any_truncated = any_truncated or result["truncated"]
        last_backend = result.get("backend", last_backend)
        total_chunks_processed += result.get("chunks_processed", 1)
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
            "chunks_processed": total_chunks_processed,
        }

    combined_candidates = "\n".join(candidate_blocks)
    merge_result = merge_candidates_into_memory(combined_candidates, current_memory, config, current_cold)
    merge_result["input_tokens"] += total_input
    merge_result["output_tokens"] += total_output
    merge_result["chunks_processed"] = total_chunks_processed
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


def _normalize_bullet(text: str) -> str:
    """Normalize a bullet for fuzzy comparison: lowercase, collapse whitespace, strip punctuation."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _fuzzy_is_duplicate(candidate: str, seen_normalized: list[str], threshold: float = 0.85) -> bool:
    """
    Return True if candidate is a near-duplicate of any already-seen bullet.
    Uses SequenceMatcher ratio on normalized text. Catches variants like
    'as a general recovery step' vs 'as a recovery step' (same rule, different ending).
    """
    import difflib
    norm = _normalize_bullet(candidate)
    if not norm:
        return False
    for existing in seen_normalized:
        if difflib.SequenceMatcher(None, norm, existing).ratio() >= threshold:
            return True
    return False


def _deduplicate_memory(content: str, fuzzy: bool = False) -> str:
    """
    Remove duplicate bullet lines within each section. Keeps the first occurrence.
    Resets dedup scope at each heading so the same bullet can appear in different sections.

    Exact-match only by default — safe to run automatically on every harvest.
    Pass fuzzy=True to also collapse near-duplicates (SequenceMatcher >= 0.85). Fuzzy
    matching is lossy: on short bullets the shared prefix dominates the ratio, so distinct
    items differing only in a trailing token ("batch one"/"batch two", "v1"/"v2") can be
    wrongly merged. It is therefore opt-in and used only by the explicit `memsync dedup`
    command, which previews a diff before writing.
    """
    lines = content.splitlines()
    seen_exact: set[str] = set()
    seen_normalized: list[str] = []
    result: list[str] = []
    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            seen_exact = set()
            seen_normalized = []
            result.append(line)
            continue
        stripped = line.strip()
        if stripped and stripped[0] in "-*+":
            key = re.sub(r"^[-*+]\s+", "", stripped.lower()).strip()
            if key in seen_exact:
                continue
            if fuzzy and _fuzzy_is_duplicate(key, seen_normalized):
                continue
            seen_exact.add(key)
            if fuzzy:
                seen_normalized.append(_normalize_bullet(key))
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
