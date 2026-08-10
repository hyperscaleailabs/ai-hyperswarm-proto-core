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


def _crashed_result(iteration: int, exc: BaseException) -> IterationResult:
    """Placeholder for a worker that raised instead of returning.

    Keeps that slot visible in the round's results (rather than silently
    shrinking the list) without fabricating any ticket/PR/CI data the crashed
    worker never got far enough to produce.
    """
    return IterationResult(
        kind="crashed",
        notes=[f"iteration {iteration} worker raised: {exc!r}"],
    )


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
    """Run ``workers`` iterations concurrently for ``rounds`` rounds.

    One worker raising must not sink its siblings' already-completed results:
    each future's exception is caught individually, so the round returns
    every surviving worker's :class:`IterationResult` plus a placeholder for
    the one that crashed, instead of propagating and losing the whole round.
    """
    workers = max(1, min(workers, cfg.max_parallel))
    results: list[IterationResult] = []
    for r in range(rounds):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_once,
                    cfg,
                    repo_dir=repo_dir,
                    dry_run=dry_run,
                    runner=runner,
                    ai_runner=ai_runner,
                    iteration=r * workers + w + 1,
                ): r * workers + w + 1
                for w in range(workers)
            }
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:  # a worker crash must not sink the whole round
                    results.append(_crashed_result(futures[fut], exc))
    return results
