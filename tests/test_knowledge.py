from dataclasses import replace
from pathlib import Path

from hsai.knowledge import (
    PRACTICE_SECTION,
    KnowledgeBase,
    Lesson,
    LessonRecord,
    Practice,
    Whitepaper,
    cited_practices,
    extract_practice_ids,
    parse_note,
    parse_practice,
    slugify,
    split_sections,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _practice(**overrides) -> Practice:
    fields = dict(
        id="swarm-error-execution-context",
        source_repo="openai/swarm",
        artifact="README.md - handoffs carry context variables",
        observation="The run's context travels with the work.",
        adaptation="Agent errors are prefixed with phase and ticket.",
    )
    fields.update(overrides)
    return Practice(**fields)


def test_slugify():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  ") == "untitled"


def test_lesson_records_remote_ci_conclusion(tmp_path):
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: poll remote CI",
        outcome="pass",
        kind="implement",
        context="ctx",
        what_happened="did the thing",
        lesson="gate on remote truth",
        ticket=7,
    )
    # before the remote check concludes, the lesson notes it as pending
    path = kb.write_lesson(lesson)
    assert "_(pending)_" in path.read_text()

    # once the orchestrator learns the true remote outcome, it is rewritten
    lesson.remote_ci = "SUCCESS"
    kb.write_lesson(lesson)
    assert "SUCCESS" in path.read_text()


def test_lesson_records_the_independent_review_verdict(tmp_path):
    """G2: the vault says who CHECKED the work, not only who wrote it."""
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: add widget",
        outcome="pass",
        kind="implement",
        context="ctx",
        what_happened="did the thing",
        lesson="kept it small",
        ticket=7,
    )
    path = kb.write_lesson(lesson)
    assert "## Independent review" in path.read_text()
    assert "_(no independent review recorded)_" in path.read_text()

    lesson.review_verdict = "- verdict: **APPROVED**\n- reviewer: `haiku`"
    kb.write_lesson(lesson)
    text = path.read_text()
    assert "## Independent review" in text
    assert "**APPROVED**" in text and "`haiku`" in text
    # and the surrounding sections are untouched
    assert "## Lesson learned" in text and "## Reproduction evidence" in text


def test_write_lesson_and_reindex(tmp_path):
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: add status command",
        outcome="pass",
        kind="implement",
        context="ctx",
        what_happened="did the thing",
        lesson="small steps win",
        ticket=7,
        pr=8,
        model="sonnet",
        references=("swarm-error-execution-context",),
    )
    path = kb.write_lesson(lesson)
    assert path.exists()
    text = path.read_text()
    assert "# implement: add status command" in text
    assert "[[Lessons MOC]]" in text  # Obsidian wikilink up to MOC
    # evidence is a practice citation, wikilinked into the registry
    assert "[[swarm-error-execution-context]]" in text
    assert "sonnet" in text

    written = kb.reindex_mocs()
    names = {p.name for p in written}
    assert names == {
        "Lessons MOC.md", "Practices MOC.md", "Whitepapers MOC.md", "Knowledge Base MOC.md",
    }
    lessons_moc = (kb.mocs_dir / "Lessons MOC.md").read_text()
    assert f"[[{lesson.note_name()}]]" in lessons_moc


def test_whitepaper_cadence(tmp_path):
    kb = KnowledgeBase(tmp_path, whitepaper_every=3)
    assert kb.should_write_whitepaper() is False
    for i in range(3):
        kb.write_lesson(
            Lesson(
                title=f"lesson {i}",
                outcome="pass",
                kind="improve",
                context="c",
                what_happened="w",
                lesson="l",
            )
        )
    assert kb.should_write_whitepaper() is True
    p = kb.write_whitepaper(
        Whitepaper(title="synthesis", summary="s", body="b", covers_lessons=tuple(kb.lesson_notes()))
    )
    assert p.exists()
    assert "[[Whitepapers MOC]]" in p.read_text()


