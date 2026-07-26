"""Continuous-integration gate.

``run_local`` mirrors what the GitHub Actions workflow does (ruff + pytest) so
the loop can pre-flight a change before it ever opens a PR. ``remote_status``
checks the actual check-run result GitHub recorded for a branch, and
``poll_remote_status`` polls it until every check run has concluded so the
orchestrator can use the real remote outcome as an explicit pre-merge gate
instead of only trusting the local run.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .proc import Runner, run


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

    lint = runner(["ruff", "check", "."], cwd=cwd)
    steps["ruff"] = lint.ok
    logs.append(f"$ ruff check .\n{lint.stdout}\n{lint.stderr}")

    tests = runner(["pytest"], cwd=cwd)
    steps["pytest"] = tests.ok
    logs.append(f"$ pytest\n{tests.stdout}\n{tests.stderr}")

    return CIResult(ok=all(steps.values()), steps=steps, log="\n\n".join(logs))


def remote_status(repo: str, branch: str, *, runner: Runner = run) -> str:
    """Return GitHub's rollup check state for ``branch`` (e.g. SUCCESS/FAILURE/PENDING)."""
    p = runner(
        [
            "gh", "api",
            f"repos/{repo}/commits/{branch}/check-runs",
            "--jq", "[.check_runs[].conclusion] | join(\",\")",
        ]
    )
    return p.stdout.strip()


def _is_pending(status: str) -> bool:
    """True while any check run in ``status`` has not concluded yet."""
    parts = status.split(",") if status else [""]
    return any(p in ("", "null") for p in parts)


def remote_ok(status: str) -> bool:
    """True if every concluded check run in ``status`` succeeded (or none ran)."""
    parts = [p for p in status.split(",") if p and p != "null"]
    return all(p in ("success", "skipped", "neutral") for p in parts)


def poll_remote_status(
    repo: str,
    branch: str,
    *,
    runner: Runner = run,
    max_attempts: int = 30,
    interval: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Poll :func:`remote_status` until every check run has concluded.

    Stops early once nothing is pending; otherwise gives up after
    ``max_attempts`` polls and returns whatever the last poll observed (still
    useful - a lingering pending run is itself a signal worth recording).
    """
    status = remote_status(repo, branch, runner=runner)
    attempts = 1
    while _is_pending(status) and attempts < max_attempts:
        sleep(interval)
        status = remote_status(repo, branch, runner=runner)
        attempts += 1
    return status
