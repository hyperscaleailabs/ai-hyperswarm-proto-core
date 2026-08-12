import json

import pytest

from hsai import ledger, memory
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson


def _cfg():
    return load_config()


def _lesson(root, *, title, outcome="pass", kind="implement", created="2026-01-01",
            lesson="Something was learned.", what_happened="The agent ran.",
            ticket=None, pr=None, references=()):
    """Write a lesson through the real renderer, so parsing stays honest."""
    return KnowledgeBase(root).write_lesson(
        Lesson(
            title=title, outcome=outcome, kind=kind, created=created,
            context="ctx", what_happened=what_happened, lesson=lesson,
            ticket=ticket, pr=pr, references=references,
        )
    )


def _cost(root, *, ticket, attempts=1, seconds=100.0, model="opus", outcome="merged"):
    return ledger.append_record(
        root / "knowledge" / "ledger" / "iterations.jsonl",
        ledger.LedgerRecord(
            iteration=1, block=1, ticket=ticket, kind="implement", tier="heavy",
            model=model, wall_clock_seconds=seconds, attempts=attempts, outcome=outcome,
        ),
    )


# --- the corpus: a lesson joined with what it cost ----------------------------


def test_corpus_joins_lessons_with_the_cost_ledger_by_ticket(tmp_path):
    _lesson(tmp_path, title="Poll the remote rollup", ticket=42,
            lesson="Remote CI is the only gate that matters.")
    _cost(tmp_path, ticket=42, attempts=2, seconds=1022.8, model="opus")

    corpus = memory.Corpus.load(tmp_path, _cfg())

    assert len(corpus) == 1
    record = corpus.records[0]
    assert record.ticket == 42
    assert record.attempts == 2 and record.model == "opus"
    assert record.wall_clock_seconds == pytest.approx(1022.8)
    assert record.ledger_outcome == "merged"
    assert record.label() == "pass/implement, #42, 2 attempt(s), 1023s, `opus`"


def test_a_lesson_with_no_ledger_row_still_loads_with_no_cost(tmp_path):
    """Hand-written and pre-ledger lessons must not be dropped from memory."""
    _lesson(tmp_path, title="Bootstrap the loop", ticket=1)

    record = memory.Corpus.load(tmp_path, _cfg()).records[0]

    assert record.ticket == 1
    assert record.attempts == 0 and record.model == "" and record.cost() == ""
    assert record.label() == "pass/implement, #1"


def test_a_memory_carries_the_reference_repos_its_lesson_cited(tmp_path):
    _lesson(tmp_path, title="Adopt a numeric CI gate", ticket=9,
            references=("run-llama/llama_index", "assafelovic/gpt-researcher"))

    record = memory.Corpus.load(tmp_path, _cfg()).records[0]

    assert record.references == ("run-llama/llama_index", "assafelovic/gpt-researcher")


def test_an_empty_or_missing_vault_is_a_supported_state(tmp_path):
    assert len(memory.Corpus.load(tmp_path, _cfg())) == 0
    assert memory.Corpus.load(tmp_path, _cfg()).retrieve("anything") == []


def test_a_corrupt_ledger_degrades_to_lessons_without_costs(tmp_path):
    _lesson(tmp_path, title="Poll the remote rollup", ticket=42)
    path = tmp_path / "knowledge" / "ledger" / "iterations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all\n")

    record = memory.Corpus.load(tmp_path, _cfg()).records[0]

    assert record.ticket == 42 and record.attempts == 0


# --- ranking ------------------------------------------------------------------


def test_ranking_is_deterministic_for_a_fixed_query(tmp_path):
    _lesson(tmp_path, title="Remote continuous integration gate", created="2026-01-01",
            lesson="Remote continuous integration decides whether merging happens.")
    _lesson(tmp_path, title="Obsidian vault wikilinks", created="2026-01-02",
            lesson="Wikilinks connect notes inside the graph view.")

    corpus = memory.Corpus.load(tmp_path, _cfg())
    ranked = [h.record.note_name for h in corpus.retrieve("remote integration gate", k=5)]

    assert ranked == ["2026-01-01-remote-continuous-integration-gate"]
    # same query, same corpus, same answer - every time
    assert ranked == [h.record.note_name for h in corpus.retrieve("remote integration gate", k=5)]
    # a memory that shares no vocabulary with the query is never returned
    assert all("obsidian" not in name for name in ranked)


def test_ties_break_on_recency_then_note_name(tmp_path):
    _lesson(tmp_path, title="Budget gate", created="2026-01-01",
            lesson="The budget gate halts new work.")
    _lesson(tmp_path, title="Budget gate", created="2026-02-01",
            lesson="The budget gate halts new work.")

    hits = memory.Corpus.load(tmp_path, _cfg()).retrieve("budget gate halts", k=2)

    assert hits[0].score == hits[1].score          # a genuine tie...
    assert [h.record.note_name for h in hits] == [  # ...broken by recency
        "2026-02-01-budget-gate", "2026-01-01-budget-gate",
    ]


