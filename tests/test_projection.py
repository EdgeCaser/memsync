from pathlib import Path

import pytest

from memsync.config import Config
from memsync.projection import (
    SKILL_DESCRIPTION_CAP,
    build_projection,
    build_skill_description,
    check_budget,
    core_path,
    describe,
    projection_root,
    render_skill,
    short_title,
    skill_root,
    slugify,
    split_sections,
    topics_path,
    write_projection,
)


def local_config(tmp_path, **kwargs):
    """Config with generated output pinned under tmp_path, never the real home."""
    return Config(
        projection_root=tmp_path / "local",
        skill_root=tmp_path / "skills" / "memsync-memory",
        **kwargs,
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

    def test_preserves_underscores_in_identifiers(self):
        # Stripping `_` as markdown emphasis rewrote seattle_love_letter into
        # seattleloveletter, putting a path that does not exist in the index.
        _, sections = split_sections(
            "### T\n- Repo `/home/x/github/seattle_love_letter`; backend claude_code\n"
        )
        result = describe(sections[0])
        assert "seattle_love_letter" in result
        assert "claude_code" in result

    def test_still_strips_backticks_and_asterisks(self):
        _, sections = split_sections("### T\n- Repo `path` and **bold**\n")
        result = describe(sections[0])
        assert "`" not in result
        assert "*" not in result


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

    def test_core_indexes_every_topic_without_the_skill(self):
        config = Config(skill_enabled=False)
        projection = build_projection(SAMPLE_HOT, config)
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

    def test_index_uses_absolute_paths_when_skill_disabled(self, tmp_path):
        projection = build_projection(
            SAMPLE_HOT, Config(skill_enabled=False), tmp_path / "topics"
        )
        assert str(tmp_path / "topics") in projection.core

    def test_core_only_points_at_the_skill_when_enabled(self):
        # With the skill carrying the index, the core must not also carry it —
        # that would pay the cost twice.
        projection = build_projection(SAMPLE_HOT, Config(skill_enabled=True))
        assert "memsync-memory" in projection.core
        assert "seattle-love-letter-tracking" not in projection.core
        assert "Repo lives on the Pi" not in projection.core

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
class TestShortTitle:
    def test_drops_parenthetical(self):
        assert short_title("Memsync (active)") == "Memsync"

    def test_drops_status_after_dash(self):
        assert short_title("GitHub off OneDrive — Phase 5 in progress") == "GitHub off OneDrive"

    def test_keeps_plain_title(self):
        assert short_title("Cold Storage") == "Cold Storage"

    def test_never_returns_empty(self):
        assert short_title("(only a parenthetical)") == "(only a parenthetical)"


@pytest.mark.smoke
class TestSkillDescription:
    def _topics(self, n):
        return build_projection(
            "\n".join(f"### Area number {i}\n- bullet {i}\n" for i in range(n)), Config()
        ).topics

    def test_names_every_topic_when_they_fit(self):
        description = build_skill_description(self._topics(5))
        for i in range(5):
            assert f"Area number {i}" in description

    def test_stays_within_the_listing_cap(self):
        description = build_skill_description(self._topics(400))
        assert len(description) <= SKILL_DESCRIPTION_CAP

    def test_counts_the_overflow_rather_than_hiding_it(self):
        # A memory that outgrows the cap has to say so, or it silently stops
        # being routable for the topics that fell off the end.
        description = build_skill_description(self._topics(400))
        assert "more area(s)" in description

    def test_no_overflow_note_when_everything_fits(self):
        assert "more area(s)" not in build_skill_description(self._topics(3))

    def test_handles_no_topics(self):
        assert build_skill_description([]).endswith("(no topics recorded yet).")

    def test_real_shaped_memory_fits(self, tmp_path):
        projection = build_projection(SAMPLE_HOT, local_config(tmp_path))
        assert len(build_skill_description(projection.topics)) <= SKILL_DESCRIPTION_CAP


@pytest.mark.smoke
class TestRenderSkill:
    def test_has_frontmatter_with_name_and_description(self, tmp_path):
        config = local_config(tmp_path)
        skill = render_skill(build_projection(SAMPLE_HOT, config), config)
        assert skill.startswith("---\n")
        assert "name: memsync-memory" in skill
        assert "description: " in skill

    def test_frontmatter_parses_as_yaml(self, tmp_path):
        # The description reads "Long-term memory: standing detail...", and an
        # unquoted scalar containing ": " parses as a nested mapping — that
        # breaks the frontmatter outright rather than degrading it.
        yaml = pytest.importorskip("yaml")
        config = local_config(tmp_path)
        skill = render_skill(build_projection(SAMPLE_HOT, config), config)
        _, frontmatter, _ = skill.split("---", 2)
        parsed = yaml.safe_load(frontmatter)
        assert parsed["name"] == "memsync-memory"
        assert parsed["description"].startswith("Long-term memory:")

    def test_frontmatter_survives_quotes_and_backslashes_in_titles(self, tmp_path):
        yaml = pytest.importorskip("yaml")
        config = local_config(tmp_path)
        hot = '### The "Big" project at C:\\Users\\x\n- detail\n'
        skill = render_skill(build_projection(hot, config), config)
        _, frontmatter, _ = skill.split("---", 2)
        parsed = yaml.safe_load(frontmatter)
        assert 'The "Big" project at C:\\Users\\x' in parsed["description"]

    def test_body_indexes_topics_by_relative_path(self, tmp_path):
        # Relative paths are what make the skill directory portable.
        config = local_config(tmp_path)
        skill = render_skill(build_projection(SAMPLE_HOT, config), config)
        assert "`topics/memsync.md`" in skill
        assert "`topics/seattle-love-letter-tracking.md`" in skill

    def test_body_excludes_topic_bodies(self, tmp_path):
        # Body stays in context once loaded, so it is an index and nothing more.
        config = local_config(tmp_path)
        skill = render_skill(build_projection(SAMPLE_HOT, config), config)
        assert "Backend order is claude_code then gemini" not in skill


@pytest.mark.smoke
class TestWriteProjection:
    def test_writes_core_topics_and_skill(self, tmp_path):
        config = local_config(tmp_path)
        write_projection(build_projection(SAMPLE_HOT, config), config)
        assert core_path(config).exists()
        assert (topics_path(config) / "memsync.md").exists()
        assert (topics_path(config) / "seattle-love-letter-tracking.md").exists()
        assert (skill_root(config) / "SKILL.md").exists()

    def test_topics_live_inside_the_skill_directory(self, tmp_path):
        config = local_config(tmp_path)
        assert topics_path(config).parent == skill_root(config)

    def test_output_never_lands_in_the_synced_store(self, tmp_path):
        # The whole point of the machine-local root: generated files name
        # absolute paths and every machine rebuilds them, so syncing them
        # would break the paths elsewhere and make the files ping-pong.
        store = tmp_path / "synced"
        store.mkdir()
        config = local_config(tmp_path)
        write_projection(build_projection(SAMPLE_HOT, config), config)
        assert list(store.iterdir()) == []

    def test_no_skill_written_when_disabled(self, tmp_path):
        config = local_config(tmp_path, skill_enabled=False)
        write_projection(build_projection(SAMPLE_HOT, config), config)
        assert not (skill_root(config) / "SKILL.md").exists()
        assert (topics_path(config) / "memsync.md").exists()

    def test_removes_topic_files_for_deleted_sections(self, tmp_path):
        config = local_config(tmp_path)
        write_projection(build_projection(SAMPLE_HOT, config), config)
        assert (topics_path(config) / "memsync.md").exists()

        shrunk = SAMPLE_HOT.replace(
            "### Memsync (active)\n"
            "- Daemon on the Pi runs a batched merge with a fail-fast timeout\n"
            "- Backend order is claude_code then gemini\n\n",
            "",
        )
        write_projection(build_projection(shrunk, config), config)
        assert not (topics_path(config) / "memsync.md").exists()
        assert (topics_path(config) / "seattle-love-letter-tracking.md").exists()

    def test_leaves_unrelated_files_alone(self, tmp_path):
        config = local_config(tmp_path)
        topics = topics_path(config)
        topics.mkdir(parents=True)
        keep = topics / "notes.txt"
        keep.write_text("not a topic", encoding="utf-8")
        write_projection(build_projection(SAMPLE_HOT, config), config)
        assert keep.exists()

    def test_rewrite_is_idempotent(self, tmp_path):
        config = local_config(tmp_path)
        projection = build_projection(SAMPLE_HOT, config)
        write_projection(projection, config)
        first = core_path(config).read_text(encoding="utf-8")
        skill_first = (skill_root(config) / "SKILL.md").read_text(encoding="utf-8")
        write_projection(projection, config)
        assert core_path(config).read_text(encoding="utf-8") == first
        assert (skill_root(config) / "SKILL.md").read_text(encoding="utf-8") == skill_first

    def test_does_not_touch_the_source_file(self, tmp_path):
        config = local_config(tmp_path)
        source = tmp_path / "GLOBAL_MEMORY.md"
        source.write_text(SAMPLE_HOT, encoding="utf-8")
        write_projection(build_projection(SAMPLE_HOT, config), config)
        assert source.read_text(encoding="utf-8") == SAMPLE_HOT


@pytest.mark.smoke
class TestProjectionRoots:
    def test_defaults_are_machine_local_not_synced(self):
        config = Config()
        assert projection_root(config) == Path.home() / ".claude" / "memsync"
        assert skill_root(config) == Path.home() / ".claude" / "skills" / "memsync-memory"

    def test_config_overrides_are_honoured(self, tmp_path):
        config = local_config(tmp_path)
        assert projection_root(config) == tmp_path / "local"
        assert skill_root(config) == tmp_path / "skills" / "memsync-memory"

    def test_skill_name_drives_the_default_skill_path(self):
        config = Config(skill_name="custom-memory")
        assert skill_root(config).name == "custom-memory"
