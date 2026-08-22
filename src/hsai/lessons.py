"""Evidence-driven lesson synthesis.

Before this module, :mod:`hsai.orchestrator` wrote a CONSTANT string as the
lesson text - every passing iteration recorded "Change merged cleanly under a
green build." and every failing one a fixed sentence about auto-merge
holding. G3 asks the loop to grow a durable body of knowledge; a repeated
sentence is not one. Meanwhile the genuinely informative evidence an
iteration already holds - the diff, the failing CI log, which guards fired,
reverted workflow edits, repro evidence, the remote CI verdict, the attempt
count - was thrown away.

Two functions carry the whole feature:

- :func:`build_evidence` assembles a compact, structured bundle from what
  :func:`hsai.orchestrator.run_once` already has in hand (no new git/CI
  work beyond a couple of cheap read-only calls).
- :func:`synthesize_lesson` turns that bundle into three short fields - what
  was attempted, what actually happened, and one transferable rule - via a
  cheap-tier (:func:`synthesis_tier` - light/standard, NEVER heavy) headless
  ``claude -p`` call with a hard timeout and output-size cap.

Fail-safe by construction: on any error, timeout, empty or unparseable
output, a disabled config, or a hard block-budget breach, synthesis falls
back to TODAY'S deterministic text, byte-for-byte, and the fallback is
recorded (:attr:`SynthesisResult.used_fallback`) rather than silently
substituted. The lesson-per-PR invariant (G2) must never depend on this
model call succeeding, and it never routes to the heavy tier or spends
quota once a block has hard-breached its budget.

Synthesis: assafelovic/gpt-researcher (an evidence-to-narrative method
rather than an asserted conclusion), OpenBMB/ChatDev (a role separate from
the agent that produced the artifact, whose job is to report what actually
happened), and run-llama/llama_index (turning a soft convention - "write a
real lesson" - into a mechanically-checked CI gate; see the boilerplate
detector in :mod:`hsai.knowledge` and the ``ci.yml`` SDLC-evidence step).
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
from .ci import CIResult
from .config import CoreConfig
from .models import ModelChoice
from .proc import Runner, run

PROMPT_MARKER = "You are the LESSON SYNTHESIZER"

# Today's deterministic text - unchanged, so a fallback lesson stays
# byte-identical to what every lesson looked like before this module existed
# (and so hsai.knowledge.BOILERPLATE_PHRASES can still recognize it).
FALLBACK_PASS = "Change merged cleanly under a green build."
FALLBACK_FAIL = (
    "Change did not reach green; auto-merge will hold until CI passes. "
    "Investigate the failure captured above before the next attempt."
)

DEFAULT_TIMEOUT_SECONDS = 180.0
# A hard cap on the reply BEFORE it is parsed - "hard token cap" in spirit
# without depending on the CLI's own tokenizer; a reply this long has already
# failed to be "short" regardless of what it contains.
DEFAULT_MAX_OUTPUT_CHARS = 4000
DEFAULT_MAX_DIFFSTAT_CHARS = 4000
DEFAULT_MAX_LOG_CHARS = 1600

# The last fenced JSON *object* in the reply is the answer (prose around it is
# tolerated, exactly as in hsai.review.parse_verdict / hsai.synthesis).
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class EvidenceBundle:
    """Everything :func:`synthesize_lesson` is shown - nothing more.

    Deliberately narrow: the model synthesizes a lesson from what the
    iteration actually observed, not from the whole worktree.
    """

    kind: str
    ticket_title: str
    outcome: str  # "pass" | "fail"
    changed_paths: list[str] = field(default_factory=list)
    diffstat: str = ""
    ci_log_tail: str = ""  # only the FAILING portion; "" on a green build
    guards: list[str] = field(default_factory=list)  # which guards fired, and their verdicts
    reverted_workflows: list[str] = field(default_factory=list)
    repro_evidence: str = ""
    remote_ci: str = ""  # "" when not yet known at synthesis time (pre-push)
    attempts: int = 1
    tier: str = ""  # the author's model tier
    shadow_tier: str = ""  # this synthesis call's OWN tier - see synthesis_tier()
    references: tuple[str, ...] = ()  # reference-set repos available to cite
    agent_error: str = ""

    def render(self) -> str:
        """Compact, deterministic textual form fed into the prompt."""
        lines = [
            f"Ticket: {self.ticket_title}",
            f"Kind: {self.kind}",
            f"Outcome: {self.outcome}",
            f"Attempt: {self.attempts}",
            f"Author model tier: {self.tier or '(unknown)'}",
            "",
            f"Changed files ({len(self.changed_paths)}):",
            *([f"- {p}" for p in self.changed_paths] or ["- (none reported)"]),
            "",
            "Diffstat:",
            "```",
            self.diffstat or "(empty)",
            "```",
            "",
            "Guards fired:",
            *([f"- {g}" for g in self.guards] or ["- (none)"]),
        ]
        if self.reverted_workflows:
            lines += ["", f"Reverted workflow edits: {self.reverted_workflows}"]
        if self.ci_log_tail:
            lines += ["", "Failing CI tail:", "```", self.ci_log_tail, "```"]
        if self.agent_error:
            lines += ["", "Agent error:", "```", self.agent_error, "```"]
        if self.repro_evidence:
            lines += ["", "Repro evidence:", self.repro_evidence]
        if self.remote_ci:
            lines += ["", f"Remote CI: {self.remote_ci}"]
        if self.references:
            lines += ["", f"Reference-set projects available to cite: {', '.join(self.references)}"]
        return "\n".join(lines)


@dataclass
class SynthesisResult:
    """The synthesizer's answer - or the deterministic fallback."""

    attempted: str
    happened: str
    rule: str
    references: tuple[str, ...] = ()
    used_fallback: bool = False
    fallback_reason: str = ""
    model: str = ""
    tier: str = ""

    def lesson_text(self) -> str:
        """What lands in ``Lesson.lesson``.

        The fallback path returns :data:`FALLBACK_PASS` / :data:`FALLBACK_FAIL`
        byte-for-byte (so :func:`hsai.knowledge.detect_boilerplate` still
        recognizes it as the template phrase it is); a real synthesis renders
        the three required fields.
        """
        if self.used_fallback:
            return self.rule
        return (
            f"**Attempted:** {self.attempted}\n\n"
            f"**Happened:** {self.happened}\n\n"
            f"**Rule:** {self.rule}"
        )

    def fallback_note(self) -> str:
        """A short, never-silent explanation for ``What happened`` when this
        fell back - "" when a real synthesis ran."""
        if not self.used_fallback:
            return ""
        return f"Lesson synthesis fell back to the deterministic template ({self.fallback_reason})."


