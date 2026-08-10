"""Failure taxonomy and the retry policy built on top of it.

Before this module every failed iteration looked the same to the loop: the PR
was closed, ``attempts:N`` ticked up, the ticket went back to the backlog, and
the next worker retried with the same tier and the same prompt. A lint slip, an
agent that hung, a red remote build, a merge conflict and a worker that edited
``.github/workflows/`` were indistinguishable - in the ledger, in the lesson and
in the review brief. A loop that cannot say *why* it failed cannot get safer or
cheaper (G4), and its audit trail (G2) stops at "it failed".

Two pure pieces, both trivially testable:

- :func:`classify` maps the signals one iteration produced onto exactly one
  :class:`FailureClass`, over an explicitly ordered rule list.
- :func:`action_for` maps a class onto the :class:`RetryAction` the
  orchestrator should take, driven by ``execution.retry_policy`` in
  ``.ai-swarm/core.yaml`` with :data:`DEFAULT_RETRY_POLICY` as the fallback.

Neither touches the filesystem, the network, or a model.

**Precedence.** Signals co-occur constantly (a worker that hung usually also
leaves a red build), so the rule order below *is* the specification:

1. ``workflow_tamper`` beats everything. A worker that edited the CI checks
   moved the goalposts it is judged by; that is a safety event, not a build
   error, and no downstream signal can outrank it.
2. A guard verdict beats a CI signal. The guards (completeness, then
   reproduce-before-fix) reason about the *diff*; local CI only reports on the
   tree that diff produced, so the guard is the more specific cause.
3. ``timeout`` beats ``agent_error``. A killed agent exits non-zero, so the
   generic "agent failed" signal is always present too and would mask the
   actionable one.

Beyond those three, the order runs from the most specific cause to the least:
a structural blocker (``merge_conflict``), then how the agent itself ended
(``timeout``, ``agent_error``), then what the local build said (``lint``,
``test_failure``), then the remote build (``remote_infra``), and finally
``unknown`` for a failure that left no recognised signal at all.

Synthesis: run-llama/llama_index (``issue_classifier.yml`` - classify incoming
work so it can be *routed* rather than hand-triaged), assafelovic/gpt-researcher
(its batch-by-theme history: group failures into classes first, fix them as a
class), and OpenBMB/ChatDev (reflect between phases before acting again - the
class is what the next attempt reflects on).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

# --- the taxonomy ------------------------------------------------------------
# `NONE` is not a failure; it is what a clean iteration classifies as.
NONE = ""
LINT = "lint"
TEST_FAILURE = "test_failure"
TIMEOUT = "timeout"
GUARD_INCOMPLETE = "guard_incomplete"
GUARD_NO_REPRO = "guard_no_repro"
WORKFLOW_TAMPER = "workflow_tamper"
MERGE_CONFLICT = "merge_conflict"
REMOTE_INFRA = "remote_infra"
AGENT_ERROR = "agent_error"
UNKNOWN = "unknown"

#: Every class the loop can record, in taxonomy (not precedence) order.
CLASSES = (
    LINT,
    TEST_FAILURE,
    TIMEOUT,
    GUARD_INCOMPLETE,
    GUARD_NO_REPRO,
    WORKFLOW_TAMPER,
    MERGE_CONFLICT,
    REMOTE_INFRA,
    AGENT_ERROR,
    UNKNOWN,
)

LABEL_PREFIX = "failure:"

# What `ci.SUCCESS` / `ci.TIMEOUT` are, without importing the CI wrapper: this
# module stays dependency-free so it can be unit-tested in isolation.
_REMOTE_SUCCESS = "SUCCESS"
_REMOTE_TIMEOUT = "TIMEOUT"

_CONFLICT_MARKERS = (
    "merge conflict",
    "conflicts",
    "non-fast-forward",
    "fetch first",
    "rejected",
    "cannot lock ref",
)


def looks_like_merge_conflict(text: str) -> bool:
    """Does ``text`` (a failed push/merge's output) read as a conflict?

    Kept here rather than in :mod:`hsai.gitops` because it is a *classification*
    judgement, and because the taxonomy's tests are the natural place to pin it.
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _CONFLICT_MARKERS)


