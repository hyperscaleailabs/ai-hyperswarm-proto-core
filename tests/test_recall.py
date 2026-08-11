"""Retrieval over the repo's own knowledge base.

Every ranking here is asserted EXACTLY: recall is deterministic by design (no
model call, stable tie-break on note name), so a fuzzy assertion would hide the
very regressions this module can suffer.
"""
from dataclasses import replace
from pathlib import Path

from hsai.config import load_config
from hsai.recall import (
    HEADING,
    Corpus,
    RecallConfig,
    for_task,
    render,
    tokenize,
)


def _cfg(**recall_overrides):
    """The real core.yaml with the `knowledge.recall` block overridden."""
    base = load_config()
    knowledge = dict(base.knowledge)
    knowledge["recall"] = {**(knowledge.get("recall") or {}), **recall_overrides}
    return replace(base, knowledge=knowledge)


def _write_note(
    root: Path,
    rel_dir: str,
    name: str,
    *,
    title: str,
    body: str,
    outcome: str = "pass",
    kind: str = "implement",
    section: str = "Lesson learned",
) -> Path:
    directory = root / rel_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        "---\n"
        "tags:\n"
        "  - lesson\n"
        f"  - outcome/{outcome}\n"
        f"  - kind/{kind}\n"
        "created: 2026-01-01\n"
        "---\n"
        f"\n# {title}\n\n## {section}\n{body}\n"
    )
    return path


def _lesson(root: Path, name: str, **kwargs) -> Path:
    return _write_note(root, "knowledge/lessons", name, **kwargs)


# --- tokenizer ---------------------------------------------------------------


def test_tokenizer_keeps_short_domain_terms_and_splits_hyphens():
    # `ci`, `pr` and `gh` are the vocabulary of this repo; a 4-char minimum
    # (as the whitepaper synthesizer uses) would throw all three away.
    assert tokenize("Remote CI gate for a PR") == ["remote", "ci", "gate", "pr"]
    # hyphenated compounds must match their spelled-out query form
    assert tokenize("knowledge-only diff") == ["knowledge", "only", "diff"]
    # stopwords and single characters are dropped
    assert tokenize("a the it x") == []


# --- exact ranking -----------------------------------------------------------


def _tf_corpus(root: Path) -> Corpus:
    """Four same-length notes differing only in how often `alpha` occurs.

    Names are chosen so that alphabetical order is the REVERSE of score order:
    a passing ranking cannot be an accident of `sorted()`.
    """
    _lesson(root, "c-note-one", title="Note one", body="alpha alpha alpha f1 f2 f3")
    _lesson(root, "b-note-two", title="Note two", body="alpha alpha g1 g2 g3 g4")
    _lesson(root, "a-note-three", title="Note three", body="alpha h1 h2 h3 h4 h5")
    _lesson(root, "d-note-four", title="Note four", body="i1 i2 i3 i4 i5 i6")
    return Corpus.load(root)


def test_ranking_is_exact_and_beats_alphabetical_order(tmp_path):
    corpus = _tf_corpus(tmp_path)
    assert len(corpus) == 4

    hits = corpus.search("alpha", 3)
    assert [h.note_name for h in hits] == ["c-note-one", "b-note-two", "a-note-three"]
    # strictly decreasing: term frequency drives the order
    assert hits[0].score > hits[1].score > hits[2].score
    # the note that never mentions `alpha` is not a low-ranked hit, it is no hit
    assert "d-note-four" not in {h.note_name for h in corpus.search("alpha", 10)}


def test_ranking_is_reproducible(tmp_path):
    corpus = _tf_corpus(tmp_path)
    first = corpus.search("alpha", 3)
    assert first == corpus.search("alpha", 3) == Corpus.load(tmp_path).search("alpha", 3)


def test_a_failing_lesson_ranks_first_for_a_query_from_its_own_title(tmp_path):
    fail_title = "Remote CI is the only merge gate that counts"
    _lesson(
        tmp_path, "2026-01-02-remote-ci-merge-gate",
        title=fail_title, outcome="fail", kind="implement",
        body="Local CI passed while the remote check rollup was still failing.",
    )
    _lesson(
        tmp_path, "2026-01-01-obsidian-vault-layout",
        title="Obsidian vault layout and wikilinks",
        body="Wikilinks up to a MOC make the graph view useful.",
    )
    _lesson(
        tmp_path, "2026-01-03-worktree-cleanup",
        title="Worktree cleanup after every iteration",
        body="A stranded worktree leaves the branch claimed forever.",
    )
    corpus = Corpus.load(tmp_path, _cfg())

    hits = corpus.search(fail_title, 3)
    assert hits[0].note_name == "2026-01-02-remote-ci-merge-gate"
    assert hits[0].outcome == "fail"
    assert hits[0].snippet.startswith("Local CI passed")


