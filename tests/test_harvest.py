from __future__ import annotations

import json
import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memsync.config import Config
from memsync.harvest import (
    chunk_transcript,
    cwd_to_project_key,
    find_latest_session,
    find_project_dir,
    list_sessions,
    load_harvested_index,
    read_session_transcript,
    save_harvested_index,
)
from memsync.sync import (
    extract_candidates_from_chunk,
    harvest_memory_content,
    merge_candidates_into_memory,
)

SAMPLE_MEMORY = """\
<!-- memsync v0.2 -->
# Global Memory

## Identity & context
- Test user, product leader

## Current priorities
- Finish harvest feature

## Hard constraints
- Never rewrite from scratch
- Always backup before writing
"""


# ---------------------------------------------------------------------------
# cwd_to_project_key
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestCwdToProjectKey:
    def test_unix_path(self, tmp_path, monkeypatch):
        if platform.system() == "Windows":
            pytest.skip("Unix path test")
        p = Path("/Users/ian/projects/foo")
        assert cwd_to_project_key(p) == "-Users-ian-projects-foo"

    def test_leading_dash(self, tmp_path):
        if platform.system() == "Windows":
            pytest.skip("Unix path test")
        p = Path("/a/b/c")
        key = cwd_to_project_key(p)
        assert key.startswith("-")

    def test_nested_path(self):
        if platform.system() == "Windows":
            pytest.skip("Unix path test")
        p = Path("/Users/ian/Documents/GitHub/memsync")
        assert cwd_to_project_key(p) == "-Users-ian-Documents-GitHub-memsync"

    def test_spaces_in_path_unix(self):
        if platform.system() == "Windows":
            pytest.skip("Unix path test")
        p = Path("/Users/ian/Documents/Untitled Gods Book")
        assert cwd_to_project_key(p) == "-Users-ian-Documents-Untitled-Gods-Book"

    def test_spaces_in_path_windows(self, monkeypatch):
        if platform.system() != "Windows":
            pytest.skip("Windows path test")
        p = Path(r"C:\Users\ianfe\OneDrive\Documents\Untitled Gods Book")
        key = cwd_to_project_key(p)
        assert "Untitled-Gods-Book" in key
        assert " " not in key


