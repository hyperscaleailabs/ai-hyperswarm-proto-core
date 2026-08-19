import re

from hsai import practices as practices_mod
from hsai.knowledge import (
    KnowledgeBase,
    Lesson,
    LessonRecord,
    Whitepaper,
    parse_note,
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


def test_failed_lesson_carries_a_failure_class_tag_and_row(tmp_path):
    """G4: the Obsidian graph can filter failures by cause (see hsai.postmortem)."""
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: add widget",
        outcome="fail",
        kind="implement",
        context="ctx",
        what_happened="agent errored out",
        lesson="investigate",
        ticket=7,
        failure_class="agent_error",
    )
    path = kb.write_lesson(lesson)
    text = path.read_text()
    assert "  - failure/agent_error" in text
    assert "| failure class | `agent_error` |" in text

    # read back through the same path recall/synthesis use
    record = parse_note(path)
    assert record.outcome == "fail"
    assert record.failure_class == "agent_error"


def test_passing_lesson_omits_the_failure_class_tag_and_row(tmp_path):
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: add widget",
        outcome="pass",
        kind="implement",
        context="ctx",
        what_happened="all green",
        lesson="shipped cleanly",
        ticket=7,
        # A stray value here must never leak into a passing note's frontmatter.
        failure_class="agent_error",
    )
    path = kb.write_lesson(lesson)
    text = path.read_text()
    assert "failure/" not in text
    assert "failure class" not in text

    record = parse_note(path)
    assert record.failure_class == ""


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
        "Lessons MOC.md", "Whitepapers MOC.md", "Practices MOC.md", "Knowledge Base MOC.md",
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


# --- Practices MOC: grouped by source project, linked from the root MOC ------

def test_practices_moc_groups_by_source_project(tmp_path):
    kb = KnowledgeBase(tmp_path)
    practices_mod.append(
        tmp_path,
        practices_mod.build_practice(
            title="session durability", source_project="OpenBMB/ChatDev",
            source_artifact="harness_design", evidence="PR #104", adopted_pr=104,
        ),
    )
    practices_mod.append(
        tmp_path,
        practices_mod.build_practice(
            title="reconciliation discipline", source_project="OpenBMB/ChatDev",
            source_artifact="harness_design", evidence="PR #104", adopted_pr=104,
        ),
    )
    practices_mod.append(
        tmp_path,
        practices_mod.build_practice(
            title="cost accounting", source_project="assafelovic/gpt-researcher",
            source_artifact="source_code", evidence="PR #47", adopted_pr=47,
        ),
    )

    written = kb.reindex_mocs()
    names = {p.name for p in written}
    assert "Practices MOC.md" in names

    text = (kb.mocs_dir / "Practices MOC.md").read_text()
    assert "### `OpenBMB/ChatDev`" in text
    assert "### `assafelovic/gpt-researcher`" in text
    assert "session durability" in text and "reconciliation discipline" in text
    assert "Total: **3**" in text
    # OpenBMB/ChatDev's two entries both sit under its own heading, not split
    assert text.index("### `OpenBMB/ChatDev`") < text.index("### `assafelovic/gpt-researcher`")

    root_moc = (kb.mocs_dir / "Knowledge Base MOC.md").read_text()
    assert "[[Practices MOC]] - 3 practice(s)" in root_moc


def test_practices_moc_reindex_is_deterministic(tmp_path):
    """`hsai reindex` run twice on an unchanged registry must not diff."""
    kb = KnowledgeBase(tmp_path)
    practices_mod.append(
        tmp_path,
        practices_mod.build_practice(
            title="hard numeric CI gate", source_project="run-llama/llama_index",
            source_artifact="ci_cd", evidence="PR #47",
        ),
    )
    kb.reindex_mocs()
    first = (kb.mocs_dir / "Practices MOC.md").read_text()
    kb.reindex_mocs()
    second = (kb.mocs_dir / "Practices MOC.md").read_text()
    assert first == second


def test_practices_moc_placeholder_when_empty(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.reindex_mocs()
    text = (kb.mocs_dir / "Practices MOC.md").read_text()
    assert "No practices recorded yet" in text
    assert "Total: **0**" in text


# --- most-recalled lessons: which knowledge is earning its keep --------------


def _recalling_lesson(kb: KnowledgeBase, title: str, recalled: tuple[str, ...]) -> str:
    """Write a lesson that records `recalled` as the notes it was shown."""
    lesson = Lesson(
        title=title, outcome="pass", kind="implement", context="ctx",
        what_happened="did the thing", lesson="a lesson", recalled=recalled,
    )
    kb.write_lesson(lesson)
    return lesson.note_name()


def test_recalled_frontmatter_round_trips_without_polluting_tags(tmp_path):
    """`recalled:` and `tags:` are both YAML lists; neither may absorb the other."""
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: retrieval", outcome="fail", kind="implement", context="c",
        what_happened="w", lesson="l", tags=("area/recall",),
        recalled=("2026-01-01-a", "2026-01-02-b"), failure_class="remote_ci_fail",
    )
    record = parse_note(kb.write_lesson(lesson))

    assert record.recalled == ("2026-01-01-a", "2026-01-02-b")
    # the recalled note names must NOT have been filed as tags...
    assert "2026-01-01-a" not in record.tags
    # ...and the real tags are all still there
    assert "area/recall" in record.tags
    assert record.outcome == "fail" and record.failure_class == "remote_ci_fail"


