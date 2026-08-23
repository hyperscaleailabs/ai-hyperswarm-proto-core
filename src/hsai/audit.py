"""Pre-PR acceptance audit and diff-hygiene gate.

Every gate before this one answers "is the build green?". None of them answers
"did this diff actually satisfy the ticket?" - the completeness guard is purely
structural (any file outside ``knowledge/`` counts), so a one-line change that
ignores five of six checkboxes passes it, merges, and leaves behind a lesson
saying "Change merged cleanly under a green build". Nor does anything look at
what the worker did to the *worktree*: a new dependency in ``pyproject.toml``,
a raised ``execution.permission_mode``, an edited ``constraints:`` block, a
credential-shaped string, or a 2000-line diff on a ``size:M`` ticket would all
have merged silently.

Two layers, deliberately asymmetric:

**Layer A - deterministic hygiene.** Pure functions over the ticket's labels and
the working-tree diff: conventional-commit title prefix, diff size banded
against the ``size:`` label, added dependencies in ``pyproject.toml``, any edit
that raises ``execution.permission_mode`` or touches ``constraints:`` in
``core.yaml``, and a deny-list scan for credential-shaped strings. This is the
ONLY layer that can hard-fail, because it is the only one whose verdict is
reproducible.

**Layer B - semantic auditor.** A separate :func:`hsai.ai.run_agent` call on a
fresh context, given the ticket text and the diff and *nothing else* - never
the implementer's prompt or output, so it cannot be talked into agreeing with
the author (OpenBMB/ChatDev holds the reviewer role strictly apart from the
coder role). It answers with a per-criterion ``satisfied``/``partially``/
``unmet`` verdict plus an evidence pointer. It is **fail-open**: an error, a
timeout, or an unparseable reply annotates the PR and the lesson and blocks
nothing. A gate that could stall the loop by being unavailable is not a
guardrail.

The gate runs after the completeness and repro guards and BEFORE any commit, so
a rejection costs no push and no PR; it routes through the orchestrator's
existing ``_recover_failed`` retry policy under the ``AUDIT_FAILED`` reason,
so there is no new stall state. It runs at most once per iteration, so a
rejection can never loop.

One tradeoff worth stating: the deny-list scan cannot tell a leaked credential
from a deliberately fake test fixture, so a change that adds a secret-shaped
literal to a test is blocked too. That is the safe direction to be wrong in,
and the patterns are config-driven (``audit.deny_list``) precisely so the
architect can retune without a code change. A match is reported by *pattern*,
never by quoting the offending line - the finding is rendered into a public PR
body.

Synthesis: crewAIInc/crewAI (per-PR mechanical gates - `pr-size.yml`,
`pr-title.yml`, `vulnerability-scan.yml` - here moved orchestrator-side,
because worker edits to `.github/workflows/**` are auto-reverted),
SWE-agent/SWE-agent (issue-to-PR automation is only trustworthy with a harness
that scores the patch against the issue rather than against the build),
OpenBMB/ChatDev (reviewer role separated from the coder role), and
run-llama/llama_index (hard numeric CI gates, which is why the size bands are
numbers in config rather than a model's opinion).
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops, ledger
from .ai import run_agent
from .config import CORE_PATH, CoreConfig
from .models import ModelChoice
from .proc import Runner, run
from .tickets import acceptance_criteria, size_from_labels

# Recognisable opening line: the auditor prompt is never the worker prompt and
# never the reviewer prompt.
PROMPT_MARKER = "You are the ACCEPTANCE AUDITOR"

# The last fenced JSON *object* in the reply is the verdict (prose around it is
# tolerated, exactly as in synthesis.parse_ticket_specs and review.parse_verdict).
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# --- finding severities ------------------------------------------------------
BLOCK = "block"
WARN = "warn"

# --- per-criterion statuses (the closed vocabulary Layer B must answer in) ----
SATISFIED = "satisfied"
PARTIALLY = "partially"
UNMET = "unmet"
STATUSES = (SATISFIED, PARTIALLY, UNMET)

# --- Layer A check ids -------------------------------------------------------
TITLE_PREFIX = "title_prefix"
DIFF_SIZE = "diff_size"
NEW_DEPENDENCY = "new_dependency"
CONFIG_ESCALATION = "config_escalation"
DENY_LIST = "deny_list"
CHECKS = (TITLE_PREFIX, DIFF_SIZE, NEW_DEPENDENCY, CONFIG_ESCALATION, DENY_LIST)

# Which checks stop a change. A non-conventional TITLE is advisory on purpose:
# the loop does not author most ticket titles, and refusing a ticket over its
# heading would cost a retry for something no attempt can fix. Everything else
# describes a change the architect would have refused outright.
DEFAULT_SEVERITY = {
    TITLE_PREFIX: WARN,
    DIFF_SIZE: BLOCK,
    NEW_DEPENDENCY: BLOCK,
    CONFIG_ESCALATION: BLOCK,
    DENY_LIST: BLOCK,
}

# Changed lines (added + deleted) tolerated per size band, before the diff is
# treated as having outgrown its ticket. Overridable via ``audit.size_bands``.
DEFAULT_SIZE_BANDS = {"S": 300, "M": 800, "L": 2000}

CONVENTIONAL_PREFIXES = (
    "feat", "fix", "docs", "chore", "refactor", "test", "perf",
    "ci", "build", "style", "revert", "skill",
)

# Credential shapes, biased toward vendor-prefixed high-signal tokens so an
# ordinary code diff cannot trip them by accident. The generic assignment rule
# demands a long opaque value for the same reason.
DEFAULT_DENY_LIST = (
    r"sk-ant-[A-Za-z0-9_\-]{8,}",
    r"gh[pousr]_[A-Za-z0-9]{16,}",
    r"AKIA[0-9A-Z]{12,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"""(?i)\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*["'][A-Za-z0-9/+=_\-]{16,}["']""",
)

