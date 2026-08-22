"""Evidence-driven lesson synthesis.

Before this module, ``orchestrator.run_once`` wrote a constant sentence into
every lesson's ``## Lesson learned`` field - the same one on every pass, and
the same one on every fail. Fourteen of the notes in ``knowledge/lessons/``
were therefore near-identical in their most important field, and every
downstream artifact (whitepapers, persona articles) synthesizes FROM those
lessons, so the boilerplate compounded through the whole knowledge base.

:func:`build_evidence` assembles a compact, structured bundle from what the
orchestrator already has in hand for the iteration - the changed-file list and
diffstat, the failing portion of the CI log, which guards fired, reverted
workflow edits, repro evidence, the independent review verdict, the remote CI
outcome, the attempt count, and the tier actually used next to the tier that
would have run without a soft-budget demotion (the "shadow tier"). It is pure
data assembly: no model call, no side effect.

:func:`synthesize_lesson` is the one model call: a cheap-tier (light or
standard, NEVER heavy) ``claude -p`` run over that evidence, prompted to
report three short fields - what was attempted, what actually happened, and
one transferable lesson stated as a rule - citing the reference project when
the change was practice-driven. It is deliberately fail-safe: on any error,
timeout, empty output, or a hard block-budget breach, it falls back to the
same deterministic text the orchestrator always wrote, and marks the note as
a fallback (see :data:`hsai.knowledge.FALLBACK_TAG`) rather than pretending
synthesis ran. The lesson-per-PR invariant (G2) must never depend on this
call succeeding, and this call must never be able to push a block over its
quota ceiling (G4) - so a hard budget breach skips it entirely, before any
``claude -p`` process is spawned.

Synthesis: assafelovic/gpt-researcher (build a cited narrative FROM gathered
evidence rather than asserting a conclusion), OpenBMB/ChatDev (a role
separate from the one that produced the artifact reports what it observed).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger
from .ai import run_agent
from .config import CoreConfig
from .knowledge import FALLBACK_FAIL, FALLBACK_PASS, FALLBACK_TAG
from .models import ModelChoice
from .proc import Runner, run

# Recognisable opening line: never confused with the worker prompt or the
# independent-review prompt (see hsai.review.PROMPT_MARKER).
PROMPT_MARKER = "You are the LESSON SYNTHESIZER"

DEFAULT_TIMEOUT = 180.0
# Hard caps: this call must stay cheap. The evidence bundle is truncated
# before it reaches the prompt, and every field the model returns is
# truncated again on the way back - a rambling reply cannot inflate the note.
DEFAULT_MAX_EVIDENCE_CHARS = 6000
DEFAULT_MAX_LOG_CHARS = 2000
MAX_FIELD_CHARS = 800

# Cheap tiers only, cheapest first - `_synthesis_choice` never returns heavy.
_CHEAP_TIERS = ("light", "standard")

# The last fenced JSON *object* in the reply is the verdict (prose around it
# is tolerated, exactly as in hsai.review.parse_verdict).
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass(frozen=True)
class GuardOutcome:
    """One guard's verdict for this iteration - evidence, not narrative."""

    name: str
    fired: bool  # True: the guard flagged a problem. False: it ran clear.
    detail: str = ""


@dataclass(frozen=True)
class EvidenceBundle:
    """Everything the synthesis prompt is allowed to see for one iteration."""

    kind: str
    ticket_title: str
    ticket_body: str
    outcome: str  # pass | fail
    changed_files: tuple[str, ...] = ()
    diffstat: str = ""
    ci_log_tail: str = ""  # only the FAILING portion; empty when CI is green
    guards: tuple[GuardOutcome, ...] = ()
    reverted_workflows: tuple[str, ...] = ()
    repro_evidence: str = ""
    review_verdict: str = ""
    remote_ci: str = ""
    attempts: int = 1
    tier: str = ""
    shadow_tier: str = ""  # the tier that would have run without a soft-budget demotion
    agent_ok: bool = True
    agent_error: str = ""

    def render(self) -> str:
        """Compact plain text for the synthesis prompt - not markdown, not JSON."""
        lines = [
            f"kind: {self.kind}",
            f"ticket: {self.ticket_title}",
            f"outcome: {self.outcome}",
            f"attempts: {self.attempts}",
            f"tier used: {self.tier or '-'}"
            + (
                f" (would have been {self.shadow_tier} without a budget demotion)"
                if self.shadow_tier and self.shadow_tier != self.tier
                else ""
            ),
            f"files changed ({self.diffstat or 'no diffstat'}): "
            + (", ".join(self.changed_files) or "(none)"),
        ]
        if self.reverted_workflows:
            lines.append(f"reverted workflow edits: {', '.join(self.reverted_workflows)}")
        if self.guards:
            lines.append("guards:")
            lines.extend(
                f"  - {g.name}: {'FIRED' if g.fired else 'clear'}"
                + (f" - {g.detail}" if g.detail else "")
                for g in self.guards
            )
        if self.review_verdict:
            lines.append(f"independent review:\n{self.review_verdict}")
        if self.repro_evidence:
            lines.append(f"repro evidence:\n{self.repro_evidence}")
        if self.remote_ci:
            lines.append(f"remote CI: {self.remote_ci}")
        if not self.agent_ok:
            lines.append(f"agent error: {self.agent_error}")
        if self.ci_log_tail:
            lines.append(f"CI failure log (tail):\n```\n{self.ci_log_tail}\n```")
        return "\n".join(lines)