# ---------------------------------------------------------------------------
# find_project_dir
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestFindProjectDir:
    def test_returns_path_when_exists(self, tmp_path):
        if platform.system() == "Windows":
            pytest.skip("Unix path test")
        cwd = Path("/Users/ian/projects/foo")
        key = "-Users-ian-projects-foo"
        project_dir = tmp_path / key
        project_dir.mkdir()
        result = find_project_dir(cwd, claude_projects_dir=tmp_path)
        assert result == project_dir

    def test_returns_none_when_missing(self, tmp_path):
        if platform.system() == "Windows":
            pytest.skip("Unix path test")
        cwd = Path("/Users/ian/projects/nonexistent")
        result = find_project_dir(cwd, claude_projects_dir=tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# list_sessions / find_latest_session
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestSessionDiscovery:
    def _make_session(self, directory: Path, name: str, mtime_offset: float = 0) -> Path:
        import time
        path = directory / f"{name}.jsonl"
        path.write_text('{"type":"user","message":{"role":"user","content":"hi"}}', encoding="utf-8")
        t = time.time() + mtime_offset
        import os
        os.utime(path, (t, t))
        return path

    def test_lists_jsonl_files(self, tmp_path):
        self._make_session(tmp_path, "aaa")
        self._make_session(tmp_path, "bbb")
        sessions = list_sessions(tmp_path)
        assert len(sessions) == 2

    def test_sorted_newest_first(self, tmp_path):
        older = self._make_session(tmp_path, "older", mtime_offset=-100)
        newer = self._make_session(tmp_path, "newer", mtime_offset=0)
        sessions = list_sessions(tmp_path)
        assert sessions[0].stem == "newer"
        assert sessions[1].stem == "older"

    def test_ignores_non_jsonl(self, tmp_path):
        (tmp_path / "notes.md").write_text("stuff")
        sessions = list_sessions(tmp_path)
        assert len(sessions) == 0

    def test_find_latest_returns_newest(self, tmp_path):
        self._make_session(tmp_path, "old", mtime_offset=-100)
        self._make_session(tmp_path, "new", mtime_offset=0)
        result = find_latest_session(tmp_path)
        assert result.stem == "new"

    def test_find_latest_skips_excluded(self, tmp_path):
        self._make_session(tmp_path, "old", mtime_offset=-100)
        self._make_session(tmp_path, "new", mtime_offset=0)
        result = find_latest_session(tmp_path, exclude={"new"})
        assert result.stem == "old"

    def test_find_latest_returns_none_when_all_excluded(self, tmp_path):
        self._make_session(tmp_path, "only", mtime_offset=0)
        result = find_latest_session(tmp_path, exclude={"only"})
        assert result is None

    def test_find_latest_returns_none_for_empty_dir(self, tmp_path):
        assert find_latest_session(tmp_path) is None


# ---------------------------------------------------------------------------
# read_session_transcript
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestReadSessionTranscript:
    def _write_jsonl(self, path: Path, entries: list[dict]) -> None:
        lines = [json.dumps(e) for e in entries]
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_extracts_user_string_content(self, tmp_path):
        path = tmp_path / "session.jsonl"
        self._write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "Hello there"}},
        ])
        transcript, count = read_session_transcript(path)
        assert "Hello there" in transcript
        assert count == 1

    def test_extracts_assistant_text_blocks(self, tmp_path):
        path = tmp_path / "session.jsonl"
        self._write_jsonl(path, [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Sure, I can help with that."}
            ]}},
        ])
        transcript, count = read_session_transcript(path)
        assert "Sure, I can help with that." in transcript
        assert count == 1

    def test_skips_tool_result_entries(self, tmp_path):
        path = tmp_path / "session.jsonl"
        self._write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "abc", "content": "file contents"}
            ]}},
        ])
        transcript, count = read_session_transcript(path)
        assert transcript == ""
        assert count == 0

    def test_skips_thinking_blocks(self, tmp_path):
        path = tmp_path / "session.jsonl"
        self._write_jsonl(path, [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "Here is the answer."},
            ]}},
        ])
        transcript, count = read_session_transcript(path)
        assert "Let me think" not in transcript
        assert "Here is the answer" in transcript

    def test_skips_tool_use_blocks(self, tmp_path):
        path = tmp_path / "session.jsonl"
        self._write_jsonl(path, [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {}},
            ]}},
        ])
        transcript, count = read_session_transcript(path)
        assert transcript == ""
        assert count == 0

    def test_skips_non_message_entry_types(self, tmp_path):
        path = tmp_path / "session.jsonl"
        self._write_jsonl(path, [
            {"type": "file-history-snapshot", "snapshot": {}},
            {"type": "progress", "data": {}},
            {"type": "user", "message": {"role": "user", "content": "actual message"}},
        ])
        transcript, count = read_session_transcript(path)
        assert "actual message" in transcript
        assert count == 1

    def test_multi_turn_conversation(self, tmp_path):
        path = tmp_path / "session.jsonl"
        self._write_jsonl(path, [
            {"type": "user", "message": {"role": "user", "content": "What is X?"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "X is Y."}
            ]}},
            {"type": "user", "message": {"role": "user", "content": "Thanks!"}},
        ])
        transcript, count = read_session_transcript(path)
        assert "What is X?" in transcript
        assert "X is Y." in transcript
        assert "Thanks!" in transcript
        assert count == 3

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text("", encoding="utf-8")
        transcript, count = read_session_transcript(path)
        assert transcript == ""
        assert count == 0

    def test_handles_malformed_json_lines_gracefully(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            'not valid json\n'
            '{"type":"user","message":{"role":"user","content":"good line"}}\n',
            encoding="utf-8",
        )
        transcript, count = read_session_transcript(path)
        assert "good line" in transcript


