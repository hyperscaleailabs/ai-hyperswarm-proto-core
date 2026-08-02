"""Adversarial acceptance-criteria review gate, run before a PR is opened.

:mod:`hsai.tickets` makes every substantial ticket carry ``## Acceptance
criteria`` checkboxes, and the worker prompt demands that all of them be
satisfied - but until this module existed nothing ever *checked*. The pre-PR
guards only proved that some non-``knowledge/`` file changed, that no workflow
was edited, and (for heal/bugfix work) that a regression test flipped. A worker
could satisfy all three while implementing two of five criteria.

This module adds the missing role: an independent REVIEWER that runs after the
worker and before commit/push.

- **Separate role** (OpenBMB/ChatDev): the agent that wrote the code is never
  the agent that signs it off, and the reviewer is pinned one tier *below* the
  implementation tier - never ``heavy`` - so a critique always costs materially
  less than the work it critiques.
- **Blocking, not advisory** (microsoft/semantic-kernel's devflow-pr-review +
  merge-gatekeeper pair): an explicit ``FAIL`` routes the iteration into the
  existing recovery path with ``UNMET_CRITERIA``; no PR is opened, the ticket
  goes back to the backlog, and the attempt is counted.
- **Structured, re-validated output** (assafelovic/gpt-researcher): the verdict
  must arrive as a fenced JSON object and is normalised and re-validated rather
  than trusted as a string. Anything unparseable, timed out or errored fails
  **open** - a broken reviewer must never wedge the loop.
- **Persisted trajectory** (SWE-agent/SWE-agent): the review pack and the
  verdict are written under ``.hsai/reviews/`` with environment secrets
  redacted, so a blocked iteration can be audited after the worktree is gone.

Everything here is config-driven under the ``review:`` block of ``core.yaml``
(``enabled``, ``tier_offset``, ``timeout_seconds``, ``fail_open``,
``permission_mode``), so the gate can be switched off without a code change.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import gitops
from .ai import run_agent
from .config import CoreConfig
from .models import ModelChoice
from .proc import Runner, run
from .tickets import ACCEPTANCE_HEADING

# Overall review statuses.
PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
SKIPPED = "SKIPPED"

# Per-criterion statuses (the normalised form of the model's `met` field).
MET = "met"
UNMET = "unmet"
UNCLEAR = "unclear"

# The remote-slot marker recorded when the gate blocks an iteration.
UNMET_CRITERIA = "UNMET_CRITERIA"

# Tiers ordered cheap -> expensive. The reviewer never resolves to `heavy`.
_TIER_ORDER = ("light", "standard", "heavy")
_NEVER_TIER = "heavy"

REVIEWS_DIR = ".hsai/reviews"

ACCEPTANCE_REVIEW_HEADING = re.compile(
    r"^#{2,3}\s*acceptance review\s*$", re.IGNORECASE | re.MULTILINE
)

_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_CHECKBOX_LINE = re.compile(r"^\s*-\s*\[[ xX]?\]\s+(\S.*)$")
_JSON_OBJECT_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_ID_DIGITS = re.compile(r"(\d+)")

# Env vars whose *values* are scrubbed out of persisted artifacts. Matching by
# name keeps the artifact readable (paths and locales survive) while no token
# ever lands on disk.
_SECRET_NAME_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|SESSION|COOKIE)", re.IGNORECASE
)
_MIN_SECRET_LEN = 8

_MET_WORDS = {
    "true": MET, "met": MET, "yes": MET, "pass": MET, "passed": MET, "satisfied": MET,
    "false": UNMET, "unmet": UNMET, "not met": UNMET, "no": UNMET, "fail": UNMET,
    "failed": UNMET, "missing": UNMET,
    "unclear": UNCLEAR, "unknown": UNCLEAR, "partial": UNCLEAR, "partially": UNCLEAR,
    "maybe": UNCLEAR, "n/a": UNCLEAR,
}


# --- ticket parsing ---------------------------------------------------------

@dataclass(frozen=True)
class Criterion:
    """One documented acceptance-criterion checkbox, with a stable id."""

    id: str
    text: str


def parse_criteria(body: str) -> tuple[Criterion, ...]:
    """Extract the ``## Acceptance criteria`` checkboxes from a ticket body.

    Ids are positional (``AC1``, ``AC2``, ...) so the reviewer and the renderers
    agree on how to name a criterion without the ticket having to number them.
    """
    match = ACCEPTANCE_HEADING.search(body or "")
    if not match:
        return ()
    rest = (body or "")[match.end():]
    nxt = _HEADING_RE.search(rest)
    section = rest[: nxt.start()] if nxt else rest
    criteria: list[Criterion] = []
    for line in section.splitlines():
        line_match = _CHECKBOX_LINE.match(line)
        if line_match:
            criteria.append(
                Criterion(id=f"AC{len(criteria) + 1}", text=line_match.group(1).strip())
            )
    return tuple(criteria)


# --- redaction --------------------------------------------------------------

def redact(
    text: str,
    cfg: CoreConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Mask secret-shaped environment values before an artifact hits disk.

    Every variable listed in ``constraints.forbid_env`` is scrubbed, plus any
    whose name looks like a credential. Longest values are replaced first so a
    value that contains another is masked whole. Values shorter than
    ``_MIN_SECRET_LEN`` are left alone: no real credential is that short, and
    blind-replacing e.g. ``"1"`` would shred the artifact.
    """
    out = text or ""
    env = dict(os.environ if environ is None else environ)
    forbidden = set(cfg.forbidden_env) if cfg is not None else set()
    targets = [
        (name, value)
        for name, value in env.items()
        if len(value or "") >= _MIN_SECRET_LEN
        and (name in forbidden or _SECRET_NAME_RE.search(name))
    ]
    for name, value in sorted(targets, key=lambda kv: -len(kv[1])):
        if value in out:
            out = out.replace(value, f"***REDACTED:{name}***")
    return out