DEFAULT_TIER = "light"
DEFAULT_TIMEOUT = 600.0
DEFAULT_MAX_DIFF_CHARS = 20000

UNPARSEABLE = "auditor produced no parseable verdict JSON block"

_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)\s*$")
_DEP_ARRAY_OPEN = re.compile(r"^[+\- ]?\s*[\w.\-]*dependencies\s*=\s*\[", re.IGNORECASE)
_DEP_ARRAY_CLOSE = re.compile(r"^[+\- ]?\s*\]")
_DEP_LINE = re.compile(r"""^\+\s*["']([A-Za-z][\w.\-]*)\s*(?:[<>=!~;@\[].*?)?["']\s*,?\s*$""")
_DEP_PINNED = re.compile(r"[<>=!~@]")
_PERMISSION_LINE = re.compile(r"^([+\-])\s*permission_mode:\s*([A-Za-z]+)")

# Ordered least -> most privileged. An unknown mode ranks above every known one:
# a value this repo has never audited is the riskiest thing a diff can set.
_PERMISSION_RANK = {"plan": 0, "default": 1, "acceptedits": 2, "bypasspermissions": 3}
_UNKNOWN_PERMISSION_RANK = len(_PERMISSION_RANK)

# The hard rules in core.yaml's `constraints:` block. Any diff line naming one
# of these is an edit to the loop's own safety contract.
_CONSTRAINT_KEYS = (
    "constraints:",
    "subscription_only",
    "forbid_env",
    "never_commit_to_default_branch",
    "require_ticket_per_pr",
    "require_green_ci_to_merge",
    "require_lesson_per_pr",
)


# --- data ---------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """One deterministic (Layer A) hygiene result."""

    check: str
    severity: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.severity == BLOCK

    def render(self) -> str:
        label = "BLOCK" if self.blocking else "warn"
        return f"- **{label}** `{self.check}`: {self.detail}"


