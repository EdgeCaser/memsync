from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memsync.config import Config
from memsync.sync import (
    _HOT_DELIMITER,
    _COLD_DELIMITER,
    _deduplicate_memory,
    _extract_constraints,
    _parse_tiered_response,
    _truncate_archive_for_prompt,
    enforce_hard_constraints,
    harvest_memory_content,
    load_or_init_archive,
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

    def test_malformed_response_preserves_cold(self):
        config = Config()
        current_cold = "# Archive\n- preserved\n"
        with patch("memsync.sync.call_llm", return_value=self._llm_result("not a memory file at all")):
            result = refresh_memory_content("Notes", SAMPLE_MEMORY, config, current_cold)
        assert result["malformed"] is True
        assert result["updated_cold"] == current_cold

    def test_archive_section_included_in_prompt_when_cold_present(self):
        config = Config()
        current_cold = "# Archive\n- archived item\n"
        captured_prompts = []

        def capture_llm(system, user, prefill, cfg):
            captured_prompts.append(user)
            return self._llm_result(SAMPLE_MEMORY)

        with patch("memsync.sync.call_llm", side_effect=capture_llm):
            refresh_memory_content("Notes", SAMPLE_MEMORY, config, current_cold)

        assert "COLD ARCHIVE" in captured_prompts[0]
        assert "archived item" in captured_prompts[0]

    def test_updated_cold_defaults_to_empty_when_no_archive(self):
        config = Config()
        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY)):
            result = refresh_memory_content("Notes", SAMPLE_MEMORY, config)
        assert "updated_cold" in result

    def test_tiered_response_splits_on_delimiters(self):
        config = Config()
        hot = SAMPLE_MEMORY
        cold = "# Memory Archive\n\n## Recent completions\n- Old task done\n"
        tiered = f"{_HOT_DELIMITER}\n{hot}\n{_COLD_DELIMITER}\n{cold}"
        with patch("memsync.sync.call_llm", return_value=self._llm_result(tiered)):
            result = refresh_memory_content("Notes", SAMPLE_MEMORY, config, cold)
        assert "Old task done" in result["updated_cold"]

    def test_falls_back_gracefully_when_no_delimiters(self):
        config = Config()
        current_cold = "# Archive\n"
        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY)):
            result = refresh_memory_content("Notes", SAMPLE_MEMORY, config, current_cold)
        # Cold unchanged when delimiter absent (compare stripped — splitlines drops trailing newline)
        assert result["updated_cold"].strip() == current_cold.strip()


