from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from memsync.journal import list_prunable_journal, log_transaction, prune_journal


def _read_entry(journal_dir: Path) -> dict:
    files = list(journal_dir.glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def _make_entry_file(journal_dir: Path, transaction_type: str, ts: datetime) -> Path:
    """Create a journal file named exactly like log_transaction would, with a
    controllable timestamp so prune tests are deterministic."""
    journal_dir.mkdir(parents=True, exist_ok=True)
    name = f"{transaction_type}_{ts.strftime('%Y%m%d_%H%M%S_%f')}.json"
    p = journal_dir / name
    p.write_text("{}", encoding="utf-8")
    return p


class TestLogTransaction:
    def test_writes_one_json_file_named_by_type(self, tmp_path):
        jd = tmp_path / "journal"
        log_transaction("refresh", {"notes": "x"}, "before\n", "before\nafter\n",
                         {"model": "m"}, journal_dir=str(jd))
        files = list(jd.glob("*.json"))
        assert len(files) == 1
        assert files[0].name.startswith("refresh_")

    def test_stores_diff_not_full_memory(self, tmp_path):
        # backups/ already keeps full timestamped snapshots, so the journal must
        # store only the diff, never a second full copy of the memory content.
        jd = tmp_path / "journal"
        before = "line one\nline two\n"
        after = "line one\nline two\nline three\n"
        log_transaction("refresh", {}, before, after, {}, journal_dir=str(jd))
        entry = _read_entry(jd)
        assert "memory_before" not in entry
        assert "memory_after" not in entry
        assert "diff" in entry
        assert "line three" in entry["diff"]
        assert any(line.startswith("+") for line in entry["diff"].splitlines())

    def test_stores_integrity_hashes(self, tmp_path):
        jd = tmp_path / "journal"
        before = "a\n"
        after = "a\nb\n"
        log_transaction("refresh", {}, before, after, {}, journal_dir=str(jd))
        entry = _read_entry(jd)
        assert entry["memory_before_sha256"] == hashlib.sha256(before.encode("utf-8")).hexdigest()
        assert entry["memory_after_sha256"] == hashlib.sha256(after.encode("utf-8")).hexdigest()

    def test_records_transaction_metadata(self, tmp_path):
        jd = tmp_path / "journal"
        log_transaction("harvest", {"transcript_path": "s.md"}, "x\n", "x\n",
                         {"model": "haiku", "success": True}, journal_dir=str(jd))
        entry = _read_entry(jd)
        assert entry["transaction_type"] == "harvest"
        assert entry["input_data"] == {"transcript_path": "s.md"}
        assert entry["llm_metadata"]["model"] == "haiku"
        assert "transaction_id" in entry
        assert "timestamp" in entry
        assert entry["schema_version"] == 2

    def test_failure_is_swallowed(self, tmp_path):
        # journal_dir points at an existing file, so the write cannot succeed;
        # an audit-log failure must never propagate and break refresh/harvest.
        f = tmp_path / "not-a-dir"
        f.write_text("x", encoding="utf-8")
        log_transaction("refresh", {}, "a\n", "b\n", {}, journal_dir=str(f))  # must not raise


class TestPruneJournal:
    def test_removes_old_entries(self, tmp_path):
        jd = tmp_path / "journal"
        old = _make_entry_file(jd, "refresh", datetime(2020, 1, 1, 0, 0, 0))
        deleted = prune_journal(jd, keep_days=30)
        assert old in deleted
        assert not old.exists()

    def test_keeps_recent_entries(self, tmp_path):
        jd = tmp_path / "journal"
        recent = _make_entry_file(jd, "refresh", datetime.now())
        deleted = prune_journal(jd, keep_days=30)
        assert recent not in deleted
        assert recent.exists()

    def test_handles_underscore_transaction_type(self, tmp_path):
        # "harvest_all" contains an underscore; timestamp parsing must still work.
        jd = tmp_path / "journal"
        old = _make_entry_file(jd, "harvest_all", datetime(2020, 1, 1))
        deleted = prune_journal(jd, keep_days=30)
        assert old in deleted
        assert not old.exists()

    def test_skips_unparseable_names(self, tmp_path):
        jd = tmp_path / "journal"
        jd.mkdir()
        stray = jd / "not-a-journal.json"
        stray.write_text("{}", encoding="utf-8")
        deleted = prune_journal(jd, keep_days=0)
        assert stray.exists()
        assert stray not in deleted

    def test_returns_list_of_paths(self, tmp_path):
        jd = tmp_path / "journal"
        _make_entry_file(jd, "refresh", datetime(2020, 1, 1))
        _make_entry_file(jd, "harvest", datetime(2020, 1, 2))
        deleted = prune_journal(jd, keep_days=30)
        assert len(deleted) == 2
        assert all(isinstance(p, Path) for p in deleted)


class TestListPrunableJournal:
    def test_lists_old_entries_without_deleting(self, tmp_path):
        jd = tmp_path / "journal"
        old = _make_entry_file(jd, "refresh", datetime(2020, 1, 1))
        prunable = list_prunable_journal(jd, keep_days=30)
        assert old in prunable
        assert old.exists()  # listing must not delete