def _fallback(outcome: str, reason: str) -> SynthesisResult:
    text = FALLBACK_PASS if outcome == "pass" else FALLBACK_FAIL
    return SynthesisResult(
        attempted="", happened="", rule=text, used_fallback=True, fallback_reason=reason,
    )


def dry_run_result(outcome: str) -> SynthesisResult:
    """The result for a dry-run iteration: no model call is ever made."""
    return _fallback(outcome, "dry-run: no model call made")


def synthesis_tier(cfg: CoreConfig) -> str:
    """The tier this synthesis call itself runs at - cheap, and NEVER heavy.

    It runs "in the shadow" of the iteration's own (possibly heavy) model
    choice, hence the evidence bundle's ``shadow_tier`` field. Prefers
    ``light`` (cheapest), falls back to ``standard``, and never resolves to
    ``heavy`` even if that is all a misconfigured repo has defined.
    """
    for t in ("light", "standard"):
        if t in cfg.tiers:
            return t
    return cfg.default_tier if cfg.default_tier != "heavy" else "standard"


def _lesson_synthesis_cfg(cfg: CoreConfig) -> dict:
    return cfg.knowledge.get("lesson_synthesis", {}) or {}


def is_enabled(cfg: CoreConfig) -> bool:
    return bool(_lesson_synthesis_cfg(cfg).get("enabled", True))


