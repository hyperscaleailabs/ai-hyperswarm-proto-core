"""Continuous-integration gate.

``run_local`` mirrors what the GitHub Actions workflow does (ruff + pytest) so
the loop can pre-flight a change before it ever opens a PR. ``wait_remote``
blocks until a PR's real GitHub checks conclude - that remote result is the
source of truth for whether a change may merge. ``disposition`` then turns
that rollup outcome into what the caller should actually DO about it: a
TIMEOUT is infrastructure latency, not a model failure, and must never be
scored - or retried - like one.
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

# Dispositions returned by `disposition()` - what run_once should do about a
# rollup outcome.
MERGE = "merge"
RECOVER = "recover"
REQUEUE = "requeue"

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


def _fetch_rollup(pr_number: int, repo: str, *, runner: Runner) -> list[dict]:
    """Raw ``statusCheckRollup`` array for a PR (``[]`` if not yet reported)."""
    p = runner(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "statusCheckRollup"]
    )
    try:
        return json.loads(p.stdout or "{}").get("statusCheckRollup", []) or []
    except json.JSONDecodeError:
        return []


def poll_remote(pr_number: int, repo: str, *, runner: Runner = run) -> str:
    """One-shot: reduce a PR's current check rollup to SUCCESS/FAILURE/PENDING."""
    return _rollup_result(_fetch_rollup(pr_number, repo, runner=runner))


def wait_remote(
    pr_number: int,
    repo: str,
    *,
    timeout: float = 300,
    interval: float = 10,
    max_timeout: float | None = None,
    backoff_factor: float = 2.0,
    runner: Runner = run,
    sleep=time.sleep,
) -> str:
    """Block until a PR's remote checks conclude. Returns SUCCESS/FAILURE/TIMEOUT.

    Polls at ``interval`` while GitHub has not yet reported any checks at all
    (an empty rollup - the webhook may simply be slow to register them, so
    hammering the base cadence is cheap and correct). Once checks appear and
    are actually running, the wait between polls backs off exponentially
    (``backoff_factor``, capped at the ceiling) so a slow build is not
    polled needlessly. ``timeout`` is the ceiling used when ``max_timeout`` is
    not given; when both are set, the larger one wins, so callers can pass a
    generous ``max_timeout`` without shrinking an existing ``timeout``. TIMEOUT
    is returned only once that ceiling is reached with the checks still
    genuinely unresolved - it is infrastructure latency, not a verdict, and
    callers must not treat it as FAILURE.
    """
    ceiling = max(timeout, max_timeout or 0.0, interval)
    deadline = time.monotonic() + ceiling
    wait = interval
    while True:
        rollup = _fetch_rollup(pr_number, repo, runner=runner)
        result = _rollup_result(rollup)
        if result != PENDING:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return TIMEOUT
        sleep(min(wait, remaining))
        if rollup:
            # Checks have started reporting - back off so we stop polling a
            # long-running build every `interval` seconds.
            wait = min(wait * backoff_factor, ceiling)


@dataclass(frozen=True)
class Disposition:
    """What `run_once` should do about a remote-CI rollup outcome."""

    action: str  # merge | recover | requeue
    remote: str  # the rollup outcome (SUCCESS/FAILURE/PENDING/TIMEOUT/...) that produced it

    @property
    def should_merge(self) -> bool:
        return self.action == MERGE


def disposition(remote: str) -> Disposition:
    """Map a rollup outcome to the action `run_once` should take.

    SUCCESS is the only outcome that may merge. TIMEOUT means the checks are
    genuinely still unresolved after the full backoff ceiling - that is
    infrastructure latency, not a model failure, so it is requeued: the PR
    stays open, the branch is kept, and no attempt is charged. Everything else
    (FAILURE, and any other non-SUCCESS outcome such as an unresolved PENDING
    or a guard sentinel like INCOMPLETE/NO_REPRO) recovers exactly as before:
    the PR is closed and an attempt is consumed.
    """
    if remote == SUCCESS:
        return Disposition(MERGE, remote)
    if remote == TIMEOUT:
        return Disposition(REQUEUE, remote)
    return Disposition(RECOVER, remote)