@dataclass
class Signals:
    """Everything one iteration observed that could explain a failure.

    Assembled incrementally by ``orchestrator.run_once`` as the iteration
    progresses, then handed to :func:`classify` at each terminal exit.
    """

    #: The iteration did not deliver a merged change. Without this, an
    #: iteration that left no signal at all classifies as clean, not `unknown`.
    failed: bool = False
    #: Paths under `.github/workflows/` the worker touched (and we reverted).
    workflow_paths: tuple[str, ...] = ()
    #: Completeness guard: did a code ticket produce code?
    completeness_ok: bool = True
    #: Reproduce-before-fix guard; ``None`` when it did not apply.
    repro_ok: bool | None = None
    #: The push (or merge) came back looking like a conflict.
    merge_conflict: bool = False
    #: The agent hit ``execution.agent_timeout_seconds``.
    agent_timed_out: bool = False
    #: The `claude -p` process exited zero.
    agent_ok: bool = True
    #: Its stderr, for the reason string only - never for the verdict, because
    #: a successful run may still write warnings there.
    agent_error: str = ""
    #: Local CI *after* the agent ran, step by step (`{"ruff": True, ...}`).
    ci_steps: dict[str, bool] = field(default_factory=dict)
    #: The remote check rollup's conclusion, or "" if we never got that far.
    remote_ci: str = ""


@dataclass(frozen=True)
class FailureClass:
    """One classification: the class name plus why that rule fired."""

    name: str
    reason: str = ""

    @property
    def is_failure(self) -> bool:
        return self.name != NONE

    @property
    def label(self) -> str:
        """The GitHub label carrying this class (empty for a clean run)."""
        return f"{LABEL_PREFIX}{self.name}" if self.is_failure else ""

    def __str__(self) -> str:
        return self.name or "none"


def _tamper(s: Signals) -> str:
    if not s.workflow_paths:
        return ""
    return f"worker edited CI workflow(s): {', '.join(s.workflow_paths)}"


def _incomplete(s: Signals) -> str:
    return "" if s.completeness_ok else "code ticket produced a knowledge-only diff"


def _no_repro(s: Signals) -> str:
    return "" if s.repro_ok is not False else "no failing-then-passing reproduction"


def _conflict(s: Signals) -> str:
    return "branch could not be pushed cleanly (conflict)" if s.merge_conflict else ""


def _timeout(s: Signals) -> str:
    if s.agent_timed_out:
        return "agent exceeded execution.agent_timeout_seconds"
    if s.remote_ci == _REMOTE_TIMEOUT:
        return "remote CI did not conclude within ci_remote_timeout_seconds"
    return ""


def _agent_error(s: Signals) -> str:
    # Verdict from the exit status only: a healthy run may still write to
    # stderr, so the text is evidence for the reason, never for the rule.
    if s.agent_ok:
        return ""
    detail = s.agent_error.strip().splitlines()[0] if s.agent_error.strip() else ""
    return f"agent exited non-zero{': ' + detail if detail else ''}"


def _lint(s: Signals) -> str:
    return "" if s.ci_steps.get("ruff", True) else "ruff check failed locally"


def _tests(s: Signals) -> str:
    return "" if s.ci_steps.get("pytest", True) else "pytest failed locally"


def _remote(s: Signals) -> str:
    if s.remote_ci in ("", _REMOTE_SUCCESS):
        return ""
    # Local was green (the local rules above did not fire) yet the remote build
    # was not: the two environments diverged, or a check flaked.
    return f"remote CI concluded {s.remote_ci} while local CI was green"


#: The ordered rule list. Order is the precedence specification - see the
#: module docstring. Each rule returns its reason, or "" when it does not fire.
_RULES: tuple[tuple[str, Callable[[Signals], str]], ...] = (
    (WORKFLOW_TAMPER, _tamper),
    (GUARD_INCOMPLETE, _incomplete),
    (GUARD_NO_REPRO, _no_repro),
    (MERGE_CONFLICT, _conflict),
    (TIMEOUT, _timeout),
    (AGENT_ERROR, _agent_error),
    (LINT, _lint),
    (TEST_FAILURE, _tests),
    (REMOTE_INFRA, _remote),
)


def classify(signals: Signals) -> FailureClass:
    """Reduce ``signals`` to exactly one :class:`FailureClass`. Pure."""
    for name, rule in _RULES:
        reason = rule(signals)
        if reason:
            return FailureClass(name, reason)
    if signals.failed:
        return FailureClass(UNKNOWN, "iteration failed but left no recognised signal")
    return FailureClass(NONE, "no failure signal")


# --- what to do about it -----------------------------------------------------

RETRY_SAME_TIER = "retry_same_tier"
RETRY_WITH_REMEDIATION = "retry_with_remediation"
ESCALATE_TIMEOUT = "escalate_timeout"
DEMOTE_TIER = "demote_tier"
BLOCK_IMMEDIATELY = "block_immediately"