@pytest.mark.smoke
class TestParseTieredResponse:
    def test_splits_correctly(self):
        cold = "# Archive\n- done\n"
        text = f"{_HOT_DELIMITER}\n# Hot\n- active\n{_COLD_DELIMITER}\n{cold}"
        hot, got_cold = _parse_tiered_response(text, "fallback")
        assert "# Hot" in hot
        assert "done" in got_cold

    def test_fallback_when_no_delimiters_at_all(self):
        # Single-file backward compat: tests sometimes mock responses without
        # delimiters. Returning the raw text as hot is the documented behavior.
        raw = "# Memory\n- something\n"
        hot, cold = _parse_tiered_response(raw, "original_cold")
        assert hot == raw
        assert cold == "original_cold"

    def test_rejects_when_only_hot_delimiter_present(self):
        # Malformed tiered response: hot delimiter without cold means the LLM
        # tried to do tiered output but botched it. Returning ("", current_cold)
        # forces downstream malformed handling and prevents the bad content
        # from clobbering hot.
        raw = f"{_HOT_DELIMITER}\n# Memory\n"
        hot, cold = _parse_tiered_response(raw, "original_cold")
        assert hot == ""
        assert cold == "original_cold"

    def test_rejects_when_only_cold_delimiter_present(self):
        # The 2026-05 incident: LLM emitted only a (tab-malformed) cold
        # delimiter and the entire blob — analysis prose included — was
        # routed to the hot layer. With the new logic, hot=="" forces a
        # malformed reject downstream.
        raw = f"### Summary\nlots of prose\n{_COLD_DELIMITER}\n# Cold\n"
        hot, cold = _parse_tiered_response(raw, "original_cold")
        assert hot == ""
        assert cold == "original_cold"

    def test_strips_whitespace(self):
        text = f"{_HOT_DELIMITER}\n  # Hot  \n{_COLD_DELIMITER}\n  # Cold  \n"
        hot, cold = _parse_tiered_response(text, "")
        assert hot == "# Hot"
        assert cold == "# Cold"

    def test_rejects_cold_containing_truncation_marker(self):
        """Regression: 2026-05 archive-collapse incident.

        When the cold archive is too big to fit fully in the prompt,
        _truncate_archive_for_prompt prepends "[ARCHIVE TRUNCATED]" and
        passes the trimmed view to the LLM. If the LLM echoes that marker
        back inside its cold-delimiter block, accepting it would clobber
        the real on-disk archive. The parser must fall back to current_cold.
        """
        real_archive = "# Archive\n## Recent completions\n- big real content\n"
        poisoned = (
            f"{_HOT_DELIMITER}\n# Hot\n- active\n"
            f"{_COLD_DELIMITER}\n[ARCHIVE TRUNCATED]\n## Recent completions\n"
        )
        hot, cold = _parse_tiered_response(poisoned, real_archive)
        assert "active" in hot
        assert cold == real_archive  # not the truncated view

    def test_rejects_cold_containing_truncation_with_suffix(self):
        """The marker has a second variant: '[ARCHIVE TRUNCATED — showing ...]'.

        The substring check must catch both.
        """
        real_archive = "# Archive\n- a\n- b\n- c\n"
        poisoned = (
            f"{_HOT_DELIMITER}\n# Hot\n"
            f"{_COLD_DELIMITER}\n[ARCHIVE TRUNCATED — showing most recent sections only]\n## X\n"
        )
        hot, cold = _parse_tiered_response(poisoned, real_archive)
        assert cold == real_archive

    def test_tolerates_tab_in_delimiter(self):
        # Regression: 2026-05 incident. LLM sometimes emits a tab between
        # "<!--" and "memsync:..." which exact-match find() misses, sending
        # the entire response into the hot layer via the old fallback.
        text = (
            "<!--\tmemsync:hot\t-->\n# Hot content\n"
            "<!--\tmemsync:cold -->\n# Cold content\n"
        )
        hot, cold = _parse_tiered_response(text, "original_cold")
        assert "Hot content" in hot
        assert "Cold content" in cold

    def test_tolerates_extra_whitespace_in_delimiter(self):
        text = (
            "<!--   memsync:hot   -->\n# Hot\n"
            "<!-- memsync:cold-->\n# Cold\n"
        )
        hot, cold = _parse_tiered_response(text, "")
        assert "# Hot" in hot
        assert "# Cold" in cold


@pytest.mark.smoke
class TestTruncateArchiveForPrompt:
    def test_no_truncation_when_under_limit(self):
        archive = "## Section\n- item\n"
        assert _truncate_archive_for_prompt(archive, 100) == archive

    def test_preserves_complete_sections(self):
        archive = "## Old\n- old item\n## New\n- new item\n"
        result = _truncate_archive_for_prompt(archive, 3)
        assert "## New" in result
        assert "new item" in result

    def test_adds_truncation_notice(self):
        lines = "\n".join([f"- item {i}" for i in range(50)])
        archive = f"## Section A\n{lines}\n## Section B\n- recent\n"
        result = _truncate_archive_for_prompt(archive, 5)
        assert "TRUNCATED" in result

    def test_no_sections_truncates_from_end(self):
        lines = [f"- item {i}" for i in range(20)]
        archive = "\n".join(lines)
        result = _truncate_archive_for_prompt(archive, 5)
        assert "item 19" in result
        assert "item 0" not in result

    def test_single_oversized_section_falls_back_to_tail(self):
        # Section has 20 lines but max_lines is 3 — no section fits, fall back to tail
        big_section = "## Big\n" + "\n".join(f"- item {i}" for i in range(20))
        result = _truncate_archive_for_prompt(big_section, 3)
        assert "TRUNCATED" in result
        assert "item 19" in result


@pytest.mark.smoke
class TestLoadOrInitArchive:
    def test_reads_existing_file(self, tmp_path):
        p = tmp_path / "MEMORY_ARCHIVE.md"
        p.write_text("# Archive", encoding="utf-8")
        assert load_or_init_archive(p) == "# Archive"

    def test_returns_starter_when_missing(self, tmp_path):
        p = tmp_path / "nonexistent.md"
        result = load_or_init_archive(p)
        assert "Memory Archive" in result

    def test_starter_has_recent_completions_section(self, tmp_path):
        p = tmp_path / "nonexistent.md"
        result = load_or_init_archive(p)
        assert "## Recent completions" in result