def test_read_lessons_round_trips_written_lessons(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(
        Lesson(
            title="implement: add status command",
            outcome="pass",
            kind="implement",
            context="ctx",
            what_happened="did the thing",
            lesson="small steps win",
        )
    )
    records = kb.read_lessons()
    assert len(records) == 1
    r = records[0]
    assert isinstance(r, LessonRecord)
    assert r.title == "implement: add status command"
    assert r.outcome == "pass"
    assert r.kind == "implement"
    assert r.lesson_text == "small steps win"
    assert r.what_happened == "did the thing"


def test_recalled_notes_are_written_as_a_frontmatter_list(tmp_path):
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: gate on remote CI",
        outcome="pass",
        kind="implement",
        context="c",
        what_happened="w",
        lesson="l",
        recalled=("2026-01-01-a", "2026-01-02-b"),
    )
    path = kb.write_lesson(lesson)
    frontmatter = path.read_text().split("---\n")[1]
    assert "recalled:\n  - 2026-01-01-a\n  - 2026-01-02-b\n" in frontmatter

    # the recalled list is a SEPARATE key: it must not leak back in as a tag
    record = parse_note(path)
    assert record.tags == ("lesson", "outcome/pass", "kind/implement")
    assert record.outcome == "pass" and record.kind == "implement"

    # nothing recalled -> the key is absent entirely, so a run with recall
    # disabled renders exactly as it did before recall existed
    lesson.recalled = ()
    assert "recalled" not in kb.write_lesson(lesson).read_text().split("---\n")[1]


def test_parse_note_reads_any_vault_note_not_just_lessons(tmp_path):
    path = tmp_path / "0007-some-adr.md"
    path.write_text("# ADR-0007: Some decision\n\n## Decision\nDo the thing.\n")
    record = parse_note(path)
    assert record.note_name == "0007-some-adr"
    assert record.title == "ADR-0007: Some decision"
    # no outcome/kind tags on an ADR - they degrade, they do not crash
    assert record.outcome == "unknown" and record.kind == "unknown"
    assert record.body.startswith("# ADR-0007")
    assert split_sections(record.body)["decision"] == "Do the thing."


def test_synthesize_whitepaper_groups_outcomes_and_surfaces_recurring_failures(tmp_path):
    kb = KnowledgeBase(tmp_path, whitepaper_every=4)
    kb.write_lesson(
        Lesson(
            title="heal: fix flaky retry",
            outcome="fail",
            kind="heal",
            context="c",
            what_happened="w",
            lesson="Timeout handling was missing around the retry loop.",
        )
    )
    kb.write_lesson(
        Lesson(
            title="heal: fix another timeout",
            outcome="fail",
            kind="heal",
            context="c",
            what_happened="w",
            lesson="Another timeout surfaced because retry logic was incomplete.",
        )
    )
    kb.write_lesson(
        Lesson(
            title="implement: add feature",
            outcome="pass",
            kind="implement",
            context="c",
            what_happened="w",
            lesson="Small, well-tested changes shipped cleanly.",
        )
    )
    paper = kb.synthesize_whitepaper()
    assert paper.covers_lessons == tuple(kb.lesson_notes())
    assert "| fail | 2 |" in paper.body
    assert "| pass | 1 |" in paper.body
    assert "| heal | 2 |" in paper.body
    assert "| implement | 1 |" in paper.body
    assert "Recurring failures" in paper.body
    assert "timeout" in paper.body.lower()
    assert "retry" in paper.body.lower()

    path = kb.write_whitepaper(paper)
    assert "[[Whitepapers MOC]]" in path.read_text()


# --- the practice registry: evidence you can resolve -------------------------


def test_extract_practice_ids_reads_citations_in_order_without_duplicates():
    body = (
        "## Practices cited\n"
        "- `practice:swarm-error-execution-context` - openai/swarm\n"
        "- `practice:metagpt-explicit-phase-artifacts` - MetaGPT\n"
        "- `practice:swarm-error-execution-context` (again)\n"
    )
    assert extract_practice_ids(body) == (
        "swarm-error-execution-context", "metagpt-explicit-phase-artifacts",
    )
    # nothing cited is a real answer, not an excuse to invent one
    assert extract_practice_ids("no citations here") == ()
    assert extract_practice_ids("") == ()


def test_practice_note_carries_source_artifact_observation_and_adaptation(tmp_path):
    kb = KnowledgeBase(tmp_path)
    path = kb.write_practice(_practice())

    assert path == kb.practices_dir / "swarm-error-execution-context.md"
    text = path.read_text()
    assert "source_repo: openai/swarm" in text
    assert "README.md - handoffs carry context variables" in text
    assert "## Observation" in text and "## Adaptation" in text
    assert "`practice:swarm-error-execution-context`" in text
    # up-links make lesson -> practice -> MOC a walkable path in the vault
    assert "[[Practices MOC]]" in text and "[[Knowledge Base MOC]]" in text
    assert "- practice" in text and "- source/openai-swarm" in text


