"""Verify-and-repair: show a worker the CI failure it is not allowed to see.

An iteration used to get exactly one agent run. If ``ruff`` or ``pytest`` was
red afterwards, the orchestrator held the failing text in ``CIResult.log``,
never showed it to the model that could fix it, and burned one of only
``max_ticket_attempts`` attempts on a change that was often a lint error away
from green. That asymmetry is structural, not incidental: loop workers are
sandboxed *without* permission to run ruff or pytest, so an agent cannot
self-verify from inside its own run. The orchestrator is the only thing that
can close the feedback loop.

This module is the pure half of that loop:

- :func:`build_repair_prompt` renders the ticket, the failing step names and a
  bounded tail of the CI output into a fix-only instruction. No subprocess, no
  model call, no I/O - it is a string function and is unit-tested as one.
- :func:`truncate_log` keeps pytest's ``short test summary info`` lines even
  when the tail it can afford would have cut them off; they name *what* broke,
  and a naive tail keeps only the trailing counters.
- :func:`skip_reason` and :func:`describe_transition` are the loop's other two
  decisions - whether to repair at all, and how to record what a pass achieved.

The orchestrator drives the impure half (:func:`hsai.orchestrator.run_once`
step 6a): at most ``repair.max_attempts`` extra calls to the SAME model in the
SAME worktree, each followed by a re-run of local CI, each re-checked by every
post-agent guard, each metered on the quota ledger. The merge gate is
untouched - ``ci.wait_remote`` remains the only thing that authorizes a merge.

Synthesis: SWE-agent/SWE-agent (its agent-computer interface loops
run-tests -> read-output -> edit instead of acting once), crewAIInc/crewAI
(a failed turn is isolated with a budget rather than aborting the flow), and
FoundationAgents/MetaGPT (an explicit QA step between engineering and
integration). run-llama/llama_index supplies the constraint that the
reproduction evidence must be re-proven after each pass, not just once.
"""
from __future__ import annotations

import re

from .ci import CIResult
from .config import CoreConfig

# Recognisable opening line: a repair prompt is never a worker or reviewer prompt.
PROMPT_MARKER = "You are the REPAIR pass"

# How a repair pass shows up on the quota ledger (and in the block aggregate).
LEDGER_KIND = "repair"
LEDGER_OUTCOME = "repair"

DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_MAX_LOG_CHARS = 4000

ELIDED = "... (earlier CI output elided)\n"
SUMMARY_HEADING = "pytest short test summary (kept verbatim):"

_SUMMARY_HEADER = re.compile(r"=+\s*short test summary info\s*=+")
_SECTION_RULE = re.compile(r"^=+.*=+$")
_OUTCOME_PREFIXES = ("FAILED ", "ERROR ", "XPASS ")


# --- configuration ----------------------------------------------------------

def is_enabled(cfg: CoreConfig) -> bool:
    return bool(cfg.repair.get("enabled", True))


def max_attempts(cfg: CoreConfig) -> int:
    """Extra agent calls allowed per iteration when local CI is red."""
    return max(0, int(cfg.repair.get("max_attempts", DEFAULT_MAX_ATTEMPTS)))


def max_log_chars(cfg: CoreConfig) -> int:
    """Ceiling on the CI output tail handed to a repair pass."""
    return max(0, int(cfg.repair.get("max_log_chars", DEFAULT_MAX_LOG_CHARS)))


def skip_reason(cfg: CoreConfig, *, demote_tier: bool) -> str:
    """Why this iteration must NOT repair (empty string = go ahead).

    ``demote_tier`` is the caller's soft-budget breach: a block already burning
    its quota must not spend extra agent calls polishing one change.
    """
    if not is_enabled(cfg):
        return "disabled in cfg.repair"
    if demote_tier:
        return "soft budget breach (selection demoted a tier)"
    if max_attempts(cfg) < 1:
        return "cfg.repair.max_attempts is 0"
    return ""


# --- rendering --------------------------------------------------------------

def failing_steps(ci_result: CIResult) -> list[str]:
    """The names of the local CI steps that failed, in the order they ran."""
    return [name for name, ok in ci_result.steps.items() if not ok]