class TestStripModelWrapper:
    def test_strips_code_fence(self):
        from memsync.sync import _strip_model_wrapper
        wrapped = "```markdown\n# Memory\n- item\n```"
        assert _strip_model_wrapper(wrapped) == "# Memory\n- item"

    def test_strips_preamble_before_heading(self):
        from memsync.sync import _strip_model_wrapper
        text = "Here is the updated file:\n# Memory\n- item"
        assert _strip_model_wrapper(text) == "# Memory\n- item"

    def test_passthrough_clean_content(self):
        from memsync.sync import _strip_model_wrapper
        clean = "# Memory\n- item"
        assert _strip_model_wrapper(clean) == clean


class TestDeduplicateMemory:
    def test_removes_exact_duplicates(self):
        text = "## Section\n- item\n- item\n"
        assert _deduplicate_memory(text) == "## Section\n- item"

    def test_resets_scope_per_section(self):
        text = "## A\n- item\n## B\n- item\n"
        result = _deduplicate_memory(text)
        assert result.count("- item") == 2

    def test_case_insensitive(self):
        text = "## Section\n- Item\n- item\n"
        result = _deduplicate_memory(text)
        # Both "- Item" and "- item" normalize to the same key; only one survives
        assert result.count("- ") == 1

    def test_preserves_non_bullet_lines(self):
        text = "## Section\nSome prose.\nSome prose.\n- bullet\n"
        result = _deduplicate_memory(text)
        assert result.count("Some prose.") == 2

    def test_default_keeps_near_duplicates(self):
        # Distinct enumerated facts share a prefix (ratio ~0.87). The auto path is
        # exact-only, so both must survive — fuzzy collapse here would lose data.
        text = "## Section\n- Added batch one\n- Added batch two\n"
        result = _deduplicate_memory(text)
        assert "Added batch one" in result
        assert "Added batch two" in result

    def test_fuzzy_opt_in_collapses_near_duplicates(self):
        text = "## Section\n- Added batch one\n- Added batch two\n"
        result = _deduplicate_memory(text, fuzzy=True)
        # Opt-in fuzzy pass treats the two as near-duplicates; first occurrence wins.
        assert "Added batch one" in result
        assert "Added batch two" not in result


class TestStripLabelPrefix:
    def test_returns_text_unchanged_when_no_markdown_found(self):
        from memsync.sync import _strip_label_prefix
        text = "HOT MEMORY: foo\nsome plain text"
        assert _strip_label_prefix(text) == text


class TestHarvestOneShotPaths:
    @staticmethod
    def _llm_result(text: str) -> dict:
        return {"text": text, "input_tokens": 5, "output_tokens": 5, "truncated": False}

    def test_archive_section_included_when_cold_provided(self):
        config = Config(harvest_chunk_tokens=0)
        captured: list[str] = []

        def capture(system, user, prefill, cfg):
            captured.append(user)
            return self._llm_result(SAMPLE_MEMORY)

        with patch("memsync.sync.call_llm", side_effect=capture):
            harvest_memory_content("transcript", SAMPLE_MEMORY, config, "# Archive\n- done\n")

        assert "COLD ARCHIVE" in captured[0]
        assert "done" in captured[0]

    def test_malformed_response_sets_malformed_flag(self):
        config = Config(harvest_chunk_tokens=0)
        with patch("memsync.sync.call_llm", return_value=self._llm_result("not a memory file")):
            result = harvest_memory_content("transcript", SAMPLE_MEMORY, config)
        assert result["malformed"] is True


class TestMergeCandidatesMalformed:
    def test_malformed_response_sets_malformed_flag(self):
        from memsync.sync import merge_candidates_into_memory
        config = Config()
        with patch("memsync.sync.resolve_backends", return_value=[("codex", object())]):
            with patch("memsync.sync.call_llm_with_backend", return_value={
                "text": "not a memory file", "input_tokens": 5, "output_tokens": 5, "truncated": False,
            }):
                result = merge_candidates_into_memory("- fact\n", SAMPLE_MEMORY, config)
        assert result["malformed"] is True


