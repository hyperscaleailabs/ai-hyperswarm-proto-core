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
CHECKBOX = re.compile(r"^\s*-\s*\[[ xX]?\]\s+\S", re.MULTILINE)

NEEDS_REFINEMENT = "needs-refinement"
SIZE_LABELS = ("size:S", "size:M", "size:L")

# How a filed ticket names the reference-set practices it adopts. Written by
# `TicketSpec.render`, read back by `practice_ids_in` when the orchestrator
# records the lesson - so the label is a contract, not decoration.
PRACTICE_IDS_LABEL = "**Practice IDs** (see `knowledge/reference/`)"
_PRACTICE_IDS_RE = re.compile(
    r"^\*\*Practice IDs\*\*[^\n:]*:(.+)$", re.IGNORECASE | re.MULTILINE
)
_BACKTICKED_RE = re.compile(r"`([^`]+)`")

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
    practice_ids: tuple[str, ...] = ()  # observed practices this ticket adopts
    labels: tuple[str, ...] = ()

    def render(self) -> str:
        ac = "\n".join(f"- [ ] {c}" for c in self.acceptance_criteria)
        vp = "\n".join(f"- [ ] {v}" for v in self.verification_plan)
        goals = ", ".join(self.goal_ids) or "-"
        # Named practices, not bare repo slugs: this is the line the orchestrator
        # reads back so a merged PR's lesson can say WHICH observed practice it
        # adopted (G1/G2). Keep the label stable - `practice_ids_in` parses it.
        practices = (
            f"\n\n{PRACTICE_IDS_LABEL}: "
            + ", ".join(f"`{p}`" for p in self.practice_ids)
            if self.practice_ids
            else ""
        )
        synth = (
            f"\n## Synthesis rationale\n{self.synthesis_rationale}{practices}\n"
            if self.synthesis_rationale or practices
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
{synth}
## Meta
- goals: {goals}
- size: {self.size}
"""

    def all_labels(self) -> list[str]:
        base = [f"size:{self.size}", *self.labels]
        return list(dict.fromkeys(base))  # dedupe, keep order


def practice_ids_in(body: str) -> tuple[str, ...]:
    """The practice_ids a filed ticket claims, in order, deduped.

    Tolerant by design: a ticket a human wrote (or one filed before practice ids
    existed) simply yields ``()`` rather than failing the run.
    """
    ids: list[str] = []
    for line in _PRACTICE_IDS_RE.findall(body):
        for pid in _BACKTICKED_RE.findall(line):
            if pid not in ids:
                ids.append(pid)
    return tuple(ids)


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