# --- the review pack --------------------------------------------------------

@dataclass
class ReviewPack:
    """Everything the reviewer is shown: ticket, criteria, diff, CI result."""

    ticket: int | None
    ticket_title: str
    ticket_body: str
    criteria: tuple[Criterion, ...]
    diff: str
    changed_paths: tuple[str, ...]
    ci_summary: str
    kind: str = ""
    diff_limit: int = 60_000

    def render(self) -> str:
        criteria = "\n".join(f"- {c.id}: {c.text}" for c in self.criteria) or "- _(none)_"
        paths = "\n".join(f"- `{p}`" for p in self.changed_paths) or "- _(none)_"
        diff = self.diff or "(empty diff)"
        if len(diff) > self.diff_limit:
            diff = diff[: self.diff_limit] + "\n... [diff truncated]"
        return f"""### Ticket #{self.ticket or '-'} ({self.kind or 'implement'})
{self.ticket_title}

{self.ticket_body}

### Acceptance criteria under review
{criteria}

### Changed paths
{paths}

### Local CI
{self.ci_summary}

### Diff against the merge base
```diff
{diff}
```"""

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "ticket_title": self.ticket_title,
            "ticket_body": self.ticket_body,
            "kind": self.kind,
            "criteria": [{"id": c.id, "text": c.text} for c in self.criteria],
            "changed_paths": list(self.changed_paths),
            "ci_summary": self.ci_summary,
            "diff": self.diff,
        }


def build_pack(
    *,
    cfg: CoreConfig,
    wt: str,
    ticket: int | None,
    ticket_title: str,
    ticket_body: str,
    kind: str,
    ci_summary: str,
    runner: Runner = run,
) -> ReviewPack:
    """Collect the worker's (still uncommitted) change into a review pack.

    The worker's edits are staged first so untracked new files appear in the
    diff; ``commit_all`` stages again later, so this is a no-op for the happy
    path and costs nothing on the blocked path (the worktree is discarded).
    """
    base = gitops.merge_base(
        "HEAD", f"origin/{cfg.default_branch}", cwd=wt, runner=runner
    ) or f"origin/{cfg.default_branch}"
    gitops.stage_all(cwd=wt, runner=runner)
    return ReviewPack(
        ticket=ticket,
        ticket_title=ticket_title,
        ticket_body=ticket_body,
        criteria=parse_criteria(ticket_body),
        diff=gitops.staged_diff(base, cwd=wt, runner=runner),
        changed_paths=tuple(gitops.changed_paths(cwd=wt, runner=runner)),
        ci_summary=ci_summary,
        kind=kind,
    )