def build_evidence(
    *,
    kind: str,
    ticket_title: str,
    ticket_body: str,
    outcome: str,
    changed_files: list[str] | tuple[str, ...] = (),
    ci_ok: bool = True,
    ci_log: str = "",
    guards: list[GuardOutcome] | tuple[GuardOutcome, ...] = (),
    reverted_workflows: list[str] | tuple[str, ...] = (),
    repro_evidence: str = "",
    review_verdict: str = "",
    remote_ci: str = "",
    attempts: int = 1,
    tier: str = "",
    shadow_tier: str = "",
    agent_ok: bool = True,
    agent_error: str = "",
    max_log_chars: int = DEFAULT_MAX_LOG_CHARS,
) -> EvidenceBundle:
    """Assemble a compact evidence bundle from what ``run_once`` has in hand.

    Pure data assembly - no model call, no I/O. ``ci_log`` is only kept when
    ``ci_ok`` is False (and only its tail): a green build's log is not
    evidence of anything a lesson needs to explain.
    """
    changed = tuple(changed_files)
    diffstat = f"{len(changed)} file(s) changed" if changed else "no files changed"
    ci_log_tail = "" if ci_ok else (ci_log or "")[-max_log_chars:]
    return EvidenceBundle(
        kind=kind,
        ticket_title=ticket_title,
        ticket_body=ticket_body,
        outcome=outcome,
        changed_files=changed,
        diffstat=diffstat,
        ci_log_tail=ci_log_tail,
        guards=tuple(guards),
        reverted_workflows=tuple(reverted_workflows),
        repro_evidence=repro_evidence,
        review_verdict=review_verdict,
        remote_ci=remote_ci,
        attempts=attempts,
        tier=tier,
        shadow_tier=shadow_tier,
        agent_ok=agent_ok,
        agent_error=(agent_error or "")[:500],
    )


@dataclass(frozen=True)
class SynthesizedLesson:
    """What the synthesis call (or its fallback) produced."""

    what_attempted: str
    what_happened: str
    lesson_text: str
    references: tuple[str, ...] = field(default_factory=tuple)
    fallback: bool = False
    fallback_reason: str = ""
    model: str = ""
    tier: str = ""

    @property
    def tags(self) -> tuple[str, ...]:
        """Frontmatter tags to fold into the written lesson."""
        return (FALLBACK_TAG,) if self.fallback else ()

    def render_lesson(self) -> str:
        """The text for the note's ``## Lesson learned`` field.

        A fallback renders EXACTLY the deterministic text (byte-for-byte,
        matching :data:`hsai.knowledge.KNOWN_BOILERPLATE_PHRASES`) - the
        fallback is marked via :attr:`tags`, not by mutating this text, so
        the boilerplate detector still recognises it as boilerplate and the
        CI gate can tell it apart from a lazy, non-fallback duplicate.
        """
        if self.fallback:
            return self.lesson_text
        text = self.lesson_text
        if self.references and not any(r in text for r in self.references):
            cited = ", ".join(f"`{r}`" for r in self.references)
            text = f"{text} (cf. {cited})"
        return text


def _fallback(text: str, reason: str) -> SynthesizedLesson:
    return SynthesizedLesson(
        what_attempted="", what_happened="", lesson_text=text,
        fallback=True, fallback_reason=reason,
    )


def fallback_lesson(outcome: str, reason: str) -> SynthesizedLesson:
    """The deterministic lesson text, unconditionally - the invariant path.

    Used directly by the orchestrator in dry-run (no model call is ever made
    in dry-run) and internally by :func:`synthesize_lesson` on every failure
    mode, so there is exactly one source of truth for the fallback wording.
    """
    text = FALLBACK_PASS if outcome == "pass" else FALLBACK_FAIL
    return _fallback(text, reason)


