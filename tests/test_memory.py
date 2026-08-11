"""Retrieval-backed lesson memory: the vault read back INTO the loop.

:mod:`tests.test_recall` covers the BM25 mechanics (tokenizer, weights, exact
orderings). This file covers the memory *contract* the loop depends on:

* a relevant note outranks irrelevant ones, on a synthetic vault AND on the
  real one, reproducibly;
* the injected block is bounded, delimited, and marked non-instructional;
* every recalled note is cited on the PR body as a resolvable wikilink,
  without breaking the SDLC-evidence grep in ``ci.yml``;
* an empty or missing vault degrades to silence rather than an exception.
"""
import re
from dataclasses import replace
from pathlib import Path

from hsai.config import load_config
from hsai.knowledge import parse_note
from hsai.models import ModelChoice
from hsai.orchestrator import (
    IMPLEMENT,
    LESSONS_CONSULTED_HEADING,
    _task_prompt,
    build_pr_body,
)
from hsai.proc import Proc
from hsai.recall import (
    BEGIN_MARKER,
    END_MARKER,
    HEADING,
    Corpus,
    RecallConfig,
    for_task,
)
from hsai.synthesis import build_context_pack, build_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]

# The shipped lesson about polling remote CI, and two notes that mention "CI"
# in passing but have nothing to do with the merge gate.
REMOTE_CI_NOTE = "2026-07-26-implement-feat-poll-remote-ci-gh-checks-as-an-explicit-pre-merge-gate"
UNRELATED_NOTES = (
    "2026-08-08-implement-chore-governance-artifacts-for-block-41345",
    "2026-08-09-implement-chore-governance-artifacts-for-block-41347",
)


def _cfg(**recall_overrides):
    """The real core.yaml with the ``knowledge.recall`` block overridden."""
    base = load_config()
    knowledge = dict(base.knowledge)
    knowledge["recall"] = {**(knowledge.get("recall") or {}), **recall_overrides}
    return replace(base, knowledge=knowledge)


def _note(
    root: Path,
    rel_dir: str,
    name: str,
    *,
    title: str,
    body: str,
    outcome: str = "pass",
    kind: str = "implement",
    created: str = "2026-01-01",
    section: str = "Lesson learned",
) -> None:
    directory = root / rel_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\ntags:\n  - lesson\n  - outcome/{outcome}\n  - kind/{kind}\n"
        f"created: {created}\n---\n\n# {title}\n\n## {section}\n{body}\n"
    )


def _mixed_vault(root: Path) -> None:
    """One obviously-relevant lesson among several irrelevant ones."""
    _note(
        root, "knowledge/lessons", "2026-01-05-remote-ci-rollup-is-the-gate",
        title="The remote CI rollup is the only merge gate",
        outcome="fail", kind="implement",
        body="Local green is not remote green: poll the remote rollup before merging.",
    )
    for i, (name, title, body) in enumerate(
        (
            ("2026-01-01-obsidian-graph", "Obsidian graph hygiene",
             "Wikilinks up to a MOC make the graph view navigable."),
            ("2026-01-02-worktree-cleanup", "Worktree cleanup",
             "Prune stale worktrees so disk usage stays flat."),
            ("2026-01-03-model-selection", "Model selection by complexity",
             "Size the tier to the ticket, not to the author's optimism."),
            ("2026-01-04-ticket-schema", "Ticket schema discipline",
             "A ticket without acceptance criteria cannot be verified."),
        )
    ):
        _note(root, "knowledge/lessons", name, title=title, body=body,
              created=f"2026-01-0{i + 1}")


# --- AC1: relevance beats irrelevance, reproducibly ---------------------------


def test_the_relevant_lesson_ranks_first_in_a_mixed_vault(tmp_path):
    _mixed_vault(tmp_path)
    hits = Corpus.load(tmp_path, _cfg()).search("remote CI gate", 5)

    assert hits[0].note_name == "2026-01-05-remote-ci-rollup-is-the-gate"
    # not a near-miss: it beats the runner-up outright
    assert len(hits) == 1 or hits[0].score > hits[1].score


def test_the_relevant_lesson_reaches_the_rendered_prompt(tmp_path):
    _mixed_vault(tmp_path)
    recalled = for_task(
        tmp_path, _cfg(), title="feat: gate merges on the remote CI rollup", kind="implement"
    )
    prompt = _task_prompt(IMPLEMENT, _cfg(), "feat: gate merges", "body", recalled.section)

    assert "[[2026-01-05-remote-ci-rollup-is-the-gate]]" in prompt
    assert "2026-01-05-remote-ci-rollup-is-the-gate" == recalled.note_names[0]