# ---------------------------------------------------------------------------
# Harvest index
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestHarvestIndex:
    def test_returns_empty_dict_when_no_file(self, tmp_path):
        result = load_harvested_index(tmp_path)
        assert result == {}

    def test_round_trip(self, tmp_path):
        original = {"uuid-1": 42, "uuid-2": 7, "uuid-3": 100}
        save_harvested_index(tmp_path, original)
        loaded = load_harvested_index(tmp_path)
        assert loaded == original

    def test_handles_corrupted_file(self, tmp_path):
        (tmp_path / "harvested.json").write_text("not json", encoding="utf-8")
        result = load_harvested_index(tmp_path)
        assert result == {}

    def test_save_is_sorted(self, tmp_path):
        save_harvested_index(tmp_path, {"ccc": 1, "aaa": 2, "bbb": 3})
        raw = json.loads((tmp_path / "harvested.json").read_text(encoding="utf-8"))
        assert list(raw.keys()) == sorted(raw.keys())

    def test_migrates_old_list_format(self, tmp_path):
        # Old harvested.json was a list of stems
        (tmp_path / "harvested.json").write_text(
            '["uuid-a", "uuid-b"]', encoding="utf-8"
        )
        result = load_harvested_index(tmp_path)
        assert result == {"uuid-a": -1, "uuid-b": -1}

    def test_ignores_invalid_dict_entries(self, tmp_path):
        (tmp_path / "harvested.json").write_text(
            '{"good": 5, "bad": "not-an-int"}', encoding="utf-8"
        )
        result = load_harvested_index(tmp_path)
        assert result == {"good": 5}


# ---------------------------------------------------------------------------
# harvest_memory_content (mocked API)
# ---------------------------------------------------------------------------

class TestHarvestMemoryContent:
    @staticmethod
    def _llm_result(text: str, truncated: bool = False) -> dict:
        """Return a call_llm-style result dict."""
        return {"text": text, "input_tokens": 10, "output_tokens": 20, "truncated": truncated}

    def test_returns_changed_true_when_content_differs(self):
        config = Config(harvest_chunk_tokens=0)
        updated = SAMPLE_MEMORY.replace("- Finish harvest feature", "- Finish harvest feature\n- Deploy to Pi")

        with patch("memsync.sync.call_llm", return_value=self._llm_result(updated)):
            result = harvest_memory_content("transcript text", SAMPLE_MEMORY, config)

        assert result["changed"] is True
        assert "Deploy to Pi" in result["updated_content"]

    def test_returns_changed_false_when_identical(self):
        config = Config(harvest_chunk_tokens=0)

        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY)):
            result = harvest_memory_content("transcript text", SAMPLE_MEMORY, config)

        assert result["changed"] is False

    def test_uses_model_from_config(self):
        # Model selection is handled inside llm.py; sync.py just passes config through.
        config = Config(gemini_model="gemini-1.5-pro", harvest_chunk_tokens=0)

        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY)) as mock_llm:
            harvest_memory_content("transcript", SAMPLE_MEMORY, config)

        _, _, _, passed_config = mock_llm.call_args.args
        assert passed_config.gemini_model == "gemini-1.5-pro"

    def test_detects_truncation(self):
        config = Config(harvest_chunk_tokens=0)

        with patch("memsync.sync.call_llm", return_value=self._llm_result(SAMPLE_MEMORY, truncated=True)):
            result = harvest_memory_content("transcript", SAMPLE_MEMORY, config)

        assert result["truncated"] is True

    def test_hard_constraints_enforced(self):
        config = Config(harvest_chunk_tokens=0)
        without_constraint = SAMPLE_MEMORY.replace("- Never rewrite from scratch\n", "")

        with patch("memsync.sync.call_llm", return_value=self._llm_result(without_constraint)):
            result = harvest_memory_content("transcript", SAMPLE_MEMORY, config)

        assert "Never rewrite from scratch" in result["updated_content"]


