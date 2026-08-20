"""Adversarial second-opinion gate: a different model reviews the diff.

Until this module existed, the model that wrote a change was the only
intelligence that ever inspected it. Every other guard is a *shape* check -
``_requires_code`` counts non-knowledge files, the workflow guard matches a path
prefix, :mod:`hsai.repro` runs a test - so a plausible-looking but wrong change
merged as long as ruff and pytest were green.

The gate runs after local CI passes and BEFORE a PR is opened:

1. a reviewer model on a **different tier** than the author (see
   :func:`hsai.models.select_reviewer`) is shown the ticket, its parsed
   acceptance criteria, and the branch diff,
2. it must answer with a fenced JSON ``ReviewVerdict``; :func:`parse_verdict` is
   deliberately *fail-closed* - prose, garbage or silence is a non-approval,
3. a blocking verdict routes through the orchestrator's existing
   ``_recover_failed`` retry policy, so there is no new stall state,
4. every run appends a ``kind='review'`` ledger record, so the second opinion is
   metered like any other spend, and a hard budget breach skips it rather than
   deadlocking a budget-exhausted block.

All model work goes through :mod:`hsai.ai`, so the gate stays subscription-only.

Synthesis: microsoft/semantic-kernel (review as a gate distinct from build/test),
assafelovic/gpt-researcher (machine-parseable pass/fail quality contract),
FoundationAgents/MetaGPT (reviewer role separated from the engineer role), and
OpenBMB/ChatDev (review phases run on cheaper agents).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops, ledger
from .ai import run_agent
from .config import CoreConfig
from .models import ModelChoice, select_reviewer
from .proc import Runner, run
from .tickets import ACCEPTANCE_HEADING, CHECKBOX

# The last fenced JSON *object* in the reply is the verdict (prose around it is
# tolerated, exactly as in synthesis.parse_ticket_specs).
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_CHECKBOX_PREFIX = re.compile(r"^\s*-\s*\[[ xX]?\]\s+")
_NEXT_HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)

# Recognisable opening line: the reviewer prompt is never the worker prompt.
PROMPT_MARKER = "You are the INDEPENDENT REVIEWER"

FAIL_CLOSED = "reviewer produced no parseable verdict JSON block"

DEFAULT_MAX_BLOCKING = 5
DEFAULT_MAX_DIFF_CHARS = 20000
DEFAULT_TIMEOUT = 900.0


@dataclass
class ReviewVerdict:
    """One reviewer's answer. ``approve`` is the only thing that opens a PR."""

    approve: bool
    blocking: list[str] = field(default_factory=list)
    advisory: list[str] = field(default_factory=list)
    rationale: str = ""
    reviewer_model: str = ""
    reviewer_tier: str = ""
    skipped: bool = False  # the gate did not run (disabled / budget / red CI)

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        return "approve" if self.approve else "blocked"

    def summary(self) -> str:
        """One line, for iteration notes and ``IterationResult.describe()``."""
        who = self.reviewer_model or "-"
        if self.skipped:
            return f"skipped ({self.rationale})" if self.rationale else "skipped"
        return f"{self.status} by `{who}` ({len(self.blocking)} blocking)"

    def render(self) -> str:
        """The verdict verbatim, for the PR body and the lesson (G2)."""
        if self.skipped:
            return f"_(not run: {self.rationale or 'no reason recorded'})_"
        head = "**APPROVED**" if self.approve else "**BLOCKED**"
        lines = [
            f"- verdict: {head}",
            f"- reviewer: `{self.reviewer_model or '-'}` "
            f"(tier: `{self.reviewer_tier or '-'}`)",
        ]
        blocking = "\n".join(f"- {b}" for b in self.blocking) or "- _(none)_"
        advisory = "\n".join(f"- {a}" for a in self.advisory) or "- _(none)_"
        return (
            "\n".join(lines)
            + f"\n\n**Blocking findings**\n{blocking}"
            + f"\n\n**Advisory findings**\n{advisory}"
            + f"\n\n**Rationale**\n{self.rationale or '_(none given)_'}"
        )


def skip_review(reason: str) -> ReviewVerdict:
    """A verdict for a gate that deliberately did not run.

    Approving on purpose: the gate is additive, so it must never be able to
    *stop* an iteration by being unavailable. The reason is recorded either way.
    """
    return ReviewVerdict(approve=True, rationale=reason, skipped=True)


