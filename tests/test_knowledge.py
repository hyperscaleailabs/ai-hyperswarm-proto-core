from pathlib import Path

from hsai.knowledge import (
    KnowledgeBase,
    Lesson,
    LessonRecord,
    Practice,
    Whitepaper,
    cited_practice_ids,
    parse_note,
    parse_practice,
    slugify,
    split_sections,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _practice(**overrides) -> Practice:
    fields = {
        "id": "metagpt-phase-artifacts",
        "source_repo": "FoundationAgents/MetaGPT",
        "artifact": "metagpt/roles/",
        "observation": "Each SOP role emits a named document for the next phase.",
        "adaptation": "`_phase_artifacts()` renders per-phase deliverables into the PR body.",
    }
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
        references=("swarm-error-context",),
    )
    path = kb.write_lesson(lesson)
    assert path.exists()
    text = path.read_text()
    assert "# implement: add status command" in text
    assert "[[Lessons MOC]]" in text  # Obsidian wikilink up to MOC
    # evidence is a wikilink to a practice note, so the graph edge is real
    assert "[[swarm-error-context]]" in text
    assert "sonnet" in text

    written = kb.reindex_mocs()
    names = {p.name for p in written}
    assert names == {
        "Lessons MOC.md",
        "Whitepapers MOC.md",
        "Practices MOC.md",
        "Knowledge Base MOC.md",
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


# --- the practice registry: G1 evidence that resolves --------------------------


def test_practice_note_carries_source_artifact_observation_and_adaptation(tmp_path):
    kb = KnowledgeBase(tmp_path)
    practice = _practice(adopted_by=("ticket #43", "PR #46"))

    path = kb.write_practice(practice)
    text = path.read_text()
    assert path.name == "metagpt-phase-artifacts.md"
    assert "source_repo: FoundationAgents/MetaGPT" in text
    assert "artifact: metagpt/roles/" in text
    assert "## Observation" in text and "## Adaptation" in text
    assert "[[Practices MOC]]" in text  # up-link, so the graph stays connected
    assert "- ticket #43" in text and "- PR #46" in text

    # and it reads back as the same record, generated tags included exactly once
    back = parse_practice(path)
    assert back == practice
    assert kb.write_practice(back).read_text() == text


def test_practice_ids_are_what_a_citation_must_resolve_to(tmp_path):
    kb = KnowledgeBase(tmp_path)
    assert kb.practice_ids() == set()
    kb.write_practice(_practice())
    kb.write_practice(_practice(id="swarm-error-context", source_repo="openai/swarm"))
    assert kb.practice_ids() == {"metagpt-phase-artifacts", "swarm-error-context"}


def test_cited_practice_ids_reads_evidence_sections_and_ignores_everything_else():
    body = """## Problem
Provenance is faked. See `openai/swarm` and run `pytest` for the details.

## Practices cited
- `metagpt-phase-artifacts` - `FoundationAgents/MetaGPT` - metagpt/roles/
- `swarm-error-context` - `openai/swarm` - swarm/core.py

## Meta
- goals: G1
"""
    assert cited_practice_ids(body) == (
        "metagpt-phase-artifacts",
        "swarm-error-context",
    )
    # a repo slug, a bare command and prose outside the evidence section are
    # never mistaken for evidence
    assert "openai/swarm" not in cited_practice_ids(body)
    assert cited_practice_ids("## Problem\n`some-thing` in prose only\n") == ()

    # a lesson cites the same ids as wikilinks, and reads back identically
    lesson_note = (
        "## References (reference-set evidence)\n- [[metagpt-phase-artifacts]]\n"
    )
    assert cited_practice_ids(lesson_note) == ("metagpt-phase-artifacts",)
    assert cited_practice_ids("## References (reference-set evidence)\n- _(none cited)_\n") == ()


def test_reindex_builds_a_practices_moc_grouped_by_source_repo(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_practice(_practice())
    kb.write_practice(
        _practice(id="metagpt-roles-review", artifact="metagpt/roles/qa_engineer.py")
    )
    kb.write_practice(_practice(id="swarm-error-context", source_repo="openai/swarm"))

    kb.reindex_mocs()
    moc = (kb.mocs_dir / "Practices MOC.md").read_text()

    assert "### FoundationAgents/MetaGPT" in moc and "### openai/swarm" in moc
    # every note in the registry is listed, as a resolvable wikilink
    for note in kb.practice_notes():
        assert f"[[{note}]]" in moc
    assert "Total: **3** across 2 project(s)" in moc
    # both MetaGPT practices sit under the one heading, in id order
    metagpt = moc.split("### FoundationAgents/MetaGPT")[1].split("###")[0]
    assert metagpt.index("metagpt-phase-artifacts") < metagpt.index("metagpt-roles-review")

    # and the root MOC points at it, so the vault has no orphan map
    assert "[[Practices MOC]]" in (kb.mocs_dir / "Knowledge Base MOC.md").read_text()


def test_synthesize_whitepaper_lists_the_practices_adopted_in_its_window(tmp_path):
    kb = KnowledgeBase(tmp_path, whitepaper_every=2)
    kb.write_practice(_practice())
    kb.write_lesson(
        Lesson(
            title="implement: explicit phase artifacts",
            outcome="pass",
            kind="implement",
            context="c",
            what_happened="w",
            lesson="Phase deliverables belong in the PR body.",
            references=("metagpt-phase-artifacts", "not-in-the-registry"),
        )
    )
    kb.write_lesson(
        Lesson(
            title="implement: something uncited",
            outcome="pass",
            kind="implement",
            context="c",
            what_happened="w",
            lesson="Nothing from the reference set informed this.",
        )
    )

    paper = kb.synthesize_whitepaper()
    # only ids that resolve are carried forward - a dangling link would break
    # the lesson -> practice -> MOC path the whitepaper claims to demonstrate
    assert paper.cites_practices == ("metagpt-phase-artifacts",)

    text = kb.write_whitepaper(paper).read_text()
    assert "## Practices adopted in this window" in text
    assert "- [[metagpt-phase-artifacts]] - `FoundationAgents/MetaGPT`" in text
    assert "not-in-the-registry" not in text


def test_a_window_that_cited_nothing_says_so(tmp_path):
    kb = KnowledgeBase(tmp_path, whitepaper_every=1)
    kb.write_lesson(
        Lesson(title="heal: fix it", outcome="pass", kind="heal",
               context="c", what_happened="w", lesson="l")
    )
    paper = kb.synthesize_whitepaper()
    assert paper.cites_practices == ()
    assert "No lesson in this window cited a practice" in kb.write_whitepaper(paper).read_text()


# --- the committed registry itself --------------------------------------------


def _committed_practices() -> list[Practice]:
    return KnowledgeBase(REPO_ROOT).read_practices()


def test_the_committed_registry_is_backfilled_with_real_evidence():
    """The registry starts truthful: practices already adopted, really sourced."""
    practices = _committed_practices()
    assert len(practices) >= 5

    pinned = {
        line.split("repo:", 1)[1].strip()
        for line in (REPO_ROOT / ".ai-swarm" / "core.yaml").read_text().splitlines()
        if line.strip().startswith("- repo:")
    }
    for p in practices:
        assert p.source_repo in pinned, f"{p.id} cites {p.source_repo}, not a pinned project"
        assert p.artifact and p.observation and p.adaptation, f"{p.id} is missing a field"
        assert p.adopted_by, f"{p.id} claims no adoption"
        assert "-" in p.id  # citable: a one-word id is indistinguishable from prose


def test_the_committed_practices_moc_is_what_reindex_would_write(tmp_path):
    """The checked-in MOC must not drift from the generator that owns it."""
    kb = KnowledgeBase(tmp_path)
    for practice in _committed_practices():
        kb.write_practice(practice)
    kb.reindex_mocs()

    def body(text: str) -> str:
        return text.split("---\n", 2)[2]  # drop the frontmatter's `updated:` date

    generated = (kb.mocs_dir / "Practices MOC.md").read_text()
    committed = (REPO_ROOT / "knowledge" / "MOCs" / "Practices MOC.md").read_text()
    assert body(generated) == body(committed)