def test_practice_round_trips_through_the_registry_byte_for_byte(tmp_path):
    kb = KnowledgeBase(tmp_path)
    # a colon-bearing artifact is exactly what a planner writes, and would break
    # the frontmatter if it were not quoted
    original = _practice(
        artifact="ci.yml: the gate that fails the PR",
        adopted_by=("#41", "#203"),
        created="2026-08-13",
    )
    path = kb.write_practice(original)

    parsed = parse_practice(path)
    assert parsed.id == original.id
    assert parsed.source_repo == original.source_repo
    assert parsed.artifact == original.artifact
    assert parsed.observation == original.observation
    assert parsed.adaptation == original.adaptation
    assert parsed.adopted_by == ("#41", "#203")
    assert parsed.created == "2026-08-13"

    # re-writing a parsed note reproduces it exactly (tags do not accumulate)
    assert kb.write_practice(parsed).read_text() == path.read_text()
    assert kb.practice_ids() == {"swarm-error-execution-context"}


def test_a_practice_that_was_never_adopted_says_so(tmp_path):
    kb = KnowledgeBase(tmp_path)
    text = kb.write_practice(_practice()).read_text()
    assert "_(not yet adopted)_" in text
    assert parse_practice(kb.practices_dir / "swarm-error-execution-context.md").adopted_by == ()


def test_practices_moc_groups_every_note_by_source_repo(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_practice(_practice())
    kb.write_practice(
        _practice(
            id="metagpt-explicit-phase-artifacts",
            source_repo="FoundationAgents/MetaGPT",
            artifact="README.md - the SOP diagram",
        )
    )
    kb.reindex_mocs()

    moc = (kb.mocs_dir / "Practices MOC.md").read_text()
    assert "## openai/swarm" in moc and "## FoundationAgents/MetaGPT" in moc
    for note in kb.practice_notes():
        assert f"[[{note}]]" in moc          # every note is reachable from the MOC
    assert "Total: **2**" in moc
    assert "[[Practices MOC]]" in (kb.mocs_dir / "Knowledge Base MOC.md").read_text()


def test_a_lesson_cites_the_practices_it_was_given_and_nothing_else(tmp_path):
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="improve: adopt phase artifacts",
        outcome="pass", kind="improve", context="c", what_happened="w", lesson="l",
        references=("metagpt-explicit-phase-artifacts",),
    )
    text = kb.write_lesson(lesson).read_text()
    assert f"## {PRACTICE_SECTION}" in text
    assert "- [[metagpt-explicit-phase-artifacts]]" in text
    assert cited_practices(text) == ("metagpt-explicit-phase-artifacts",)

    # a run that cited nothing records that, rather than borrowing a repo name
    bare = kb.write_lesson(replace(lesson, references=())).read_text()
    assert "- _(none cited)_" in bare
    assert cited_practices(bare) == ()


def test_whitepaper_lists_the_practices_adopted_in_its_window(tmp_path):
    kb = KnowledgeBase(tmp_path, whitepaper_every=3)
    kb.write_practice(_practice())
    kb.write_lesson(
        Lesson(
            title="improve: carry phase context in errors",
            outcome="pass", kind="improve", context="c", what_happened="w",
            lesson="Errors that name their phase are traceable.",
            references=("swarm-error-execution-context",),
        )
    )
    kb.write_lesson(
        Lesson(
            title="implement: something uncited",
            outcome="pass", kind="implement", context="c", what_happened="w",
            lesson="Nothing from the field informed this one.",
            references=("a-practice-nobody-registered",),
        )
    )

    paper = kb.synthesize_whitepaper()
    assert "## Practices adopted in this window" in paper.body
    assert "[[swarm-error-execution-context]] - from `openai/swarm`" in paper.body
    # an unresolvable citation is reported as such, never rendered as evidence
    assert "`a-practice-nobody-registered` - **not in the practice registry**" in paper.body

    empty = KnowledgeBase(tmp_path / "empty").synthesize_whitepaper()
    assert "_No lesson in this window cited a practice._" in empty.body


def test_the_backfilled_registry_is_real_and_indexed():
    """The committed registry starts truthful: real repos, real artifacts."""
    kb = KnowledgeBase(REPO_ROOT)
    practices = kb.read_practices()
    assert len(practices) >= 5
    # the id in the frontmatter is the file name is the citation key
    assert {p.id for p in practices} == set(kb.practice_notes())

    moc = (kb.mocs_dir / "Practices MOC.md").read_text()
    for practice in practices:
        assert practice.id == practice.note_name()          # id == file name == citation
        assert "/" in practice.source_repo                  # a real owner/name slug
        assert len(practice.artifact) > 5
        assert practice.observation.strip() and practice.adaptation.strip()
        assert f"[[{practice.note_name()}]]" in moc
        assert f"## {practice.source_repo}" in moc
