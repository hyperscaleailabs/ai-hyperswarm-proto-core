from hsai.knowledge import (
    FieldNote,
    KnowledgeBase,
    Lesson,
    LessonRecord,
    Observation,
    Whitepaper,
    parse_field_note,
    parse_note,
    practice_id,
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


def test_lesson_renders_the_execution_trace_section(tmp_path):
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
    # No model run this iteration (e.g. a dry run): the section still appears,
    # with an explicit placeholder rather than being silently omitted.
    path = kb.write_lesson(lesson)
    text = path.read_text()
    assert "## Execution trace" in text
    assert "_(no model run this iteration)_" in text

    lesson.execution_trace = (
        "| field | value |\n| --- | --- |\n| tokens | 1500 in / 320 out |\n"
        "| exit status | ok |"
    )
    kb.write_lesson(lesson)
    text = path.read_text()
    assert "1500 in / 320 out" in text
    assert "| exit status | ok |" in text
    # and the surrounding sections are untouched
    assert "## Lesson learned" in text and "## Independent review" in text


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


# --- reference field notes: the durable, append-only half of G1 ---------------

def _observation(practice: str, *, day: str, artifact: str = "`.github/workflows/x.yml`"):
    return Observation(
        practice=practice,
        artifact=artifact,
        what="does a specific, checkable thing",
        why="we could mirror it",
        observed=day,
    )


def test_field_note_carries_frontmatter_and_a_dated_artifact_citing_entry(tmp_path):
    kb = KnowledgeBase(tmp_path)
    path = kb.append_field_note(
        FieldNote(
            repo="run-llama/llama_index",
            stars=51099,
            license="MIT",
            snapshot_date="2026-07-25",
            observations=(_observation("automated inbound triage", day="2026-08-14"),),
        )
    )

    assert path.name == "run-llama-llama_index.md"
    text = path.read_text()
    frontmatter = text.split("---\n")[1]
    assert "repo: run-llama/llama_index" in frontmatter
    assert "stars: 51099" in frontmatter
    assert "license: MIT" in frontmatter
    assert "snapshot_date: 2026-07-25" in frontmatter
    assert "[[Reference MOC]]" in text  # joins the Obsidian graph
    assert "## Observations" in text
    # the entry is dated, cites its artifact, and carries a stable practice_id
    pid = practice_id("run-llama/llama_index", "automated inbound triage")
    assert f"### 2026-08-14 - `{pid}`" in text
    assert "`.github/workflows/x.yml`" in text


def test_appending_a_field_note_never_rewrites_prior_entries(tmp_path):
    """The crewAI `[docs-freeze]` discipline: a pass adds, it never overwrites."""
    kb = KnowledgeBase(tmp_path)
    note = FieldNote(
        repo="crewAIInc/crewAI",
        stars=56129,
        license="MIT",
        snapshot_date="2026-07-25",
        observations=(_observation("dated snapshot commits", day="2026-08-01"),),
    )
    path = kb.append_field_note(note)
    first_pass = path.read_text()

    note.observations = (_observation("append-only docs", day="2026-08-14"),)
    kb.append_field_note(note)
    second_pass = path.read_text()

    # every byte of the first pass survives, in place, as the prefix
    assert second_pass.startswith(first_pass)
    # ...and the second pass really did add a new dated entry
    assert second_pass != first_pass
    assert "### 2026-08-01 - " in second_pass
    assert "### 2026-08-14 - " in second_pass
    assert second_pass.count("## Observations") == 1  # one log, not two headers
    assert second_pass.count("snapshot_date:") == 1   # one frontmatter block


def test_a_pass_that_observed_nothing_writes_nothing(tmp_path):
    """An entry with no artifact behind it would be folklore, not evidence."""
    kb = KnowledgeBase(tmp_path)
    path = kb.append_field_note(FieldNote(repo="openai/swarm"))
    assert not path.exists()
    assert kb.reference_notes() == []


def test_parse_field_note_reads_repo_and_practice_ids_back(tmp_path):
    kb = KnowledgeBase(tmp_path)
    note = FieldNote(
        repo="openai/swarm",
        observations=(
            _observation("handoff protocol", day="2026-08-01"),
            _observation("routine abstraction", day="2026-08-01"),
        ),
    )
    kb.append_field_note(note)
    # a second pass re-observes ONE of the same practices on a later date
    note.observations = (_observation("handoff protocol", day="2026-08-14"),)
    path = kb.append_field_note(note)

    record = parse_field_note(path)
    assert record.repo == "openai/swarm"
    assert record.observed_dates == ("2026-08-01", "2026-08-01", "2026-08-14")
    # three entries, but a practice is only listed once - the id is the key
    assert record.practice_ids == (
        practice_id("openai/swarm", "handoff protocol"),
        practice_id("openai/swarm", "routine abstraction"),
    )
    assert kb.read_field_notes() == [record]


def test_reference_moc_wikilinks_every_field_note(tmp_path):
    kb = KnowledgeBase(tmp_path)
    for repo in ("openai/swarm", "crewAIInc/crewAI"):
        kb.append_field_note(
            FieldNote(repo=repo, observations=(_observation("a practice", day="2026-08-14"),))
        )

    kb.reindex_mocs()
    moc = (kb.mocs_dir / "Reference MOC.md").read_text()
    assert "[[openai-swarm]]" in moc
    assert "[[crewAIInc-crewAI]]" in moc
    assert "Up: [[Knowledge Base MOC]]" in moc
    # and the root MOC links down to it, so the graph is connected both ways
    assert "[[Reference MOC]]" in (kb.mocs_dir / "Knowledge Base MOC.md").read_text()


def test_lesson_carries_practice_ids_in_frontmatter_and_references(tmp_path):
    """A merged PR must trace back to named practices, not bare repo slugs."""
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: dedupe gate",
        outcome="pass",
        kind="implement",
        context="c",
        what_happened="w",
        lesson="l",
        references=("run-llama/llama_index",),
        practices=("run-llama-llama-index--automated-inbound-triage",),
    )
    path = kb.write_lesson(lesson)
    text = path.read_text()
    frontmatter = text.split("---\n")[1]
    assert "practices:\n  - run-llama-llama-index--automated-inbound-triage\n" in frontmatter
    assert "### Practices adopted" in text
    assert "`run-llama-llama-index--automated-inbound-triage`" in text

    # they round-trip, which is what the adoption index reads
    record = kb.read_lessons()[0]
    assert record.practices == ("run-llama-llama-index--automated-inbound-triage",)
    # and the practice list never leaks back in as a tag
    assert record.tags == ("lesson", "outcome/pass", "kind/implement")

    # nothing cited -> the key is absent and the section says so explicitly
    lesson.practices = ()
    plain = kb.write_lesson(lesson).read_text()
    assert "practices:" not in plain.split("---\n")[1]
    assert "no practice_id cited" in plain
