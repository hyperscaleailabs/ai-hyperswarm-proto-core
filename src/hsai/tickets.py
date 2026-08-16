"""Structured tickets: schema, rendering, and the well-formedness gate.

A ticket is *well-formed* when it carries real acceptance criteria and a
verification plan. The orchestrator refuses to implement malformed feature /
improvement tickets - it labels them ``needs-refinement`` instead - so vague
one-liners can never again be "satisfied" by a trivial diff.

A synthesized ticket carries one further obligation: :func:`check_spec` refuses
a :class:`TicketSpec` whose ``prior_art`` does not cite at least one *internal*
artifact by ref - a vault note, a ticket number, or a ledger figure. External
inspiration (the reference projects, in ``synthesis_rationale``) says where an
idea came from; prior art says what in our own record motivated it, and which
evidence a reviewer can check it against.
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

# Kinds of tickets exempt from the substantial-schema gate (docs and chores may
# be legitimately small; heal tickets are filed by the loop itself mid-incident).
_EXEMPT_PREFIXES = ("docs:", "chore:", "ci: main is red")

PRIOR_ART_HEADING = "Prior art (internal evidence)"

# The three shapes an internal citation can take. Deliberately narrow: a claim
# like "we learned this before" cites nothing a reviewer can open, whereas each
# of these resolves to an artifact in the repo or the backlog.
_WIKILINK_CITATION = re.compile(r"\[\[[^\[\]]+\]\]")
_ISSUE_CITATION = re.compile(r"#\d+")
# A ledger figure: the word "ledger" and a number close enough to be one claim
# ("ledger: 1425s per merged PR", "the ledger shows 3 heavy runs this block").
_LEDGER_CITATION = re.compile(r"ledger\b[^\n]{0,120}?\d+", re.IGNORECASE)

# A re-proposal of a previously failed idea has to say what is different this
# time; without it, citing the failure is just an acknowledgement, not a plan.
WHAT_CHANGED = re.compile(r"what changed\s*[:\-]", re.IGNORECASE)


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
    prior_art: str = ""  # which of OUR artifacts motivated this, cited by ref
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
        prior = (
            f"\n## {PRIOR_ART_HEADING}\n{self.prior_art}\n" if self.prior_art else ""
        )
        return f"""## Problem
{self.problem}

## Proposal
{self.proposal}

## Acceptance criteria
{ac}

## Verification plan
{vp}
{prior}{synth}
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


def prior_art_citations(text: str) -> list[str]:
    """Every internal artifact ``text`` cites, in order of appearance.

    Pure and side-effect free: this is the one place "did this ticket cite our
    own evidence?" is decided, so the synthesis gate and any future consumer
    agree on what counts.
    """
    found: list[tuple[int, str]] = []
    for pattern in (_WIKILINK_CITATION, _ISSUE_CITATION, _LEDGER_CITATION):
        found.extend((m.start(), m.group(0)) for m in pattern.finditer(text or ""))
    return [ref for _, ref in sorted(found)]


def check_spec(spec: TicketSpec) -> WellFormedness:
    """Is this synthesized spec fileable?

    The schema gate for machine-authored tickets, applied *before* anything is
    filed. It is stricter than :func:`check_well_formed` (which grades tickets
    already on the backlog, including human-written ones) in exactly one way:
    a synthesized ticket must ground itself in our own record.
    """
    reasons: list[str] = []
    if not spec.title.strip():
        reasons.append("empty title")
    if len(spec.acceptance_criteria) < 2:
        reasons.append(
            f"needs >= 2 acceptance criteria (found {len(spec.acceptance_criteria)})"
        )
    if not spec.verification_plan:
        reasons.append("empty verification plan")
    if not spec.prior_art.strip():
        reasons.append("empty 'prior_art': no internal evidence cited")
    elif not prior_art_citations(spec.prior_art):
        reasons.append(
            "'prior_art' cites no internal artifact - expected a [[note-name]], "
            "a #ticket number, or a ledger figure"
        )
    return WellFormedness(ok=not reasons, reasons=reasons)


def issue_well_formed(issue: Issue) -> WellFormedness:
    return check_well_formed(issue.title, issue.body)


def size_of(issue: Issue) -> str:
    """Read the size label off an issue ('M' when unlabeled)."""
    for lbl in issue.labels:
        if lbl.startswith("size:"):
            return lbl.split(":", 1)[1]
    return "M"