def is_enabled(cfg: CoreConfig) -> bool:
    return bool(cfg.review.get("enabled", True))


def acceptance_criteria(body: str) -> list[str]:
    """The ticket's acceptance-criteria checkboxes, in order.

    Reuses :data:`hsai.tickets.CHECKBOX` so "what a ticket promises" is parsed
    in exactly one way across the well-formedness gate and this reviewer.
    """
    heading = ACCEPTANCE_HEADING.search(body)
    region = body[heading.end():] if heading else body
    nxt = _NEXT_HEADING.search(region)
    if nxt:
        region = region[: nxt.start()]
    return [
        _CHECKBOX_PREFIX.sub("", line).strip()
        for line in region.splitlines()
        if CHECKBOX.match(line)
    ]


def build_prompt(
    *,
    ticket_title: str,
    ticket_body: str,
    criteria: list[str],
    paths: list[str],
    diff: str,
    author: ModelChoice,
) -> str:
    """The reviewer's instruction: strict, evidence-first, JSON-terminated."""
    listed = "\n".join(f"- {c}" for c in criteria) or "- _(none parsed from the ticket)_"
    touched = "\n".join(f"- {p}" for p in paths) or "- _(no files reported)_"
    return f"""{PROMPT_MARKER} for ai-hyperswarm-proto-core, an autonomous
self-improving AI-swarm harness. A DIFFERENT model (`{author.model}`, tier
`{author.tier}`) wrote the change below. You did not write it; do not defend it.
Your job is to decide whether it may open a pull request.

Judge exactly three things, in this order:
1. CORRECTNESS - does the diff actually do what the ticket asks, without
   breaking an existing invariant of the loop (ticket-linked PRs, green-gated
   merges, subscription-only model usage, append-only ledger)?
2. COVERAGE - is every acceptance criterion below genuinely satisfied by code
   in this diff, with tests as evidence where the criterion implies them?
3. SCOPE - is the change the smallest correct one, free of unrelated edits?

Ticket: {ticket_title}

{ticket_body}

Acceptance criteria to check off:
{listed}

Files touched on this branch:
{touched}

Branch diff:
```diff
{diff or "(empty diff)"}
```

Rules for your verdict:
- BLOCK only on a defect you can point at in the diff (name the file, and what
  is wrong). Blocking is expensive: it closes the branch and costs the ticket a
  retry. Style opinions, speculation, and "could also do X" are ADVISORY.
- If the diff satisfies the ticket and you have no evidence-backed defect,
  APPROVE - even if you would have written it differently.
- If `approve` is false, `blocking` MUST be non-empty and each entry MUST be
  actionable by the next attempt.

Answer with prose if you like, but END your reply with a fenced ```json block
containing exactly this object:
{{"approve": true or false,
  "blocking": ["..."],
  "advisory": ["..."],
  "rationale": "2-4 sentences justifying the verdict"}}
"""


def parse_verdict(output: str) -> ReviewVerdict:
    """Extract the verdict from a reviewer's reply - fail-closed.

    Anything we cannot read as a verdict object (prose only, truncated output,
    an empty reply, a crashed CLI) is a NON-approval: the gate refuses to let a
    change through on the strength of output it did not understand. An
    ``approve: true`` that still lists blocking findings is also a non-approval;
    the findings are the evidence, the flag is only a claim.
    """
    text = (output or "").strip()
    blocks = _JSON_BLOCK.findall(text)
    if not blocks and text.startswith("{") and text.endswith("}"):
        blocks = [text]  # a reviewer that answered with bare JSON
    if not blocks:
        return ReviewVerdict(approve=False, blocking=[FAIL_CLOSED], rationale=FAIL_CLOSED)
    try:
        raw = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return ReviewVerdict(approve=False, blocking=[FAIL_CLOSED], rationale=FAIL_CLOSED)
    if not isinstance(raw, dict):
        return ReviewVerdict(approve=False, blocking=[FAIL_CLOSED], rationale=FAIL_CLOSED)

    blocking = [str(b).strip() for b in _as_list(raw.get("blocking")) if str(b).strip()]
    advisory = [str(a).strip() for a in _as_list(raw.get("advisory")) if str(a).strip()]
    approve = raw.get("approve") is True and not blocking
    rationale = str(raw.get("rationale", "")).strip()
    if not approve and not blocking:
        blocking = ["reviewer withheld approval without naming a blocking finding"]
    return ReviewVerdict(
        approve=approve, blocking=blocking, advisory=advisory, rationale=rationale
    )


