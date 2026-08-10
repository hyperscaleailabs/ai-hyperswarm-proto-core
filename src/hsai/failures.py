"""Why an iteration failed, named - and what the loop should do about it.

Before this module every failure looked the same to the loop: close the PR,
bump ``attempts:N``, unassign, retry with the same tier and the same prompt,
and after ``execution.max_ticket_attempts`` label the ticket ``blocked`` with
no recorded cause. A lint slip, an agent timeout, a red remote build, a merge
conflict and a worker that edited ``.github/workflows/`` were indistinguishable
in the ledger, the lesson and the review brief.

Two pure pieces live here:

- :func:`classify` - ordered rules over the signals one iteration produces,
  returning one of the :data:`CLASSES` (or ``""`` when nothing failed).
- :func:`action_for` - look up ``retry_policy`` from ``core.yaml`` and return
  the :class:`RetryAction` the orchestrator applies.

Both are side-effect free so the taxonomy can be reasoned about (and tested)
without a GitHub, a worktree, or a model.

Synthesis: run-llama/llama_index's ``issue_classifier.yml`` (classify incoming
work so it can be *routed* rather than hand-triaged - here we route retries),
OpenBMB/ChatDev (reflect on the previous phase before acting again, rather than
repeating blind), and assafelovic/gpt-researcher's batch-by-theme habit
(group failures into classes first, then fix them as a class - which is what
the taxonomy table in the brief and whitepaper gives the architect).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# --- the taxonomy -------------------------------------------------------------
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

#: Every class the loop can attribute a failure to, in precedence order.
CLASSES: tuple[str, ...] = (
    WORKFLOW_TAMPER,
    MERGE_CONFLICT,
    GUARD_INCOMPLETE,
    GUARD_NO_REPRO,
    TIMEOUT,
    LINT,
    TEST_FAILURE,
    AGENT_ERROR,
    REMOTE_INFRA,
    UNKNOWN,
)

#: Guard verdicts :func:`classify` understands, mapped to their class.
_GUARD_CLASSES = {"incomplete": GUARD_INCOMPLETE, "no_repro": GUARD_NO_REPRO}

# --- retry actions ------------------------------------------------------------
RETRY_SAME_TIER = "retry_same_tier"
RETRY_WITH_REMEDIATION = "retry_with_remediation"
ESCALATE_TIMEOUT = "escalate_timeout"
DEMOTE_TIER = "demote_tier"
BLOCK_IMMEDIATELY = "block_immediately"

#: Label a retried ticket carries so the next worker gets a longer agent budget.
ESCALATE_LABEL = "escalate:timeout"
#: Label a retried ticket carries so the next worker is selected one tier cheaper.
DEMOTE_LABEL = "tier:demote"
#: Label prefix recording *why* the previous attempt failed.
FAILURE_LABEL_PREFIX = "failure:"

_TIMEOUT_RE = re.compile(r"(?i)\btimed?\s*out\b|\btimeout after\b|killed after \d+s")
_CONFLICT_RE = re.compile(
    r"(?i)\bmerge conflict\b|^CONFLICT \(|\bautomatic merge failed\b|"
    r"^<<<<<<< |\bfix conflicts and then commit\b",
    re.MULTILINE,
)


def failure_label(failure_class: str) -> str:
    """The ``failure:<class>`` label a recovered ticket carries."""
    return f"{FAILURE_LABEL_PREFIX}{failure_class}"


def is_timeout(text: str) -> bool:
    """Does this agent/CI text describe a run that ran out of wall clock?

    ``proc.run`` renders an expired subprocess as ``timeout after <n>s``; the
    CLI and the shell have their own phrasings, so match the family.
    """
    return bool(_TIMEOUT_RE.search(text or ""))


def has_merge_conflict(text: str) -> bool:
    """Does this text carry git's conflict vocabulary (or a conflict marker)?"""
    return bool(_CONFLICT_RE.search(text or ""))