def test_prefer_outcome_promotes_the_expensive_knowledge(tmp_path):
    _lesson(tmp_path, title="Workflow edits diverge", outcome="fail", created="2026-01-01",
            lesson="Editing workflows made local and remote diverge.")
    _lesson(tmp_path, title="Workflow edits diverge", outcome="pass", created="2026-01-02",
            lesson="Editing workflows made local and remote diverge.")

    corpus = memory.Corpus.load(tmp_path, _cfg())
    plain = corpus.retrieve("workflow edits diverge", k=2)
    preferred = corpus.retrieve("workflow edits diverge", k=2, prefer_outcome="fail")

    assert plain[0].record.outcome == "pass"       # newest wins a pure tie
    assert preferred[0].record.outcome == "fail"   # the failure outranks it
    assert preferred[0].score > plain[0].score


def test_score_is_length_normalised_so_a_rambling_note_cannot_win(tmp_path):
    _lesson(tmp_path, title="Budget gate", created="2026-01-01",
            lesson="The budget gate halts new work.")
    _lesson(tmp_path, title="Budget gate rambling", created="2026-01-02",
            lesson="The budget gate halts new work. " + "unrelated vocabulary words " * 40)

    hits = memory.Corpus.load(tmp_path, _cfg()).retrieve("budget gate halts", k=2)

    assert hits[0].record.note_name == "2026-01-01-budget-gate"
    assert hits[0].score > hits[1].score


def test_score_of_an_unrelated_query_is_zero(tmp_path):
    _lesson(tmp_path, title="Budget gate", lesson="The budget gate halts new work.")
    corpus = memory.Corpus.load(tmp_path, _cfg())

    assert corpus.score("zzzznomatch qqqqnothing", corpus.records[0]) == 0.0
    assert corpus.retrieve("zzzznomatch qqqqnothing", k=3) == []
    assert corpus.retrieve("budget gate", k=0) == []


# --- rendering into a prompt --------------------------------------------------


def test_failures_render_as_warnings_and_successes_as_precedent(tmp_path):
    _lesson(tmp_path, title="Editing the workflows", outcome="fail", ticket=7,
            lesson="Never edit .github/workflows from a worker.")
    _lesson(tmp_path, title="Editing the lesson notes", outcome="pass", ticket=8,
            lesson="Editing lesson notes is safe and expected.")

    section = memory.for_task(
        tmp_path, _cfg(), title="editing files", body="workflows notes"
    )

    assert memory.HEADING in section.section
    assert "- AVOID [[2026-01-01-editing-the-workflows]]" in section.section
    assert "- PRECEDENT [[2026-01-01-editing-the-lesson-notes]]" in section.section
    assert set(section.note_names) == {
        "2026-01-01-editing-the-workflows", "2026-01-01-editing-the-lesson-notes",
    }
    assert bool(section) is True