def _as_list(value: object) -> list:
    """Tolerate a reviewer that answered with a bare string instead of a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def review_change(
    cfg: CoreConfig,
    *,
    repo_root: str | Path,
    wt: str,
    base_ref: str,
    ticket_title: str,
    ticket_body: str,
    author: ModelChoice,
    iteration: int = 0,
    block: int = 0,
    ticket: int | None = None,
    attempts: int = 1,
    runner: Runner = run,
    ai_runner: Runner = run,
) -> ReviewVerdict:
    """Run the gate over the branch at ``wt`` and return its verdict.

    Skips (approving, with the reason recorded) when the gate is disabled or the
    block is in a hard budget breach - a gate that could halt a
    quota-exhausted block would be a deadlock, not a guardrail. Every run that
    does spend quota appends a ``kind='review'`` ledger record, so the second
    opinion shows up in the block aggregate and counts against ``cfg.budget``.
    """
    if not is_enabled(cfg):
        return skip_review("independent review disabled in cfg.review")

    try:
        records = ledger.read_records(ledger.ledger_path(cfg, repo_root))
    except (OSError, ValueError):
        # An unreadable ledger must not decide whether a change gets reviewed;
        # grade the block as unspent and let the review run.
        records = []
    decision = ledger.evaluate_budget(ledger.aggregate_block(records, block), cfg.budget)
    if decision.halt:
        return skip_review(f"hard budget breach ({decision.reason})")

    choice = select_reviewer(author, cfg)
    paths = gitops.diff_paths(base_ref, cwd=wt, runner=runner)
    diff = gitops.diff_text(base_ref, cwd=wt, runner=runner)
    max_chars = int(cfg.review.get("max_diff_chars", DEFAULT_MAX_DIFF_CHARS))
    if max_chars and len(diff) > max_chars:
        diff = diff[:max_chars] + "\n... (diff truncated for review)"

    prompt = build_prompt(
        ticket_title=ticket_title,
        ticket_body=ticket_body,
        criteria=acceptance_criteria(ticket_body),
        paths=paths,
        diff=diff,
        author=author,
    )
    started = time.time()
    ares = run_agent(
        prompt, choice, cfg, cwd=wt, runner=ai_runner,
        timeout=float(cfg.review.get("timeout_seconds", DEFAULT_TIMEOUT)),
    )
    # A reviewer that crashed produced no verdict, which is fail-closed too.
    verdict = parse_verdict(ares.text if ares.ok else "")
    if not ares.ok and ares.error:
        verdict.blocking.append(f"reviewer run failed: {ares.error[:200]}")
    verdict.reviewer_model = choice.model
    verdict.reviewer_tier = choice.tier

    cap = int(cfg.review.get("max_blocking_findings", DEFAULT_MAX_BLOCKING))
    if cap and len(verdict.blocking) > cap:
        dropped = len(verdict.blocking) - cap
        verdict.blocking = [
            *verdict.blocking[:cap],
            f"... {dropped} further blocking finding(s) elided (max_blocking_findings={cap})",
        ]

    tokens = ledger.parse_tokens(ares.payload)
    ledger.append_record(
        ledger.ledger_path(cfg, repo_root),
        ledger.LedgerRecord(
            iteration=iteration,
            block=block,
            ticket=ticket,
            kind="review",
            tier=choice.tier,
            model=choice.model,
            wall_clock_seconds=round(max(0.0, time.time() - started), 3),
            attempts=attempts,
            outcome=verdict.status,
            input_tokens=tokens[0] if tokens else None,
            output_tokens=tokens[1] if tokens else None,
            # No routing features: a reviewer tier comes from `review.tier_policy`,
            # not from the scored router, so this record is deliberately NOT a
            # training example for hsai.calibrate - only the strategy id is kept.
            strategy=choice.strategy,
        ),
    )
    return verdict
