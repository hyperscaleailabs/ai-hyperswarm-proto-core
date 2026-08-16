"""Retrieval over the repo's own knowledge base.

Every ranking here is asserted EXACTLY: recall is deterministic by design (no
model call, stable tie-break on note name), so a fuzzy assertion would hide the
very regressions this module can suffer.
"""
import json
from dataclasses import replace
from pathlib import Path

from hsai.config import load_config
from hsai.proc import Proc
from hsai.recall import (
    DEFAULT_PRIOR_ART_CHARS,
    HEADING,
    PRIOR_ART_HEADING,
    Corpus,
    PriorArtItem,
    RecallConfig,
    build_prior_art,
    cost_pressure,
    for_task,
    issue_documents,
    ledger_documents,
    render,
    render_prior_art,
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


def test_notes_without_outcome_tags_are_labelled_by_source(tmp_path):
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "0004-plain.md").write_text(
        "# ADR-0004: Plain\n\n## Decision\nAdopt the plain thing.\n"
    )
    hit = Corpus.load(tmp_path).search("plain", 1)[0]
    assert hit.outcome == "unknown" and hit.label() == "adr"
    assert hit.render() == "- [[0004-plain]] (adr) - Adopt the plain thing."


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


# --- prior art: the planner's four-source view of our own record --------------

CLOSED = [
    {
        "number": 142, "title": "feat: quota telemetry ledger",
        "labels": [{"name": "self-improve"}], "assignees": [], "body": "",
        "closedAt": "2026-08-01T00:00:00Z",
    },
]
BLOCKED = [
    {
        "number": 77, "title": "feat: widget scheduler",
        "labels": [{"name": "blocked"}], "assignees": [],
        "body": "## Problem\nThe widget scheduler starves long tasks.\n",
    },
    {
        "number": 78, "title": "feat: unrelated open work",
        "labels": [], "assignees": [], "body": "## Problem\nSomething else.\n",
    },
]


def _gh(*, closed=None, blocked=None, broken=False):
    """A fake `gh` answering both issue-list states (or failing outright)."""

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        if broken:
            return Proc(cmd, 127, "", "gh: command not found")
        if cmd[:3] == ["gh", "issue", "list"]:
            state = cmd[cmd.index("--state") + 1] if "--state" in cmd else "open"
            data = (CLOSED if closed is None else closed) if state == "closed" else (
                BLOCKED if blocked is None else blocked
            )
            return Proc(cmd, 0, json.dumps(data), "")
        return Proc(cmd, 0, "", "")

    return runner


def _ledger(root: Path, cfg, *, blocks=((41339, 1425.0, "heavy"),)) -> None:
    from hsai import ledger as ledger_mod

    for i, (block, seconds, tier) in enumerate(blocks):
        ledger_mod.append_record(
            ledger_mod.ledger_path(cfg, root),
            ledger_mod.LedgerRecord(
                iteration=i, block=block, ticket=None, kind="implement", tier=tier,
                model="opus", wall_clock_seconds=seconds, attempts=1, outcome="merged",
            ),
        )


def test_prior_art_spans_lessons_whitepapers_ledger_and_closed_issues(tmp_path):
    cfg = _cfg()
    _lesson(tmp_path, "2026-01-02-widget-fail", title="Widget scheduling starves tasks",
            outcome="fail", body="The widget scheduler starved long tasks.")
    _write_note(tmp_path, "knowledge/whitepapers", "2026-01-03-widget-paper",
                title="Widget synthesis", body="Widget scheduling recurs.", section="Summary")
    _ledger(tmp_path, cfg)

    art = build_prior_art("widget scheduling quota ledger", 4000,
                          root=tmp_path, cfg=cfg, k=10, runner=_gh())

    by_source = {i.source for i in art.items}
    assert {"lesson", "whitepaper", "ledger", "issue"} <= by_source
    refs = art.refs
    assert "[[2026-01-02-widget-fail]]" in refs      # a note is cited as a wikilink
    assert "#77" in refs                              # a ticket as its number
    assert "`ledger:block-41339`" in refs             # a ledger block by name
    # the blocked ticket is prior art; an ordinary open one is not
    assert "#78" not in refs
    # each item carries the label that says what kind of evidence it is
    labels = {i.ref: i.detail for i in art.items}
    assert labels["[[2026-01-02-widget-fail]]"] == "outcome/fail"
    assert labels["#77"] == "blocked" and labels["#142"] == "closed"
    assert labels["`ledger:block-41339`"] == "ledger"


