import subprocess

import pytest

from memsync import store
from memsync.cli import _snapshot_store, cmd_project, cmd_store, cmd_store_conflicts
from memsync.config import Config


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover
        return False
    return True


needs_git = pytest.mark.skipif(not _git_available(), reason="git not installed")


def _args(**kwargs):
    class Namespace:
        pass

    ns = Namespace()
    for key, value in kwargs.items():
        setattr(ns, key, value)
    return ns


@pytest.fixture
def store_config(tmp_path):
    """A config whose memory root and generated output are both under tmp_path."""
    root = tmp_path / "sync" / ".claude-memory"
    root.mkdir(parents=True)
    (root / "GLOBAL_MEMORY.md").write_text(
        "### Project one\n- detail one\n\n## Hard constraints\n- Only rule\n",
        encoding="utf-8",
    )
    (root / "MEMORY_ARCHIVE.md").write_text("# Archive\n", encoding="utf-8")
    return Config(
        sync_root=tmp_path / "sync",
        provider="custom",
        projection_root=tmp_path / "local",
        skill_root=tmp_path / "skills" / "memsync-memory",
        claude_md_target=tmp_path / "targets" / "CLAUDE.md",
        codex_agents_target=tmp_path / "targets" / "AGENTS.md",
    ), root


@pytest.mark.smoke
class TestStoreStatusCommand:
    def test_reports_non_repo(self, store_config, capsys):
        config, _ = store_config
        assert cmd_store(_args(store_command="status"), config) == 0
        assert "not a repository" in capsys.readouterr().out

    def test_warns_about_syncthing(self, store_config, capsys):
        config, root = store_config
        (root.parent / ".stfolder").mkdir()
        cmd_store(_args(store_command="status"), config)
        out = capsys.readouterr().out
        assert "Syncthing folder" in out
        assert "corrupts the repository" in out

    def test_warns_about_conflict_files(self, store_config, capsys):
        config, root = store_config
        (root / "GLOBAL_MEMORY.sync-conflict-20260715-1-A.md").write_text("x", encoding="utf-8")
        cmd_store(_args(store_command="status"), config)
        assert "conflict file(s) present" in capsys.readouterr().out


@needs_git
@pytest.mark.smoke
class TestStoreInitCommand:
    def test_initialises(self, store_config, capsys):
        config, root = store_config
        assert cmd_store(_args(store_command="init", allow_syncthing=False), config) == 0
        assert store.is_repo(root)
        assert "No remote is configured" in capsys.readouterr().out

    def test_refuses_on_syncthing_and_reports_why(self, store_config, capsys):
        config, root = store_config
        (root.parent / ".stfolder").mkdir()
        assert cmd_store(_args(store_command="init", allow_syncthing=False), config) == 1
        assert not store.is_repo(root)
        assert "Syncthing" in capsys.readouterr().err

    def test_sync_without_repo_is_an_error(self, store_config, capsys):
        config, _ = store_config
        assert cmd_store(_args(store_command="sync"), config) == 1
        assert "not a git repository" in capsys.readouterr().err


@pytest.mark.smoke
class TestStoreConflictsCommand:
    def test_reports_nothing_when_clean(self, store_config, capsys):
        config, _ = store_config
        assert cmd_store_conflicts(_args(), config) == 0
        assert "No Syncthing conflict files" in capsys.readouterr().out

    def test_identifies_lines_only_in_the_conflict_copy(self, store_config, capsys):
        # The question worth answering before deleting a conflict copy is
        # whether the fork lost anything, not how many copies there are.
        config, root = store_config
        (root / "GLOBAL_MEMORY.sync-conflict-20260715-1-A.md").write_text(
            "### Project one\n- detail one\n- a line that never made it back\n",
            encoding="utf-8",
        )
        assert cmd_store_conflicts(_args(), config) == 0
        out = capsys.readouterr().out
        assert "a line that never made it back" in out
        assert "exist only in conflict copies" in out

    def test_says_safe_to_delete_when_nothing_unique(self, store_config, capsys):
        config, root = store_config
        (root / "GLOBAL_MEMORY.sync-conflict-20260715-1-A.md").write_text(
            "### Project one\n- detail one\n", encoding="utf-8"
        )
        cmd_store_conflicts(_args(), config)
        assert "safe to delete" in capsys.readouterr().out

    def test_reworded_lines_do_not_count_as_lost(self, store_config, capsys):
        # A fork mostly produces variants of lines the live file still has.
        # Counting those as losses turns a handful of real ones into hundreds
        # of false ones, and a report that cries wolf does not get read.
        config, root = store_config
        (root / "GLOBAL_MEMORY.md").write_text(
            "### Project one\n"
            "- The hot layer limit is 100 lines and 38,000 chars, with 120 lines "
            "accepted as the working floor because the char ceiling binds first\n",
            encoding="utf-8",
        )
        (root / "GLOBAL_MEMORY.sync-conflict-20260715-1-A.md").write_text(
            "### Project one\n"
            "- The hot layer limit is 100 lines and 38,000 chars, with 120 lines "
            "accepted as the working floor\n",
            encoding="utf-8",
        )
        cmd_store_conflicts(_args(), config)
        assert "safe to delete" in capsys.readouterr().out

    def test_handles_conflict_with_no_live_counterpart(self, store_config, capsys):
        config, root = store_config
        (root / "GONE.sync-conflict-20260715-1-A.md").write_text("- x\n", encoding="utf-8")
        assert cmd_store_conflicts(_args(), config) == 0
        assert "no live counterpart" in capsys.readouterr().out


