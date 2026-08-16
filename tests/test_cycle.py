"""Budget-gate and durability behavior of a simulated half-day block.

Drives ``run_cycle`` with a fake ``run_once`` that appends escalating cost to
the quota ledger, and asserts the warn-then-halt transitions: cheaper-tier
biasing on a soft breach, then a new-work halt on a hard breach that still lets
the already-started (in-flight) iteration finish and merge. The heavy tail of a
cycle (synthesis, whitepaper, governance PR) is stubbed so the test isolates the
gate.

The second half drives the same block through a crash and a resume, with a fake
GitHub runner that records every write, so "a resumed block files nothing twice"
is an assertion rather than a hope.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hsai import cycle, github, journal, ledger, trajectory
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson
from hsai.orchestrator import IterationResult
from hsai.proc import Proc
from hsai.synthesis import SynthesisResult


class _Runner:
    """Answers the git/gh calls run_cycle makes around the implementation loop."""

    def __init__(self) -> None:
        self.review_bodies: list[str] = []
        self._issue = 900

    def __call__(
        self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
    ) -> Proc:
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, "[]", "")
        if cmd[:3] == ["gh", "issue", "create"]:
            self.review_bodies.append(cmd[cmd.index("--body") + 1])
            self._issue += 1
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self._issue}\n", "")
        return Proc(cmd, 0, "", "")


def _make_fake_run_once(ledger_file, *, tier: str, seconds: float):
    """A run_once stand-in: records one ledger entry per call and 'merges' a PR.

    A demoted call records a cheaper tier, mirroring the real select() bias, so
    the block aggregate reflects the gate's effect.
    """
    state = {"demotes": [], "started_prs": [], "merged_prs": []}

    def fake(cfg, *, repo_dir, runner, ai_runner, iteration, block=None,
             demote_tier=False, dry_run=False):
        state["demotes"].append(demote_tier)
        pr = 500 + len(state["demotes"])
        state["started_prs"].append(pr)
        recorded_tier = "standard" if demote_tier else tier
        ledger.append_record(
            ledger_file,
            ledger.LedgerRecord(
                iteration=iteration, block=iteration // 100, ticket=iteration,
                kind="implement", tier=recorded_tier, model=recorded_tier,
                wall_clock_seconds=seconds, attempts=1, outcome="merged",
            ),
        )
        # An iteration that was green-lit runs to completion and merges - the
        # gate only stops NEW starts, it never aborts this in-flight PR.
        state["merged_prs"].append(pr)
        return IterationResult(
            kind="implement", ticket=iteration, pr=pr, model=recorded_tier, merged=True
        )

    return fake, state


def test_block_soft_biases_then_hard_halts_but_inflight_merges(tmp_path, monkeypatch):
    cfg = load_config()
    # Budget calibrated so the seconds ceiling drives the transitions:
    # soft at 0.8*100 = 80s, hard at 100s; each iteration books 40s.
    cfg.budget.clear()
    cfg.budget.update({"max_seconds_per_block": 100, "soft_ratio": 0.8})
    cfg.cycle["block_size"] = 10  # would run 10 iterations if never halted

    runner = _Runner()
    ledger_file = ledger.ledger_path(cfg, tmp_path)
    fake, state = _make_fake_run_once(ledger_file, tier="heavy", seconds=40.0)

    # Isolate the gate: skip synthesis, governance PR; keep the real review
    # brief so we can assert it renders the cost summary.
    monkeypatch.setattr(cycle, "run_once", fake)
    monkeypatch.setattr(cycle, "_well_formed_backlog", lambda cfg, *, runner: 999)
    monkeypatch.setattr(cycle, "_governance_pr", lambda *a, **k: 0)

    res = cycle.run_cycle(cfg, repo_dir=str(tmp_path), cycle_index=1, runner=runner)

    # Cumulative seconds per pre-iteration check: 0, 40, 80(soft), 120(hard).
    # -> two normal starts, one demoted start, then a halt before the 4th.
    assert state["demotes"] == [False, False, True]
    assert len(state["started_prs"]) == 3  # halted before block_size (10)

    # The demoted (soft-breach) iteration still merged its PR: the later hard
    # breach halted only NEW work, never the in-flight merge.
    inflight_pr = state["started_prs"][-1]
    assert inflight_pr in state["merged_prs"]
    assert inflight_pr in res.report.merged_prs

    # A hard-breach note explains why fewer than block_size iterations ran.
    assert any("hard breach" in n and "halting new work" in n for n in res.report.notes)
    assert any("soft breach" in n for n in res.report.notes)

    # The block aggregate is attached to the report and reflects the biasing:
    # two heavy + one demoted (standard) iteration, 120s total.
    assert res.report.cost is not None
    assert res.report.cost.iterations == 3
    assert res.report.cost.heavy_iterations == 2
    assert res.report.cost.tier_counts.get("standard") == 1
    assert res.report.cost.total_seconds == 120.0

    # The review brief renders the block cost summary for the architect.
    assert runner.review_bodies, "a review issue should have been opened"
    brief = runner.review_bodies[-1]
    assert "Cost this block (quota ledger)" in brief
    assert "heavy-tier=2" in brief

    # The ledger on disk is append-only and every line is parseable.
    lines = ledger_file.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)


# --- synthesis duplicate rejections reach the review brief -------------------

def test_synthesis_duplicate_rejections_surface_in_block_notes(tmp_path, monkeypatch):
    """`SynthesisResult.rejected` must reach `BlockReport.notes` (and the brief)."""
    cfg = load_config()
    cfg.budget.clear()
    cfg.cycle["block_size"] = 0  # isolate: no implementation iterations needed

    def fake_synthesize(cfg, *, cycle_index, runner, ai_runner):
        return SynthesisResult(
            ok=True, studied=["a/b"], filed=[901],
            rejected=1, rejected_titles=["feat: already open ticket"],
            refusals=["feat: x: exact duplicate of prior work: 'feat: already open ticket'"],
            demoted_titles=["feat: a reworded variant"],
            prior_art_cited=1,
        )

    runner = _Runner()
    monkeypatch.setattr(cycle, "synthesize", fake_synthesize)
    monkeypatch.setattr(cycle, "_well_formed_backlog", lambda cfg, *, runner: 0)
    monkeypatch.setattr(cycle, "_governance_pr", lambda *a, **k: 0)

    res = cycle.run_cycle(cfg, repo_dir=str(tmp_path), cycle_index=1, runner=runner)

    assert res.report.synthesized == [901]
    note = next(n for n in res.report.notes if n.startswith("synthesis:"))
    assert "1 candidate(s) refused" in note
    assert "feat: already open ticket" in note

    # A demoted near-duplicate is reported separately: it was kept, not dropped.
    demoted = next(n for n in res.report.notes if "demoted" in n)
    assert "feat: a reworded variant" in demoted

    # The review brief renders every note verbatim, plus the coverage line.
    assert runner.review_bodies, "a review issue should have been opened"
    body = runner.review_bodies[-1]
    assert note in body and demoted in body
    assert "prior art coverage: **1/1**" in body


def test_a_journal_written_before_prior_art_screening_still_replays(tmp_path, monkeypatch):
    """Resuming a block journaled by an older build must not crash on new keys."""
    cfg = load_config()
    cfg.budget.clear()
    cfg.cycle["block_size"] = 0

    def legacy_synthesis_step(cfg, *, idx, runner, ai_runner, dry_run):
        return {"ran": True, "filed": [902], "error": "", "rejected": 0, "rejected_titles": []}

    runner = _Runner()
    monkeypatch.setattr(cycle, "_synthesis_step", legacy_synthesis_step)
    monkeypatch.setattr(cycle, "_governance_pr", lambda *a, **k: 0)

    res = cycle.run_cycle(cfg, repo_dir=str(tmp_path), cycle_index=2, runner=runner)
    assert res.report.synthesized == [902]
    assert res.report.prior_art_cited == 0


# --- plain-text agent output must not break article generation --------------

def test_persona_articles_survive_output_without_a_json_envelope(tmp_path):
    """`payload is None` degrades to the raw text, it never skips the article."""
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    kb.whitepapers_dir.mkdir(parents=True, exist_ok=True)
    (kb.whitepapers_dir / "2026-08-04-block-1.md").write_text("# Block paper\n\nBody.\n")

    article = "# What this block changed\n\nPlain text, no JSON envelope.\n"

    def ai_runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        return Proc(cmd, 0, article, "")

    written = cycle._persona_articles(
        cfg, kb, "2026-08-04-block-1", repo_root=tmp_path, ai_runner=ai_runner
    )

    assert len(written) == len(cfg.personas)
    for rel in written:
        text = (tmp_path / rel).read_text()
        assert "Plain text, no JSON envelope." in text


# --- trajectory retention ---------------------------------------------------

def test_cycle_prunes_trajectory_blocks_beyond_retention(tmp_path, monkeypatch):
    cfg = load_config()
    cfg.cycle["block_size"] = 0            # no iterations; isolate the pruning step
    cfg = replace(cfg, trajectory_retention_blocks=2)

    for block in range(5):
        trajectory.write(
            trajectory.Trajectory(
                iteration=block * 100 + 1, ticket=1, kind="implement", tier="standard",
                model="sonnet", prompt="p", block=block,
            ),
            tmp_path,
        )

    runner = _Runner()
    monkeypatch.setattr(cycle, "_well_formed_backlog", lambda cfg, *, runner: 999)
    monkeypatch.setattr(cycle, "_governance_pr", lambda *a, **k: 0)

    res = cycle.run_cycle(cfg, repo_dir=str(tmp_path), cycle_index=4, runner=runner)

    kept = sorted(p.name for p in trajectory.trajectory_dir(tmp_path).iterdir())
    assert kept == ["3", "4"]              # only the newest `retention_blocks`
    assert any("pruned trajectories" in n for n in res.report.notes)


# --- crash + resume: the cycle journal --------------------------------------
#
# A block is a long chain of expensive, side-effecting steps. These tests kill
# it mid-chain and resume it, asserting the two properties that make the
# journal worth having: no GitHub write happens twice, and the resumed block
# produces the brief an uninterrupted one would have.

BLOCK = 7          # the cycle index every resume test drives
ARTICLE = "# Block article\n\nWhat this block changed.\n"


class _GhRunner:
    """Records every git/gh call so duplicate GitHub writes are provable.

    Reports a dirty tree so the governance PR path (ticket -> branch -> PR)
    actually runs instead of short-circuiting on "nothing to commit".
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.issue_titles: list[str] = []
        self.pr_heads: list[str] = []
        self.review_bodies: list[str] = []
        self._issue = 900
        self._pr = 700

    def __call__(
        self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
    ) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, "[]", "")
        if cmd[:3] == ["gh", "issue", "create"]:
            self._issue += 1
            title = cmd[cmd.index("--title") + 1]
            self.issue_titles.append(title)
            if title.startswith("review:"):
                self.review_bodies.append(cmd[cmd.index("--body") + 1])
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self._issue}\n", "")
        if cmd[:3] == ["gh", "pr", "create"]:
            self._pr += 1
            self.pr_heads.append(cmd[cmd.index("--head") + 1])
            return Proc(cmd, 0, f"https://github.com/o/r/pull/{self._pr}\n", "")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return Proc(cmd, 0, " M knowledge/whitepapers/block.md\n", "")
        return Proc(cmd, 0, "", "")

    def count(self, *prefix: str) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == list(prefix))

    def titles_like(self, prefix: str) -> list[str]:
        return [t for t in self.issue_titles if t.startswith(prefix)]


