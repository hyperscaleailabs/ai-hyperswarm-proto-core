from hsai.knowledge import KnowledgeBase, Lesson, Whitepaper, slugify


def test_slugify():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  ") == "untitled"


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
    assert names == {"Lessons MOC.md", "Whitepapers MOC.md", "Knowledge Base MOC.md"}
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