# ---------------------------------------------------------------------------
# chunk_transcript
# ---------------------------------------------------------------------------

SEPARATOR = "\n\n---\n\n"


def _make_transcript(*turns: str) -> str:
    return SEPARATOR.join(turns)


class TestChunkTranscript:
    def test_empty_returns_empty_list(self):
        assert chunk_transcript("", max_tokens=1000) == []
        assert chunk_transcript("   \n  ", max_tokens=1000) == []

    def test_short_transcript_is_single_chunk(self):
        t = _make_transcript("[USER]\nHello", "[ASSISTANT]\nHi there")
        chunks = chunk_transcript(t, max_tokens=1000)
        assert len(chunks) == 1
        assert chunks[0] == t

    def test_long_transcript_splits_into_multiple_chunks(self):
        # Each turn is ~30 chars; max_tokens=5 → max_chars=20. Forces a split after the first turn.
        turn_a = "[USER]\nHello there"      # 18 chars
        turn_b = "[ASSISTANT]\nHi back"     # 20 chars
        turn_c = "[USER]\nThanks a lot"     # 19 chars
        t = _make_transcript(turn_a, turn_b, turn_c)
        chunks = chunk_transcript(t, max_tokens=5)  # 20 chars max
        assert len(chunks) > 1

    def test_chunks_contain_only_whole_turns(self):
        turns = [f"[USER]\nMessage number {i}" for i in range(10)]
        t = _make_transcript(*turns)
        chunks = chunk_transcript(t, max_tokens=10)  # small to force many chunks
        # Recombine and confirm all turns are present
        recombined = SEPARATOR.join(chunks)
        assert recombined == t

    def test_oversized_single_turn_becomes_its_own_chunk(self):
        # A turn that alone exceeds max_chars must not be dropped.
        long_turn = "[USER]\n" + "x" * 1000
        short_turn = "[USER]\nshort"
        t = _make_transcript(long_turn, short_turn)
        chunks = chunk_transcript(t, max_tokens=10)  # 40 chars — long_turn alone exceeds this
        assert len(chunks) == 2
        assert long_turn in chunks[0]
        assert short_turn in chunks[1]

    def test_single_turn_is_not_split(self):
        t = "[USER]\nJust one turn"
        chunks = chunk_transcript(t, max_tokens=1)  # tiny budget
        assert len(chunks) == 1
        assert chunks[0] == t

    def test_no_content_lost_across_chunk_boundary(self):
        turns = ["[USER]\nA", "[ASSISTANT]\nB", "[USER]\nC", "[ASSISTANT]\nD"]
        t = _make_transcript(*turns)
        chunks = chunk_transcript(t, max_tokens=3)  # ~12 chars, forces splits
        recombined = SEPARATOR.join(chunks)
        for turn in turns:
            assert turn in recombined


# ---------------------------------------------------------------------------
# Two-phase chunked harvest
# ---------------------------------------------------------------------------

