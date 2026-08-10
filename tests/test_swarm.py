from hsai import swarm
from hsai.config import load_config
from hsai.orchestrator import IterationResult


def test_run_parallel_survives_a_worker_raising(monkeypatch):
    """One worker raising must not propagate out of `run_parallel` and lose
    the whole round's results - the other workers' results still come back."""
    cfg = load_config()

    def fake_run_once(
        cfg, *, repo_dir, dry_run, runner, ai_runner, iteration, demote_tier=False
    ):
        if iteration == 2:
            raise RuntimeError("worker blew up")
        return IterationResult(kind="implement", ticket=iteration, merged=True)

    monkeypatch.setattr(swarm, "run_once", fake_run_once)

    results = swarm.run_parallel(cfg, repo_dir="/tmp/does-not-matter", workers=3, rounds=1)

    assert len(results) == 3
    errored = [r for r in results if r.kind == "error"]
    assert len(errored) == 1
    assert "worker blew up" in errored[0].notes[0]
    assert "RuntimeError" in errored[0].notes[0]

    survivors = [r for r in results if r.kind == "implement"]
    assert len(survivors) == 2
    assert all(r.merged for r in survivors)


def test_run_parallel_all_workers_raising_still_returns_one_result_each(monkeypatch):
    cfg = load_config()

    def always_raises(cfg, *, repo_dir, dry_run, runner, ai_runner, iteration, demote_tier=False):
        raise ValueError(f"boom {iteration}")

    monkeypatch.setattr(swarm, "run_once", always_raises)

    results = swarm.run_parallel(cfg, repo_dir="/tmp/does-not-matter", workers=2, rounds=1)

    assert len(results) == 2
    assert all(r.kind == "error" for r in results)
    assert all("ValueError" in r.notes[0] for r in results)


def test_run_parallel_caps_workers_at_max_parallel(monkeypatch):
    cfg = load_config()  # max_parallel == 3 in the pinned core.yaml
    seen_iterations = []

    def fake_run_once(cfg, *, repo_dir, dry_run, runner, ai_runner, iteration, demote_tier=False):
        seen_iterations.append(iteration)
        return IterationResult(kind="implement", ticket=iteration)

    monkeypatch.setattr(swarm, "run_once", fake_run_once)

    results = swarm.run_parallel(cfg, repo_dir="/tmp/x", workers=50, rounds=1)

    assert len(results) == cfg.max_parallel
    assert len(seen_iterations) == cfg.max_parallel