def skip_reason(cfg: CoreConfig, repo_root: str | Path, block: int) -> str:
    """"" when synthesis should run; else why it must not spend any quota."""
    if not is_enabled(cfg):
        return "lesson synthesis disabled in knowledge.lesson_synthesis"
    try:
        records = ledger.read_records(ledger.ledger_path(cfg, repo_root))
    except (OSError, ValueError):
        # An unreadable ledger must not decide this either way; grade the
        # block as unspent and let synthesis run.
        records = []
    decision = ledger.evaluate_budget(ledger.aggregate_block(records, block), cfg.budget)
    if decision.halt:
        return f"hard budget breach ({decision.reason})"
    return ""


def build_evidence(
    *,
    kind: str,
    ticket_title: str,
    outcome: str,
    wt: str,
    base_ref: str,
    ci_result: CIResult,
    guards: Sequence[str],
    reverted_workflows: Sequence[str] = (),
    repro_evidence: str = "",
    remote_ci: str = "",
    attempts: int = 1,
    tier: str = "",
    shadow_tier: str = "",
    references: Sequence[str] = (),
    agent_error: str = "",
    runner: Runner = run,
    max_diff_chars: int = DEFAULT_MAX_DIFFSTAT_CHARS,
    max_log_chars: int = DEFAULT_MAX_LOG_CHARS,
) -> EvidenceBundle:
    """Assemble the evidence bundle from what the orchestrator already holds.

    The only NEW work here is two cheap, read-only git calls (paths + a
    ``--stat`` diffstat, never the full diff text) - everything else is
    handed straight through from the caller's own iteration state.
    """
    changed_paths = gitops.diff_paths(base_ref, cwd=wt, runner=runner)
    diffstat = gitops.diff_stat(base_ref, cwd=wt, runner=runner)
    if max_diff_chars and len(diffstat) > max_diff_chars:
        diffstat = diffstat[:max_diff_chars] + "\n... (diffstat truncated)"

    ci_log_tail = ""
    if not ci_result.ok and ci_result.log:
        ci_log_tail = ci_result.log[-max_log_chars:] if max_log_chars else ci_result.log

    return EvidenceBundle(
        kind=kind,
        ticket_title=ticket_title,
        outcome=outcome,
        changed_paths=changed_paths,
        diffstat=diffstat,
        ci_log_tail=ci_log_tail,
        guards=list(guards),
        reverted_workflows=list(reverted_workflows),
        repro_evidence=repro_evidence,
        remote_ci=remote_ci,
        attempts=attempts,
        tier=tier,
        shadow_tier=shadow_tier,
        references=tuple(references),
        agent_error=agent_error[:800] if agent_error else "",
    )


def build_prompt(evidence: EvidenceBundle) -> str:
    """The synthesizer's instruction: evidence-first, JSON-terminated."""
    return f"""{PROMPT_MARKER} for ai-hyperswarm-proto-core, an autonomous
self-improving AI-swarm harness. You did not write this change and did not
review it; your only job is to read the EVIDENCE below - not the ticket's
aspirations - and report what actually happened, the way a distinct observer
would (never the author grading its own work).

{evidence.render()}

Write exactly three things, grounded ONLY in the evidence above - never invent
a detail it does not support:

1. ATTEMPTED - one or two sentences: what this iteration set out to do.
2. HAPPENED - one or two sentences: what the evidence shows actually
   occurred. Be specific - name the guard, the failing step, or the file the
   evidence points at.
3. RULE - exactly ONE transferable lesson, phrased as a rule a FUTURE
   iteration could follow. If the reference-set projects listed above show
   this change was informed by one of their practices, name that project
   (e.g. `owner/repo`) in the rule.

Answer with prose if you like, but END your reply with a fenced ```json block
containing exactly this object:
{{"attempted": "...", "happened": "...", "rule": "...", "references": ["owner/repo", ...]}}
"""


def parse_synthesis(output: str) -> SynthesisResult | None:
    """Extract the three fields from a synthesizer's reply, or ``None``.

    ``None`` covers every shape of "could not read this as an answer": no
    fenced JSON block, invalid JSON, a non-object, or a missing/blank
    required field. The caller falls back to the deterministic text in every
    such case - this function never raises.
    """
    text = (output or "").strip()
    if not text:
        return None
    blocks = _JSON_BLOCK.findall(text)
    if not blocks and text.startswith("{") and text.endswith("}"):
        blocks = [text]
    if not blocks:
        return None
    try:
        raw = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    attempted = str(raw.get("attempted", "")).strip()
    happened = str(raw.get("happened", "")).strip()
    rule = str(raw.get("rule", "")).strip()
    if not (attempted and happened and rule):
        return None

    refs_raw = raw.get("references")
    if isinstance(refs_raw, str):
        refs_raw = [refs_raw]
    references = tuple(str(r).strip() for r in (refs_raw or []) if str(r).strip())
    return SynthesisResult(attempted=attempted, happened=happened, rule=rule, references=references)


