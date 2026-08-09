from hsai.knowledge import KnowledgeBase, Lesson, LessonRecord, Whitepaper, slugify
from hsai.practices import Practice, PracticeRef, PracticeRegistry


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
        "Lessons MOC.md", "Whitepapers MOC.md", "Practices MOC.md", "Knowledge Base MOC.md",
    }
    lessons_moc = (kb.mocs_dir / "Lessons MOC.md").read_text()
    assert f"[[{lesson.note_name()}]]" in lessons_moc


def test_practices_moc_indexes_every_note_and_is_idempotent(tmp_path):
    kb = KnowledgeBase(tmp_path)
    registry = PracticeRegistry(tmp_path)
    registry.queue(Practice(
        id="crewaiinc-crewai-docs-freeze", source_repo="crewAIInc/crewAI",
        artifact="commit [docs-freeze]", summary="durable provenance artifact per change",
    ))
    registry.mark_adopted(
        (PracticeRef("openai/swarm", "tiny inspectable control loop"),), ticket=3, pr=4
    )

    kb.reindex_mocs()
    moc = kb.mocs_dir / "Practices MOC.md"
    text = moc.read_text()
    for practice in registry.read_all():
        assert f"[[{practice.note_name()}]]" in text
    assert "### crewAIInc/crewAI - 1 practice(s)" in text
    assert "| openai/swarm | 0 | 1 | 0 | 1 |" in text        # per-project coverage
    assert "PR #4" in text
    assert "[[Practices MOC]]" in (kb.mocs_dir / "Knowledge Base MOC.md").read_text()

    # derived index: rebuilding it must not churn the vault
    kb.reindex_mocs()
    assert moc.read_text() == text


def test_practices_moc_is_written_for_an_empty_registry(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.reindex_mocs()
    assert "No practices extracted yet" in (kb.mocs_dir / "Practices MOC.md").read_text()


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
