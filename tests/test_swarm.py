from hsai import swarm
from hsai.config import load_config
from hsai.orchestrator import IterationResult


def test_run_parallel_survives_a_worker_raising(monkeypatch):
    """One crashed worker must not sink its siblings' results (or the round)."""
    cfg = load_config()

    def fake_run_once(cfg, *, repo_dir, dry_run, runner, ai_runner, iteration):
        if iteration == 2:
            raise RuntimeError("boom")
        return IterationResult(
            kind="implement", ticket=iteration, pr=100 + iteration, merged=True,
        )

    monkeypatch.setattr(swarm, "run_once", fake_run_once)

    results = swarm.run_parallel(cfg, repo_dir=".", workers=3, dry_run=True)

    # every worker gets a slot in the round, including the one that crashed
    assert len(results) == 3
    crashed = [r for r in results if r.kind == "crashed"]
    assert len(crashed) == 1
    assert "iteration 2 worker raised" in crashed[0].notes[0]

    # the surviving workers' real results are intact, not lost with the round
    survivors = [r for r in results if r.kind != "crashed"]
    assert {r.ticket for r in survivors} == {1, 3}
    assert all(r.merged for r in survivors)


def test_run_parallel_all_workers_succeed(monkeypatch):
    cfg = load_config()

    def fake_run_once(cfg, *, repo_dir, dry_run, runner, ai_runner, iteration):
        return IterationResult(kind="implement", ticket=iteration, merged=True)

    monkeypatch.setattr(swarm, "run_once", fake_run_once)

    results = swarm.run_parallel(cfg, repo_dir=".", workers=2, dry_run=True)

    assert len(results) == 2
    assert all(r.kind == "implement" and r.merged for r in results)