class TestChunkedHarvest:
    @staticmethod
    def _extract_result(candidates: str = "", tokens: int = 5) -> dict:
        return {"candidates": candidates, "input_tokens": tokens, "output_tokens": tokens}

    @staticmethod
    def _llm_result(text: str, truncated: bool = False) -> dict:
        return {"text": text, "input_tokens": 10, "output_tokens": 10, "truncated": truncated}

    def test_empty_transcript_returns_unchanged(self):
        config = Config(harvest_chunk_tokens=100)
        result = harvest_memory_content("", SAMPLE_MEMORY, config)
        assert result["changed"] is False
        assert result["input_tokens"] == 0

    def test_short_transcript_still_runs_extract_then_merge(self):
        # Transcript fits in one chunk → 1 extract call + 1 merge call = 2 total
        config = Config(harvest_chunk_tokens=6000)
        updated = SAMPLE_MEMORY.replace("- Finish harvest feature", "- Finish harvest feature\n- New item")
        call_count = []

        def fake_llm(system, user, prefill, cfg):
            call_count.append(system[:20])
            if "scanning" in system.lower():
                # extract call → return a candidate fact
                return {"text": "- New item", "input_tokens": 5, "output_tokens": 5, "truncated": False}
            # merge call → return updated memory
            return {"text": updated, "input_tokens": 10, "output_tokens": 10, "truncated": False}

        with patch("memsync.sync.call_llm", side_effect=fake_llm):
            result = harvest_memory_content("[USER]\nDid a thing", SAMPLE_MEMORY, config)

        assert len(call_count) == 2
        assert result["changed"] is True

    def test_all_chunks_return_none_skips_merge(self):
        # When every chunk yields no candidates, merge should not be called.
        config = Config(harvest_chunk_tokens=6000)
        call_count = []

        def fake_llm(system, user, prefill, cfg):
            call_count.append(1)
            return {"text": "NONE", "input_tokens": 3, "output_tokens": 1, "truncated": False}

        with patch("memsync.sync.call_llm", side_effect=fake_llm):
            result = harvest_memory_content("[USER]\nNothing important", SAMPLE_MEMORY, config)

        # Only the extract call(s) fired; no merge.
        assert len(call_count) == 1
        assert result["changed"] is False
        assert result["input_tokens"] > 0  # extract tokens still counted

    def test_multiple_chunks_each_call_extract(self):
        # Force 3 chunks with a tiny chunk size.
        turns = [f"[USER]\n{'word ' * 50}turn {i}" for i in range(3)]
        transcript = SEPARATOR.join(turns)
        config = Config(harvest_chunk_tokens=20)  # ~80 chars — each turn forces a new chunk

        extract_calls = []
        merge_calls = []
        updated = SAMPLE_MEMORY.replace("- Finish harvest feature", "- Finish harvest feature\n- Done")

        def fake_llm(system, user, prefill, cfg):
            if "scanning" in system.lower():
                extract_calls.append(1)
                return {"text": "- Something happened", "input_tokens": 5, "output_tokens": 3, "truncated": False}
            merge_calls.append(1)
            return {"text": updated, "input_tokens": 10, "output_tokens": 10, "truncated": False}

        with patch("memsync.sync.call_llm", side_effect=fake_llm):
            result = harvest_memory_content(transcript, SAMPLE_MEMORY, config)

        assert len(extract_calls) == 3
        assert len(merge_calls) == 1
        assert result["changed"] is True
        # Token counts accumulate across all calls
        assert result["input_tokens"] == 3 * 5 + 10

    def test_hard_constraints_enforced_after_merge(self):
        config = Config(harvest_chunk_tokens=6000)
        without_constraint = SAMPLE_MEMORY.replace("- Never rewrite from scratch\n", "")

        def fake_llm(system, user, prefill, cfg):
            if "scanning" in system.lower():
                return {"text": "- Some new fact", "input_tokens": 3, "output_tokens": 2, "truncated": False}
            return {"text": without_constraint, "input_tokens": 10, "output_tokens": 10, "truncated": False}

        with patch("memsync.sync.call_llm", side_effect=fake_llm):
            result = harvest_memory_content("[USER]\nSomething", SAMPLE_MEMORY, config)

        assert "Never rewrite from scratch" in result["updated_content"]

    def test_chunk_tokens_zero_uses_one_shot_path(self):
        # harvest_chunk_tokens=0 should call LLM exactly once (the original path).
        config = Config(harvest_chunk_tokens=0)
        call_count = []

        def fake_llm(system, user, prefill, cfg):
            call_count.append(1)
            return {"text": SAMPLE_MEMORY, "input_tokens": 10, "output_tokens": 5, "truncated": False}

        with patch("memsync.sync.call_llm", side_effect=fake_llm):
            harvest_memory_content("[USER]\nSomething", SAMPLE_MEMORY, config)

        assert len(call_count) == 1