def test_render_drops_whole_memories_to_stay_inside_the_budget(tmp_path):
    for i in range(5):
        _lesson(tmp_path, title=f"Budget gate variant {i}", created=f"2026-01-0{i + 1}",
                lesson="The budget gate halts new work when the ceiling is reached.")
    hits = memory.Corpus.load(tmp_path, _cfg()).retrieve("budget gate ceiling", k=5)
    assert len(hits) == 5

    full = memory.render(hits, 10_000)
    assert len(full.note_names) == 5

    tight = memory.render(hits, len(full.section) // 2)
    assert 0 < len(tight.note_names) < 5
    assert len(tight.section) <= len(full.section) // 2
    # the audit trail never claims more than was actually injected
    for name in tight.note_names:
        assert f"[[{name}]]" in tight.section
    assert tight.section.count("\n") == len(tight.note_names)

    # a budget too small even for the preamble renders nothing at all
    assert memory.render(hits, 5) == memory.MemorySection()
    assert memory.render(hits, 0) == memory.MemorySection()
    assert memory.render([], 10_000) == memory.MemorySection()


def test_for_task_never_exceeds_the_configured_prompt_budget(tmp_path):
    """An oversized corpus must still fit the ceiling core.yaml declares."""
    cfg = _cfg()
    budget = memory.MemoryConfig.from_core(cfg).max_prompt_chars
    for i in range(40):
        _lesson(
            tmp_path, title=f"Budget gate ceiling variant {i}", created="2026-01-01",
            lesson="The budget gate halts new work when the ceiling is reached. " * 30,
        )

    section = memory.for_task(tmp_path, cfg, title="budget gate ceiling", body="halts work")

    assert section
    assert len(section.section) <= budget
    assert len(section.note_names) <= memory.MemoryConfig.from_core(cfg).k


def test_for_task_excludes_notes_another_retriever_already_showed(tmp_path):
    _lesson(tmp_path, title="Budget gate first", created="2026-01-01",
            lesson="The budget gate halts new work.")
    _lesson(tmp_path, title="Budget gate second", created="2026-01-02",
            lesson="The budget gate halts new work.")

    both = memory.for_task(tmp_path, _cfg(), title="budget gate halts")
    assert len(both.note_names) == 2

    deduped = memory.for_task(
        tmp_path, _cfg(), title="budget gate halts",
        exclude=("2026-01-02-budget-gate-second",),
    )
    assert deduped.note_names == ("2026-01-01-budget-gate-first",)


def test_clamp_is_deterministic_and_always_within_budget():
    text = "\n".join(f"- line {i} {'x' * 50}" for i in range(20))

    assert memory.clamp(text, 10_000) == text          # nothing to do
    assert memory.clamp(text, 0) == ""
    clamped = memory.clamp(text, 200)
    assert len(clamped) <= 200
    assert clamped == memory.clamp(text, 200)          # deterministic
    assert clamped.startswith("- line 0")              # keeps the head, drops the tail
    assert "- line 19" not in clamped
    # a single line longer than the whole budget is hard-cut rather than dropped
    assert len(memory.clamp("y" * 500, 100)) == 100


# --- provenance registry ------------------------------------------------------


def test_practices_are_appended_never_rewritten(tmp_path):
    path = memory.practices_path(_cfg(), tmp_path)
    memory.append_practice(path, memory.PracticeRecord(
        ticket=1, pr=2, title="feat: alpha", reference_repos=("a/b",),
        lesson_note="2026-01-01-alpha", note="It worked.",
    ))
    memory.append_practice(path, memory.PracticeRecord(
        ticket=3, pr=4, title="feat: beta", reference_repos=("c/d", "e/f"),
        lesson_note="2026-01-02-beta", note="It also worked.",
    ))

    assert path == tmp_path / "knowledge" / "registry" / "practices.jsonl"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["title"] == "feat: alpha"    # the first write survives

    records = memory.read_practices(path)
    assert [r.ticket for r in records] == [1, 3]
    assert records[1].reference_repos == ("c/d", "e/f")
    assert records[0].created                                # stamped on write


def test_reading_a_missing_or_damaged_registry_never_raises(tmp_path):
    assert memory.read_practices(tmp_path / "nope.jsonl") == []

    path = tmp_path / "practices.jsonl"
    memory.append_practice(path, memory.PracticeRecord(ticket=1, pr=2, title="feat: alpha"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")

    records = memory.read_practices(path)
    assert [r.title for r in records] == ["feat: alpha"]


def test_adopted_digest_lists_merged_titles_newest_first_with_their_citations(tmp_path):
    path = memory.practices_path(_cfg(), tmp_path)
    memory.append_practice(path, memory.PracticeRecord(
        ticket=1, pr=2, title="feat: quota ledger", reference_repos=("assafelovic/gpt-researcher",),
    ))
    memory.append_practice(path, memory.PracticeRecord(
        ticket=3, pr=4, title="feat: retrieval memory", reference_repos=("run-llama/llama_index",),
    ))
    # the same practice retried is still ONE practice
    memory.append_practice(path, memory.PracticeRecord(
        ticket=3, pr=5, title="feat: retrieval memory", reference_repos=("run-llama/llama_index",),
    ))

    digest = memory.adopted_digest(_cfg(), root=tmp_path)

    assert memory.ADOPTED_INTRO in digest
    assert "- feat: retrieval memory (PR #5) - `run-llama/llama_index`" in digest
    assert "- feat: quota ledger (PR #2) - `assafelovic/gpt-researcher`" in digest
    assert digest.count("feat: retrieval memory") == 1          # deduplicated by title
    # newest first: the current frontier leads
    assert digest.index("retrieval memory") < digest.index("quota ledger")


def test_adopted_digest_is_empty_before_anything_has_merged(tmp_path):
    assert memory.adopted_digest(_cfg(), root=tmp_path) == ""


def test_a_practice_with_no_citations_says_so_rather_than_rendering_blank(tmp_path):
    path = memory.practices_path(_cfg(), tmp_path)
    memory.append_practice(path, memory.PracticeRecord(ticket=1, pr=None, title="chore: tidy"))

    digest = memory.adopted_digest(_cfg(), root=tmp_path)

    assert "- chore: tidy - _(none cited)_" in digest      # no PR number, no refs