def _article_runner(
    cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
) -> Proc:
    return Proc(list(cmd), 0, ARTICLE, "")


def _fake_synthesize(cfg, *, cycle_index, runner, ai_runner) -> SynthesisResult:
    """Files two tickets through the real `gh issue create` path, once."""
    filed = [
        github.create_issue(
            cfg.repo_slug, f"feat: synthesized {n} for block {cycle_index}",
            "problem/proposal", ["hsai"], runner=runner,
        )
        for n in (1, 2)
    ]
    return SynthesisResult(ok=True, studied=["a/b"], filed=filed)


def _make_run_once(ledger_file, *, crash_at: int | None = None):
    """A ``run_once`` stand-in whose PR number depends only on the iteration.

    Determinism is the point: the control run and the resumed run must produce
    identical reports, so nothing may depend on how many times it was called.
    ``crash_at`` is a 1-based position within the block; the crash happens
    before any ledger record is written, exactly like a worker dying mid-run.
    """
    state: dict[str, list[int]] = {"iterations": []}

    def fake(cfg, *, repo_dir, runner, ai_runner, iteration, block=None,
             demote_tier=False, dry_run=False):
        position = iteration % 100
        if crash_at is not None and position == crash_at:
            raise RuntimeError(f"worker died during iteration {iteration}")
        state["iterations"].append(iteration)
        ledger.append_record(
            ledger_file,
            ledger.LedgerRecord(
                iteration=iteration, block=iteration // 100, ticket=iteration,
                kind="implement", tier="standard", model="sonnet",
                wall_clock_seconds=5.0, attempts=1, outcome="merged",
            ),
        )
        return IterationResult(
            kind="implement", ticket=iteration, pr=500 + position,
            model="sonnet", merged=True,
        )

    return fake, state