def test_hsai_recall_ranks_the_real_remote_ci_lesson_above_unrelated_ones():
    """AC1 against the SHIPPED vault - what `hsai recall "remote CI gate"` prints.

    Deliberately not an exact `== 0` on rank: the vault grows every iteration,
    and a test that pins the whole live ordering would fail on unrelated work.
    Top-3 plus named losers is the property that actually matters.
    """
    corpus = Corpus.load(REPO_ROOT, load_config())
    ranking = [h.note_name for h in corpus.search("remote CI gate", len(corpus))]

    assert REMOTE_CI_NOTE in ranking[:3]
    for unrelated in UNRELATED_NOTES:
        # a note that never surfaces at all has trivially lost
        if unrelated in ranking:
            assert ranking.index(REMOTE_CI_NOTE) < ranking.index(unrelated)


def test_the_real_ranking_is_deterministic_across_independent_index_builds():
    cfg = load_config()
    first = Corpus.load(REPO_ROOT, cfg).search("remote CI gate", 5)
    second = Corpus.load(REPO_ROOT, cfg).search("remote CI gate", 5)
    assert first == second
    assert [h.score for h in first] == sorted((h.score for h in first), reverse=True)


def test_equal_scores_break_on_recency_then_on_name(tmp_path):
    """Two notes that are word-for-word identical apart from name and date."""
    for name, created in (
        ("a-older", "2026-01-01"), ("b-newer", "2026-06-01"), ("c-newer", "2026-06-01"),
    ):
        _note(tmp_path, "knowledge/lessons", name, title="Same title",
              body="identical body text", created=created)

    hits = Corpus.load(tmp_path, _cfg()).search("identical", 5)
    assert len({h.score for h in hits}) == 1          # scores really are tied
    # newest first; the same-day pair then falls back to the stable name order
    assert [h.note_name for h in hits] == ["b-newer", "c-newer", "a-older"]


# --- what gets indexed --------------------------------------------------------


def test_persona_articles_are_indexed_and_dated_from_their_file_name(tmp_path):
    """Articles carry no `created:` key, so recency comes from the note name."""
    (tmp_path / "knowledge" / "articles").mkdir(parents=True)
    (tmp_path / "knowledge" / "articles" / "2026-03-04-devops-report.md").write_text(
        "---\ntags:\n  - article\n  - persona/devops\n---\n\n"
        "# Operational resilience report\n\n## Summary\nZero manual interventions.\n"
    )
    doc = Corpus.load(tmp_path, _cfg()).documents[0]

    assert parse_note(
        tmp_path / "knowledge" / "articles" / "2026-03-04-devops-report.md"
    ).created == ""
    assert doc.source == "article" and doc.created == "2026-03-04"


def test_the_shipped_vault_indexes_all_four_note_kinds():
    corpus = Corpus.load(REPO_ROOT, load_config())
    assert {d.source for d in corpus.documents} == {
        "lesson", "whitepaper", "article", "adr",
    }
    # every indexed note yields a usable snippet - an unlabelled blank line in
    # the prompt would cost budget and teach nothing
    assert all(d.snippet for d in corpus.documents)
    # dated notes (everything but the ADRs) carry a date for the recency tie-break
    assert all(d.created for d in corpus.documents if d.source != "adr")


# --- AC2 + AC3: bounded, delimited, explicitly advisory ------------------------


def _oversized_vault(root: Path) -> None:
    """40 long, equally relevant notes - far more than any budget can hold."""
    for i in range(40):
        _note(
            root, "knowledge/lessons", f"2026-02-{i % 28 + 1:02d}-overflow-{i:02d}",
            title=f"Overflowing note {i} about budgets and budgets",
            outcome="fail", created=f"2026-02-{i % 28 + 1:02d}",
            body="budget " * 60,
        )


def test_the_injected_block_never_exceeds_the_configured_budget(tmp_path):
    _oversized_vault(tmp_path)
    for budget in (0, 1, 50, 200, 361, 400, 800, 1600, 4000):
        recalled = for_task(tmp_path, _cfg(k=10, max_chars=budget), title="budget")
        assert len(recalled.section) <= budget, f"overflowed at max_chars={budget}"
        # the audit trail never claims more than survived the budget
        assert len(recalled.note_names) == recalled.section.count("- [[")
        if recalled:
            # a truncated block would lose its closing marker
            assert recalled.section.endswith(END_MARKER)