# --- weighting ---------------------------------------------------------------


def _twin_corpus(tmp_path: Path, cfg) -> Corpus:
    """Two notes with identical text, differing only in outcome/kind tags."""
    _lesson(
        tmp_path, "a-pass-improve", title="Widget handling", body="widget widget",
        outcome="pass", kind="improve",
    )
    _lesson(
        tmp_path, "b-fail-heal", title="Widget handling", body="widget widget",
        outcome="fail", kind="heal",
    )
    return Corpus.load(tmp_path, cfg)


def test_failures_outrank_equally_relevant_successes(tmp_path):
    hits = _twin_corpus(tmp_path, _cfg(fail_weight=1.6)).search("widget", 2)
    # the failing note wins despite sorting SECOND alphabetically
    assert [h.note_name for h in hits] == ["b-fail-heal", "a-pass-improve"]
    assert hits[0].score > hits[1].score


def test_neutral_weights_fall_back_to_the_stable_name_tie_break(tmp_path):
    cfg = _cfg(fail_weight=1.0, kind_weight=1.0)
    hits = _twin_corpus(tmp_path, cfg).search("widget", 2)
    assert [h.note_name for h in hits] == ["a-pass-improve", "b-fail-heal"]
    assert hits[0].score == hits[1].score


def test_matching_kind_is_up_weighted(tmp_path):
    cfg = _cfg(fail_weight=1.0, kind_weight=1.25)
    corpus = _twin_corpus(tmp_path, cfg)
    # `b-fail-heal` is kind/heal; without the kind hint the tie breaks on name
    assert [h.note_name for h in corpus.search("widget", 2)] == [
        "a-pass-improve", "b-fail-heal",
    ]
    assert [h.note_name for h in corpus.search("widget", 2, kind="heal")] == [
        "b-fail-heal", "a-pass-improve",
    ]


# --- what gets indexed -------------------------------------------------------


def test_whitepapers_and_adrs_are_indexed_alongside_lessons(tmp_path):
    _lesson(tmp_path, "2026-01-01-a-lesson", title="Zebra lesson", body="zebra")
    _write_note(
        tmp_path, "knowledge/whitepapers", "2026-01-02-a-paper",
        title="Zebra whitepaper", body="zebra", section="Summary",
    )
    _write_note(
        tmp_path, "docs/adr", "0002-zebra", title="ADR-0002: Zebra",
        body="zebra", section="Decision",
    )
    # scaffolding must never be retrieved
    _write_note(tmp_path, "docs/adr", "TEMPLATE", title="ADR-N: Zebra", body="zebra")

    hits = Corpus.load(tmp_path, _cfg()).search("zebra", 10)
    assert {h.source for h in hits} == {"lesson", "whitepaper", "adr"}
    assert "TEMPLATE" not in {h.note_name for h in hits}


def test_snippets_come_from_each_notes_conclusion_section(tmp_path):
    _lesson(tmp_path, "l", title="A lesson", body="Gate on the remote check.")
    _write_note(
        tmp_path, "knowledge/whitepapers", "w", title="A paper",
        body="Five lessons, one theme.", section="Summary",
    )
    _write_note(
        tmp_path, "docs/adr", "0003-x", title="ADR-0003: X",
        body="Split planning from implementation.", section="Decision",
    )
    by_name = {d.note_name: d for d in Corpus.load(tmp_path).documents}
    assert by_name["l"].snippet == "Gate on the remote check."
    assert by_name["w"].snippet == "Five lessons, one theme."
    assert by_name["0003-x"].snippet == "Split planning from implementation."


def test_notes_without_outcome_tags_still_state_an_outcome(tmp_path):
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "0004-plain.md").write_text(
        "# ADR-0004: Plain\n\n## Decision\nAdopt the plain thing.\n"
    )
    hit = Corpus.load(tmp_path).search("plain", 1)[0]
    # never silently unlabelled: `unknown` is said out loud, and the `kind/`
    # half is dropped rather than rendered as a meaningless "kind: unknown"
    assert hit.outcome == "unknown" and hit.label() == "adr, outcome: unknown"
    assert hit.render() == (
        "- [[0004-plain]] (adr, outcome: unknown) - Adopt the plain thing."
    )