def synthesize_lesson(
    cfg: CoreConfig,
    evidence: EvidenceBundle,
    *,
    repo_root: str | Path,
    wt: str,
    block: int,
    iteration: int = 0,
    ticket: int | None = None,
    attempts: int = 1,
    tier: str | None = None,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Turn ``evidence`` into a lesson via a cheap-tier ``claude -p`` call.

    Fail-safe on every path: disabled config, a hard block-budget breach, a
    non-zero exit, an empty reply, unparseable output, or an unexpected
    exception all fall back to :data:`FALLBACK_PASS` / :data:`FALLBACK_FAIL`
    - the lesson-per-PR invariant never depends on this call succeeding.
    Every ATTEMPTED call (skips excluded) appends a ``kind='lesson_synthesis'``
    ledger record, metered like the independent review gate, so this spend is
    auditable in the block aggregate (G4).
    """
    reason = skip_reason(cfg, repo_root, block)
    if reason:
        return _fallback(evidence.outcome, reason)

    tier = tier or synthesis_tier(cfg)
    if tier not in cfg.tiers:
        tier = cfg.default_tier
    choice = ModelChoice(
        tier=tier,
        model=cfg.tiers[tier].model,
        rationale="lesson synthesis: cheap tier only, never heavy",
        strategy="lesson-synthesis-v1",
    )

    lesson_cfg = _lesson_synthesis_cfg(cfg)
    timeout = float(lesson_cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    max_chars = int(lesson_cfg.get("max_output_chars", DEFAULT_MAX_OUTPUT_CHARS))
    prompt = build_prompt(evidence)

    started = time.time()
    try:
        ares = run_agent(prompt, choice, cfg, cwd=wt, runner=ai_runner, timeout=timeout)
    except Exception as exc:  # belt-and-suspenders: never let synthesis crash the loop
        result = _fallback(evidence.outcome, f"synthesis call raised {type(exc).__name__}: {exc}")
        result.model, result.tier = choice.model, choice.tier
        ledger.append_record(
            ledger.ledger_path(cfg, repo_root),
            ledger.LedgerRecord(
                iteration=iteration, block=block, ticket=ticket, kind="lesson_synthesis",
                tier=choice.tier, model=choice.model,
                wall_clock_seconds=round(max(0.0, time.time() - started), 3),
                attempts=attempts, outcome="exception",
            ),
        )
        return result

    outcome = "ok"
    if not ares.ok:
        outcome = "error"
        result = _fallback(
            evidence.outcome,
            f"synthesis call failed: {(ares.error or '').strip()[:200] or 'non-zero exit'}",
        )
    else:
        text = (ares.text or "").strip()
        if not text:
            outcome = "empty"
            result = _fallback(evidence.outcome, "synthesis call returned empty output")
        else:
            if max_chars and len(text) > max_chars:
                text = text[:max_chars]
            parsed = parse_synthesis(text)
            if parsed is None:
                outcome = "unparseable"
                result = _fallback(evidence.outcome, "synthesis call returned unparseable output")
            else:
                result = parsed

    result.model, result.tier = choice.model, choice.tier

    tokens = ledger.parse_tokens(ares.payload)
    ledger.append_record(
        ledger.ledger_path(cfg, repo_root),
        ledger.LedgerRecord(
            iteration=iteration, block=block, ticket=ticket, kind="lesson_synthesis",
            tier=choice.tier, model=choice.model,
            wall_clock_seconds=round(max(0.0, time.time() - started), 3),
            attempts=attempts, outcome=outcome,
            input_tokens=tokens[0] if tokens else None,
            output_tokens=tokens[1] if tokens else None,
        ),
    )
    return result
