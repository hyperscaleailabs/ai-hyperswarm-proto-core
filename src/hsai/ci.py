"""Continuous-integration gate.

``run_local`` mirrors what the GitHub Actions workflow does (ruff + pytest) so
the loop can pre-flight a change before it ever opens a PR. ``wait_remote``
blocks until a PR's real GitHub checks conclude - that remote result is the
source of truth for whether a change may merge. ``disposition`` maps a
concluded (or timed-out) rollup to what the caller should DO about it - the
single place that answers "is TIMEOUT a FAILURE?" (no: a merely-slow build is
infrastructure latency, not a verdict on the change).
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

# Dispositions returned by `disposition()` - what run_once should DO with a
# rollup outcome, as distinct from the outcome itself.
MERGE = "merge"      # SUCCESS: the only outcome that may ever merge a PR
RECOVER = "recover"  # FAILURE: a real red build - close the PR, consume an attempt
REQUEUE = "requeue"  # TIMEOUT/PENDING: unresolved - leave PR+branch alone, no attempt spent

_PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


@dataclass(frozen=True)
class Disposition:
    """What to do about a concluded (or timed-out) rollup, and why."""

    action: str  # MERGE | RECOVER | REQUEUE
    remote: str  # the rollup outcome (SUCCESS/FAILURE/PENDING/TIMEOUT) that produced it


def disposition(remote: str) -> Disposition:
    """Pure map from a rollup outcome to the caller's next move.

    SUCCESS is the ONLY outcome that may merge. FAILURE is a real, conclusive
    red build: close the PR and consume a retry attempt exactly as before.
    TIMEOUT (the deadline passed while checks were still unresolved) and a
    bare PENDING (should not normally reach here after `wait_remote`, but is
    handled the same way defensively) both REQUEUE: this is infrastructure
    latency, not a verdict on the change, so the PR stays open, the branch
    stays intact, and no attempt is consumed.
    """
    if remote == SUCCESS:
        return Disposition(MERGE, remote)
    if remote == FAILURE:
        return Disposition(RECOVER, remote)
    return Disposition(REQUEUE, remote)


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


def _fetch_rollup(pr_number: int, repo: str, *, runner: Runner = run) -> list[dict]:
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
    max_interval: float = 60,
    backoff_multiplier: float = 2.0,
    runner: Runner = run,
    sleep=time.sleep,
) -> str:
    """Block until a PR's remote checks conclude. Returns SUCCESS/FAILURE/TIMEOUT.

    Polls with bounded exponential backoff: the wait between polls doubles
    (``backoff_multiplier``) each round, capped at ``max_interval``, so a slow
    build is not hammered with `gh` calls every ``interval`` seconds for the
    full ``timeout`` window. Backoff only grows once GitHub has actually
    reported checks (a non-empty rollup) - an empty rollup means "nothing
    reported yet" (Actions hasn't started the run), which resolves fast once
    it does and should not be starved by an already-large interval. TIMEOUT is
    returned only once the deadline passes AND the rollup is still genuinely
    unresolved (PENDING) - never in place of an actual FAILURE/SUCCESS.
    """
    deadline = time.monotonic() + timeout
    cur_interval = interval
    while True:
        rollup = _fetch_rollup(pr_number, repo, runner=runner)
        result = _rollup_result(rollup)
        if result != PENDING:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return TIMEOUT
        sleep(min(cur_interval, remaining))
        if rollup:  # checks have started reporting - back off further
            cur_interval = min(cur_interval * backoff_multiplier, max_interval)