@pytest.mark.smoke
class TestProjectCommand:
    def test_dry_run_writes_nothing(self, store_config, capsys):
        config, _ = store_config
        cmd_project(_args(dry_run=True, force=False), config)
        assert "DRY RUN" in capsys.readouterr().out
        assert not (config.projection_root / "CLAUDE_CORE.md").exists()

    def test_writes_core_topics_and_skill(self, store_config):
        config, _ = store_config
        assert cmd_project(_args(dry_run=False, force=False), config) == 0
        assert (config.projection_root / "CLAUDE_CORE.md").exists()
        assert (config.skill_root / "SKILL.md").exists()

    def test_refuses_over_budget_without_force(self, store_config, capsys):
        config, _ = store_config
        config = Config(**{**config.__dict__, "core_max_chars": 10})
        assert cmd_project(_args(dry_run=False, force=False), config) == 1
        assert "Refusing to write" in capsys.readouterr().err

    def test_force_writes_over_budget(self, store_config):
        config, _ = store_config
        config = Config(**{**config.__dict__, "core_max_chars": 10})
        assert cmd_project(_args(dry_run=False, force=True), config) == 0
        assert (config.projection_root / "CLAUDE_CORE.md").exists()

    def test_removes_legacy_synced_copy(self, store_config):
        # Phase 1 wrote generated output into the synced store; that copy has
        # wrong paths on every other machine and must not linger.
        config, root = store_config
        legacy = root / "core"
        legacy.mkdir()
        (legacy / "CLAUDE_CORE.md").write_text("stale", encoding="utf-8")
        cmd_project(_args(dry_run=False, force=False), config)
        assert not legacy.exists()


class TestHarvestHealthInStatus:
    """
    Two nightly harvests died in a row without anyone noticing: every backend
    was down, and the Slack alert meant to catch that was itself failing
    silently. Nothing distinguished "quiet because there was nothing to do"
    from "quiet because it is broken".
    """

    @staticmethod
    def _log(root, hours_ago, machine="pi"):
        import json
        from datetime import UTC, datetime, timedelta
        ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
        (root / "usage.jsonl").write_text(
            json.dumps({"ts": ts, "machine": machine, "command": "harvest"}) + "\n",
            encoding="utf-8",
        )

    def test_warns_when_no_harvest_has_succeeded_recently(self, store_config, capsys):
        from memsync.cli import _print_harvest_health
        config, root = store_config
        self._log(root, hours_ago=50)
        _print_harvest_health(config, root)
        out = capsys.readouterr().out
        assert "STALE" in out
        assert "2 days ago" in out

    def test_quiet_when_a_harvest_landed_recently(self, store_config, capsys):
        from memsync.cli import _print_harvest_health
        config, root = store_config
        self._log(root, hours_ago=3)
        _print_harvest_health(config, root)
        out = capsys.readouterr().out
        assert "STALE" not in out
        assert "3h ago" in out

    def test_says_so_when_nothing_has_ever_harvested(self, store_config, capsys):
        from memsync.cli import _print_harvest_health
        config, root = store_config
        _print_harvest_health(config, root)
        assert "never" in capsys.readouterr().out

    def test_threshold_of_zero_disables_the_warning(self, store_config, capsys):
        import dataclasses
        from memsync.cli import _print_harvest_health
        config, root = store_config
        self._log(root, hours_ago=500)
        config = dataclasses.replace(
            config, daemon=dataclasses.replace(config.daemon, harvest_stale_hours=0)
        )
        _print_harvest_health(config, root)
        assert "STALE" not in capsys.readouterr().out


