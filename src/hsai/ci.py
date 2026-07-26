"""Continuous-integration gate.

``run_local`` mirrors what the GitHub Actions workflow does (ruff + pytest) so
the loop can pre-flight a change before it ever opens a PR. ``remote_status``
checks the actual check-run result GitHub recorded for a branch.
"""
from __future__ import annotations

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
