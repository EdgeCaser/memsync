from __future__ import annotations

import json
import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memsync.config import Config
from memsync.harvest import (
    cwd_to_project_key,
    find_latest_session,
    find_project_dir,
    list_sessions,
    load_harvested_index,
    read_session_transcript,
    save_harvested_index,
)
from memsync.sync import harvest_memory_content

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
    def _make_mock_response(self, text: str, stop_reason: str = "end_turn",
                            current_memory: str = SAMPLE_MEMORY) -> MagicMock:
        """
        Simulate the API returning a continuation after the prefill.

        With assistant prefill, the API only returns the text *after* the prefill.
        The code then combines: prefill + response.content[0].text.
        So the mock must strip the prefill line from the expected output.
        """
        from memsync.sync import _build_prefill
        prefill = _build_prefill(current_memory)
        if text.startswith(prefill):
            text = text[len(prefill):]
        mock = MagicMock()
        mock.content = [MagicMock(text=text)]
        mock.stop_reason = stop_reason
        return mock

    def test_returns_changed_true_when_content_differs(self):
        config = Config()
        updated = SAMPLE_MEMORY.replace("- Finish harvest feature", "- Finish harvest feature\n- Deploy to Pi")
        mock_response = self._make_mock_response(updated)

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            result = harvest_memory_content("transcript text", SAMPLE_MEMORY, config)

        assert result["changed"] is True
        assert "Deploy to Pi" in result["updated_content"]

    def test_returns_changed_false_when_identical(self):
        config = Config()
        mock_response = self._make_mock_response(SAMPLE_MEMORY)

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            result = harvest_memory_content("transcript text", SAMPLE_MEMORY, config)

        assert result["changed"] is False

    def test_uses_model_from_config(self):
        config = Config(model="claude-haiku-4-5-20251001")
        mock_response = self._make_mock_response(SAMPLE_MEMORY)

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            harvest_memory_content("transcript", SAMPLE_MEMORY, config)

        call_kwargs = mock_client.return_value.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"

    def test_detects_truncation(self):
        config = Config()
        mock_response = self._make_mock_response(SAMPLE_MEMORY, stop_reason="max_tokens")

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            result = harvest_memory_content("transcript", SAMPLE_MEMORY, config)

        assert result["truncated"] is True

    def test_hard_constraints_enforced(self):
        config = Config()
        without_constraint = SAMPLE_MEMORY.replace("- Never rewrite from scratch\n", "")
        mock_response = self._make_mock_response(without_constraint)

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.return_value = mock_response
            result = harvest_memory_content("transcript", SAMPLE_MEMORY, config)

        assert "Never rewrite from scratch" in result["updated_content"]
