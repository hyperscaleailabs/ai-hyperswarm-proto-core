"""Continuous-integration gate.

``run_local`` mirrors what the GitHub Actions workflow does (ruff + pytest),
plus a `kb-lint` pre-flight the remote workflow does not yet have (see the
module docstring of :mod:`hsai.kblint` and the ``ci.yml`` patch this ticket's
PR body carries for the architect to apply) so the loop can pre-flight a
change before it ever opens a PR. ``wait_remote`` blocks until a PR's real
GitHub checks conclude - that remote result is the source of truth for
whether a change may merge.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .proc import Runner, run

# Rollup outcomes returned by wait_remote / _rollup_result.
SUCCESS = "SUCCESS"
FAILURE = "FAILURE"
PENDING = "PENDING"
TIMEOUT = "TIMEOUT"

_PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


@dataclass
class CIResult:
    ok: bool
    steps: dict[str, bool] = field(default_factory=dict)
    log: str = ""

    def summary(self) -> str:
        marks = ", ".join(f"{name}={'pass' if ok else 'FAIL'}" for name, ok in self.steps.items())
        return f"CI {'green' if self.ok else 'red'} ({marks})" if marks else (
            "CI green" if self.ok else "CI red"
        )


def run_local(*, cwd: str | None = None, runner: Runner = run) -> CIResult:
    """Run ruff + pytest + kb-lint locally. This defines what a 'green build' means.

    ``kb-lint`` runs last: it is cheap and structural (MetaGPT's pre-commit
    discipline - a fast check before anything expensive), but it also depends
    on the vault as the worktree left it, so it comes after the code gates
    rather than racing them.
    """
    steps: dict[str, bool] = {}
    logs: list[str] = []

    lint = runner(["ruff", "check", "."], cwd=cwd)
    steps["ruff"] = lint.ok
    logs.append(f"$ ruff check .\n{lint.stdout}\n{lint.stderr}")

    tests = runner(["pytest"], cwd=cwd)
    steps["pytest"] = tests.ok
    logs.append(f"$ pytest\n{tests.stdout}\n{tests.stderr}")

    kb_lint = runner(["hsai", "kb-lint"], cwd=cwd)
    steps["kb-lint"] = kb_lint.ok
    logs.append(f"$ hsai kb-lint\n{kb_lint.stdout}\n{kb_lint.stderr}")

    return CIResult(ok=all(steps.values()), steps=steps, log="\n\n".join(logs))


def _rollup_result(rollup: list[dict]) -> str:
    """Reduce a PR's statusCheckRollup array to SUCCESS / FAILURE / PENDING."""
    if not rollup:
        return PENDING
    for item in rollup:
        # CheckRun (GitHub Actions) uses status/conclusion; StatusContext uses state.
        if item.get("__typename") == "StatusContext" or "state" in item and "status" not in item:
            state = (item.get("state") or "").upper()
            if state in ("PENDING", "EXPECTED", ""):
                return PENDING
            if state not in _PASS_CONCLUSIONS:
                return FAILURE
        else:
            if (item.get("status") or "").upper() != "COMPLETED":
                return PENDING
            if (item.get("conclusion") or "").upper() not in _PASS_CONCLUSIONS:
                return FAILURE
    return SUCCESS


def poll_remote(pr_number: int, repo: str, *, runner: Runner = run) -> str:
    """One-shot: reduce a PR's current check rollup to SUCCESS/FAILURE/PENDING."""
    p = runner(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "statusCheckRollup"]
    )
    try:
        rollup = json.loads(p.stdout or "{}").get("statusCheckRollup", []) or []
    except json.JSONDecodeError:
        rollup = []
    return _rollup_result(rollup)


def wait_remote(
    pr_number: int,
    repo: str,
    *,
    timeout: float = 300,
    interval: float = 10,
    runner: Runner = run,
    sleep=time.sleep,
) -> str:
    """Block until a PR's remote checks conclude. Returns SUCCESS/FAILURE/TIMEOUT."""
    deadline = time.monotonic() + timeout
    while True:
        result = poll_remote(pr_number, repo, runner=runner)
        if result != PENDING:
            return result
        if time.monotonic() >= deadline:
            return TIMEOUT
        sleep(interval)