def build_prompt(pack: ReviewPack) -> str:
    """The reviewer's instruction: adversarial, evidence-bound, JSON-terminated."""
    ids = ", ".join(c.id for c in pack.criteria) or "(none)"
    return f"""You are the independent ACCEPTANCE REVIEWER for
ai-hyperswarm-proto-core. You did NOT write this change and you are not here to
be agreeable. Another agent claims it implemented the ticket below. Your only
job is to decide, criterion by criterion, whether the diff actually delivers
what the ticket promised.

{pack.render()}

Rules:
- Judge ONLY against the documented acceptance criteria: {ids}.
- A criterion is `met` only if you can point at concrete evidence in this diff -
  a `path/to/file.py:123` location or a test name. Intentions, comments,
  documentation of a thing, and TODOs are not evidence that the thing exists.
- A criterion asking for a test is met only if a test that actually exercises it
  was added or modified.
- If the diff is inconclusive for a criterion, say `unclear` - do not guess `true`.
- Be adversarial: default to `false` when the evidence is thin.

End your reply with a single fenced ```json code block - it MUST be the last
fenced block - containing exactly this object:
{{
  "verdict": "PASS" or "FAIL",
  "criteria": [
    {{"id": "AC1", "met": true, "evidence": "src/hsai/foo.py:42"}},
    {{"id": "AC2", "met": false, "evidence": "no test covers this"}}
  ],
  "blocking_reasons": ["AC2: ... "]
}}

Report on every criterion id listed above, exactly once. "verdict" is "PASS"
only when no criterion is false. Put every reason the change must not ship in
"blocking_reasons"."""


# --- verdict parsing + strict validation ------------------------------------

@dataclass(frozen=True)
class CriterionVerdict:
    id: str
    met: str  # met | unmet | unclear
    evidence: str = ""
    text: str = ""


@dataclass(frozen=True)
class Verdict:
    """A validated reviewer verdict (or an ``INCONCLUSIVE`` shell with a reason)."""

    verdict: str
    criteria: tuple[CriterionVerdict, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    error: str = ""

    @property
    def unmet(self) -> tuple[CriterionVerdict, ...]:
        return tuple(c for c in self.criteria if c.met == UNMET)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "criteria": [
                {"id": c.id, "met": c.met, "evidence": c.evidence, "text": c.text}
                for c in self.criteria
            ],
            "blocking_reasons": list(self.blocking_reasons),
            "error": self.error,
        }


def _inconclusive(reason: str) -> Verdict:
    return Verdict(verdict=INCONCLUSIVE, error=reason)


def _normalise_id(value: object) -> str:
    """Map ``1`` / ``"ac1"`` / ``"AC-1"`` / ``"criterion 1"`` onto ``"AC1"``."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return f"AC{value}"
    digits = _ID_DIGITS.search(str(value))
    return f"AC{int(digits.group(1))}" if digits else ""


def _normalise_met(value: object) -> str | None:
    """Normalise the model's ``met`` field; ``None`` means 'not recognisable'."""
    if isinstance(value, bool):
        return MET if value else UNMET
    if isinstance(value, str):
        return _MET_WORDS.get(value.strip().lower())
    return None