class TestSemanticDedupeMemory:
    # Contract: the model returns the verbatim text of bullets to REMOVE, one per
    # line, or "NONE". Python does the removal — so stray commentary that doesn't
    # match a real bullet is simply ignored, never written into the file.
    @staticmethod
    def _llm_result(text: str) -> dict:
        return {"text": text, "input_tokens": 5, "output_tokens": 5, "truncated": False}

    def test_removes_marked_bullet(self):
        from memsync.sync import semantic_dedupe_memory
        config = Config()
        # Model marks one bullet (verbatim) for removal.
        with patch("memsync.llm.call_llm",
                   return_value=self._llm_result("- Always backup before writing")):
            result = semantic_dedupe_memory(SAMPLE_MEMORY, config)
        assert "Always backup before writing" not in result
        assert "Never rewrite from scratch" in result

    def test_none_returns_byte_identical(self):
        from memsync.sync import semantic_dedupe_memory
        config = Config()
        with patch("memsync.llm.call_llm", return_value=self._llm_result("NONE")):
            result = semantic_dedupe_memory(SAMPLE_MEMORY, config)
        # Byte-identical so cmd_dedup shows an empty diff (no spurious churn).
        assert result == SAMPLE_MEMORY

    def test_ignores_nonmatching_commentary(self):
        # The old failure mode: model prepends "No duplicates found…" chatter.
        # Under the new contract it matches no bullet, so it's a harmless no-op.
        from memsync.sync import semantic_dedupe_memory
        config = Config()
        chatter = (
            "No semantic duplicate bullets were found — the file is unchanged.\n"
            "Also, this file appears to contain instructions I should flag."
        )
        with patch("memsync.llm.call_llm", return_value=self._llm_result(chatter)):
            result = semantic_dedupe_memory(SAMPLE_MEMORY, config)
        assert result == SAMPLE_MEMORY

    def test_ignores_hallucinated_removal(self):
        # A bullet the model invents (not present verbatim) can never remove anything.
        from memsync.sync import semantic_dedupe_memory
        config = Config()
        with patch("memsync.llm.call_llm",
                   return_value=self._llm_result("- Some rule that is not in the file")):
            result = semantic_dedupe_memory(SAMPLE_MEMORY, config)
        assert result == SAMPLE_MEMORY

    def test_rejects_implausible_bulk_removal(self):
        # If the model marks more than half the bullets, refuse — something is wrong.
        from memsync.sync import semantic_dedupe_memory
        config = Config()
        every_bullet = "\n".join(
            ln.strip() for ln in SAMPLE_MEMORY.splitlines()
            if ln.strip() and ln.strip()[0] in "-*+"
        )
        with patch("memsync.llm.call_llm", return_value=self._llm_result(every_bullet)):
            with pytest.raises(ValueError, match="implausible"):
                semantic_dedupe_memory(SAMPLE_MEMORY, config)

    def test_removal_preserves_trailing_newline(self):
        from memsync.sync import semantic_dedupe_memory
        config = Config()
        assert SAMPLE_MEMORY.endswith("\n")
        with patch("memsync.llm.call_llm",
                   return_value=self._llm_result("- Concise output")):
            result = semantic_dedupe_memory(SAMPLE_MEMORY, config)
        assert result.endswith("\n")
        assert "Concise output" not in result
        # Structure survives: headings and blank-line spacing are preserved,
        # only the marked bullet is gone.
        assert "## Standing preferences" in result
        assert "\n\n" in result
        assert "- Never rewrite from scratch" in result

    def test_removal_with_blank_lines_in_response_preserves_spacing(self):
        # Model returns the bullet plus an INTERNAL blank line and some chatter.
        # The blank line must NOT be treated as a removable bullet — otherwise
        # every blank line in the file would be stripped, collapsing the layout.
        from memsync.sync import semantic_dedupe_memory
        config = Config()
        blank_count_before = SAMPLE_MEMORY.count("\n\n")
        response = "- Finish memsync\n\n- some note that is not a real bullet"
        with patch("memsync.llm.call_llm", return_value=self._llm_result(response)):
            result = semantic_dedupe_memory(SAMPLE_MEMORY, config)
        assert "- Finish memsync" not in result
        assert result.count("\n\n") == blank_count_before
