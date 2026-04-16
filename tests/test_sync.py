from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memsync.config import Config
from memsync.sync import (
    _chunk_transcript,
    _extract_constraints,
    enforce_hard_constraints,
    harvest_memory_content,
    load_or_init_memory,
    log_session_notes,
    refresh_memory_content,
)

SAMPLE_MEMORY = """\
<!-- memsync v0.2 -->
# Global Memory

## Identity & context
- Test user, product leader

## Current priorities
- Finish memsync

## Hard constraints
- Never rewrite from scratch
- Always backup before writing

## Standing preferences
- Concise output
"""


@pytest.mark.smoke
class TestExtractConstraints:
    def test_extracts_bullet_lines(self):
        constraints = _extract_constraints(SAMPLE_MEMORY)
        assert "- Never rewrite from scratch" in constraints
        assert "- Always backup before writing" in constraints

    def test_excludes_other_sections(self):
        constraints = _extract_constraints(SAMPLE_MEMORY)
        assert "- Test user, product leader" not in constraints
        assert "- Finish memsync" not in constraints

    def test_empty_when_no_section(self):
        text = "# Memory\n\n## Identity\n- Some user\n"
        assert _extract_constraints(text) == []

    def test_handles_constraints_heading_variant(self):
        text = "# Memory\n\n## Constraints\n- Rule one\n- Rule two\n"
        constraints = _extract_constraints(text)
        assert "- Rule one" in constraints
        assert "- Rule two" in constraints


@pytest.mark.smoke
class TestEnforceHardConstraints:
    def test_no_op_when_nothing_dropped(self):
        result = enforce_hard_constraints(SAMPLE_MEMORY, SAMPLE_MEMORY)
        assert result == SAMPLE_MEMORY

    def test_reappends_dropped_constraint(self):
        # Simulate model removing one constraint
        dropped = SAMPLE_MEMORY.replace("- Never rewrite from scratch\n", "")
        result = enforce_hard_constraints(SAMPLE_MEMORY, dropped)
        assert "Never rewrite from scratch" in result

    def test_preserves_remaining_content(self):
        dropped = SAMPLE_MEMORY.replace("- Never rewrite from scratch\n", "")
        result = enforce_hard_constraints(SAMPLE_MEMORY, dropped)
        assert "Always backup before writing" in result
        assert "Test user, product leader" in result

    def test_handles_all_constraints_dropped(self):
        # Remove entire section from new content
        lines = [ln for ln in SAMPLE_MEMORY.splitlines()
                 if "Never rewrite" not in ln and "Always backup" not in ln]
        stripped = "\n".join(lines)
        result = enforce_hard_constraints(SAMPLE_MEMORY, stripped)
        assert "Never rewrite from scratch" in result
        assert "Always backup before writing" in result

    def test_handles_no_section_in_new(self):
        old = "# Memory\n\n## Hard constraints\n- Keep this\n"
        new = "# Memory\n\n## Identity\n- User\n"
        result = enforce_hard_constraints(old, new)
        assert "Keep this" in result


@pytest.mark.smoke
class TestLoadOrInitMemory:
    def test_reads_existing_file(self, tmp_path):
        p = tmp_path / "GLOBAL_MEMORY.md"
        p.write_text("# existing", encoding="utf-8")
        assert load_or_init_memory(p) == "# existing"

    def test_returns_template_when_missing(self, tmp_path):
        p = tmp_path / "nonexistent.md"
        result = load_or_init_memory(p)
        assert result.startswith("<!-- memsync v0.2 -->")
        assert "## Hard constraints" in result

    def test_template_has_version_comment(self, tmp_path):
        p = tmp_path / "nonexistent.md"
        result = load_or_init_memory(p)
        assert "<!-- memsync v0.2 -->" in result