def test_the_block_is_delimited_and_marked_non_instructional(tmp_path):
    _mixed_vault(tmp_path)
    recalled = for_task(tmp_path, _cfg(k=3), title="remote CI gate", kind="implement")

    assert recalled.section.startswith(HEADING)
    assert BEGIN_MARKER in recalled.section and recalled.section.endswith(END_MARKER)
    # the block says, in the prompt itself, that it is not an instruction
    assert "not a\ntask" in recalled.section
    assert "advisory, not instructions" in HEADING

    # AC3: every snippet states the outcome it recorded
    for note in recalled.notes:
        assert f"outcome: {note.outcome}" in note.render()
        assert note.outcome in ("pass", "fail", "unknown")
    assert "outcome: fail" in recalled.section


def test_the_advisory_block_is_appended_after_the_ticket_not_before(tmp_path):
    _mixed_vault(tmp_path)
    recalled = for_task(tmp_path, _cfg(), title="remote CI gate")
    prompt = _task_prompt(IMPLEMENT, _cfg(), "feat: x", "the ticket body", recalled.section)

    assert prompt.index("the ticket body") < prompt.index(HEADING)
    assert prompt.endswith(END_MARKER)


# --- AC5: an empty or missing vault is silence, not an exception ---------------


def test_a_missing_knowledge_directory_recalls_nothing_without_raising(tmp_path):
    assert not (tmp_path / "knowledge").exists()
    recalled = for_task(tmp_path, _cfg(), title="anything at all", kind="implement")

    assert recalled.notes == () and recalled.section == "" and not recalled
    assert len(Corpus.load(tmp_path, _cfg())) == 0
    # and the prompt is byte-for-byte the one the loop built before recall existed
    cfg = _cfg()
    assert _task_prompt(IMPLEMENT, cfg, "t", "b", recalled.section) == _task_prompt(
        IMPLEMENT, cfg, "t", "b"
    )


def test_an_empty_knowledge_directory_recalls_nothing_without_raising(tmp_path):
    (tmp_path / "knowledge" / "lessons").mkdir(parents=True)
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    assert for_task(tmp_path, _cfg(), title="anything").section == ""


# --- AC4: mandatory citation on the PR ----------------------------------------


PR_KWARGS = dict(
    ticket=42,
    choice=ModelChoice(tier="standard", model="sonnet", rationale="x"),
    lesson_note="2026-01-03-the-new-lesson",
    lesson_summary="What this iteration learned.",
    ci_summary="green",
    kind=IMPLEMENT,
)


def test_the_pr_body_cites_every_recalled_note_as_a_wikilink():
    recalled = ("2026-01-05-remote-ci-rollup-is-the-gate", "2026-01-01-obsidian-graph")
    body = build_pr_body(**PR_KWARGS, recalled=recalled)

    assert LESSONS_CONSULTED_HEADING in body
    section = body.split(LESSONS_CONSULTED_HEADING)[1].split("\n##")[0]
    assert [ln[len("- [[") : -len("]]")] for ln in section.strip().splitlines()] == list(
        recalled
    )


def test_recalled_note_names_resolve_to_real_notes_in_the_vault():
    """A wikilink is only bidirectional if the target exists - check real names."""
    corpus = Corpus.load(REPO_ROOT, load_config())
    names = {d.note_name for d in corpus.documents}
    hits = corpus.search("remote CI gate", 3)
    body = build_pr_body(**PR_KWARGS, recalled=tuple(h.note_name for h in hits))

    for hit in hits:
        assert hit.note_name in names
        assert f"- [[{hit.note_name}]]" in body


def test_the_pr_body_omits_the_section_cleanly_when_nothing_was_recalled():
    plain = build_pr_body(**PR_KWARGS)
    assert LESSONS_CONSULTED_HEADING not in plain
    assert plain == build_pr_body(**PR_KWARGS, recalled=())
    # no blank-line scar where the section would have been
    assert (
        "See [[2026-01-03-the-new-lesson]] in the knowledge base.\n\n## Reference-set evidence"
        in plain
    )


