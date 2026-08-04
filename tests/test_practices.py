"""End-to-end provenance: upstream artifact -> ticket -> PR -> lesson.

Covers the reference-practice registry (`hsai.practices`), the durable
reference notes `build_context_pack` now leaves behind, the `Reference MOC`,
and the single-writer rule that keeps parallel workers off the shared registry.
"""
from __future__ import annotations

import json
from pathlib import Path

from hsai import practices, synthesis
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase
from hsai.orchestrator import run_once
from hsai.practices import ADOPTED, PROPOSED, REJECTED, Practice
from hsai.proc import Proc
from hsai.synthesis import ContextPack, build_context_pack, build_prompt
from test_orchestrator import WELL_FORMED_BODY, FakeRunner


def _cfg():
    return load_config()


def _practice(**kw) -> Practice:
    base = dict(
        source_repo="run-llama/llama_index",
        source_artifact="commit: index the corpus once, retrieve many times",
        description="feat: persist the study digest as a retrievable note",
    )
    base.update(kw)
    return Practice.new(**base)


# --- registry ---------------------------------------------------------------
def test_registry_round_trips_with_stable_ids(tmp_path):
    path = tmp_path / "practices.yaml"
    practice = _practice(ticket=42)

    saved = practices.save(path, [practice])
    assert saved.exists()
    (loaded,) = practices.load(path)
    assert loaded == practice

    # The id is derived from repo + description, not from insertion order.
    assert loaded.id == practices.make_id(practice.source_repo, practice.description)
    assert loaded.id.startswith("run-llama-llama-index--")


def test_upserting_the_same_practice_twice_does_not_duplicate_a_row(tmp_path):
    path = tmp_path / "practices.yaml"
    registry: list[Practice] = []

    registry = practices.upsert(registry, _practice(ticket=42))
    registry = practices.upsert(registry, _practice(ticket=42))
    practices.save(path, registry)

    assert len(practices.load(path)) == 1


def test_a_reproposal_never_walks_a_settled_practice_back_to_proposed():
    settled = _practice(ticket=42, pr=99, lesson="2026-08-04-did-it", status=ADOPTED)
    registry = practices.upsert([settled], _practice(ticket=42, status=PROPOSED))

    assert len(registry) == 1
    assert registry[0].status == ADOPTED
    assert registry[0].pr == 99          # known fields are never erased by an empty one


def test_practice_is_derived_from_the_tickets_synthesis_rationale():
    cfg = _cfg()
    rationale = (
        "Combines three projects. run-llama/llama_index supplies the architectural "
        "move: index an external corpus once into durable artifacts. "
        "SWE-agent/SWE-agent supplies the traceability standard."
    )
    practice = practices.practice_from_ticket(
        title="feat: reference-practice registry",
        rationale=rationale,
        known_repos=[r.repo for r in cfg.reference_top10],
        studied=["openai/swarm"],
        ticket=7,
    )

    # The repo named FIRST in the rationale is the source, and the artifact is
    # the sentence citing it - not just the bare repo name.
    assert practice.source_repo == "run-llama/llama_index"
    assert "index an external corpus once" in practice.source_artifact
    assert practice.status == PROPOSED and practice.ticket == 7


def test_journal_is_folded_into_the_registry_idempotently(tmp_path):
    cfg = _cfg()
    practice = _practice(ticket=42)
    practices.save(practices.registry_path(cfg, tmp_path), [practice])
    practices.record_transition(
        practices.journal_path(cfg, tmp_path),
        practices.Transition(
            practice_id=practice.id, status=ADOPTED, ticket=42, pr=99,
            lesson="2026-08-04-did-it",
        ),
    )

    first = practices.apply_transitions(cfg, tmp_path)
    assert [p.status for p in first] == [ADOPTED]
    after_first = practices.registry_path(cfg, tmp_path).read_text()

    # Replaying the append-only journal converges on the same registry.
    practices.apply_transitions(cfg, tmp_path)
    assert practices.registry_path(cfg, tmp_path).read_text() == after_first


