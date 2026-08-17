"""Failure taxonomy for the quota ledger, plus a postmortem-driven backlog trigger.

Before this module, a failed iteration only carried an opaque ``outcome``
string (``incomplete``, ``no_repro``, ``recovered``, ...) on its
:class:`hsai.ledger.LedgerRecord`. Nothing aggregated failures by *cause*, so a
systemic defect - agent timeouts on heavy tickets, repro-guard false
positives, a flaky lint rule - was invisible across a block unless a human
read every lesson one at a time. G4 asks the loop to get safer and cheaper
over time; that needs a causal signal, not just a pass/fail one.

- :func:`classify` maps one iteration's evidence (agent result, guard
  verdicts, the local CI step map, the remote CI rollup) to exactly one
  member of the closed :data:`FAILURE_CLASSES` vocabulary. ``unknown`` is an
  explicit branch, never a silent default.
- :func:`pareto_table` folds a block's ledger records into a per-class
  histogram (count, share, an exemplar iteration/ticket), ranked highest-count
  first - what both the review brief and ``hsai postmortem`` render.
- :func:`file_postmortem_ticket` is the closed loop: when one class clears
  BOTH a configurable share-of-failures ratio and a minimum absolute count,
  it deterministically builds and files a single well-formed P1 ticket,
  deduped against anything already open by its (block-independent) title -
  so a systemic cause gets investigated exactly once, not once per block.

Synthesis: SWE-agent (fix streams driven by post-hoc analysis of failed
trajectories), langchain (issue/PR triage automation acting on the repo's own
backlog signal), llama_index (hard numeric CI gates, not advisory warnings),
and ChatDev (explicit reflection on failed rounds feeding the next round).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import github
from .config import CoreConfig
from .ledger import LedgerRecord
from .proc import Runner, run
from .tickets import TicketSpec

# --- the closed vocabulary ---------------------------------------------------
AGENT_TIMEOUT = "agent_timeout"
AGENT_ERROR = "agent_error"
INCOMPLETE_DIFF = "incomplete_diff"
NO_REPRO = "no_repro"
LINT_FAIL = "lint_fail"
TEST_FAIL = "test_fail"
REMOTE_CI_FAIL = "remote_ci_fail"
REMOTE_CI_TIMEOUT = "remote_ci_timeout"
MERGE_CONFLICT = "merge_conflict"
BUDGET_HALT = "budget_halt"
UNKNOWN = "unknown"

FAILURE_CLASSES = (
    AGENT_TIMEOUT, AGENT_ERROR, INCOMPLETE_DIFF, NO_REPRO, LINT_FAIL, TEST_FAIL,
    REMOTE_CI_FAIL, REMOTE_CI_TIMEOUT, MERGE_CONFLICT, BUDGET_HALT, UNKNOWN,
)

# `hsai.proc.run` stamps a timed-out subprocess's stderr with exactly this
# phrase (see proc.py's `subprocess.TimeoutExpired` handler), so it is a
# reliable, code-derived signal rather than a guess about agent error text.
_TIMEOUT_MARKERS = ("timeout after", "timed out")


@dataclass
class FailureEvidence:
    """Everything :func:`classify` is allowed to look at for one iteration.

    Every field defaults to "nothing went wrong here" so a caller only needs
    to set what it actually observed - an early-return guard (completeness,
    repro) typically sets exactly one field.
    """

    agent_ok: bool = True
    agent_error: str = ""
    completeness_ok: bool = True          # False: the "code ticket, no code" guard fired
    repro_ok: bool | None = None          # None: guard did not run; False: it blocked
    review_approved: bool | None = None   # None: review skipped/disabled; False: blocked
    ci_steps: dict[str, bool] = field(default_factory=dict)  # local CI step map
    remote_ci: str = ""                   # SUCCESS | FAILURE | TIMEOUT | "" (not reached)
    merge_conflict: bool = False
    budget_halted: bool = False


def classify(evidence: FailureEvidence) -> str:
    """Map ``evidence`` to exactly one member of :data:`FAILURE_CLASSES`.

    Ordered to mirror the sequence of guards inside
    :func:`hsai.orchestrator.run_once`: the guard that actually stopped (or
    would have stopped) the iteration is the one that explains it. A review
    verdict that withheld approval is classified as ``agent_error`` - the
    diff the agent produced was substantively wrong, which is what the
    reviewer caught - rather than inventing a class outside the closed
    vocabulary. ``unknown`` is reached only when none of the evidence lines
    up with a known cause; it is never a silent default.
    """
    if evidence.budget_halted:
        return BUDGET_HALT
    if evidence.merge_conflict:
        return MERGE_CONFLICT
    lowered = evidence.agent_error.lower()
    if any(marker in lowered for marker in _TIMEOUT_MARKERS):
        return AGENT_TIMEOUT
    if not evidence.agent_ok:
        return AGENT_ERROR
    if not evidence.completeness_ok:
        return INCOMPLETE_DIFF
    if evidence.repro_ok is False:
        return NO_REPRO
    if evidence.review_approved is False:
        return AGENT_ERROR
    if evidence.ci_steps.get("ruff") is False:
        return LINT_FAIL
    if evidence.ci_steps.get("pytest") is False:
        return TEST_FAIL
    if evidence.remote_ci == "FAILURE":
        return REMOTE_CI_FAIL
    if evidence.remote_ci == "TIMEOUT":
        return REMOTE_CI_TIMEOUT
    return UNKNOWN


def default_detail(failure_class: str, evidence: FailureEvidence) -> str:
    """A short, generic explanation for ``failure_class``.

    Callers that already hold a more specific reason (a guard's own message,
    a reviewer's blocking findings) should pass that instead; this is the
    fallback for callers that only have the evidence struct.
    """
    if failure_class == AGENT_TIMEOUT:
        return evidence.agent_error[:200] or "agent run exceeded its timeout"
    if failure_class == AGENT_ERROR:
        return evidence.agent_error[:200] or (
            "independent review blocked the change"
            if evidence.review_approved is False
            else "agent run reported an error"
        )
    if failure_class == INCOMPLETE_DIFF:
        return "knowledge-only diff on a code ticket"
    if failure_class == NO_REPRO:
        return "reproduce-before-fix guard rejected the change"
    if failure_class == LINT_FAIL:
        return "ruff check failed"
    if failure_class == TEST_FAIL:
        return "pytest failed"
    if failure_class == REMOTE_CI_FAIL:
        return f"remote CI concluded {evidence.remote_ci or 'FAILURE'}"
    if failure_class == REMOTE_CI_TIMEOUT:
        return "remote CI did not conclude before the poll timeout"
    if failure_class == MERGE_CONFLICT:
        return "branch could not be merged onto the default branch"
    if failure_class == BUDGET_HALT:
        return "block budget ceiling breached before this iteration started"
    return "no classifier rule matched this iteration's evidence"


def classify_with_detail(evidence: FailureEvidence) -> tuple[str, str]:
    """:func:`classify` plus :func:`default_detail` in one call."""
    failure_class = classify(evidence)
    return failure_class, default_detail(failure_class, evidence)


# --- Pareto analysis ----------------------------------------------------------

@dataclass(frozen=True)
class ParetoRow:
    failure_class: str
    count: int
    share: float                 # 0..1 of this block's failures
    exemplar_iteration: int
    exemplar_ticket: int | None


def pareto_table(records: list[LedgerRecord], block: int) -> list[ParetoRow]:
    """Per-class failure histogram for one block, highest-count first.

    Only records carrying a non-empty ``failure_class`` count - a merged
    iteration never contributes one. Ties break alphabetically so the
    ordering (and therefore the "dominant class") is deterministic.
    """
    failures = [r for r in records if r.block == block and r.failure_class]
    total = len(failures)
    if not total:
        return []
    by_class: dict[str, list[LedgerRecord]] = {}
    for r in failures:
        by_class.setdefault(r.failure_class, []).append(r)
    rows = [
        ParetoRow(
            failure_class=cls,
            count=len(items),
            share=len(items) / total,
            exemplar_iteration=items[0].iteration,
            exemplar_ticket=items[0].ticket,
        )
        for cls, items in by_class.items()
    ]
    rows.sort(key=lambda r: (-r.count, r.failure_class))
    return rows


def render_pareto_table(rows: list[ParetoRow]) -> str:
    """Markdown table for the review brief and ``hsai postmortem``."""
    if not rows:
        return "_no failure-class records for this block_"
    lines = ["| class | count | share | exemplar |", "| --- | --- | --- | --- |"]
    for r in rows:
        exemplar = f"iteration {r.exemplar_iteration}"
        if r.exemplar_ticket:
            exemplar += f" (ticket #{r.exemplar_ticket})"
        lines.append(f"| `{r.failure_class}` | {r.count} | {r.share:.0%} | {exemplar} |")
    return "\n".join(lines)


# --- the backlog trigger -------------------------------------------------------

DEFAULT_RATIO_THRESHOLD = 0.4
DEFAULT_MIN_COUNT = 3

POSTMORTEM_LABELS = ("priority:P1", "hsai", "postmortem")


def dominant_failure(
    rows: list[ParetoRow], *, ratio_threshold: float, min_count: int
) -> ParetoRow | None:
    """The block's #1 failure class, if it clears BOTH configured ceilings."""
    if not rows:
        return None
    top = rows[0]
    if top.share >= ratio_threshold and top.count >= min_count:
        return top
    return None


def postmortem_ticket_title(failure_class: str) -> str:
    """The dedup key: stable per CLASS, not per block, so a cause that keeps
    recurring across many blocks gets exactly one open ticket."""
    return f"fix: recurring {failure_class} failures dominate the loop's postmortems"


def build_postmortem_ticket(
    row: ParetoRow, *, block: int, ratio_threshold: float, min_count: int
) -> TicketSpec:
    """A fully-structured, well-formed ticket for the dominant failure class."""
    exemplar = f"iteration {row.exemplar_iteration}"
    if row.exemplar_ticket:
        exemplar += f" (ticket #{row.exemplar_ticket})"
    problem = (
        f"In block {block}, `{row.failure_class}` accounted for {row.count} of the "
        f"block's failures ({row.share:.0%}) - at or above the configured postmortem "
        f"trigger (ratio >= {ratio_threshold:g}, count >= {min_count}; see "
        "`postmortem.ratio_threshold` / `postmortem.min_count` in .ai-swarm/core.yaml). "
        f"Exemplar: {exemplar}. Left uninvestigated, a systemic failure cause stays "
        "invisible across blocks and keeps burning quota on the same doomed retry "
        "pattern (G4)."
    )
    proposal = (
        f"Run `hsai postmortem --block {block}` for the full Pareto breakdown and "
        "`hsai traj <iteration>` on the exemplar to see what actually happened. "
        f"Diagnose the root cause of `{row.failure_class}` failures and land a fix - a "
        "guard, a prompt change, a timeout adjustment, or whatever the evidence points "
        "to - that reduces this class's share of failures in the next block."
    )
    return TicketSpec(
        title=postmortem_ticket_title(row.failure_class),
        problem=problem,
        proposal=proposal,
        acceptance_criteria=(
            f"Root cause of the dominant `{row.failure_class}` failure pattern is "
            "documented with evidence from the exemplar iteration (and others, if found)",
            "A concrete fix (guard, prompt, config, or code change) is implemented and "
            "covered by a test",
            f"The fix's lesson cites this ticket so a later `hsai postmortem` can confirm "
            f"`{row.failure_class}`'s share dropped",
        ),
        verification_plan=(
            f"Compare `hsai postmortem --block {block}` against a later block's Pareto "
            f"for `{row.failure_class}`'s share",
            "A new or updated regression test covers the failure pattern where feasible",
        ),
        size="M",
        goal_ids=("G4", "G2"),
        synthesis_rationale=(
            "Filed automatically by the postmortem trigger (src/hsai/postmortem.py): a "
            "per-block failure-class Pareto crossed the configured ratio/count threshold."
        ),
        labels=POSTMORTEM_LABELS,
    )


def file_postmortem_ticket(
    cfg: CoreConfig,
    records: list[LedgerRecord],
    *,
    block: int,
    runner: Runner = run,
) -> int:
    """File at most one P1 ticket for this block's dominant failure class.

    Returns the filed issue number, or ``0`` when nothing was filed: no class
    cleared both thresholds, or an open ticket with the same (class-scoped)
    title already exists - so a persistent cause spanning many blocks gets
    exactly one open ticket, never one per block.
    """
    ratio = float(cfg.postmortem.get("ratio_threshold", DEFAULT_RATIO_THRESHOLD))
    min_count = int(cfg.postmortem.get("min_count", DEFAULT_MIN_COUNT))
    row = dominant_failure(
        pareto_table(records, block), ratio_threshold=ratio, min_count=min_count
    )
    if row is None:
        return 0
    title = postmortem_ticket_title(row.failure_class)
    if any(i.title == title for i in github.list_open_issues(cfg.repo_slug, runner=runner)):
        return 0
    spec = build_postmortem_ticket(row, block=block, ratio_threshold=ratio, min_count=min_count)
    return github.create_issue(
        cfg.repo_slug, spec.title, spec.render(), spec.all_labels(), runner=runner
    )