def _run_git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def store_with_remote(store_config, tmp_path):
    """A store with an origin it shares with a second machine."""
    config, root = store_config
    config = Config(**{**config.__dict__, "git_enabled": True, "git_autosync": True})
    store.init_repo(root)
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True
    )
    branch = store.status(root).branch
    _run_git(root, "remote", "add", "origin", str(bare))
    _run_git(root, "push", "-u", "origin", branch)
    return config, root, bare, branch


def _other_machine_pushes(bare, tmp_path, branch, name, text):
    """A commit that reaches origin from somewhere other than this store."""
    clone = tmp_path / f"other-{name}"
    subprocess.run(
        ["git", "clone", str(bare), str(clone)], check=True, capture_output=True, text=True
    )
    (clone / name).write_text(text, encoding="utf-8")
    _run_git(clone, "add", "-A")
    _run_git(
        clone, "-c", "user.email=other@localhost", "-c", "user.name=other",
        "commit", "-m", "the other machine wrote this",
    )
    _run_git(clone, "push", "origin", branch)


@needs_git
@pytest.mark.smoke
class TestAutosyncOrder:
    def test_pulls_remote_work_that_a_local_write_would_have_blocked(
        self, store_with_remote, tmp_path, capsys
    ):
        # The store is dirty by definition when this runs — a memory write just
        # landed. `git pull --rebase` refuses over unstaged changes, so pulling
        # before committing meant never pulling at all: every machine only ever
        # pushed, and the divergence surfaced later as a rejected push.
        config, root, bare, branch = store_with_remote
        _other_machine_pushes(bare, tmp_path, branch, "from-other.md", "remote work\n")
        (root / "GLOBAL_MEMORY.md").write_text("### Project one\n- local\n", encoding="utf-8")

        _snapshot_store(config, root, "memsync: harvest")

        assert "pull skipped" not in capsys.readouterr().err
        assert (root / "from-other.md").exists()
        st = store.status(root)
        assert (st.ahead, st.behind, st.dirty) == (0, 0, False)

    def test_pushes_a_backlog_left_by_an_earlier_failure(self, store_with_remote):
        # A push that failed while the network was down leaves the machine
        # ahead. Gating the push on "did this run commit anything" stranded that
        # commit until some later run happened to write.
        config, root, _, _ = store_with_remote
        (root / "GLOBAL_MEMORY.md").write_text("- unpushed\n", encoding="utf-8")
        store.snapshot(root, "memsync: an earlier write")
        assert store.status(root).ahead == 1

        _snapshot_store(config, root, "memsync: dedup")

        assert store.status(root).ahead == 0

    def test_a_failed_pull_stops_the_push(self, store_with_remote, tmp_path, capsys):
        # Pushing over a divergence git could not rebase would be rejected
        # anyway; reporting the pull failure once is clearer than reporting both.
        config, root, bare, branch = store_with_remote
        _other_machine_pushes(bare, tmp_path, branch, "GLOBAL_MEMORY.md", "theirs\n")
        (root / "GLOBAL_MEMORY.md").write_text("ours\n", encoding="utf-8")

        _snapshot_store(config, root, "memsync: harvest")

        captured = capsys.readouterr()
        assert "pull skipped" in captured.err
        assert "pushed" not in captured.out
        assert not (root / ".git" / "rebase-merge").exists()

    def test_manual_sync_commits_hand_edits_before_pulling(
        self, store_with_remote, tmp_path, capsys
    ):
        # `store sync` is the command someone runs after editing the memory by
        # hand, which is exactly the state that blocks a rebase.
        config, root, bare, branch = store_with_remote
        _other_machine_pushes(bare, tmp_path, branch, "from-other.md", "remote work\n")
        (root / "GLOBAL_MEMORY.md").write_text("- edited by hand\n", encoding="utf-8")

        assert cmd_store(_args(store_command="sync"), config) == 0

        assert (root / "from-other.md").exists()
        st = store.status(root)
        assert (st.ahead, st.behind, st.dirty) == (0, 0, False)