def parse_verdict(output: str, criteria: Sequence[Criterion] = ()) -> Verdict:
    """Extract and strictly validate the reviewer's fenced JSON verdict.

    Every failure mode - no block, bad JSON, wrong shape, an unrecognised
    ``met`` value, or a criterion the reviewer silently skipped - returns
    ``INCONCLUSIVE`` with a reason rather than raising, so the caller can decide
    to fail open. The verdict string itself is re-derived from the per-criterion
    data: a "PASS" that reports an unmet criterion is corrected to ``FAIL``.
    """
    blocks = _JSON_OBJECT_BLOCK.findall(output or "")
    if not blocks:
        return _inconclusive("no fenced JSON object in the reviewer output")
    try:
        data = json.loads(blocks[-1])
    except (ValueError, TypeError) as exc:
        return _inconclusive(f"reviewer JSON did not parse: {exc}")
    if not isinstance(data, dict):
        return _inconclusive("reviewer JSON is not an object")

    claimed = str(data.get("verdict", "")).strip().upper()
    if claimed not in (PASS, FAIL):
        return _inconclusive(f"unrecognised verdict {data.get('verdict')!r}")

    raw_criteria = data.get("criteria")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        return _inconclusive("reviewer JSON has no 'criteria' array")

    reported: dict[str, CriterionVerdict] = {}
    for item in raw_criteria:
        if not isinstance(item, dict):
            return _inconclusive("a 'criteria' entry is not an object")
        cid = _normalise_id(item.get("id"))
        if not cid:
            return _inconclusive(f"unrecognised criterion id {item.get('id')!r}")
        met = _normalise_met(item.get("met"))
        if met is None:
            return _inconclusive(f"{cid}: unrecognised 'met' value {item.get('met')!r}")
        reported[cid] = CriterionVerdict(
            id=cid, met=met, evidence=str(item.get("evidence", "") or "").strip()
        )

    raw_blocking = data.get("blocking_reasons", [])
    if not isinstance(raw_blocking, list):
        return _inconclusive("'blocking_reasons' is not an array")
    blocking = [str(r).strip() for r in raw_blocking if str(r).strip()]

    if criteria:
        missing = [c.id for c in criteria if c.id not in reported]
        if missing:
            return _inconclusive(
                f"reviewer did not report on {', '.join(missing)}"
            )
        ordered = tuple(
            CriterionVerdict(
                id=c.id,
                met=reported[c.id].met,
                evidence=reported[c.id].evidence,
                text=c.text,
            )
            for c in criteria
        )
    else:
        ordered = tuple(reported[k] for k in sorted(reported, key=_sort_key))

    # Re-validate rather than trust the string: an unmet criterion is a FAIL
    # regardless of what the model typed into "verdict".
    unmet = [c for c in ordered if c.met == UNMET]
    verdict = FAIL if unmet else claimed
    if verdict == FAIL and not blocking:
        blocking = [
            f"{c.id}: {c.text or 'criterion'} - {c.evidence or 'no evidence cited'}"
            for c in unmet
        ] or ["reviewer returned FAIL without naming a reason"]
    return Verdict(verdict=verdict, criteria=ordered, blocking_reasons=tuple(blocking))


def _sort_key(cid: str) -> tuple[int, str]:
    digits = _ID_DIGITS.search(cid)
    return (int(digits.group(1)) if digits else 0, cid)


# --- reviewer tier selection ------------------------------------------------

def reviewer_tier(cfg: CoreConfig, impl_tier: str) -> str:
    """Resolve the reviewer's tier: ``tier_offset`` steps below the work, never heavy.

    The critique must cost materially less than the work it critiques, so
    ``heavy`` is excluded structurally - no ``tier_offset`` value can reach it.
    """
    offset = max(0, int(cfg.review.get("tier_offset", 1)))
    try:
        idx = _TIER_ORDER.index(impl_tier)
    except ValueError:
        idx = _TIER_ORDER.index(cfg.default_tier) if cfg.default_tier in _TIER_ORDER else 1
    idx = max(0, idx - offset)
    allowed = [t for t in _TIER_ORDER[: idx + 1] if t != _NEVER_TIER and t in cfg.tiers]
    if allowed:
        return allowed[-1]
    fallback = next((t for t in _TIER_ORDER if t != _NEVER_TIER and t in cfg.tiers), "")
    return fallback or cfg.default_tier


def reviewer_choice(cfg: CoreConfig, impl_tier: str) -> ModelChoice:
    tier = reviewer_tier(cfg, impl_tier)
    model = cfg.tiers[tier].model if tier in cfg.tiers else cfg.tiers[cfg.default_tier].model
    return ModelChoice(
        tier=tier,
        model=model,
        rationale=(
            f"acceptance review runs {tier} - pinned below the implementation tier "
            f"({impl_tier}) and never heavy, so the critique costs less than the work"
        ),
        strategy="review-v1",
    )