@dataclass(frozen=True)
class FileStat:
    """One file's line churn, as reported by ``git diff --numstat``."""

    added: int
    deleted: int
    path: str

    @property
    def changed(self) -> int:
        return self.added + self.deleted


@dataclass(frozen=True)
class CriterionVerdict:
    """The auditor's answer for one acceptance criterion."""

    criterion: str
    status: str
    evidence: str = ""

    @property
    def met(self) -> bool:
        return self.status == SATISFIED


@dataclass
class AuditVerdict:
    """Layer B's parsed answer. Advisory: it annotates, it never hard-fails."""

    criteria: list[CriterionVerdict] = field(default_factory=list)
    overall: str = ""     # pass | fail | "" when the auditor did not say
    rationale: str = ""
    error: str = ""       # why Layer B produced nothing usable (fail-open)

    @property
    def usable(self) -> bool:
        return not self.error and bool(self.criteria)

    def gaps(self) -> list[CriterionVerdict]:
        """Criteria the auditor did NOT consider satisfied."""
        return [c for c in self.criteria if not c.met]

    def gap_list(self) -> str:
        """One line per unsatisfied criterion - the next attempt's starting point."""
        return "; ".join(f"{c.status}: {c.criterion}" for c in self.gaps())

    def render(self) -> str:
        if self.error:
            return f"_(semantic audit unavailable: {self.error}; not treated as a failure)_"
        if not self.criteria:
            return "_(the ticket declared no acceptance criteria to audit)_"
        rows = "\n".join(
            f"| {c.criterion} | `{c.status}` | {c.evidence or '_(none given)_'} |"
            for c in self.criteria
        )
        head = f"- auditor verdict: **{(self.overall or 'not stated').upper()}**"
        return (
            f"{head}\n\n| criterion | status | evidence |\n| --- | --- | --- |\n{rows}"
            + f"\n\n**Rationale**\n{self.rationale or '_(none given)_'}"
        )


@dataclass
class AuditReport:
    """The whole gate's answer: Layer A findings plus Layer B's verdict."""

    ok: bool = True
    skipped: bool = False
    reason: str = ""          # why the gate did not run
    findings: list[Finding] = field(default_factory=list)
    verdict: AuditVerdict = field(default_factory=AuditVerdict)
    auditor_model: str = ""
    auditor_tier: str = ""
    files_changed: int = 0
    changed_lines: int = 0

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        return "pass" if self.ok else "fail"

    def summary(self) -> str:
        """One line, for iteration notes."""
        if self.skipped:
            return f"skipped ({self.reason})" if self.reason else "skipped"
        gaps = len(self.verdict.gaps())
        return (
            f"{self.status} ({len(self.blocking)} blocking, "
            f"{len(self.findings) - len(self.blocking)} advisory, {gaps} unmet criterion(s), "
            f"{self.changed_lines} changed lines across {self.files_changed} file(s))"
        )

    def failure_detail(self) -> str:
        """Why the gate refused, plus the concrete gap list for the next attempt."""
        parts = [f.detail for f in self.blocking]
        gaps = self.verdict.gap_list()
        if gaps:
            parts.append(f"unmet criteria - {gaps}")
        return "; ".join(parts) or "acceptance audit refused the change"

    def render(self) -> str:
        """The verdict verbatim, for the PR body and the lesson (G2)."""
        if self.skipped:
            return f"_(not run: {self.reason or 'no reason recorded'})_"
        head = "**PASS**" if self.ok else "**FAILED**"
        hygiene = "\n".join(f.render() for f in self.findings) or "- _(no findings)_"
        return (
            f"- result: {head}\n"
            f"- auditor: `{self.auditor_model or '-'}` (tier: `{self.auditor_tier or '-'}`)\n"
            f"- diff: {self.changed_lines} changed line(s) across {self.files_changed} file(s)\n\n"
            f"**Diff hygiene (deterministic)**\n{hygiene}\n\n"
            f"**Acceptance criteria (semantic audit)**\n{self.verdict.render()}"
        )