def test_a_lesson_with_no_recall_records_no_backlinks(tmp_path):
    kb = KnowledgeBase(tmp_path)
    record = parse_note(kb.write_lesson(Lesson(
        title="implement: cold start", outcome="pass", kind="implement",
        context="c", what_happened="w", lesson="l",
    )))
    assert record.recalled == ()
    assert kb.most_recalled() == []


def test_most_recalled_ranks_by_how_often_a_lesson_was_retrieved(tmp_path):
    kb = KnowledgeBase(tmp_path)
    popular = _recalling_lesson(kb, "implement: popular", ())
    niche = _recalling_lesson(kb, "implement: niche", ())
    # three later iterations cite `popular`, one cites `niche`
    _recalling_lesson(kb, "implement: one", (popular,))
    _recalling_lesson(kb, "implement: two", (popular, niche))
    _recalling_lesson(kb, "implement: three", (popular,))

    assert kb.most_recalled() == [(popular, 3), (niche, 1)]
    assert kb.most_recalled(limit=1) == [(popular, 3)]


def test_most_recalled_drops_names_that_no_longer_resolve(tmp_path):
    """A MOC must never emit an unresolved [[wikilink]] (acceptance criterion)."""
    kb = KnowledgeBase(tmp_path)
    real = _recalling_lesson(kb, "implement: real", ())
    _recalling_lesson(kb, "implement: citing", (real, "2020-01-01-deleted-note"))

    assert kb.most_recalled() == [(real, 1)]


def test_lessons_moc_surfaces_the_most_recalled_and_every_link_resolves(tmp_path):
    kb = KnowledgeBase(tmp_path)
    popular = _recalling_lesson(kb, "implement: popular", ())
    citing = _recalling_lesson(kb, "implement: citing", (popular,))
    kb.reindex_mocs()

    text = (kb.mocs_dir / "Lessons MOC.md").read_text()
    assert "## Most-recalled lessons" in text
    assert f"- [[{popular}]] - retrieved into **1** later prompt(s)" in text
    # the full listing survives alongside the new section
    assert "## All lessons" in text
    assert f"[[{citing}]]" in text

    # Every wikilink in the MOC resolves to a note that exists in the vault.
    on_disk = set(kb.lesson_notes()) | {"Knowledge Base MOC"}
    assert set(re.findall(r"\[\[([^\]]+)\]\]", text)) <= on_disk


def test_lessons_moc_says_so_when_nothing_has_been_recalled_yet(tmp_path):
    """A fresh clone: the section exists but claims nothing."""
    kb = KnowledgeBase(tmp_path)
    kb.reindex_mocs()
    text = (kb.mocs_dir / "Lessons MOC.md").read_text()
    assert "## Most-recalled lessons" in text
    assert "Nothing retrieved yet" in text
    assert "No lessons recorded yet" in text


def test_whitepaper_states_the_recall_comparison_explicitly(tmp_path):
    """The block whitepaper must say whether recall changed the pass rate."""
    kb = KnowledgeBase(tmp_path)
    prior = _recalling_lesson(kb, "implement: prior", ())
    # two iterations ran with a pack (one passed), one ran cold (and passed)
    kb.write_lesson(Lesson(
        title="implement: packed pass", outcome="pass", kind="implement",
        context="c", what_happened="w", lesson="l", recalled=(prior,),
    ))
    kb.write_lesson(Lesson(
        title="implement: packed fail", outcome="fail", kind="implement",
        context="c", what_happened="w", lesson="l", recalled=(prior,),
    ))

    body = kb.synthesize_whitepaper().body
    assert "## Effect of lesson recall" in body
    assert "with prior lessons recalled into the prompt: 1/2 passed (50%)" in body
    # `prior` itself recalled nothing, so it is the cold arm
    assert "without (cold start): 1/1 passed (100%)" in body
    assert "not yet a comparison" not in body


def test_whitepaper_declines_to_compare_recall_with_only_one_arm(tmp_path):
    kb = KnowledgeBase(tmp_path)
    _recalling_lesson(kb, "implement: cold", ())

    body = kb.synthesize_whitepaper().body
    assert "with prior lessons recalled into the prompt: _none in this window_" in body
    assert "without (cold start): 1/1 passed (100%)" in body
    assert "not yet a comparison" in body


def test_lessons_moc_reindex_is_deterministic(tmp_path):
    """Ties break on note name, so `hsai reindex` twice must not diff."""
    kb = KnowledgeBase(tmp_path)
    a = _recalling_lesson(kb, "implement: a", ())
    b = _recalling_lesson(kb, "implement: b", ())
    # a and b are recalled equally often - only the tie-break orders them
    _recalling_lesson(kb, "implement: citing", (a, b))

    kb.reindex_mocs()
    first = (kb.mocs_dir / "Lessons MOC.md").read_text()
    kb.reindex_mocs()
    assert (kb.mocs_dir / "Lessons MOC.md").read_text() == first
    assert kb.most_recalled() == [(a, 1), (b, 1)]
