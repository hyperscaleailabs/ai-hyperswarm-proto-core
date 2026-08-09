"""Continuous-integration gate.

``run_local`` mirrors what the GitHub Actions workflow does (ruff + pytest) so
the loop can pre-flight a change before it ever opens a PR. ``wait_remote``
blocks until a PR's real GitHub checks conclude - that remote result is the
source of truth for whether a change may merge.
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

# The single definition of "a green build", as (step name, argv) pairs.
# ``run_local`` executes exactly these, and :mod:`hsai.ciguard` compares them
# against what the GitHub workflow declares - so the two can never silently
# drift apart. Adding a gate here is what makes it legal to add it remotely.
LOCAL_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ruff", ("ruff", "check", ".")),
    ("pytest", ("pytest",)),
)


def local_commands() -> tuple[str, ...]:
    """The shell form of every gate ``run_local`` executes, in order."""
    return tuple(" ".join(argv) for _, argv in LOCAL_STEPS)


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
    """Run ruff + pytest locally. This defines what a 'green build' means."""
    steps: dict[str, bool] = {}
    logs: list[str] = []

    for name, argv in LOCAL_STEPS:
        p = runner(list(argv), cwd=cwd)
        steps[name] = p.ok
        logs.append(f"$ {' '.join(argv)}\n{p.stdout}\n{p.stderr}")

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