def classify(
    *,
    agent_ok: bool = True,
    agent_error: str = "",
    timed_out: bool = False,
    workflow_paths: Sequence[str] = (),
    guard: str = "",
    ci_steps: Mapping[str, bool] | None = None,
    remote: str = "",
    merge_conflict: bool = False,
    failed: bool = False,
) -> str:
    """Name the failure behind one iteration's signals. ``""`` = nothing failed.

    The rules are *ordered*, and the order is the contract when signals
    co-occur - a failing iteration usually trips several at once:

    1. ``workflow_tamper`` beats everything. A worker that edited
       ``.github/workflows/**`` moved the goalposts it is judged by, so no
       other signal from that run can be trusted at face value.
    2. ``merge_conflict`` next: the branch cannot integrate at all, so
       downstream lint/test verdicts describe a tree that will never merge.
    3. A **guard verdict beats a CI signal**. ``guard_incomplete`` and
       ``guard_no_repro`` are statements about whether the work was *done*;
       a red build is a statement about the work that *was* done. Knowing the
       diff was knowledge-only is strictly more actionable than knowing pytest
       is unhappy about it.
    4. ``timeout`` beats ``agent_error``. A killed agent also exits non-zero,
       so the generic "the CLI failed" reading would mask the specific cause.
    5. Then the concrete local CI steps - ``lint`` before ``test_failure``,
       because ruff runs first and a lint slip is the cheaper, more certain
       fix. Both outrank ``agent_error``: a red ruff/pytest *names* the repair,
       where a non-zero exit alone does not.
    6. ``agent_error`` - the run itself failed with nothing more specific said.
    7. ``remote_infra`` - local was clean but the remote build did not conclude
       SUCCESS, so the divergence is environmental rather than in the diff.
    8. ``unknown`` - the caller knows it failed and no signal explains it. Worth
       a distinct name: a growing ``unknown`` count in the brief means the
       taxonomy itself needs work.
    """
    if workflow_paths:
        return WORKFLOW_TAMPER
    if merge_conflict or has_merge_conflict(agent_error):
        return MERGE_CONFLICT
    if guard in _GUARD_CLASSES:
        return _GUARD_CLASSES[guard]
    if timed_out or is_timeout(agent_error):
        return TIMEOUT

    steps = dict(ci_steps or {})
    if steps.get("ruff") is False:
        return LINT
    if steps.get("pytest") is False:
        return TEST_FAILURE

    if not agent_ok:
        return AGENT_ERROR
    if remote and remote != "SUCCESS":
        return REMOTE_INFRA
    if failed:
        return UNKNOWN
    return ""


@dataclass(frozen=True)
class RetryAction:
    """What ``_recover_failed`` does about one failure class.

    ``consumes_attempt`` is the load-bearing field: a class we can already
    prove is not worth re-running (tampering, an unmergeable branch) blocks the
    ticket *without* burning one of the two attempts, so the architect sees the
    real cause instead of a ticket that quietly exhausted its retries.
    """

    name: str
    blocks: bool = False
    consumes_attempt: bool = True
    labels: tuple[str, ...] = ()

    @property
    def remediate(self) -> bool:
        """Should the next prompt carry the previous attempt's failure?"""
        return self.name in (RETRY_WITH_REMEDIATION, ESCALATE_TIMEOUT, DEMOTE_TIER)


_ACTIONS: dict[str, RetryAction] = {
    RETRY_SAME_TIER: RetryAction(RETRY_SAME_TIER),
    RETRY_WITH_REMEDIATION: RetryAction(RETRY_WITH_REMEDIATION),
    ESCALATE_TIMEOUT: RetryAction(ESCALATE_TIMEOUT, labels=(ESCALATE_LABEL,)),
    DEMOTE_TIER: RetryAction(DEMOTE_TIER, labels=(DEMOTE_LABEL,)),
    BLOCK_IMMEDIATELY: RetryAction(
        BLOCK_IMMEDIATELY, blocks=True, consumes_attempt=False
    ),
}

#: Every action name ``retry_policy`` may name (used to validate core.yaml).
ACTION_NAMES: tuple[str, ...] = tuple(_ACTIONS)

#: Used when ``core.yaml`` carries no ``retry_policy`` at all - the historical
#: behaviour, so an un-migrated config keeps working unchanged.
DEFAULT_ACTION = RETRY_SAME_TIER


def action_for(failure_class: str, retry_policy: Mapping[str, object] | None) -> RetryAction:
    """Resolve ``retry_policy`` (from ``core.yaml``) for one failure class.

    An unknown class, an unknown action name, or a missing policy all fall back
    to the configured default and then to :data:`DEFAULT_ACTION`: a policy typo
    must never strand a ticket.
    """
    policy = dict(retry_policy or {})
    classes = policy.get("classes")
    default = str(policy.get("default") or DEFAULT_ACTION)
    name = default
    if isinstance(classes, Mapping) and failure_class in classes:
        name = str(classes[failure_class] or default)
    return _ACTIONS.get(name) or _ACTIONS.get(default) or _ACTIONS[DEFAULT_ACTION]


def render_taxonomy_table(counts: Mapping[str, int] | None) -> str:
    """The 'Failure taxonomy' markdown table shared by the brief and whitepaper.

    Ordered by count (then name) so the mode the architect should batch-fix
    sits at the top, which is the whole point of naming classes.
    """
    rows = {k: v for k, v in (counts or {}).items() if v}
    if not rows:
        return "_No failures recorded in this window._"
    ordered = sorted(rows.items(), key=lambda kv: (-kv[1], kv[0]))
    body = "\n".join(f"| `{name}` | {count} |" for name, count in ordered)
    return "| failure class | count |\n| --- | --- |\n" + body