def _block_cfg(*, block_size: int = 3):
    cfg = load_config()
    cfg.budget.clear()                      # budget gate is exercised separately
    cfg.cycle["block_size"] = block_size
    return cfg


def _seed_lesson(root) -> None:
    """One lesson, so the whitepaper + persona-article steps actually run."""
    KnowledgeBase.from_config(load_config(), root).write_lesson(
        Lesson(
            title="Green gate held", outcome="pass", kind="implement",
            context="c", what_happened="w", lesson="Remote CI is the truth.",
            created="2026-08-05",
        )
    )


def _drive(root, cfg, monkeypatch, *, crash_at=None, resume=False,
           cycle_index=BLOCK, dry_run=False):
    """Run one block against fresh fakes; returns (result, gh runner, state)."""
    runner = _GhRunner()
    fake, state = _make_run_once(ledger.ledger_path(cfg, root), crash_at=crash_at)
    monkeypatch.setattr(cycle, "run_once", fake)
    monkeypatch.setattr(cycle, "synthesize", _fake_synthesize)
    res = cycle.run_cycle(
        cfg, repo_dir=str(root), cycle_index=cycle_index, resume=resume,
        dry_run=dry_run, runner=runner, ai_runner=_article_runner,
    )
    return res, runner, state


def _brief_fields(report) -> tuple:
    return (
        report.synthesized, report.iterations, report.merged_prs,
        report.whitepaper, report.articles,
    )