# --- degenerate inputs -------------------------------------------------------


def test_empty_corpus_and_empty_query_yield_nothing(tmp_path):
    assert len(Corpus.load(tmp_path)) == 0
    assert Corpus.load(tmp_path).search("anything", 3) == []
    _lesson(tmp_path, "l", title="A lesson", body="content")
    assert Corpus.load(tmp_path).search("", 3) == []
    assert Corpus.load(tmp_path).search("content", 0) == []
    assert Corpus.load(tmp_path).search("nomatchwhatsoever", 3) == []


# --- rendering + the character budget ----------------------------------------


def _oversized_corpus(tmp_path: Path) -> None:
    for i in range(40):
        _lesson(
            tmp_path, f"2026-01-{i:02d}-overflow",
            title=f"Overflowing note {i} about budgets",
            outcome="fail",
            body="budget " * 40,
        )


def test_injected_text_never_exceeds_max_chars(tmp_path):
    _oversized_corpus(tmp_path)
    for budget in (0, 40, 120, 200, 400, 1200):
        cfg = _cfg(k=10, max_chars=budget)
        recalled = for_task(tmp_path, cfg, title="budget", kind="implement")
        assert len(recalled.section) <= budget
        # the audit trail lists exactly the notes that survived the budget
        assert len(recalled.note_names) == recalled.section.count("- [[")
        for name in recalled.note_names:
            assert f"[[{name}]]" in recalled.section


def test_a_budget_too_small_for_the_header_renders_nothing(tmp_path):
    _oversized_corpus(tmp_path)
    recalled = for_task(tmp_path, _cfg(k=3, max_chars=len(HEADING)), title="budget")
    assert recalled.section == "" and recalled.note_names == ()
    assert not recalled


def test_render_drops_whole_notes_rather_than_truncating_one(tmp_path):
    _oversized_corpus(tmp_path)
    hits = Corpus.load(tmp_path, _cfg()).search("budget", 10)
    generous = render(hits, 10_000)
    tight = render(hits, len(generous.section) - 1)
    assert len(tight.notes) < len(generous.notes)
    # every surviving line is a complete rendered note, never a fragment
    assert set(tight.section.splitlines()) <= set(generous.section.splitlines())


def test_for_task_respects_k_and_leads_with_the_heading(tmp_path):
    _oversized_corpus(tmp_path)
    recalled = for_task(tmp_path, _cfg(k=2, max_chars=2000), title="budget note")
    assert recalled.section.startswith(HEADING)
    assert len(recalled.notes) == 2
    assert recalled.note_names == tuple(n.note_name for n in recalled.notes)


def test_disabled_recall_returns_an_empty_result(tmp_path):
    _oversized_corpus(tmp_path)
    recalled = for_task(tmp_path, _cfg(enabled=False), title="budget")
    assert recalled.notes == () and recalled.section == ""


def test_an_empty_corpus_renders_nothing_even_when_enabled(tmp_path):
    recalled = for_task(tmp_path, _cfg(enabled=True), title="budget")
    assert recalled.notes == () and recalled.section == ""


# --- config plumbing ---------------------------------------------------------


def test_recall_config_reads_core_yaml_and_documents_its_defaults():
    shipped = RecallConfig.from_core(load_config())
    assert shipped.enabled is True
    assert shipped.k >= 1 and shipped.max_chars > len(HEADING)
    assert shipped.fail_weight > 1.0  # failures are the expensive knowledge

    # an absent block falls back to the dataclass defaults, not a crash
    bare = RecallConfig.from_core(replace(load_config(), knowledge={}))
    assert bare == RecallConfig()
    assert RecallConfig.from_core(None) == RecallConfig()


def test_the_real_repo_corpus_is_searchable():
    """The shipped vault must actually index - a smoke test against real notes."""
    cfg = load_config()
    root = Path(__file__).resolve().parents[1]
    corpus = Corpus.load(root, cfg)
    assert len(corpus) >= 10
    hits = corpus.search("remote CI gate", 3)
    assert hits and all(h.snippet for h in hits)
    assert all(h.score > 0 for h in hits)