class TestLogSessionNotes:
    def test_creates_dated_file(self, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        log_session_notes("Worked on tests", sessions)
        files = list(sessions.glob("*.md"))
        assert len(files) == 1

    def test_appends_on_same_day(self, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        log_session_notes("First note", sessions)
        log_session_notes("Second note", sessions)
        files = list(sessions.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "First note" in content
        assert "Second note" in content

    def test_content_includes_notes(self, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        log_session_notes("my session notes here", sessions)
        content = list(sessions.glob("*.md"))[0].read_text(encoding="utf-8")
        assert "my session notes here" in content


class TestRefreshMemoryContent:
    @staticmethod
    def _llm_result(text: str, truncated: bool = False) -> dict:
        """Return a call_llm-style result dict."""
        return {"text": text, "input_tokens": 10, "output_tokens": 20, "truncated": truncated}

    def test_returns_changed_true_when_content_differs(self):
        config = Config()
        updated = SAMPLE_MEMORY.replace("- Finish memsync", "- Finish memsync\n- New priority")

        with patch("memsync.sync.call_llm", return_value=self._llm_result(updated)):
            result = refresh_memory_content("Added new priority", SAMPLE_MEMORY, config)

        assert result["changed"] is True
        assert "New priority" in result["updated_content"]

    def test_returns_changed_false_when_content_same(self):
        config = Config()

        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY)):
            result = refresh_memory_content("Notes", SAMPLE_MEMORY, config)

        assert result["changed"] is False

    def test_uses_model_from_config(self):
        # Model selection is handled inside llm.py; sync.py just passes config through.
        # Verify call_llm receives the config object (model routing tested in test_llm.py).
        config = Config(gemini_model="gemini-1.5-pro")

        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY)) as mock_llm:
            refresh_memory_content("Notes", SAMPLE_MEMORY, config)

        _, _, _, passed_config = mock_llm.call_args.args
        assert passed_config.gemini_model == "gemini-1.5-pro"

    def test_detects_truncation_via_stop_reason(self):
        config = Config()

        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY, truncated=True)):
            result = refresh_memory_content("Notes", SAMPLE_MEMORY, config)

        assert result["truncated"] is True

    def test_no_truncation_on_end_turn(self):
        config = Config()

        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY, truncated=False)):
            result = refresh_memory_content("Notes", SAMPLE_MEMORY, config)

        assert result["truncated"] is False

    def test_hard_constraints_enforced_even_if_model_drops_them(self):
        config = Config()
        # Model drops one constraint
        without_constraint = SAMPLE_MEMORY.replace("- Never rewrite from scratch\n", "")

        with patch("memsync.sync.call_llm", return_value=self._llm_result(without_constraint)):
            result = refresh_memory_content("Notes", SAMPLE_MEMORY, config)

        assert "Never rewrite from scratch" in result["updated_content"]


class TestChunkTranscript:
    def test_small_transcript_stays_single(self):
        transcript = "[USER]\nHello\n\n---\n\n[ASSISTANT]\nHi there"
        chunks = _chunk_transcript(transcript)
        assert len(chunks) == 1
        assert chunks[0] == transcript

    def test_large_transcript_splits(self):
        # Build a transcript larger than CHUNK_SIZE
        turns = [f"[USER]\n{'x' * 2000}" for _ in range(10)]
        transcript = "\n\n---\n\n".join(turns)
        chunks = _chunk_transcript(transcript)
        assert len(chunks) > 1
        # Reconstructing should give back the original
        reconstructed = "\n\n---\n\n".join(chunks)
        assert reconstructed == transcript

    def test_single_huge_turn_gets_own_chunk(self):
        small = "[USER]\nSmall turn"
        huge = "[USER]\n" + "x" * 20000
        transcript = f"{small}\n\n---\n\n{huge}\n\n---\n\n{small}"
        chunks = _chunk_transcript(transcript)
        # The huge turn should be isolated
        assert any(len(c) > 20000 for c in chunks)


class TestHarvestChunked:
    @staticmethod
    def _llm_result(text: str, truncated: bool = False) -> dict:
        return {"text": text, "input_tokens": 10, "output_tokens": 20, "truncated": truncated}

    def test_small_transcript_uses_single_path(self):
        config = Config()
        short = "[USER]\nDid something small"
        updated = SAMPLE_MEMORY.replace("- Finish memsync", "- Finish memsync\n- Small thing done")

        with patch("memsync.sync.call_llm", return_value=self._llm_result(updated)) as mock:
            result = harvest_memory_content(short, SAMPLE_MEMORY, config)

        assert result["changed"] is True
        # Should only call LLM once (single path, not chunked)
        assert mock.call_count == 1

    def test_large_transcript_uses_chunked_path(self):
        config = Config()
        # Build transcript > CHUNK_THRESHOLD
        turns = [f"[USER]\n{'discussion ' * 200}" for _ in range(10)]
        transcript = "\n\n---\n\n".join(turns)
        assert len(transcript) > 8000

        extract_result = self._llm_result("- Found something notable")
        merge_result = self._llm_result(
            SAMPLE_MEMORY.replace("- Finish memsync", "- Finish memsync\n- Found something notable")
        )

        call_count = [0]
        def fake_llm(system, user, prefill, cfg):
            call_count[0] += 1
            # Last call is the merge pass
            if "CURRENT GLOBAL MEMORY:" in user:
                return merge_result
            return extract_result

        with patch("memsync.sync.call_llm", side_effect=fake_llm):
            result = harvest_memory_content(transcript, SAMPLE_MEMORY, config)

        assert result["changed"] is True
        # Multiple extraction calls + 1 merge call
        assert call_count[0] > 2

    def test_chunked_nothing_notable_returns_unchanged(self):
        config = Config()
        turns = [f"[USER]\n{'chatter ' * 200}" for _ in range(10)]
        transcript = "\n\n---\n\n".join(turns)

        nothing_result = self._llm_result("NOTHING_NOTABLE")

        with patch("memsync.sync.call_llm", return_value=nothing_result):
            result = harvest_memory_content(transcript, SAMPLE_MEMORY, config)

        assert result["changed"] is False
        assert result["malformed"] is False