def skip_audit(reason: str) -> AuditReport:
    """A report for a gate that deliberately did not run.

    ``ok`` on purpose: the gate is additive, so being unavailable must never be
    able to stop an iteration. The reason is recorded either way.
    """
    return AuditReport(ok=True, skipped=True, reason=reason)


def is_enabled(cfg: CoreConfig) -> bool:
    return bool(cfg.audit.get("enabled", True))


# --- Layer A: pure, deterministic checks --------------------------------------

def parse_numstat(text: str) -> list[FileStat]:
    """Parse ``git diff --numstat`` output into per-file line churn.

    Binary files report ``-`` instead of a count; they are kept (the path still
    changed) with zero churn rather than dropped or crashed on.
    """
    stats: list[FileStat] = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0].strip(), parts[1].strip(), parts[-1].strip()
        if not path:
            continue
        stats.append(
            FileStat(
                added=int(added) if added.isdigit() else 0,
                deleted=int(deleted) if deleted.isdigit() else 0,
                path=path,
            )
        )
    return stats


def added_lines(diff: str) -> list[str]:
    """Every line the diff ADDS, without its ``+`` marker (file headers excluded)."""
    return [
        line[1:]
        for line in (diff or "").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def file_hunks(diff: str) -> dict[str, list[str]]:
    """Split a unified diff into ``post-image path -> the lines under it``."""
    hunks: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in (diff or "").splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            current = hunks.setdefault(header.group(2), [])
            continue
        if current is not None:
            current.append(line)
    return hunks


def check_title(title: str) -> str:
    """Conventional-commit prefix check ("" when clean)."""
    lowered = title.strip().lower()
    if any(lowered.startswith((f"{p}:", f"{p}(")) for p in CONVENTIONAL_PREFIXES):
        return ""
    return (
        f"ticket title {title.strip()!r} carries no conventional-commit prefix "
        f"(expected one of: {', '.join(CONVENTIONAL_PREFIXES)})"
    )


def diff_size_band(
    size: str, stats: Sequence[FileStat], bands: dict[str, int] | None = None
) -> str:
    """Band the diff's churn against the ticket's ``size:`` label ("" when within).

    An unknown or absent band falls back to ``M``: an unlabeled ticket is
    treated as an ordinary one, never as unbounded.
    """
    table = bands if bands is not None else DEFAULT_SIZE_BANDS
    ceiling = table.get(size.upper()) or table.get("M")
    if not ceiling:
        return ""
    changed = sum(s.changed for s in stats)
    if changed <= int(ceiling):
        return ""
    return (
        f"diff changes {changed} lines across {len(stats)} file(s), over the "
        f"size:{size} ceiling of {int(ceiling)} - split the ticket or resize it"
    )


def check_new_dependencies(diff: str) -> str:
    """Dependencies added to ``pyproject.toml`` ("" when none).

    A line is a dependency when it is added inside a ``*dependencies = [`` array
    (tracked through the hunk's context lines), or when it is a quoted
    requirement carrying a version specifier - the second rule catches an add
    that landed too far from the array header to keep it in context, without
    ever mistaking a ``keywords`` entry for a dependency.
    """
    in_array = False
    found: list[str] = []
    for line in file_hunks(diff).get("pyproject.toml", []):
        if _DEP_ARRAY_OPEN.match(line):
            in_array = True
            continue
        if in_array and _DEP_ARRAY_CLOSE.match(line):
            in_array = False
            continue
        match = _DEP_LINE.match(line)
        if match and (in_array or _DEP_PINNED.search(line)):
            found.append(match.group(1))
    if not found:
        return ""
    names = ", ".join(f"`{n}`" for n in dict.fromkeys(found))
    return (
        f"pyproject.toml adds {len(found)} dependency/dependencies ({names}); "
        "a new third-party dependency is an architect decision, not a ticket's"
    )


def check_config_escalation(diff: str, *, config_path: str = CORE_PATH) -> str:
    """Privilege or safety-contract escalation in ``core.yaml`` ("" when none)."""
    lines = file_hunks(diff).get(config_path, [])
    if not lines:
        return ""
    reasons: list[str] = []

    raised_to, lowered_from = -1, -1
    for line in lines:
        match = _PERMISSION_LINE.match(line)
        if not match:
            continue
        rank = _PERMISSION_RANK.get(match.group(2).lower(), _UNKNOWN_PERMISSION_RANK)
        if match.group(1) == "+":
            raised_to = max(raised_to, rank)
        else:
            lowered_from = max(lowered_from, rank)
    if raised_to > lowered_from:
        reasons.append(
            f"raises execution.permission_mode in {config_path} "
            "(a worker must not widen its own permissions)"
        )

    if any(
        line.startswith(("+", "-")) and any(key in line for key in _CONSTRAINT_KEYS)
        for line in lines
    ):
        reasons.append(
            f"edits the `constraints:` block in {config_path} "
            "(subscription-only, ticket-per-PR and green-merge are hard rules)"
        )
    return "; ".join(reasons)


def scan_deny_list(diff: str, patterns: Sequence[str] = DEFAULT_DENY_LIST) -> list[str]:
    """Deny-list scan over the diff's ADDED lines, one detail string per hit.

    Reports the pattern that matched, never the line that matched it: these
    findings are rendered into a public PR body, and echoing the match would
    publish the very string the scan exists to catch.
    """
    hits: list[str] = []
    lines = added_lines(diff)
    for pattern in patterns:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            hits.append(f"deny-list pattern `{pattern}` is not a valid regex ({exc})")
            continue
        matched = sum(1 for line in lines if rx.search(line))
        if matched:
            hits.append(
                f"{matched} added line(s) match the credential deny-list pattern "
                f"`{pattern}` (value withheld)"
            )
    return hits


def hygiene_findings(
    *,
    ticket_title: str,
    size: str,
    stats: Sequence[FileStat],
    diff: str,
    settings: dict | None = None,
) -> list[Finding]:
    """Run every Layer A check and return its findings, severity resolved."""
    settings = settings or {}
    bands = {
        str(k).upper(): int(v)
        for k, v in (settings.get("size_bands") or DEFAULT_SIZE_BANDS).items()
    }
    patterns = tuple(settings.get("deny_list") or DEFAULT_DENY_LIST)
    severity = {
        **DEFAULT_SEVERITY,
        **{str(k): str(v) for k, v in (settings.get("severity") or {}).items()},
    }

    findings: list[Finding] = []

    def record(check: str, detail: str) -> None:
        if detail:
            findings.append(
                Finding(check=check, severity=severity.get(check, BLOCK), detail=detail)
            )

    record(TITLE_PREFIX, check_title(ticket_title))
    record(DIFF_SIZE, diff_size_band(size, stats, bands))
    record(NEW_DEPENDENCY, check_new_dependencies(diff))
    record(CONFIG_ESCALATION, check_config_escalation(diff))
    for hit in scan_deny_list(diff, patterns):
        record(DENY_LIST, hit)
    return findings


# --- Layer B: the semantic auditor ---------------------------------------------

def build_prompt(
    *,
    ticket_title: str,
    ticket_body: str,
    criteria: Sequence[str],
    paths: Sequence[str],
    diff: str,
) -> str:
    """The auditor's instruction - built from the TICKET and the DIFF only.

    Deliberately has no parameter for the implementer's prompt, reasoning, or
    output: the auditor must score the patch against the issue, and cannot be
    argued into agreeing with an author it never hears from.
    """
    listed = (
        "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, start=1))
        or "_(the ticket declared no acceptance criteria)_"
    )
    touched = "\n".join(f"- {p}" for p in paths) or "- _(no files reported)_"
    schema = json.dumps(
        {
            "criteria": [
                {
                    "criterion": "<the criterion text, verbatim>",
                    "status": f"one of {'|'.join(STATUSES)}",
                    "evidence": "<file:symbol or test name proving it, or why it is unmet>",
                }
            ],
            "verdict": "pass or fail",
            "rationale": "2-4 sentences",
        },
        indent=2,
    )
    return f"""{PROMPT_MARKER} for ai-hyperswarm-proto-core, an autonomous
self-improving AI-swarm harness. You are given a ticket and the diff that
claims to close it - and nothing else. You did not write this change, you
cannot see who did or why they say it works, and you must not assume it works.

Score the DIFF against the TICKET, criterion by criterion. For each one answer:
- `{SATISFIED}` - code in this diff genuinely implements it, and where the
  criterion implies a test, a test in this diff proves it;
- `{PARTIALLY}` - partly implemented, or implemented without the evidence the
  criterion asks for;
- `{UNMET}` - nothing in this diff addresses it.

Every answer MUST carry a one-line evidence pointer into the diff (a file and
symbol, or a test name). "Looks fine" is not evidence. Judge only what the diff
contains; do not credit intent, comments, or a plan for a later change.

Ticket: {ticket_title}

{ticket_body}

Acceptance criteria to score:
{listed}

Files touched:
{touched}

Diff:
```diff
{diff or "(empty diff)"}
```

Answer with prose if you like, but END your reply with a fenced ```json block
containing exactly this object (one entry per criterion, in order):
```json
{schema}
```
"""


def parse_audit_json(output: str) -> AuditVerdict:
    """Extract the per-criterion verdict from an auditor's reply - fail-OPEN.

    The mirror image of :func:`hsai.review.parse_verdict`, and deliberately so.
    The reviewer is fail-closed because its approval is what opens a PR; this
    auditor only annotates, so anything unreadable becomes a recorded
    ``error`` and blocks nothing. Silence must not be able to stall the loop.
    """
    text = (output or "").strip()
    blocks = _JSON_BLOCK.findall(text)
    if not blocks and text.startswith("{") and text.endswith("}"):
        blocks = [text]  # an auditor that answered with bare JSON
    if not blocks:
        return AuditVerdict(error=UNPARSEABLE)
    try:
        raw = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return AuditVerdict(error=UNPARSEABLE)
    if not isinstance(raw, dict):
        return AuditVerdict(error=UNPARSEABLE)

    criteria: list[CriterionVerdict] = []
    for entry in raw.get("criteria") or []:
        if not isinstance(entry, dict):
            continue
        criterion = str(entry.get("criterion", "")).strip()
        if not criterion:
            continue
        status = str(entry.get("status", "")).strip().lower()
        criteria.append(
            CriterionVerdict(
                criterion=criterion,
                # An unrecognised status is not a pass: an answer we cannot read
                # is exactly as unproven as an explicit "unmet".
                status=status if status in STATUSES else UNMET,
                evidence=str(entry.get("evidence", "")).strip(),
            )
        )
    verdict = AuditVerdict(
        criteria=criteria,
        overall=str(raw.get("verdict", "")).strip().lower(),
        rationale=str(raw.get("rationale", "")).strip(),
    )
    if not criteria:
        verdict.error = "auditor returned a verdict object with no criteria"
    return verdict


def select_auditor(cfg: CoreConfig) -> ModelChoice:
    """Resolve the configured audit tier to a concrete model.

    Routed cheap by default: the audit runs on every change, so paying heavy
    tier to grade an implementation would cost more than the implementation.
    """
    tier = str(cfg.audit.get("tier", DEFAULT_TIER))
    if tier not in cfg.tiers:
        tier = DEFAULT_TIER if DEFAULT_TIER in cfg.tiers else cfg.default_tier
    return ModelChoice(
        tier=tier,
        model=cfg.tiers[tier].model,
        rationale=f"pre-PR acceptance audit on the `{tier}` tier",
        strategy="auditor-v1",
    )


# --- the gate ------------------------------------------------------------------

def run_audit(
    cfg: CoreConfig,
    *,
    repo_root: str | Path,
    wt: str,
    ticket_title: str,
    ticket_body: str,
    labels: Sequence[str] = (),
    iteration: int = 0,
    block: int = 0,
    ticket: int | None = None,
    attempts: int = 1,
    runner: Runner = run,
    ai_runner: Runner = run,
) -> AuditReport:
    """Audit the UNCOMMITTED work in ``wt`` against its ticket.

    The only impure function here: ``runner`` reads the worktree diff through
    :mod:`hsai.gitops` and ``ai_runner`` drives Layer B through :mod:`hsai.ai`
    (so the audit stays subscription-only). Everything it decides on is a pure
    function of what those two returned.

    Layer B is skipped - annotated, never blocking - when the block is in a
    hard budget breach, for the same reason the review gate skips: a gate that
    could halt a quota-exhausted block would be a deadlock, not a guardrail.
    """
    if not is_enabled(cfg):
        return skip_audit("acceptance audit disabled in cfg.audit")

    settings = dict(cfg.audit)

    # New files are invisible to `git diff` until git knows they exist, and a
    # feature's diff is mostly new files.
    gitops.stage_intent_to_add(cwd=wt, runner=runner)
    diff = gitops.worktree_diff(cwd=wt, runner=runner)
    stats = parse_numstat(gitops.worktree_numstat(cwd=wt, runner=runner))

    findings = hygiene_findings(
        ticket_title=ticket_title,
        size=size_from_labels(list(labels)),
        stats=stats,
        diff=diff,
        settings=settings,
    )
    report = AuditReport(
        ok=not any(f.blocking for f in findings),
        findings=findings,
        files_changed=len(stats),
        changed_lines=sum(s.changed for s in stats),
    )

    choice = select_auditor(cfg)
    report.auditor_model, report.auditor_tier = choice.model, choice.tier

    try:
        records = ledger.read_records(ledger.ledger_path(cfg, repo_root))
    except (OSError, ValueError):
        # An unreadable ledger must not decide whether a change gets audited.
        records = []
    decision = ledger.evaluate_budget(ledger.aggregate_block(records, block), cfg.budget)
    if decision.halt:
        report.verdict = AuditVerdict(error=f"hard budget breach ({decision.reason})")
        return report

    max_chars = int(settings.get("max_diff_chars", DEFAULT_MAX_DIFF_CHARS))
    shown = diff
    if max_chars and len(shown) > max_chars:
        shown = shown[:max_chars] + "\n... (diff truncated for audit)"

    prompt = build_prompt(
        ticket_title=ticket_title,
        ticket_body=ticket_body,
        criteria=acceptance_criteria(ticket_body),
        paths=[s.path for s in stats],
        diff=shown,
    )
    started = time.time()
    ares = run_agent(
        prompt, choice, cfg, cwd=wt, runner=ai_runner,
        timeout=float(settings.get("timeout_seconds", DEFAULT_TIMEOUT)),
    )
    if ares.ok:
        report.verdict = parse_audit_json(ares.text)
    else:
        # A crashed or timed-out auditor is an annotation, never a blocker.
        report.verdict = AuditVerdict(error=f"auditor run failed: {(ares.error or '')[:200]}")

    tokens = ledger.parse_tokens(ares.payload)
    ledger.append_record(
        ledger.ledger_path(cfg, repo_root),
        ledger.LedgerRecord(
            iteration=iteration,
            block=block,
            ticket=ticket,
            kind="audit",
            tier=choice.tier,
            model=choice.model,
            wall_clock_seconds=round(max(0.0, time.time() - started), 3),
            attempts=attempts,
            outcome=report.status,
            input_tokens=tokens[0] if tokens else None,
            output_tokens=tokens[1] if tokens else None,
        ),
    )
    return report