def test_resume_after_a_crash_replays_and_writes_nothing_to_github_twice(
    tmp_path, monkeypatch
):
    """Crash inside the third implementation, resume, compare against a control."""
    crashed, control = tmp_path / "crashed", tmp_path / "control"
    for root in (crashed, control):
        root.mkdir()
        _seed_lesson(root)

    # --- the uninterrupted run this block should end up equal to --------------
    expected, control_gh, control_state = _drive(control, _block_cfg(), monkeypatch)
    assert control_state["iterations"] == [701, 702, 703]

    # --- run 1: dies inside the third implementation --------------------------
    cfg = _block_cfg()
    with pytest.raises(RuntimeError):
        _drive(crashed, cfg, monkeypatch, crash_at=3)

    jpath = journal.journal_path(crashed, BLOCK)
    steps_before = [(r.step, r.key) for r in journal.read_records(jpath)]
    assert ("synthesis", "block") in steps_before
    assert ("iteration", "0") in steps_before and ("iteration", "1") in steps_before
    assert ("iteration", "2") not in steps_before        # the crashed one left no record
    assert ("review_issue", "block") not in steps_before
    assert journal.latest_resumable(crashed) == BLOCK    # journal never closed

    # --- run 2: `hsai cycle --resume` with no index finds the block ------------
    res, gh, state = _drive(crashed, cfg, monkeypatch, resume=True, cycle_index=None)

    assert res.report.cycle_index == BLOCK and res.resumed
    assert state["iterations"] == [703]                  # only the crashed one re-ran

    # Zero duplicate GitHub writes: synthesis tickets are not re-filed, and the
    # review issue / governance ticket / governance PR each happen exactly once.
    assert gh.titles_like("feat: synthesized") == []
    assert len(gh.titles_like("review:")) == 1
    assert len(gh.titles_like("chore: governance artifacts")) == 1
    assert gh.count("gh", "pr", "create") == 1
    assert len(control_gh.titles_like("feat: synthesized")) == 2

    # The resumed brief is the brief an uninterrupted run would have produced.
    assert _brief_fields(res.report) == _brief_fields(expected.report)
    assert res.report.merged_prs == [501, 502, 503]
    assert res.report.whitepaper and len(res.report.articles) == len(cfg.personas)

    # ...plus one line telling the architect the block was resumed.
    resume_notes = [n for n in res.report.notes if n.startswith("resume: replayed")]
    assert len(resume_notes) == 1
    assert resume_notes[0] in gh.review_bodies[0]

    # The journal is closed, so a later `--resume` will not pick it up again.
    assert journal.latest_resumable(crashed) is None
    assert journal.open_journal(crashed, BLOCK).terminal().status == journal.COMPLETE