# --- rendering --------------------------------------------------------------

def _cell(text: str) -> str:
    return (text or "").replace("|", r"\|").replace("\n", " ").strip()


def render_table(verdict: Verdict | None) -> str:
    """The per-criterion table embedded in the PR body and the lesson."""
    if verdict is None or not verdict.criteria:
        return "_(no per-criterion verdict recorded)_"
    header = "| id | criterion | status | evidence |\n| --- | --- | --- | --- |"
    rows = "\n".join(
        f"| {c.id} | {_cell(c.text) or '_(text unavailable)_'} | **{c.met}** | "
        f"{_cell(c.evidence) or '_(none cited)_'} |"
        for c in verdict.criteria
    )
    return f"{header}\n{rows}"


# --- the gate itself --------------------------------------------------------

@dataclass
class ReviewOutcome:
    """The gate's decision for one iteration, plus what it cost."""

    status: str  # PASS | FAIL | INCONCLUSIVE | SKIPPED
    reason: str = ""
    verdict: Verdict | None = None
    tier: str = ""
    model: str = ""
    seconds: float = 0.0
    artifact: str = ""
    pack: ReviewPack | None = None

    @property
    def blocks(self) -> bool:
        """Only an explicit FAIL stops the iteration; everything else proceeds."""
        return self.status == FAIL

    def render_section(self) -> str:
        """The ``## Acceptance review`` body shared by the PR and the lesson."""
        if self.status == SKIPPED:
            return f"_(not applicable: {self.reason})_"
        head = (
            f"- **verdict**: `{self.status}`\n"
            f"- **reviewer**: `{self.model or '-'}` (tier: `{self.tier or '-'}`, "
            f"{self.seconds:.1f}s)"
        )
        if self.status == INCONCLUSIVE:
            return (
                f"{head}\n- review was **inconclusive** and failed open: "
                f"{self.reason or 'no reason recorded'}"
            )
        blocking = ""
        if self.verdict and self.verdict.blocking_reasons:
            blocking = "\n\nBlocking reasons:\n" + "\n".join(
                f"- {r}" for r in self.verdict.blocking_reasons
            )
        return f"{head}\n\n{render_table(self.verdict)}{blocking}"


