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

# Sections a substantial ticket must carry.
ACCEPTANCE_HEADING = re.compile(r"^#{2,3}\s*acceptance criteria\s*$", re.IGNORECASE | re.MULTILINE)
VERIFICATION_HEADING = re.compile(
    r"^#{2,3}\s*verification plan\s*$", re.IGNORECASE | re.MULTILINE
)
PRIOR_ART_HEADING = re.compile(r"^#{2,3}\s*prior art\s*$", re.IGNORECASE | re.MULTILINE)
CHECKBOX = re.compile(r"^\s*-\s*\[[ xX]?\]\s+\S", re.MULTILINE)

# What a ticket says when retrieval found nothing. An explicit sentence, not an
# empty section: "we looked and there is none" and "nobody looked" must not
# render identically.
NO_PRIOR_ART = "No prior art found"

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
    practice_ids: tuple[str, ...] = ()  # new/extended entries in the practices registry
    # Citations into this repo's own knowledge base, one rendered line each
    # (``[[note-name]] (outcome) - title``); see :mod:`hsai.retrieval`.
    prior_art: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def render(self) -> str:
        ac = "\n".join(f"- [ ] {c}" for c in self.acceptance_criteria)
        vp = "\n".join(f"- [ ] {v}" for v in self.verification_plan)
        goals = ", ".join(self.goal_ids) or "-"
        practices = ", ".join(self.practice_ids) or "-"
        prior = "\n".join(f"- {p}" for p in self.prior_art) or NO_PRIOR_ART
        synth = (
            f"\n## Synthesis rationale\n{self.synthesis_rationale}\n"
            if self.synthesis_rationale
            else ""
        )
        return f"""## Problem
{self.problem}

## Proposal
{self.proposal}

## Prior art
{prior}

## Acceptance criteria
{ac}

## Verification plan
{vp}
{synth}
## Meta
- goals: {goals}
- size: {self.size}
- practice_ids: {practices}
"""

    def all_labels(self) -> list[str]:
        base = [f"size:{self.size}", *self.labels]
        return list(dict.fromkeys(base))  # dedupe, keep order


@dataclass
class WellFormedness:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def check_well_formed(
    title: str, body: str, *, require_prior_art: bool = False
) -> WellFormedness:
    """Is this ticket substantial enough to hand to an implementation agent?

    ``require_prior_art`` is opt-in on purpose: every ticket the planner files is
    grounded in the knowledge base and must carry its citations, but the open
    backlog predates the section, and those tickets stay claimable.
    """
    reasons: list[str] = []
    if require_prior_art and not PRIOR_ART_HEADING.search(body):
        reasons.append("missing '## Prior art' section")

    lowered = title.strip().lower()
    if any(lowered.startswith(p) for p in _EXEMPT_PREFIXES):
        return WellFormedness(ok=not reasons, reasons=reasons or ["exempt kind (docs/chore/heal)"])

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
