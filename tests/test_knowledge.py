from hsai.knowledge import (
    KnowledgeBase,
    Lesson,
    LessonRecord,
    Observation,
    Whitepaper,
    parse_field_note,
    parse_note,
    reference_note_name,
    slugify,
    split_sections,
)


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
        references=("openai/swarm",),
    )
    path = kb.write_lesson(lesson)
    assert path.exists()
    text = path.read_text()
    assert "# implement: add status command" in text
    assert "[[Lessons MOC]]" in text  # Obsidian wikilink up to MOC
    assert "openai/swarm" in text
    assert "sonnet" in text

    written = kb.reindex_mocs()
    names = {p.name for p in written}
    assert names == {
        "Lessons MOC.md", "Whitepapers MOC.md", "Reference MOC.md", "Knowledge Base MOC.md",
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


# --- reference field notes: durable, append-only project memory ---------------

def _obs(practice_id="crewaiinc-crewai-workflow-ci-yml", artifact=".github/workflows/ci.yml",
         detail="matrix build on 3 python versions", observed="2026-08-14"):
    return Observation(
        practice_id=practice_id, artifact=artifact, detail=detail, observed=observed
    )


def test_reference_note_name_is_stable_for_a_repo_slug():
    assert reference_note_name("crewAIInc/crewAI") == "crewaiinc-crewai"
    assert reference_note_name("run-llama/llama_index") == "run-llama-llama-index"


def test_append_observations_creates_a_note_with_frontmatter_and_a_dated_entry(tmp_path):
    kb = KnowledgeBase(tmp_path)
    path, appended = kb.append_observations(
        "crewAIInc/crewAI", [_obs()], stars=56129, license="MIT", snapshot_date="2026-07-25",
    )

    assert path == kb.reference_dir / "crewaiinc-crewai.md"
    assert [o.practice_id for o in appended] == ["crewaiinc-crewai-workflow-ci-yml"]
    text = path.read_text()
    assert "repo: crewAIInc/crewAI" in text
    assert "stars: 56129" in text
    assert "license: MIT" in text
    assert "snapshot_date: 2026-07-25" in text
    assert "## Observations" in text
    # dated, citing its artifact, and addressable by practice_id
    assert "### 2026-08-14 - `crewaiinc-crewai-workflow-ci-yml`" in text
    assert "- artifact: `.github/workflows/ci.yml`" in text
    assert "matrix build on 3 python versions" in text
    assert "[[Reference MOC]]" in text


def test_appending_is_idempotent_and_never_rewrites_prior_entries(tmp_path):
    """A second mining pass may only ADD - the bytes already on disk are frozen."""
    kb = KnowledgeBase(tmp_path)
    path, _ = kb.append_observations("crewAIInc/crewAI", [_obs()])
    first = path.read_text()

    # Same artifact, same content, mined again: nothing to say, nothing appended.
    _, appended = kb.append_observations("crewAIInc/crewAI", [_obs(observed="2026-09-01")])
    assert appended == []
    assert path.read_text() == first

    # The artifact drifted: a NEW dated entry lands and the old one is untouched.
    _, appended = kb.append_observations(
        "crewAIInc/crewAI",
        [_obs(detail="matrix build on 4 python versions", observed="2026-09-01")],
    )
    assert len(appended) == 1
    text = path.read_text()
    assert text.startswith(first)
    assert "### 2026-09-01 - `crewaiinc-crewai-workflow-ci-yml`" in text
    assert "matrix build on 3 python versions" in text  # history preserved
    assert "matrix build on 4 python versions" in text


def test_parse_field_note_reads_repo_and_recorded_practice_ids(tmp_path):
    kb = KnowledgeBase(tmp_path)
    path, _ = kb.append_observations(
        "crewAIInc/crewAI",
        [_obs(), _obs(practice_id="crewaiinc-crewai-commits", artifact="last 30 commit subjects",
                      detail="[docs-freeze] snapshot commits")],
        stars=56129, license="MIT",
    )
    note = parse_field_note(path)
    assert note.note_name == "crewaiinc-crewai"
    assert note.repo == "crewAIInc/crewAI"
    assert note.practice_ids == (
        "crewaiinc-crewai-workflow-ci-yml", "crewaiinc-crewai-commits",
    )
    assert note.observations == 2


def test_reference_moc_wikilinks_every_field_note(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.append_observations("crewAIInc/crewAI", [_obs()])
    kb.append_observations(
        "run-llama/llama_index",
        [_obs(practice_id="run-llama-llama-index-workflow-issue-classifier-yml",
              artifact=".github/workflows/issue_classifier.yml", detail="auto-labels issues")],
    )
    kb.reindex_mocs()

    moc = (kb.mocs_dir / "Reference MOC.md").read_text()
    assert "[[crewaiinc-crewai]]" in moc
    assert "[[run-llama-llama-index]]" in moc
    assert "`crewAIInc/crewAI`" in moc
    assert "Up: [[Knowledge Base MOC]]" in moc
    # ...and the root MOC links down to it, so the graph stays connected
    assert "[[Reference MOC]]" in (kb.mocs_dir / "Knowledge Base MOC.md").read_text()


def test_lesson_records_the_practices_it_adopted(tmp_path):
    """G1/G2: a merged PR must trace to a named practice, not a bare repo slug."""
    kb = KnowledgeBase(tmp_path)
    kb.append_observations("crewAIInc/crewAI", [_obs()])
    lesson = Lesson(
        title="implement: freeze docs snapshots",
        outcome="pass",
        kind="implement",
        context="c",
        what_happened="w",
        lesson="l",
        references=("crewAIInc/crewAI",),
        practices=("crewaiinc-crewai-workflow-ci-yml",),
    )
    path = kb.write_lesson(lesson)
    text = path.read_text()

    assert "practices:\n  - crewaiinc-crewai-workflow-ci-yml\n" in text.split("---\n")[1]
    assert "### Practices adopted" in text
    # the practice resolves to the field note that owns it
    assert "`crewaiinc-crewai-workflow-ci-yml` - see [[crewaiinc-crewai]]" in text

    # ...and reads back off disk, which is what the adoption index scans
    record = kb.read_lessons()[0]
    assert record.practices == ("crewaiinc-crewai-workflow-ci-yml",)
    assert record.tags == ("lesson", "outcome/pass", "kind/implement")  # not leaked as tags

    # a practice with no field note still renders, just without a dangling link
    lesson.practices = ("some-unmined-practice",)
    assert "- `some-unmined-practice`\n" in kb.write_lesson(lesson).read_text()


def test_lesson_without_practices_renders_as_before(tmp_path):
    kb = KnowledgeBase(tmp_path)
    path = kb.write_lesson(
        Lesson(title="implement: x", outcome="pass", kind="implement",
               context="c", what_happened="w", lesson="l")
    )
    text = path.read_text()
    assert "practices" not in text.split("---\n")[1]
    assert "### Practices adopted" not in text


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