def test_the_registry_shipped_in_this_repo_is_loadable_and_self_consistent():
    """Guards the checked-in provenance: ids must be the ones `make_id` derives,
    every row must name a real reference project, and adopted rows must link the
    lesson that proves them."""
    repo_root = Path(__file__).resolve().parents[1]
    cfg = _cfg()
    registry = practices.load(practices.registry_path(cfg, repo_root))
    assert registry, "the reference-practice registry should not be empty"

    known = {r.repo for r in cfg.reference_top10}
    lessons = KnowledgeBase.from_config(cfg, repo_root).lesson_notes()
    for practice in registry:
        assert practice.id == practices.make_id(practice.source_repo, practice.description)
        assert practice.source_repo in known
        assert practice.source_artifact and practice.status in practices.STATUSES
        if practice.status == ADOPTED:
            assert practice.lesson in lessons


# --- prompt memory -----------------------------------------------------------
def test_build_prompt_lists_adopted_and_rejected_practices():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    registry = [
        _practice(ticket=1, pr=2, lesson="2026-08-04-note", status=ADOPTED),
        _practice(
            source_repo="openai/swarm",
            description="feat: hand off between workers mid-iteration",
            status=REJECTED,
            reason="blocked after 2 attempt(s); last remote CI FAILURE",
        ),
    ]

    prompt = build_prompt(cfg, pack, registry)

    assert "Already adopted" in prompt and "do NOT re-propose" in prompt
    assert "feat: persist the study digest as a retrievable note" in prompt
    assert "Previously rejected" in prompt
    assert "feat: hand off between workers mid-iteration" in prompt
    assert "last remote CI FAILURE" in prompt          # the reason travels with it

    # Both sections appear for ANY non-empty registry, even when one bucket is
    # still empty - the planner should know the ledger exists.
    only_proposed = build_prompt(cfg, pack, [_practice(ticket=1)])
    assert "Already adopted" in only_proposed and "Previously rejected" in only_proposed
    assert "_(none yet)_" in only_proposed

    # An empty registry adds no noise at all.
    assert "Already adopted" not in build_prompt(cfg, pack, [])