def _synthesis_choice(cfg: CoreConfig) -> ModelChoice:
    """Cheap tier only - light preferred, else standard. Never heavy."""
    tier = next((t for t in _CHEAP_TIERS if t in cfg.tiers), cfg.default_tier)
    if tier not in cfg.tiers or tier == "heavy":
        tier = next((t for t in cfg.tiers if t != "heavy"), cfg.default_tier)
    return ModelChoice(
        tier=tier,
        model=cfg.tiers[tier].model,
        rationale="lesson synthesis: cheap tier only, never heavy",
        strategy="lesson-synthesis-v1",
    )


def build_prompt(evidence: EvidenceBundle) -> str:
    """The synthesis instruction: evidence-first, JSON-terminated."""
    return f"""{PROMPT_MARKER} for ai-hyperswarm-proto-core, an autonomous
self-improving AI-swarm harness. You did not write the change below and you
are not defending it - like a tester or reviewer reading a finished artifact
(OpenBMB/ChatDev's separation of roles), your only job is to report what the
evidence actually shows, the way a researcher writes a report FROM gathered
evidence rather than asserting a conclusion (assafelovic/gpt-researcher).

Evidence for this iteration:
{evidence.render()}

Ticket: {evidence.ticket_title}

{evidence.ticket_body}

Write three short, concrete fields, each grounded in the evidence above:
1. what_attempted - one or two sentences: what the ticket asked for and what
   was actually tried.
2. what_happened - one or two sentences: what the evidence shows actually
   occurred (guards, CI, review), stated as fact, not speculation.
3. lesson - ONE transferable lesson stated as a RULE a future iteration could
   follow. Not a restatement of what_happened.

If this change was inspired by a specific reference-set project's practice,
name it (e.g. "owner/repo") in `references`; otherwise leave it empty.

Answer with prose if you like, but END your reply with a fenced ```json block
containing exactly this object:
{{"what_attempted": "...", "what_happened": "...", "lesson": "...",
  "references": ["owner/repo", ...]}}

Keep every field under {MAX_FIELD_CHARS} characters.
"""


def _as_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _parse(text: str) -> dict | None:
    text = (text or "").strip()
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
    return raw if isinstance(raw, dict) else None


def synthesize_lesson(
    evidence: EvidenceBundle,
    cfg: CoreConfig,
    *,
    repo_root: str | Path,
    block: int,
    ai_runner: Runner = run,
) -> SynthesizedLesson:
    """Synthesize a lesson from ``evidence``, or fall back safely.

    Never raises: every failure mode (budget breach, agent error or timeout,
    empty output, unparseable or incomplete JSON) resolves to a
    :func:`fallback_lesson`, so the caller can always write a lesson.
    """
    fallback_text = FALLBACK_PASS if evidence.outcome == "pass" else FALLBACK_FAIL

    # Skip entirely on a hard budget breach - this call must never be able to
    # push a block over its own ceiling. No `claude -p` process is spawned.
    try:
        records = ledger.read_records(ledger.ledger_path(cfg, repo_root))
    except (OSError, ValueError):
        records = []
    decision = ledger.evaluate_budget(ledger.aggregate_block(records, block), cfg.budget)
    if decision.halt:
        return _fallback(fallback_text, f"hard budget breach ({decision.reason})")

    choice = _synthesis_choice(cfg)
    opts = cfg.knowledge.get("lesson_synthesis") or {}
    max_chars = int(opts.get("max_evidence_chars", DEFAULT_MAX_EVIDENCE_CHARS))
    timeout = float(opts.get("timeout_seconds", DEFAULT_TIMEOUT))

    prompt = build_prompt(evidence)
    if max_chars and len(prompt) > max_chars:
        prompt = prompt[:max_chars] + "\n... (evidence truncated)\n"

    ares = run_agent(prompt, choice, cfg, cwd=repo_root, runner=ai_runner, timeout=timeout)
    if not ares.ok:
        return _fallback(fallback_text, f"synthesis call failed: {(ares.error or 'no output')[:200]}")

    parsed = _parse(ares.text)
    if parsed is None:
        return _fallback(fallback_text, "synthesis produced no parseable JSON reply")

    what_attempted = str(parsed.get("what_attempted", "")).strip()[:MAX_FIELD_CHARS]
    what_happened = str(parsed.get("what_happened", "")).strip()[:MAX_FIELD_CHARS]
    lesson_text = str(parsed.get("lesson", "")).strip()[:MAX_FIELD_CHARS]
    if not (what_attempted and what_happened and lesson_text):
        return _fallback(fallback_text, "synthesis reply was missing a required field")

    references = tuple(
        str(r).strip() for r in _as_list(parsed.get("references")) if str(r).strip()
    )
    return SynthesizedLesson(
        what_attempted=what_attempted,
        what_happened=what_happened,
        lesson_text=lesson_text,
        references=references,
        model=choice.model,
        tier=choice.tier,
    )