@dataclass(frozen=True)
class RetryAction:
    """How the next attempt on this ticket should differ from the last one."""

    name: str
    #: Block the ticket now, for a human, WITHOUT consuming a retry.
    blocks: bool = False
    #: Show the next worker a bounded excerpt of the previous failure.
    remediate: bool = False
    #: Run the next attempt one model tier cheaper.
    demote: bool = False
    #: Give the next attempt a longer agent timeout.
    escalate: bool = False


ACTIONS: dict[str, RetryAction] = {
    RETRY_SAME_TIER: RetryAction(RETRY_SAME_TIER),
    RETRY_WITH_REMEDIATION: RetryAction(RETRY_WITH_REMEDIATION, remediate=True),
    ESCALATE_TIMEOUT: RetryAction(ESCALATE_TIMEOUT, remediate=True, escalate=True),
    DEMOTE_TIER: RetryAction(DEMOTE_TIER, remediate=True, demote=True),
    BLOCK_IMMEDIATELY: RetryAction(BLOCK_IMMEDIATELY, blocks=True),
}

#: Fallback for any class ``execution.retry_policy`` does not name. Tampering
#: and conflicts block immediately: neither is fixable by running the same
#: prompt again, so a second attempt would only burn quota before the ticket
#: reached a human anyway.
DEFAULT_RETRY_POLICY: dict[str, str] = {
    LINT: RETRY_WITH_REMEDIATION,
    TEST_FAILURE: RETRY_WITH_REMEDIATION,
    TIMEOUT: ESCALATE_TIMEOUT,
    GUARD_INCOMPLETE: RETRY_WITH_REMEDIATION,
    GUARD_NO_REPRO: RETRY_WITH_REMEDIATION,
    WORKFLOW_TAMPER: BLOCK_IMMEDIATELY,
    MERGE_CONFLICT: BLOCK_IMMEDIATELY,
    REMOTE_INFRA: RETRY_SAME_TIER,
    AGENT_ERROR: DEMOTE_TIER,
    UNKNOWN: RETRY_SAME_TIER,
}

#: A no-op action: nothing failed, so nothing about the next run should change.
NO_ACTION = RetryAction("none")


def action_for(
    failure_class: str, policy: Mapping[str, str] | None = None
) -> RetryAction:
    """Resolve the configured action for ``failure_class``.

    An unknown class, or a class mapped to an action name that does not exist,
    falls back to :data:`DEFAULT_RETRY_POLICY` and then to a plain retry: a
    typo in ``core.yaml`` must never strand a ticket.
    """
    if not failure_class:
        return NO_ACTION
    name = (policy or {}).get(failure_class) or DEFAULT_RETRY_POLICY.get(failure_class)
    return ACTIONS.get(str(name), ACTIONS[RETRY_SAME_TIER])


def label_for(failure_class: str) -> str:
    """The GitHub label carrying ``failure_class`` (empty for a clean run)."""
    return f"{LABEL_PREFIX}{failure_class}" if failure_class else ""


def class_from_labels(labels: Iterable[str]) -> str:
    """Read the recorded failure class off a ticket's labels ("" if none)."""
    for label in labels or ():
        if label.startswith(LABEL_PREFIX):
            name = label[len(LABEL_PREFIX):]
            if name in CLASSES:
                return name
    return ""


def failure_labels(labels: Iterable[str]) -> list[str]:
    """Every ``failure:*`` label present - what a fresh claim must clear."""
    return [lb for lb in labels or () if lb.startswith(LABEL_PREFIX)]


_EMPTY_TABLE = (
    "_No classified failures in this window - every iteration reached its gate "
    "cleanly._"
)


def render_failure_table(counts: Mapping[str, int]) -> str:
    """Render the *Failure taxonomy* markdown table shared by the brief,
    the block whitepaper, and anything else that reports on a window.

    Sorted by count descending then name, so the mode the architect should
    attack as a class is the first row.
    """
    live = {k: v for k, v in (counts or {}).items() if k and v}
    if not live:
        return _EMPTY_TABLE
    rows = "\n".join(
        f"| `{name}` | {count} |"
        for name, count in sorted(live.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return f"| failure class | count |\n| --- | --- |\n{rows}"


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(text: str) -> str:
    """Filesystem-safe form of ``text`` (branch names carry a ``/``)."""
    return _SLUG_RE.sub("-", (text or "").strip()).strip("-")