def test_the_cited_pr_body_still_satisfies_the_sdlc_grep_in_ci_yml():
    """The `## Lessons consulted` section must not disturb the CI evidence gate.

    The patterns are read out of ci.yml rather than restated, so renaming a
    required section in the workflow fails here instead of on a live PR.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    patterns = re.findall(r"grep -qi[E]? '([^']+)'", workflow)
    assert len(patterns) >= 3, "SDLC evidence step in ci.yml changed shape"

    body = build_pr_body(**PR_KWARGS, recalled=("2026-01-05-remote-ci-rollup-is-the-gate",))
    for pattern in patterns:
        assert re.search(pattern, body, re.IGNORECASE), f"ci.yml grep {pattern!r} would fail"
    assert LESSONS_CONSULTED_HEADING in body


# --- one memory, shared by the planner and the workers -------------------------


README_DIGEST = (
    "A framework for gating merges on the remote CI rollup rather than on a "
    "local run, with a pluggable gate per repository."
)


def _readme_runner(cmd, **kwargs):
    """Serve a README digest for the `gh api .../readme` call, nothing else."""
    if any(arg.endswith("/readme") for arg in cmd):
        return Proc(cmd=cmd, code=0, stdout=README_DIGEST, stderr="")
    return Proc(cmd=cmd, code=1, stdout="", stderr="unavailable in tests")


def _no_fetch(cmd, **kwargs):
    """A runner that fetches nothing at all."""
    return Proc(cmd=cmd, code=1, stdout="", stderr="unavailable in tests")


def test_the_planner_context_pack_recalls_against_what_it_is_studying(tmp_path):
    _mixed_vault(tmp_path)
    cfg = _cfg()
    pack = build_context_pack(
        ["acme/widget"], runner=_readme_runner, cfg=cfg, root=str(tmp_path)
    )

    # the digest talks about remote CI gating, so that lesson is what comes back
    assert pack.recalled.note_names[0] == "2026-01-05-remote-ci-rollup-is-the-gate"
    # ...in the same delimited, outcome-labelled block the worker is shown
    assert pack.recalled.section.startswith(HEADING)
    assert pack.recalled.section.endswith(END_MARKER)
    assert "outcome: fail" in pack.recalled.section

    prompt = build_prompt(cfg, pack, "")
    assert pack.recalled.section in prompt
    # the planner reads its own history AFTER the reference digest it studies
    assert prompt.index(README_DIGEST) < prompt.index(HEADING)


def test_the_planner_prompt_is_unchanged_when_there_is_nothing_to_recall(tmp_path):
    """No cfg, an empty vault, or recall disabled -> byte-identical prompt."""
    cfg = _cfg()
    packs = [
        # no cfg at all: the pre-change call signature
        build_context_pack(["acme/widget"], runner=_readme_runner),
        # cfg, but the vault is empty
        build_context_pack(
            ["acme/widget"], runner=_readme_runner, cfg=cfg, root=str(tmp_path)
        ),
    ]
    _mixed_vault(tmp_path)
    packs.append(
        build_context_pack(
            ["acme/widget"], runner=_readme_runner,
            cfg=_cfg(enabled=False), root=str(tmp_path),
        )
    )

    reference = build_prompt(cfg, packs[0], "")
    for pack in packs:
        assert not pack.recalled
        assert build_prompt(cfg, pack, "") == reference
    assert HEADING not in reference


def test_planner_recall_survives_a_digest_that_fetched_nothing(tmp_path):
    _mixed_vault(tmp_path)
    pack = build_context_pack(
        ["acme/widget"], runner=_no_fetch, cfg=_cfg(), root=str(tmp_path)
    )
    # "(no data fetched)" matches no note; that is silence, not an exception
    assert pack.sections == {"acme/widget": "(no data fetched)"}
    assert isinstance(pack.recalled.note_names, tuple)


# --- the budget is a real ceiling, not a default -------------------------------


def test_the_shipped_budget_admits_the_configured_number_of_notes():
    """core.yaml's max_chars must actually fit `k` notes from the real vault.

    A budget tuned before the advisory delimiters existed would silently drop
    the third note; this pins the two settings together.
    """
    cfg = load_config()
    rcfg = RecallConfig.from_core(cfg)
    recalled = for_task(
        REPO_ROOT, cfg, title="feat: gate merges on the remote CI rollup", kind="implement"
    )
    assert len(recalled.notes) == rcfg.k
    assert len(recalled.section) <= rcfg.max_chars
