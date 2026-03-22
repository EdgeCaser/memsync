from __future__ import annotations

import json

from memsync.usage import (
    _cost,
    _price_for_model,
    append_usage,
    format_summary,
    load_usage,
    usage_log_path,
)


class TestPriceForModel:
    def test_known_opus_model(self):
        inp, out = _price_for_model("claude-opus-4-20250514")
        assert inp == 15.0
        assert out == 75.0

    def test_known_sonnet_model(self):
        inp, out = _price_for_model("claude-sonnet-4-20250514")
        assert inp == 3.0
        assert out == 15.0

    def test_known_haiku_model(self):
        inp, out = _price_for_model("claude-haiku-4-some-date")
        assert inp == 0.80
        assert out == 4.0

    def test_fallback_for_unknown_model(self):
        inp, out = _price_for_model("totally-unknown-model")
        assert inp == 3.0  # _FALLBACK_INPUT
        assert out == 15.0  # _FALLBACK_OUTPUT


class TestCost:
    def test_basic_cost_calculation(self):
        # 1M input tokens at $3/MTok + 1M output tokens at $15/MTok = $18
        result = _cost("claude-sonnet-4-20250514", 1_000_000, 1_000_000)
        assert result == 18.0

    def test_zero_tokens(self):
        result = _cost("claude-opus-4-20250514", 0, 0)
        assert result == 0.0


class TestUsageLogPath:
    def test_returns_jsonl_path(self, tmp_path):
        result = usage_log_path(tmp_path)
        assert result == tmp_path / "usage.jsonl"


class TestAppendUsage:
    def test_creates_entry(self, tmp_path):
        append_usage(tmp_path, "refresh", "claude-sonnet-4-20250514", 1000, 500)
        path = tmp_path / "usage.jsonl"
        assert path.exists()
        entry = json.loads(path.read_text(encoding="utf-8").strip())
        assert entry["command"] == "refresh"
        assert entry["model"] == "claude-sonnet-4-20250514"
        assert entry["input_tokens"] == 1000
        assert entry["output_tokens"] == 500
        assert "ts" in entry
        assert "machine" in entry
        assert "cost_usd" in entry

    def test_appends_multiple_entries(self, tmp_path):
        append_usage(tmp_path, "refresh", "claude-sonnet-4-20250514", 100, 50)
        append_usage(tmp_path, "harvest", "claude-sonnet-4-20250514", 200, 100)
        lines = (tmp_path / "usage.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["command"] == "refresh"
        assert json.loads(lines[1])["command"] == "harvest"

    def test_records_session_id_and_changed(self, tmp_path):
        append_usage(
            tmp_path, "harvest", "claude-sonnet-4-20250514",
            100, 50, session_id="abc-123", changed=True,
        )
        entry = json.loads((tmp_path / "usage.jsonl").read_text(encoding="utf-8").strip())
        assert entry["session"] == "abc-123"
        assert entry["changed"] is True


class TestLoadUsage:
    def test_returns_empty_when_no_file(self, tmp_path):
        result = load_usage(tmp_path)
        assert result == []

    def test_returns_empty_for_empty_file(self, tmp_path):
        (tmp_path / "usage.jsonl").write_text("", encoding="utf-8")
        result = load_usage(tmp_path)
        assert result == []

    def test_loads_entries(self, tmp_path):
        append_usage(tmp_path, "refresh", "claude-sonnet-4-20250514", 100, 50)
        append_usage(tmp_path, "harvest", "claude-sonnet-4-20250514", 200, 100)
        result = load_usage(tmp_path)
        assert len(result) == 2
        assert result[0]["command"] == "refresh"
        assert result[1]["command"] == "harvest"

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "usage.jsonl"
        path.write_text(
            '{"command": "refresh", "input_tokens": 100}\n'
            "this is not json\n"
            '{"command": "harvest", "input_tokens": 200}\n',
            encoding="utf-8",
        )
        result = load_usage(tmp_path)
        assert len(result) == 2

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / "usage.jsonl"
        path.write_text(
            '{"command": "refresh"}\n'
            "\n"
            '{"command": "harvest"}\n',
            encoding="utf-8",
        )
        result = load_usage(tmp_path)
        assert len(result) == 2


class TestFormatSummary:
    def test_no_entries(self):
        result = format_summary([])
        assert result == "No usage recorded yet."

    def test_with_entries(self):
        entries = [
            {
                "ts": "2026-03-21T10:00:00+00:00",
                "machine": "laptop",
                "command": "refresh",
                "model": "claude-sonnet-4-20250514",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost_usd": 0.0105,
                "changed": True,
            },
        ]
        result = format_summary(entries)
        assert "All time:" in result
        assert "1,000" in result  # input tokens
        assert "500" in result  # output tokens
        assert "$0.0105" in result
        assert "laptop" in result
        assert "Recent (last 10):" in result

    def test_no_activity_this_month(self):
        entries = [
            {
                "ts": "2020-01-01T10:00:00+00:00",
                "machine": "old-machine",
                "command": "refresh",
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_usd": 0.001,
            },
        ]
        result = format_summary(entries)
        assert "No activity this month." in result

    def test_machine_breakdown(self):
        entries = [
            {"ts": "2026-03-21T10:00:00+00:00", "machine": "mac", "command": "refresh",
             "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001},
            {"ts": "2026-03-21T11:00:00+00:00", "machine": "windows", "command": "harvest",
             "input_tokens": 200, "output_tokens": 100, "cost_usd": 0.002},
            {"ts": "2026-03-21T12:00:00+00:00", "machine": "mac", "command": "refresh",
             "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001},
        ]
        result = format_summary(entries)
        assert "By machine:" in result
        assert "mac" in result
        assert "windows" in result

    def test_changed_flag_shown(self):
        entries = [
            {"ts": "2026-03-21T10:00:00+00:00", "machine": "laptop", "command": "refresh",
             "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001, "changed": True},
        ]
        result = format_summary(entries)
        assert "changed" in result

    def test_recent_shows_last_10(self):
        entries = [
            {"ts": f"2026-03-{i+1:02d}T10:00:00+00:00", "machine": "laptop",
             "command": "refresh", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001}
            for i in range(15)
        ]
        result = format_summary(entries)
        # Should show last 10, which starts at entry index 5 (March 6)
        assert "2026-03-06" in result
        assert "2026-03-15" in result
