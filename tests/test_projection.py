import pytest

from memsync.config import Config
from memsync.projection import (
    build_projection,
    check_budget,
    core_path,
    describe,
    slugify,
    split_sections,
    topics_path,
    write_projection,
)

SAMPLE_HOT = """\
<!-- memsync v0.2 -->

### Seattle Love Letter tracking
- Repo lives on the Pi; posting Monday, Wednesday and Friday at 9am Pacific
- Housing and zoning topics are off until August

### Memsync (active)
- Daemon on the Pi runs a batched merge with a fail-fast timeout
- Backend order is claude_code then gemini

## Hard constraints
- Never post without explicit approval
- Backups before every write
"""


@pytest.mark.smoke
class TestSplitSections:
    def test_separates_preamble_from_sections(self):
        preamble, sections = split_sections(SAMPLE_HOT)
        assert preamble == "<!-- memsync v0.2 -->"
        assert [s.title for s in sections] == [
            "Seattle Love Letter tracking",
            "Memsync (active)",
            "Hard constraints",
        ]

    def test_records_heading_level(self):
        _, sections = split_sections(SAMPLE_HOT)
        assert sections[0].level == 3
        assert sections[2].level == 2

    def test_identifies_constraints_section(self):
        _, sections = split_sections(SAMPLE_HOT)
        assert [s.is_constraints for s in sections] == [False, False, True]

    def test_constraints_heading_variant(self):
        _, sections = split_sections("## Constraints\n- Rule\n")
        assert sections[0].is_constraints

    def test_no_headings_is_all_preamble(self):
        preamble, sections = split_sections("just text\nmore text\n")
        assert preamble == "just text\nmore text"
        assert sections == []


@pytest.mark.smoke
class TestSlugify:
    def test_basic(self):
        assert slugify("Seattle Love Letter tracking") == "seattle-love-letter-tracking"

    def test_strips_parenthetical_status(self):
        assert slugify("Memsync (active)") == "memsync"

    def test_strips_markdown_emphasis(self):
        assert slugify("**Slipway** app") == "slipway-app"

    def test_handles_punctuation_and_slashes(self):
        assert slugify("Jellyfin / Pi media library") == "jellyfin-pi-media-library"

    def test_never_empty(self):
        assert slugify("!!!") == "untitled"

    def test_no_trailing_hyphen_after_truncation(self):
        assert not slugify("word " * 40).endswith("-")


@pytest.mark.smoke
class TestDescribe:
    def test_uses_first_bullet(self):
        _, sections = split_sections(SAMPLE_HOT)
        assert describe(sections[0]).startswith("Repo lives on the Pi")

    def test_truncates_on_word_boundary(self):
        _, sections = split_sections("### T\n- " + "alpha bravo " * 40 + "\n")
        result = describe(sections[0], max_chars=40)
        assert len(result) <= 41  # 40 plus the ellipsis
        assert result.endswith("…")
        assert "alph…" not in result  # never cuts mid-word

    def test_handles_section_with_no_bullets(self):
        _, sections = split_sections("### Empty\n\nsome prose\n")
        assert describe(sections[0]) == "No summary available."


