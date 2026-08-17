"""Structured tickets: schema, rendering, and the well-formedness gate.

A ticket is *well-formed* when it carries real acceptance criteria and a
verification plan. The orchestrator refuses to implement malformed feature /
improvement tickets - it labels them ``needs-refinement`` instead - so vague
one-liners can never again be "satisfied" by a trivial diff.

A ticket the *synthesizer* files carries one further obligation: a ``prior_art``
citation naming at least one internal artifact (a vault note, a ticket number, a
ledger figure). :func:`check_prior_art` enforces it. That gate deliberately sits
on :class:`TicketSpec` rather than in :func:`check_well_formed`: the latter also
grades tickets a human or an earlier cycle wrote, and retroactively demanding a
citation from them would relabel the whole standing backlog ``needs-refinement``.
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

# The citation forms this repo can actually resolve later. Prose like "we learned
# this last block" is not evidence anyone can follow, so it does not count.
INTERNAL_REF_RE = re.compile(
    r"\[\[[^\]\n]+\]\]"                # [[note-name]]      a lesson, whitepaper or ADR
    r"|#\d+"                           # #123               a GitHub ticket
    r"|ledger/block-\d+"               # ledger/block-41335 a quota-ledger aggregate
    r"|(?:knowledge|docs)/[\w./-]+"    # knowledge/...      a committed artifact path
)
CITATION_FORMS = "[[note-name]], #123, ledger/block-N, or knowledge/... | docs/..."

# How a re-proposal of a previously-failed idea states its difference.
WHAT_CHANGED_LABEL = "**What changed since the prior attempt:**"

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
    prior_art: tuple[str, ...] = ()  # internal artifacts that motivated this ticket
    what_changed: str = ""  # only when re-proposing a previously-failed idea
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
        art = f"\n## Prior art (internal evidence)\n{self.render_prior_art()}\n"
        return f"""## Problem
{self.problem}

## Proposal
{self.proposal}

## Acceptance criteria
{ac}

## Verification plan
{vp}
{art}{synth}
## Meta
- goals: {goals}
- size: {self.size}
"""

    def render_prior_art(self) -> str:
        """The citations, plus the re-proposal justification when there is one."""
        cited = "\n".join(f"- {c}" for c in self.prior_art) or "- _(none cited)_"
        if self.what_changed:
            cited += f"\n\n{WHAT_CHANGED_LABEL} {self.what_changed}"
        return cited

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


def internal_refs(text: str) -> list[str]:
    """Every resolvable internal citation in ``text`` (see :data:`INTERNAL_REF_RE`)."""
    return INTERNAL_REF_RE.findall(text or "")


def check_prior_art(prior_art: tuple[str, ...] | list[str]) -> WellFormedness:
    """Does this ticket cite internal evidence a reader can actually follow?

    The planner is asked to combine >= 3 reference projects *and* to ground the
    idea in something this loop observed about itself; without the second half a
    ticket is a plausible-sounding import with no traceable motivation (G1/G2).
    """
    entries = [str(c).strip() for c in prior_art if str(c).strip()]
    if not entries:
        return WellFormedness(
            ok=False,
            reasons=[
                "missing 'prior_art': cite >= 1 internal artifact "
                f"({CITATION_FORMS})"
            ],
        )
    if not any(internal_refs(entry) for entry in entries):
        return WellFormedness(
            ok=False,
            reasons=[
                f"'prior_art' cites no resolvable internal artifact; use {CITATION_FORMS}"
            ],
        )
    return WellFormedness(ok=True)


def validate_spec(spec: TicketSpec) -> WellFormedness:
    """The full schema gate for a spec about to be filed by the synthesizer."""
    reasons: list[str] = []
    body = check_well_formed(spec.title, spec.render())
    if not body.ok:
        reasons += body.reasons
    art = check_prior_art(spec.prior_art)
    if not art.ok:
        reasons += art.reasons
    return WellFormedness(ok=not reasons, reasons=reasons)


def issue_well_formed(issue: Issue) -> WellFormedness:
    return check_well_formed(issue.title, issue.body)


def size_of(issue: Issue) -> str:
    """Read the size label off an issue ('M' when unlabeled)."""
    for lbl in issue.labels:
        if lbl.startswith("size:"):
            return lbl.split(":", 1)[1]
    return "M"
