"""Run several loop workers concurrently, each in its own worktree.

Workers are I/O-bound (they mostly wait on git/gh/claude subprocesses), so a
thread pool is sufficient and keeps the model an ordinary function call per
worker. ``max_parallel`` comes from core.yaml and should only be raised after a
single iteration has been proven.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import CoreConfig
from .orchestrator import IterationResult, run_once
from .proc import Runner, run


def run_parallel(
    cfg: CoreConfig,
    *,
    repo_dir: str,
    workers: int,
    rounds: int = 1,
    dry_run: bool = False,
    runner: Runner = run,
    ai_runner: Runner = run,
) -> list[IterationResult]:
    """Run ``workers`` iterations concurrently for ``rounds`` rounds."""
    workers = max(1, min(workers, cfg.max_parallel))
    results: list[IterationResult] = []
    for r in range(rounds):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    run_once,
                    cfg,
                    repo_dir=repo_dir,
                    dry_run=dry_run,
                    runner=runner,
                    ai_runner=ai_runner,
                    iteration=r * workers + w + 1,
                )
                for w in range(workers)
            ]
            for fut in as_completed(futures):
                results.append(fut.result())
    return results