def short_summary_lines(log: str) -> list[str]:
    """pytest's ``short test summary info`` lines, in order, deduplicated.

    These are the densest signal a failing run produces - one line per broken
    test, naming the file, the test and the exception - and pytest prints them
    *before* the trailing counters, so a plain tail can drop exactly them.
    """
    kept: list[str] = []
    in_block = False
    for raw in (log or "").splitlines():
        line = raw.rstrip()
        if _SUMMARY_HEADER.search(line):
            in_block = True
            continue
        if in_block:
            if line.strip() and not _SECTION_RULE.match(line):
                kept.append(line)
                continue
            in_block = False
        if line.startswith(_OUTCOME_PREFIXES):
            kept.append(line)

    seen: set[str] = set()
    unique: list[str] = []
    for line in kept:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique


def _fit_lines(lines: list[str], budget: int) -> str:
    """Join ``lines`` within ``budget`` chars, dropping whole lines off the end.

    Returns "" when only the heading survives - a heading with nothing under it
    is noise, and the tail below it says the same thing.
    """
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept) if len(kept) > 1 else ""


def truncate_log(log: str, limit: int = DEFAULT_MAX_LOG_CHARS) -> str:
    """The tail of a CI log, capped at ``limit`` chars, summary lines preserved.

    Half the budget at most is spent lifting the pytest short-summary lines
    above the tail; the rest is the most recent output verbatim, which is where
    the traceback and the ruff diagnostics live.
    """
    text = (log or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text

    head = _fit_lines([SUMMARY_HEADING, *short_summary_lines(text)], limit // 2)
    if head:
        head += "\n\n"
    tail_budget = limit - len(head) - len(ELIDED)
    if tail_budget <= 0:
        # The cap is too small even for the elision marker: a bare tail carries
        # more signal than a heading with nothing under it.
        return text[-limit:]
    return f"{head}{ELIDED}{text[-tail_budget:]}"


def build_repair_prompt(
    ticket_title: str,
    ci_result: CIResult,
    attempt: int,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_log_chars: int = DEFAULT_MAX_LOG_CHARS,
) -> str:
    """The fix-only instruction handed back to the model that wrote the change."""
    steps = ", ".join(f"`{s}`" for s in failing_steps(ci_result)) or "`(none reported)`"
    log = truncate_log(ci_result.log, max_log_chars) or "(no output captured)"
    return f"""{PROMPT_MARKER} ({attempt} of {max_attempts}) for
ai-hyperswarm-proto-core. The change for the ticket below is ALREADY in this
worktree, uncommitted, but the local CI gate is RED. You are not permitted to
run `ruff` or `pytest` yourself in this sandbox, so the orchestrator ran them
for you and quoted the result here. This is the only feedback you will get.

Ticket: {ticket_title}

Failing CI step(s): {steps}

Local CI output ({ci_result.summary()}):
```
{log}
```

Fix ONLY what makes those steps fail:
- Do not implement new behaviour, refactor unrelated code, or widen the ticket.
  The change is nearly finished; this pass exists to land it, not to redo it.
- Do not weaken, skip, xfail or delete a test to make it pass, and do not relax
  the ruff configuration. Fix the code the test is judging.
- Do not touch anything under `.github/workflows/`. The CI definition is not
  yours to change, and edits there are reverted automatically.
- Keep the reproduction evidence intact: any regression test this change added
  must still fail on the pre-fix tree and pass here.
- Change the smallest number of lines that turns both `ruff check .` and
  `pytest` green.

The orchestrator re-runs local CI the moment you stop, then re-applies every
guard. A pass that leaves the build red simply costs the ticket its budget.
"""


def describe_transition(before: CIResult, after: CIResult) -> str:
    """What one repair pass achieved, e.g. ``ruff FAIL -> pass, pytest FAIL -> FAIL``."""
    failed = failing_steps(before)
    if not failed:
        return "nothing to repair"
    return ", ".join(
        f"{name} FAIL -> {'pass' if after.steps.get(name) else 'FAIL'}"
        for name in failed
    )
