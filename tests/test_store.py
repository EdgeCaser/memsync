import subprocess

import pytest

from memsync import store


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover
        return False
    return True


needs_git = pytest.mark.skipif(not _git_available(), reason="git not installed")


@pytest.fixture
def memory_root(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    (root / "GLOBAL_MEMORY.md").write_text("# Memory\n- one\n", encoding="utf-8")
    return root


@pytest.fixture
def no_git_identity(tmp_path, monkeypatch):
    """
    A machine that has never had `git config --global user.email` set.

    This is the default on a freshly imaged Pi, a new laptop, and the Windows
    and Linux GitHub runners — where this exact gap broke every store test
    while passing locally and on macOS, both of which happen to have an
    identity configured.
    """
    empty = tmp_path / "gitconfig-empty"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
    return empty


@pytest.fixture
def repo(memory_root):
    store.init_repo(memory_root)
    return memory_root


@pytest.mark.smoke
class TestSyncthingDetection:
    def test_detects_marker_in_the_store(self, memory_root):
        (memory_root / ".stfolder").mkdir()
        assert store.syncthing_markers(memory_root)

    def test_detects_marker_in_a_parent(self, memory_root):
        # The real layout: markers sit at the Syncthing *folder* root and the
        # memory store is a subdirectory, so checking only the store misses the
        # exact configuration that makes git dangerous.
        (memory_root.parent / ".stfolder").mkdir()
        assert any(".stfolder" in m for m in store.syncthing_markers(memory_root))

    def test_clean_directory_has_no_markers(self, memory_root):
        assert store.syncthing_markers(memory_root) == []

    def test_finds_conflict_files(self, memory_root):
        (memory_root / "GLOBAL_MEMORY.sync-conflict-20260715-1-A.md").write_text(
            "x", encoding="utf-8"
        )
        assert len(store.conflict_files(memory_root)) == 1


@needs_git
@pytest.mark.smoke
class TestInitRepo:
    def test_creates_repo_and_gitignore(self, memory_root):
        store.init_repo(memory_root)
        assert store.is_repo(memory_root)
        assert (memory_root / ".gitignore").exists()

    def test_gitignore_excludes_backups_and_conflicts(self, memory_root):
        store.init_repo(memory_root)
        text = (memory_root / ".gitignore").read_text(encoding="utf-8")
        assert "backups/" in text
        assert "*.sync-conflict-*" in text

    def test_union_merge_for_the_append_only_log(self, memory_root):
        # Every machine appends to usage.jsonl, so both sides of a merge are
        # wanted and a conflict would be pure friction.
        store.init_repo(memory_root)
        text = (memory_root / ".gitattributes").read_text(encoding="utf-8")
        assert "usage.jsonl merge=union" in text

    def test_structured_files_are_not_union_merged(self, memory_root):
        # harvested.json is an index, not a log. Concatenating two versions
        # would corrupt it; a real disagreement should stop and be resolved.
        store.init_repo(memory_root)
        text = (memory_root / ".gitattributes").read_text(encoding="utf-8")
        assert "harvested.json merge=union" not in text

    def test_refuses_inside_a_syncthing_folder(self, memory_root):
        (memory_root.parent / ".stfolder").mkdir()
        with pytest.raises(store.StoreError, match="Syncthing"):
            store.init_repo(memory_root)
        assert not store.is_repo(memory_root)

    def test_override_allows_syncthing_folder(self, memory_root):
        (memory_root.parent / ".stfolder").mkdir()
        store.init_repo(memory_root, allow_syncthing=True)
        assert store.is_repo(memory_root)

    def test_refuses_to_reinitialise(self, repo):
        with pytest.raises(store.StoreError, match="already a git repository"):
            store.init_repo(repo)

    def test_succeeds_with_no_git_identity_on_the_machine(
        self, memory_root, no_git_identity
    ):
        # Without a fallback identity git refuses to commit, leaving a
        # repository with no commit that init then refuses to re-run.
        store.init_repo(memory_root)
        assert store.is_repo(memory_root)
        assert store._git(memory_root, "rev-list", "--count", "HEAD") == "1"

    def test_identity_is_repo_local_not_global(self, memory_root, no_git_identity):
        # memsync must not edit anyone's global git config as a side effect.
        store.init_repo(memory_root)
        assert store._git(memory_root, "config", "user.email") == "memsync@localhost"
        assert no_git_identity.read_text(encoding="utf-8") == ""

    def test_existing_identity_is_left_alone(self, memory_root, no_git_identity):
        no_git_identity.write_text(
            "[user]\n\temail = real@example.com\n\tname = Real\n", encoding="utf-8"
        )
        store.init_repo(memory_root)
        assert store._git(memory_root, "config", "user.email") == "real@example.com"


@needs_git
@pytest.mark.smoke
class TestSnapshot:
    def test_commits_a_change(self, repo):
        (repo / "GLOBAL_MEMORY.md").write_text("# Memory\n- two\n", encoding="utf-8")
        assert store.snapshot(repo, "memsync: test") is not None

    def test_returns_none_when_nothing_changed(self, repo):
        assert store.snapshot(repo, "memsync: test") is None

    def test_returns_none_outside_a_repo(self, memory_root):
        assert store.snapshot(memory_root, "memsync: test") is None

    def test_never_raises_when_git_fails(self, repo, monkeypatch):
        # The memory write has already succeeded by this point. Failing the
        # command because history-keeping failed would report a false negative.
        def boom(*args, **kwargs):
            raise store.StoreError("simulated failure")

        monkeypatch.setattr(store, "_git", boom)
        assert store.snapshot(repo, "memsync: test") is None

    def test_commits_in_a_clone_with_no_identity(self, memory_root, no_git_identity):
        # A clone never runs init_repo, so snapshot is the first thing to need
        # an identity — and it swallows failures, so without this it would
        # silently never commit.
        store.init_repo(memory_root)
        clone = memory_root.parent / "clone"
        store._git(memory_root, "clone", "--quiet", str(memory_root), str(clone))
        (clone / "GLOBAL_MEMORY.md").write_text("# Memory\n- changed\n", encoding="utf-8")
        assert store.snapshot(clone, "memsync: test") is not None

    def test_ignored_paths_do_not_produce_commits(self, repo):
        backups = repo / "backups"
        backups.mkdir()
        (backups / "GLOBAL_MEMORY.20260101.md").write_text("old", encoding="utf-8")
        assert store.snapshot(repo, "memsync: test") is None


@needs_git
@pytest.mark.smoke
class TestStatus:
    def test_reports_repo_state(self, repo):
        st = store.status(repo)
        assert st.is_repo
        assert st.branch
        assert st.remote is None
        assert not st.dirty

    def test_reports_dirty(self, repo):
        (repo / "GLOBAL_MEMORY.md").write_text("changed", encoding="utf-8")
        assert store.status(repo).dirty

    def test_reports_non_repo_without_raising(self, memory_root):
        st = store.status(memory_root)
        assert st.is_repo is False
        assert st.branch is None

    def test_surfaces_syncthing_and_conflicts(self, memory_root):
        (memory_root.parent / ".stfolder").mkdir()
        (memory_root / "GLOBAL_MEMORY.sync-conflict-20260715-1-A.md").write_text(
            "x", encoding="utf-8"
        )
        st = store.status(memory_root)
        assert st.syncthing
        assert st.conflict_files == 1


@needs_git
@pytest.mark.smoke
class TestPullPush:
    def test_pull_without_remote_is_a_noop(self, repo):
        assert "no remote" in store.pull(repo)

    def test_push_without_remote_is_a_noop(self, repo):
        assert "no remote" in store.push(repo)