@pytest.mark.smoke
class TestBuildProjection:
    def test_constraints_stay_in_core(self):
        projection = build_projection(SAMPLE_HOT, Config())
        assert "Never post without explicit approval" in projection.core
        assert "Backups before every write" in projection.core

    def test_project_section_bodies_leave_the_core(self):
        # The index quotes each section's first bullet as its description, so
        # that line is expected to remain. Everything after it should not.
        projection = build_projection(SAMPLE_HOT, Config())
        assert "Housing and zoning topics are off until August" not in projection.core
        assert "Backend order is claude_code then gemini" not in projection.core

    def test_project_sections_become_topics(self):
        projection = build_projection(SAMPLE_HOT, Config())
        slugs = {t.slug for t in projection.topics}
        assert slugs == {"seattle-love-letter-tracking", "memsync"}

    def test_core_indexes_every_topic(self):
        projection = build_projection(SAMPLE_HOT, Config())
        for topic in projection.topics:
            assert topic.title in projection.core
            assert topic.slug in projection.core

    def test_topic_files_carry_frontmatter(self):
        projection = build_projection(SAMPLE_HOT, Config())
        topic = next(t for t in projection.topics if t.slug == "memsync")
        assert topic.content.startswith("---\n")
        assert 'title: "Memsync (active)"' in topic.content
        assert "description:" in topic.content
        assert "Daemon on the Pi" in topic.content

    def test_core_is_much_smaller_on_a_realistic_file(self):
        # Shaped like the real hot layer: many project sections with several
        # bullets each, plus a constraints section that stays resident.
        sections = "\n".join(
            f"### Project {n}\n"
            + "\n".join(f"- Detail bullet {m} for project {n} " + "x" * 120 for m in range(6))
            + "\n"
            for n in range(18)
        )
        hot = f"{sections}\n## Hard constraints\n- Only rule\n"
        projection = build_projection(hot, Config(core_max_chars=100_000))
        assert projection.core_chars < len(hot) * 0.25
        assert len(projection.topics) == 18

    def test_preamble_is_preserved(self):
        projection = build_projection(SAMPLE_HOT, Config())
        assert "<!-- memsync v0.2 -->" in projection.core

    def test_index_uses_absolute_paths_when_given(self, tmp_path):
        projection = build_projection(SAMPLE_HOT, Config(), tmp_path / "topics")
        assert str(tmp_path / "topics") in projection.core

    def test_colliding_slugs_are_suffixed_not_overwritten(self):
        # Both titles slugify to "memsync" once the parenthetical is stripped.
        hot = "### Memsync (active)\n- one\n\n### Memsync (paused)\n- two\n"
        projection = build_projection(hot, Config())
        assert [t.slug for t in projection.topics] == ["memsync", "memsync-2"]
        assert len({t.slug for t in projection.topics}) == 2

    def test_no_topics_still_produces_valid_core(self):
        hot = "## Hard constraints\n- Only rule\n"
        projection = build_projection(hot, Config())
        assert "Only rule" in projection.core
        assert projection.topics == []
        assert "Memory index" not in projection.core

    def test_oversized_constraints_are_never_projected_out(self):
        # A constraint that has to be fetched is a constraint that gets missed,
        # so the section stays resident and the budget check complains instead.
        big = "\n".join(f"- Constraint number {n} " + "x" * 200 for n in range(60))
        hot = f"## Hard constraints\n{big}\n"
        projection = build_projection(hot, Config(core_max_chars=1000))
        assert "Constraint number 59" in projection.core
        assert check_budget(projection, Config(core_max_chars=1000))


@pytest.mark.smoke
class TestCheckBudget:
    def test_passes_when_within_budget(self):
        projection = build_projection(SAMPLE_HOT, Config())
        assert check_budget(projection, Config()) == []

    def test_reports_char_overage(self):
        projection = build_projection(SAMPLE_HOT, Config())
        problems = check_budget(projection, Config(core_max_chars=10))
        assert any("chars" in p for p in problems)

    def test_reports_line_overage(self):
        projection = build_projection(SAMPLE_HOT, Config())
        problems = check_budget(projection, Config(max_hot_lines=1))
        assert any("lines" in p for p in problems)


@pytest.mark.smoke
class TestWriteProjection:
    def test_writes_core_and_topics(self, tmp_path):
        projection = build_projection(SAMPLE_HOT, Config(), topics_path(tmp_path))
        write_projection(projection, tmp_path)
        assert core_path(tmp_path).exists()
        assert (topics_path(tmp_path) / "memsync.md").exists()
        assert (topics_path(tmp_path) / "seattle-love-letter-tracking.md").exists()

    def test_removes_topic_files_for_deleted_sections(self, tmp_path):
        write_projection(build_projection(SAMPLE_HOT, Config()), tmp_path)
        assert (topics_path(tmp_path) / "memsync.md").exists()

        shrunk = SAMPLE_HOT.replace(
            "### Memsync (active)\n"
            "- Daemon on the Pi runs a batched merge with a fail-fast timeout\n"
            "- Backend order is claude_code then gemini\n\n",
            "",
        )
        write_projection(build_projection(shrunk, Config()), tmp_path)
        assert not (topics_path(tmp_path) / "memsync.md").exists()
        assert (topics_path(tmp_path) / "seattle-love-letter-tracking.md").exists()

    def test_leaves_unrelated_files_alone(self, tmp_path):
        topics = topics_path(tmp_path)
        topics.mkdir(parents=True)
        keep = topics / "notes.txt"
        keep.write_text("not a topic", encoding="utf-8")
        write_projection(build_projection(SAMPLE_HOT, Config()), tmp_path)
        assert keep.exists()

    def test_rewrite_is_idempotent(self, tmp_path):
        projection = build_projection(SAMPLE_HOT, Config(), topics_path(tmp_path))
        write_projection(projection, tmp_path)
        first = core_path(tmp_path).read_text(encoding="utf-8")
        write_projection(projection, tmp_path)
        assert core_path(tmp_path).read_text(encoding="utf-8") == first

    def test_does_not_touch_the_source_file(self, tmp_path):
        source = tmp_path / "GLOBAL_MEMORY.md"
        source.write_text(SAMPLE_HOT, encoding="utf-8")
        write_projection(build_projection(SAMPLE_HOT, Config()), tmp_path)
        assert source.read_text(encoding="utf-8") == SAMPLE_HOT
