from __future__ import annotations

import json

from memsync.usage import (
    _cost,
    _price_for_model,
    append_usage,
    format_summary,
    last_successful_harvest,
    load_usage,
    usage_log_path,
)


class TestTelemetry:
    def test_records_backend_and_duration_when_given(self, tmp_path):
        append_usage(tmp_path, "harvest", "m", 1, 1, backend="gemini", duration_ms=4200)
        entry = load_usage(tmp_path)[0]
        assert entry["backend"] == "gemini"
        assert entry["duration_ms"] == 4200

    def test_omits_them_when_absent_so_old_readers_still_parse(self, tmp_path):
        append_usage(tmp_path, "harvest", "m", 1, 1)
        entry = load_usage(tmp_path)[0]
        assert "backend" not in entry
        assert "duration_ms" not in entry

    def test_a_run_is_recorded_even_when_every_session_failed(self, tmp_path):
        # The exact shape of a night that dies silently: no per-session records
        # are written at all, so without this the run leaves no trace.
        from memsync.usage import append_run
        append_run(tmp_path, "harvest", sessions=14, updated=0, errors=14, duration_ms=180_000)
        entry = load_usage(tmp_path)[0]
        assert entry["command"] == "harvest_run"
        assert (entry["sessions"], entry["updated"], entry["errors"]) == (14, 0, 14)

    def test_summary_reports_runs_and_backend_medians(self, tmp_path):
        from memsync.usage import append_run, format_telemetry
        append_run(tmp_path, "harvest", sessions=3, updated=2, errors=1, duration_ms=60_000)
        for ms in (1000, 3000, 11000):
            append_usage(tmp_path, "harvest", "m", 1, 1, backend="claude_code", duration_ms=ms)
        out = format_telemetry(tmp_path)
        assert "3 seen" in out and "2 updated" in out and "1 errors" in out
        assert "claude_code" in out
        assert "3.0s" in out  # median of 1/3/11s, not the 5.0s mean

    def test_summary_is_calm_when_there_is_nothing_yet(self, tmp_path):
        from memsync.usage import format_telemetry
        out = format_telemetry(tmp_path)
        assert "none recorded yet" in out
        assert "no timed calls recorded yet" in out


class TestLastSuccessfulHarvest:
    """
    A harvest that fails writes no usage record, so the timestamp going stale
    IS the failure signal. Two consecutive nights of dead harvests went
    unnoticed because nothing surfaced this; the alert meant to catch it was
    also broken, and silence read as health.
    """

    @staticmethod
    def _write(root, records):
        (root / "usage.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    def test_returns_the_most_recent_across_machines(self, tmp_path):
        # Any machine harvesting keeps the memory fed. On a setup where one
        # always-on host does the work, asking only about *this* machine would
        # report stale on every laptop while everything is fine.
        self._write(tmp_path, [
            {"ts": "2026-07-01T10:00:00+00:00", "machine": "pi", "command": "harvest"},
            {"ts": "2026-07-27T09:00:00+00:00", "machine": "laptop", "command": "harvest"},
            {"ts": "2026-07-20T10:00:00+00:00", "machine": "pi", "command": "harvest"},
        ])
        machine, ts = last_successful_harvest(tmp_path)
        assert machine == "laptop"
        assert ts.isoformat() == "2026-07-27T09:00:00+00:00"

    def test_ignores_other_commands(self, tmp_path):
        # A refresh is not a harvest; counting it would mask a dead harvest.
        self._write(tmp_path, [
            {"ts": "2026-07-01T10:00:00+00:00", "machine": "pi", "command": "harvest"},
            {"ts": "2026-07-27T10:00:00+00:00", "machine": "pi", "command": "refresh"},
        ])
        machine, ts = last_successful_harvest(tmp_path)
        assert ts.isoformat() == "2026-07-01T10:00:00+00:00"

    def test_no_log_returns_none(self, tmp_path):
        assert last_successful_harvest(tmp_path) is None

    def test_no_harvest_records_returns_none(self, tmp_path):
        self._write(tmp_path, [
            {"ts": "2026-07-27T10:00:00+00:00", "machine": "pi", "command": "refresh"},
        ])
        assert last_successful_harvest(tmp_path) is None

    def test_survives_malformed_and_undated_records(self, tmp_path):
        # This log is append-only from several machines at once; a torn line
        # must not take out the health signal.
        (tmp_path / "usage.jsonl").write_text(
            '{"ts": "2026-07-01T10:00:00+00:00", "machine": "pi", "command": "harvest"}\n'
            "{not json at all\n"
            '{"machine": "pi", "command": "harvest"}\n'
            '{"ts": "nonsense", "machine": "pi", "command": "harvest"}\n',
            encoding="utf-8",
        )
        machine, ts = last_successful_harvest(tmp_path)
        assert machine == "pi"
        assert ts.isoformat() == "2026-07-01T10:00:00+00:00"


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