# --- reference notes ---------------------------------------------------------
class _GhApiRunner:
    """Recorded `gh api` responses for the three calls a digest is built from."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        target = cmd[2] if len(cmd) > 2 else ""
        if target.endswith("/readme"):
            return Proc(cmd, 0, "# llama_index\nIndex once, retrieve many times.\n", "")
        if "/commits" in target:
            return Proc(cmd, 0, "fix: stream tokens\nfeat: persistent index store\n", "")
        if "workflows" in target:
            return Proc(cmd, 0, "unit_test.yml\nlint.yml\n", "")
        return Proc(cmd, 1, "", "not found")


def test_context_pack_writes_one_obsidian_note_per_studied_repo(tmp_path):
    cfg = _cfg()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    runner = _GhApiRunner()

    build_context_pack(["run-llama/llama_index"], runner=runner, kb=kb)

    note = kb.reference_dir / "run-llama-llama_index.md"
    text = note.read_text()
    assert text.startswith("---\ntags:\n")                 # valid frontmatter
    assert "repo: run-llama/llama_index" in text
    assert "[[Reference MOC]]" in text                     # backlink up to the MOC
    assert "## Practices adopted from this project" in text
    assert "Index once, retrieve many times" in text       # the digest is durable now
    assert "feat: persistent index store" in text
    assert "unit_test.yml" in text


def test_rerunning_a_rotation_updates_the_note_instead_of_duplicating_it(tmp_path):
    cfg = _cfg()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    repos = ["run-llama/llama_index", "openai/swarm"]

    build_context_pack(repos, runner=_GhApiRunner(), kb=kb)
    first = sorted(p.name for p in kb.reference_dir.glob("*.md"))

    registry = [_practice(ticket=42, pr=99, lesson="2026-08-04-note", status=ADOPTED)]
    build_context_pack(repos, runner=_GhApiRunner(), kb=kb, registry=registry)

    assert sorted(p.name for p in kb.reference_dir.glob("*.md")) == first == [
        "openai-swarm.md", "run-llama-llama_index.md",
    ]
    # Same file, refreshed content: the adopted practice now shows on its source.
    llama = (kb.reference_dir / "run-llama-llama_index.md").read_text()
    assert "**adopted**" in llama and "[[2026-08-04-note]]" in llama
    assert "No practice has been taken" in (kb.reference_dir / "openai-swarm.md").read_text()


# --- Reference MOC -----------------------------------------------------------
def test_reference_moc_links_every_note_and_the_lessons_of_adopted_practices(tmp_path):
    cfg = _cfg()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    build_context_pack(
        ["run-llama/llama_index", "openai/swarm"], runner=_GhApiRunner(), kb=kb
    )
    practices.save(
        practices.registry_path(cfg, tmp_path),
        [_practice(ticket=42, pr=99, lesson="2026-08-04-note", status=ADOPTED)],
    )

    written = kb.reindex_mocs()
    assert "Reference MOC.md" in {p.name for p in written}

    moc = (kb.mocs_dir / "Reference MOC.md").read_text()
    for note in kb.reference_notes():
        assert f"[[{note}]]" in moc                 # every reference note is linked
    assert "[[2026-08-04-note]]" in moc             # onward to the adopted lesson
    assert "[[Knowledge Base MOC]]" in moc

    # The root MOC opens the door to it, and a rebuild is idempotent.
    assert "[[Reference MOC]]" in (kb.mocs_dir / "Knowledge Base MOC.md").read_text()
    kb.reindex_mocs()
    assert (kb.mocs_dir / "Reference MOC.md").read_text() == moc


# --- synthesis files proposed practices --------------------------------------
SYNTH_RATIONALE = (
    "run-llama/llama_index supplies the architectural move: index once. "
    "SWE-agent/SWE-agent supplies traceability. openai/swarm keeps it small."
)
SYNTH_OUTPUT = "PHASE 3:\n```json\n" + json.dumps(
    [
        {
            "title": "feat: persist the study digest as a retrievable note",
            "problem": "digests are thrown away",
            "proposal": "write them to knowledge/reference",
            "acceptance_criteria": ["note written", "note updated in place"],
            "verification_plan": ["pytest"],
            "size": "L",
            "goal_ids": ["G1"],
            "synthesis_rationale": SYNTH_RATIONALE,
        }
    ]
) + "\n```"


class _SynthesisRunner(_GhApiRunner):
    """`gh api` fixtures plus the `gh issue create` the filing step makes."""

    def __init__(self) -> None:
        super().__init__()
        self._issue = 300

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "create"]:
            self.calls.append(cmd)
            self._issue += 1
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self._issue}\n", "")
        return super().__call__(cmd, cwd=cwd, env=env, timeout=timeout, input_text=input_text)


def _ai_runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
    return Proc(list(cmd), 0, SYNTH_OUTPUT, "")


def test_synthesize_writes_reference_notes_and_proposed_practices(tmp_path):
    cfg = _cfg()
    reg_path = practices.registry_path(cfg, tmp_path)

    # Rotation 1 studies run-llama/llama_index, the project the fixture
    # rationale names first - so the proposal lands on a note we just wrote.
    res = synthesis.synthesize(
        cfg, cycle_index=1, runner=_SynthesisRunner(), ai_runner=_ai_runner,
        repo_root=tmp_path,
    )

    assert "run-llama/llama_index" in res.studied
    assert res.filed and res.proposed
    notes = sorted(p.name for p in (tmp_path / "knowledge" / "reference").glob("*.md"))
    assert len(notes) == len(res.studied) == 3

    registry = practices.load(reg_path)
    assert len(registry) == 1
    practice = registry[0]
    assert practice.status == PROPOSED
    assert practice.ticket == res.filed[0]
    assert practice.source_repo == "run-llama/llama_index"   # named first in the rationale
    assert "index once" in practice.source_artifact

    # The proposal is visible on the note of the project that inspired it.
    llama = (tmp_path / "knowledge" / "reference" / "run-llama-llama_index.md").read_text()
    assert "**proposed**" in llama

    # A second pass over the same fixtures adds no duplicate row and no
    # duplicate note - the practice id is stable across rotations.
    synthesis.synthesize(
        cfg, cycle_index=1, runner=_SynthesisRunner(), ai_runner=_ai_runner,
        repo_root=tmp_path,
    )
    assert len(practices.load(reg_path)) == 1
    assert sorted(p.name for p in (tmp_path / "knowledge" / "reference").glob("*.md")) == notes


# --- adoption / rejection through the orchestrator ---------------------------
WIDGET_ISSUE = {
    "number": 7,
    "title": "feat: widget",
    "labels": [{"name": "priority:P2"}],
    "assignees": [],
    "body": WELL_FORMED_BODY,
}


def _seed_registry(cfg, root: Path, **kw) -> Practice:
    practice = _practice(ticket=7, **kw)
    practices.save(practices.registry_path(cfg, root), [practice])
    return practice


def test_merged_pr_promotes_the_practice_to_adopted(tmp_path):
    cfg = _cfg()
    practice = _seed_registry(cfg, tmp_path)
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True],
        open_issues=[dict(WIDGET_ISSUE)], remote_ci="SUCCESS",
        worktree_status="?? src/hsai/widget.py\n",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )
    assert result.merged is True
    assert any(n == f"practice adopted={practice.id}" for n in result.notes)

    # The worker journalled it; the serialized step settles the registry.
    (settled,) = practices.apply_transitions(cfg, tmp_path)
    assert settled.status == ADOPTED
    assert settled.ticket == 7 and settled.pr == result.pr
    assert settled.lesson and Path(result.lesson_path).stem == settled.lesson

    # The PR body cites the upstream artifact, not just a list of repo names.
    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    body = pr_create[pr_create.index("--body") + 1]
    assert f"Adopted practice `{practice.id}`" in body
    assert practice.source_artifact in body


def test_attempts_exhausted_marks_the_practice_rejected(tmp_path):
    cfg = _cfg()
    practice = _seed_registry(cfg, tmp_path)
    # attempts:1 + this failure == max_ticket_attempts (2) -> blocked, not retried.
    issue = dict(WIDGET_ISSUE, labels=[{"name": "priority:P2"}, {"name": "attempts:1"}])
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True],
        open_issues=[issue], remote_ci="FAILURE",
        worktree_status="?? src/hsai/widget.py\n",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )
    assert result.merged is False and result.recovered is True
    assert any(c[:3] == ["gh", "issue", "edit"] and "blocked" in c for c in runner.calls)

    (settled,) = practices.apply_transitions(cfg, tmp_path)
    assert settled.id == practice.id
    assert settled.status == REJECTED
    assert "blocked after 2 attempt(s)" in settled.reason
    assert "FAILURE" in settled.reason


def test_run_once_only_reads_the_registry_never_writes_it(tmp_path):
    """The registry is a shared derived file: a parallel worker must not touch
    it, or concurrent PRs would conflict on it (the MOC rule, applied here)."""
    cfg = _cfg()
    _seed_registry(cfg, tmp_path)
    reg_path = practices.registry_path(cfg, tmp_path)
    before = reg_path.read_bytes()

    for iteration in (1, 2):
        runner = FakeRunner(
            repo_root=str(tmp_path), ci_sequence=[True, True],
            open_issues=[dict(WIDGET_ISSUE)], remote_ci="SUCCESS",
            worktree_status="?? src/hsai/widget.py\n",
        )
        assert run_once(
            cfg, repo_dir=str(tmp_path), dry_run=False,
            runner=runner, ai_runner=runner, iteration=iteration,
        ).merged is True

    # Two "concurrent" workers, zero writes to the shared registry...
    assert reg_path.read_bytes() == before
    # ...and their transitions are safely append-only, one line each.
    journal = practices.journal_path(cfg, tmp_path)
    assert len(practices.read_transitions(journal)) == 2
    assert len(journal.read_text().splitlines()) == 2
