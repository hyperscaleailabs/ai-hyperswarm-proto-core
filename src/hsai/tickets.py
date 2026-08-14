"""Structured tickets: schema, rendering, and the well-formedness gate.

A ticket is *well-formed* when it carries real acceptance criteria and a
verification plan. The orchestrator refuses to implement malformed feature /
improvement tickets - it labels them ``needs-refinement`` instead - so vague
one-liners can never again be "satisfied" by a trivial diff.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
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
    labels: tuple[str, ...] = ()

    def render(self) -> str:
        ac = "\n".join(f"- [ ] {c}" for c in self.acceptance_criteria)
        vp = "\n".join(f"- [ ] {v}" for v in self.verification_plan)
        goals = ", ".join(self.goal_ids) or "-"
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
{synth}
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


SYNTHESIS_HEADING = re.compile(
    r"^#{2,3}\s*synthesis rationale\s*$", re.IGNORECASE | re.MULTILINE
)
_ANY_HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)
# Code spans hold identifiers and paths (`src/hsai/ai.py`, `top10[:3]`), never
# citations: stripping them first is what keeps a path from being read as a
# project name. Cited projects are named in prose, by convention and by schema.
_CODE_SPAN = re.compile(r"`[^`]*`")
_PROJECT = re.compile(r"(?<![\w./-])([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)(?![\w/])")


def synthesis_rationale(body: str) -> str:
    """The ``## Synthesis rationale`` section of a ticket body (``""`` if absent)."""
    match = SYNTHESIS_HEADING.search(body or "")
    if not match:
        return ""
    rest = (body or "")[match.end():]
    nxt = _ANY_HEADING.search(rest)
    return (rest[: nxt.start()] if nxt else rest).strip()


def cited_projects(body: str, known: Sequence[str] = ()) -> tuple[str, ...]:
    """The reference-set projects a ticket actually cites, in first-mention order.

    This is the provenance the lesson and the PR report as reference-set
    evidence. It used to be ``reference_top10[:3]`` - a constant, presented as
    if it were a citation - which is fabricated provenance and exactly what
    goals G1 and G2 forbid. A ticket that cites nothing now yields ``()`` and
    renders as an explicit no-provenance marker instead.

    ``known`` (the configured reference set) is an allow-list: prose contains
    incidental slashes (``and/or``, ``input/output``) that are shaped like a
    repo slug but are not one. Passing nothing disables the filter, which is
    what makes the parse itself testable.
    """
    prose = _CODE_SPAN.sub(" ", synthesis_rationale(body))
    allowed = {k.lower(): k for k in known}
    cited: list[str] = []
    for match in _PROJECT.finditer(prose):
        name = match.group(1).rstrip(".,;:")
        if allowed:
            name = allowed.get(name.lower(), "")
        if name and name not in cited:
            cited.append(name)
    return tuple(cited)


def size_of(issue: Issue) -> str:
    """Read the size label off an issue ('M' when unlabeled)."""
    for lbl in issue.labels:
        if lbl.startswith("size:"):
            return lbl.split(":", 1)[1]
    return "M"
