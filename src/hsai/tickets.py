"""Structured tickets: schema, rendering, and the well-formedness gate.

A ticket is *well-formed* when it carries real acceptance criteria and a
verification plan. The orchestrator refuses to implement malformed feature /
improvement tickets - it labels them ``needs-refinement`` instead - so vague
one-liners can never again be "satisfied" by a trivial diff.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .github import Issue
from .practices import PracticeRef, render_practices_section

# Sections a substantial ticket must carry.
ACCEPTANCE_HEADING = re.compile(r"^#{2,3}\s*acceptance criteria\s*$", re.IGNORECASE | re.MULTILINE)
VERIFICATION_HEADING = re.compile(
    r"^#{2,3}\s*verification plan\s*$", re.IGNORECASE | re.MULTILINE
)
CHECKBOX = re.compile(r"^\s*-\s*\[[ xX]?\]\s+\S", re.MULTILINE)

NEEDS_REFINEMENT = "needs-refinement"
SIZE_LABELS = ("size:S", "size:M", "size:L")

# Kinds of tickets exempt from the substantial-schema gate (docs and chores may
# be legitimately small; heal tickets are filed by the loop itself mid-incident).
_EXEMPT_PREFIXES = ("docs:", "chore:", "ci: main is red")


@dataclass(frozen=True)
class TicketSpec:
    """A fully-structured ticket ready to be filed."""

    title: str
    problem: str
    proposal: str
    acceptance_criteria: tuple[str, ...]
    verification_plan: tuple[str, ...]
    size: str = "M"  # S | M | L
    goal_ids: tuple[str, ...] = ()
    synthesis_rationale: str = ""  # which reference projects were combined, and how
    # The provenance claim: the PR opened for this ticket cites exactly these,
    # so an empty tuple honestly renders as "none declared".
    practices: tuple[PracticeRef, ...] = ()
    labels: tuple[str, ...] = ()

    def render(self) -> str:
        ac = "\n".join(f"- [ ] {c}" for c in self.acceptance_criteria)
        vp = "\n".join(f"- [ ] {v}" for v in self.verification_plan)
        goals = ", ".join(self.goal_ids) or "-"
        practices = "\n" + render_practices_section(self.practices)
        synth = (
            f"\n## Synthesis rationale\n{self.synthesis_rationale}\n"
            if self.synthesis_rationale
            else ""
        )
        return f"""## Problem
{self.problem}

## Proposal
{self.proposal}

## Acceptance criteria
{ac}

## Verification plan
{vp}
{practices}{synth}
## Meta
- goals: {goals}
- size: {self.size}
"""

    def all_labels(self) -> list[str]:
        base = [f"size:{self.size}", *self.labels]
        return list(dict.fromkeys(base))  # dedupe, keep order


@dataclass
class WellFormedness:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def check_well_formed(title: str, body: str) -> WellFormedness:
    """Is this ticket substantial enough to hand to an implementation agent?"""
    lowered = title.strip().lower()
    if any(lowered.startswith(p) for p in _EXEMPT_PREFIXES):
        return WellFormedness(ok=True, reasons=["exempt kind (docs/chore/heal)"])

    reasons: list[str] = []
    if not ACCEPTANCE_HEADING.search(body):
        reasons.append("missing '## Acceptance criteria' section")
    checkboxes = CHECKBOX.findall(body)
    if len(checkboxes) < 2:
        reasons.append(f"needs >= 2 checkbox criteria (found {len(checkboxes)})")
    if not VERIFICATION_HEADING.search(body):
        reasons.append("missing '## Verification plan' section")
    return WellFormedness(ok=not reasons, reasons=reasons)


def issue_well_formed(issue: Issue) -> WellFormedness:
    return check_well_formed(issue.title, issue.body)


def size_of(issue: Issue) -> str:
    """Read the size label off an issue ('M' when unlabeled)."""
    for lbl in issue.labels:
        if lbl.startswith("size:"):
            return lbl.split(":", 1)[1]
    return "M"
