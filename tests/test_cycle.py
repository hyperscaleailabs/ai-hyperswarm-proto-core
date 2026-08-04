"""Budget-gate behavior of a simulated half-day block.

Drives ``run_cycle`` with a fake ``run_once`` that appends escalating cost to
the quota ledger, and asserts the warn-then-halt transitions: cheaper-tier
biasing on a soft breach, then a new-work halt on a hard breach that still lets
the already-started (in-flight) iteration finish and merge. The heavy tail of a
cycle (synthesis, whitepaper, governance PR) is stubbed so the test isolates the
gate.
"""
from __future__ import annotations

import json
from dataclasses import replace

from hsai import cycle, ledger, trajectory
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase
from hsai.orchestrator import IterationResult
from hsai.proc import Proc


class _Runner:
    """Answers the git/gh calls run_cycle makes around the implementation loop."""

    def __init__(self) -> None:
        self.review_bodies: list[str] = []
        self._issue = 900

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
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

    def fake(cfg, *, repo_dir, runner, ai_runner, iteration, demote_tier=False):
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


# --- plain-text agent output must not break article generation --------------

def test_persona_articles_survive_output_without_a_json_envelope(tmp_path):
    """`payload is None` degrades to the raw text, it never skips the article."""
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    kb.whitepapers_dir.mkdir(parents=True, exist_ok=True)
    (kb.whitepapers_dir / "2026-08-04-block-1.md").write_text("# Block paper\n\nBody.\n")

    article = "# What this block changed\n\nPlain text, no JSON envelope.\n"

    def ai_runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
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