def test_prior_art_never_exceeds_its_budget(tmp_path):
    cfg = _cfg()
    for i in range(30):
        _lesson(tmp_path, f"2026-02-{i:02d}-overflow", outcome="fail",
                title=f"Overflowing note {i} about budgets",
                body="budget quota spend " * 20)
    _ledger(tmp_path, cfg)

    for budget in (0, 60, 200, 500, 1200, 2500):
        art = build_prior_art("budget quota", budget, root=tmp_path, cfg=cfg, k=20,
                              runner=_gh())
        assert len(art.section) <= budget
        # the audit trail lists exactly what survived the cap, never more
        assert len(art.items) == art.section.count("\n- ")
        for ref in art.refs:
            assert ref in art.section


def test_prior_art_degrades_when_gh_is_unavailable(tmp_path):
    """A missing `gh` removes one source; it must never fail synthesis."""
    cfg = _cfg()
    _lesson(tmp_path, "2026-01-02-widget-fail", title="Widget scheduling starves tasks",
            outcome="fail", body="The widget scheduler starved long tasks.")

    assert issue_documents(cfg, runner=_gh(broken=True)) == []

    art = build_prior_art("widget scheduling", 4000, root=tmp_path, cfg=cfg,
                          runner=_gh(broken=True))
    assert art.refs == ("[[2026-01-02-widget-fail]]",)   # the vault still answers
    assert not any(i.source == "issue" for i in art.items)


def test_prior_art_is_empty_when_every_source_is_unavailable(tmp_path):
    """No vault, no ledger, no `gh` - an empty section, not an exception."""
    art = build_prior_art("anything", 4000, root=tmp_path, cfg=_cfg(),
                          runner=_gh(broken=True))
    assert art.section == "" and art.items == () and not art


def test_prior_art_carries_current_cost_pressure(tmp_path):
    cfg = _cfg()
    _lesson(tmp_path, "l", title="A lesson", body="quota spend matters")
    _ledger(tmp_path, cfg, blocks=((41339, 1000.0, "heavy"), (41341, 2000.0, "heavy")))

    line = cost_pressure(tmp_path, cfg)
    assert line.startswith("Cost pressure - latest ledger block 41341")  # newest block only
    assert "2000s wall-clock" in line
    assert "Budget verdict:" in line
    assert "max_heavy_iterations_per_block=" in line

    art = build_prior_art("quota spend", 4000, root=tmp_path, cfg=cfg, runner=_gh())
    assert art.cost_pressure == line
    assert line in art.section


def test_cost_pressure_is_silent_without_a_ledger(tmp_path):
    assert cost_pressure(tmp_path, _cfg()) == ""
    assert cost_pressure(tmp_path, None) == ""


def test_ledger_documents_are_one_per_block(tmp_path):
    cfg = _cfg()
    _ledger(tmp_path, cfg, blocks=((41339, 10.0, "heavy"), (41339, 20.0, "light"),
                                   (41341, 30.0, "heavy")))
    docs = ledger_documents(tmp_path, cfg)
    assert [d.note_name for d in docs] == ["ledger:block-41339", "ledger:block-41341"]
    assert "2 iterations" in docs[0].snippet      # both records folded into one block
    assert ledger_documents(tmp_path, None) == []


def test_prior_art_renders_the_preamble_before_anything_it_cannot_fit():
    """Below the preamble's own length there is nothing honest to render."""
    item = PriorArtItem(ref="[[n]]", source="lesson", score=1.0, excerpt="x")
    assert render_prior_art([item], 10).section == ""
    generous = render_prior_art([item], DEFAULT_PRIOR_ART_CHARS, cost="Cost pressure - none.")
    assert generous.section.startswith(PRIOR_ART_HEADING)
    assert generous.cost_pressure == "Cost pressure - none."
    assert generous.items == (item,)

    # under pressure the items give way, not the framing - and never mid-line
    tight = render_prior_art([item], len(generous.section) - 5, cost="Cost pressure - none.")
    assert tight.cost_pressure == "Cost pressure - none."
    assert tight.items == ()
    assert item.render() not in tight.section