def test_resume_after_a_hard_halt_starts_no_new_work(tmp_path, monkeypatch):
    """The budget halt is terminal: resuming replays it, it never restarts work."""
    _seed_lesson(tmp_path)
    cfg = _block_cfg(block_size=10)
    # Two iterations (5s each) then a ceiling of 8s: the third grade hard-breaches.
    cfg.budget.update({"max_seconds_per_block": 8, "soft_ratio": 0.5})

    first, gh1, state1 = _drive(tmp_path, cfg, monkeypatch)
    assert state1["iterations"] == [701, 702]            # halted before the 3rd of 10
    assert any("hard breach" in n and "halting new work" in n for n in first.report.notes)

    halt = journal.open_journal(tmp_path, BLOCK).find("budget_halt", "block")
    assert halt is not None and halt.status == journal.HALTED and halt.terminal
    assert journal.latest_resumable(tmp_path) is None   # a halted block is not "unfinished"

    # The ceiling is still breached when we come back to the block...
    spent = ledger.aggregate_block(
        ledger.read_records(ledger.ledger_path(cfg, tmp_path)), BLOCK
    )
    assert ledger.evaluate_budget(spent, cfg.budget).halt

    # ...so an explicit resume replays the halt and starts nothing new.
    second, gh2, state2 = _drive(tmp_path, cfg, monkeypatch, resume=True)

    assert state2["iterations"] == []                   # zero new implementations
    assert _brief_fields(second.report) == _brief_fields(first.report)
    assert any("hard breach" in n for n in second.report.notes)
    assert gh2.count("gh", "issue", "create") == 0      # no second review issue
    assert gh2.count("gh", "pr", "create") == 0
    assert gh1.count("gh", "pr", "create") == 1
    # The green-merge invariant is untouched: resume merged nothing at all.
    assert gh2.count("gh", "pr", "merge") == 0


def test_resume_with_no_journal_behaves_exactly_like_a_fresh_run(tmp_path, monkeypatch):
    plain, resumed = tmp_path / "plain", tmp_path / "resumed"
    for root in (plain, resumed):
        root.mkdir()
        _seed_lesson(root)

    fresh, gh_fresh, state_fresh = _drive(plain, _block_cfg(), monkeypatch)
    blind, gh_blind, state_blind = _drive(resumed, _block_cfg(), monkeypatch, resume=True)

    assert not blind.resumed
    assert _brief_fields(blind.report) == _brief_fields(fresh.report)
    assert state_blind["iterations"] == state_fresh["iterations"] == [701, 702, 703]
    for prefix in (("gh", "issue", "create"), ("gh", "pr", "create"), ("gh", "pr", "merge")):
        assert gh_blind.count(*prefix) == gh_fresh.count(*prefix)
    assert len(gh_blind.titles_like("feat: synthesized")) == 2

    # With nothing journaled anywhere, `--resume` falls back to the derived index.
    before = int(time.time()) // 43200
    assert cycle.resolve_cycle_index(tmp_path / "empty", None, resume=True) in (
        before, before + 1
    )


def test_a_second_dry_run_replays_the_journal_instead_of_re_executing(tmp_path, monkeypatch):
    """`hsai cycle --dry-run` twice: the journal is unchanged the second time."""
    _seed_lesson(tmp_path)
    cfg = _block_cfg()

    first, gh1, state1 = _drive(tmp_path, cfg, monkeypatch, dry_run=True)
    jpath = Path(first.journal_path)
    assert jpath.name == journal.DRY_RUN_JOURNAL_FILE
    assert state1["iterations"] == [701, 702, 703]
    assert gh1.count("gh", "issue", "create") == 0      # a rehearsal writes nothing
    assert gh1.count("gh", "pr", "create") == 0

    recorded = jpath.read_text()

    second, gh2, state2 = _drive(tmp_path, cfg, monkeypatch, dry_run=True)

    assert jpath.read_text() == recorded                # pure replay: nothing appended
    assert second.resumed and state2["iterations"] == []
    assert gh2.count("gh", "issue", "create") == 0
    assert _brief_fields(second.report) == _brief_fields(first.report)
    # The rehearsal never touched - nor satisfied - the live journal.
    assert not (jpath.parent / journal.JOURNAL_FILE).exists()
    assert journal.latest_resumable(tmp_path) is None