def persist_review(
    repo_root: str | Path,
    outcome: ReviewOutcome,
    cfg: CoreConfig,
    *,
    iteration: int,
    ticket: int | None,
    raw_output: str = "",
) -> Path:
    """Write the redacted review pack + verdict as a trajectory artifact.

    Lives at the repo root (not the ephemeral worktree) so a blocked iteration
    remains auditable after its worktree is torn down.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    directory = Path(repo_root) / REVIEWS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}-iter{iteration}-ticket{ticket or 0}.json"
    payload = {
        "iteration": iteration,
        "ticket": ticket,
        "created": datetime.now(timezone.utc).isoformat(),
        "status": outcome.status,
        "reason": outcome.reason,
        "reviewer": {
            "tier": outcome.tier,
            "model": outcome.model,
            "seconds": round(outcome.seconds, 3),
        },
        "pack": outcome.pack.to_dict() if outcome.pack else {},
        "verdict": outcome.verdict.to_dict() if outcome.verdict else {},
        "raw_output": raw_output,
    }
    path.write_text(redact(json.dumps(payload, indent=2, sort_keys=True), cfg))
    return path


def run_review(
    cfg: CoreConfig,
    *,
    repo_root: str,
    wt: str,
    ticket: int | None,
    ticket_title: str,
    ticket_body: str,
    kind: str,
    ci_summary: str,
    impl_tier: str,
    iteration: int = 0,
    runner: Runner = run,
    ai_runner: Runner = run,
) -> ReviewOutcome:
    """Review the worker's change against the ticket's acceptance criteria.

    Never raises: a disabled gate, a criteria-free ticket, a reviewer that
    errored or timed out, and unparseable output all return a non-blocking
    outcome. Only a validated ``FAIL`` blocks.
    """
    if not cfg.review.get("enabled", True):
        return ReviewOutcome(status=SKIPPED, reason="review.enabled is false")

    criteria = parse_criteria(ticket_body)
    if not criteria:
        return ReviewOutcome(
            status=SKIPPED, reason="ticket carries no documented acceptance criteria"
        )

    pack = build_pack(
        cfg=cfg, wt=wt, ticket=ticket, ticket_title=ticket_title,
        ticket_body=ticket_body, kind=kind, ci_summary=ci_summary, runner=runner,
    )
    choice = reviewer_choice(cfg, impl_tier)
    fail_open = bool(cfg.review.get("fail_open", True))

    started = time.time()
    raw_output = ""
    error = ""
    try:
        ares = run_agent(
            build_prompt(pack), choice, cfg, cwd=wt,
            permission_mode=cfg.review.get("permission_mode", "plan"),
            timeout=float(cfg.review.get("timeout_seconds", 600)),
            runner=ai_runner,
        )
        raw_output = ares.output
        error = "" if ares.ok else (ares.error or "reviewer exited non-zero")
    # Deliberately broad: a broken reviewer must never wedge the loop.
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    seconds = round(max(0.0, time.time() - started), 3)

    outcome = ReviewOutcome(
        status=INCONCLUSIVE, tier=choice.tier, model=choice.model,
        seconds=seconds, pack=pack,
    )
    if error:
        outcome.reason = f"reviewer run failed or timed out: {error[:300]}"
    else:
        verdict = parse_verdict(raw_output, criteria)
        outcome.verdict = verdict
        outcome.status = verdict.verdict
        outcome.reason = verdict.error or "; ".join(verdict.blocking_reasons)

    # fail_open: false turns an inconclusive review into a block (opt-in; the
    # default keeps a broken reviewer from wedging the loop).
    if outcome.status == INCONCLUSIVE and not fail_open:
        outcome.status = FAIL
        outcome.reason = f"review inconclusive and review.fail_open is false: {outcome.reason}"

    outcome.artifact = str(
        persist_review(
            repo_root, outcome, cfg, iteration=iteration, ticket=ticket,
            raw_output=raw_output,
        )
    )
    return outcome


# --- CI-side evidence gate --------------------------------------------------

@dataclass
class EvidenceResult:
    """Whether a PR body carries the evidence the SDLC requires."""

    ok: bool
    reasons: list[str] = field(default_factory=list)


def check_pr_evidence(pr_body: str, *, ticket_body: str = "") -> EvidenceResult:
    """The CI evidence step, in code.

    Enforces the long-standing invariants (ticket link, model, lesson) and adds
    the new one: a PR closing a ticket that carries acceptance criteria must
    show an ``## Acceptance review`` section with a row per criterion.
    """
    body = pr_body or ""
    reasons: list[str] = []
    if not re.search(r"closes\s+#\d+", body, re.IGNORECASE):
        reasons.append("PR body missing 'Closes #N' ticket link")
    if not re.search(r"^#{2,3}\s*model used\s*$", body, re.IGNORECASE | re.MULTILINE):
        reasons.append("PR body missing '## Model used' section")
    if not re.search(r"^#{2,3}\s*lesson learned\s*$", body, re.IGNORECASE | re.MULTILINE):
        reasons.append("PR body missing '## Lesson learned' section")

    criteria = parse_criteria(ticket_body)
    if criteria:
        match = ACCEPTANCE_REVIEW_HEADING.search(body)
        if not match:
            reasons.append(
                "ticket carries acceptance criteria but the PR body has no "
                "'## Acceptance review' section"
            )
        else:
            rest = body[match.end():]
            nxt = _HEADING_RE.search(rest)
            section = rest[: nxt.start()] if nxt else rest
            missing = [
                c.id for c in criteria
                if not re.search(rf"^\|\s*{c.id}\s*\|", section, re.MULTILINE)
            ]
            if missing:
                reasons.append(
                    "'## Acceptance review' table is missing rows for "
                    + ", ".join(missing)
                )
    return EvidenceResult(ok=not reasons, reasons=reasons)
