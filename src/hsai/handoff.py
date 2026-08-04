"""Failure-aware retry handoff: what a failed attempt leaves the next worker.

Before this module, ``_recover_failed`` closed the PR, stamped ``attempts:N``
on the ticket, and unassigned it - and that was the entire memory of the
failure. The next worker to claim the ticket got the same tier, the same
model, and a prompt byte-identical to the one that just failed: one retry,
spent blind on a known-failing configuration.

A :class:`Handoff` is the fix: a structured record of what the failed attempt
tried and how it failed, posted as a machine-parseable comment on the ticket
itself. GitHub issue comments are already the loop's durable ticket state, so
this introduces no new storage and stays fully auditable in the ticket trail -
`hsai replay <trajectory_id>` still holds the full run if deeper forensics are
needed.

Synthesis: openai/swarm's handoff primitive - transferring control to another
agent *carrying the conversation state* rather than starting cold - is the
central idea; SWE-agent's history processors condition a retry on the prior
observation instead of restarting blind, which is why :meth:`Handoff.render_evidence`
is deliberately evidence-only (failing steps, error text, files touched) and
never a "conclusion" the next worker is told to trust.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from .github import add_issue_comment, list_issue_comments
from .proc import Runner, run

# A stable heading every handoff comment carries, so `read_latest` can find the
# most recent one among a ticket's ordinary human/bot comments without
# depending on comment ordering metadata beyond "most recent wins".
HEADING = "## hsai handoff"

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# What the rendered evidence quotes of the agent's own error text.
_ERROR_EXCERPT_CHARS = 800


def clip_error(text: str, limit: int = _ERROR_EXCERPT_CHARS) -> str:
    """Trim an agent error to a bounded excerpt before it goes into a Handoff."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"


@dataclass(frozen=True)
class Handoff:
    """What one failed attempt leaves behind for the worker that retries it."""

    attempt: int
    tier: str
    model: str
    remote_ci: str
    failing_steps: tuple[str, ...] = ()
    agent_error: str = ""
    changed_paths: tuple[str, ...] = ()
    trajectory_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    def render_comment(self) -> str:
        """The full ticket comment: a stable heading over a fenced JSON block."""
        return f"{HEADING}\n\n```json\n{self.to_json()}\n```\n"

    def render_evidence(self) -> str:
        """Human-readable evidence for `_task_prompt`'s 'Previous attempt' section.

        Evidence only, never a verdict: the next worker is pointed at what
        happened and told to diagnose independently, not handed a conclusion
        to trust (see SWE-agent's history-processor discipline).
        """
        lines = [
            f"- attempt {self.attempt} used tier `{self.tier}` (model `{self.model}`)",
            f"- remote CI concluded: {self.remote_ci}",
        ]
        if self.failing_steps:
            lines.append(f"- failing local CI steps: {', '.join(self.failing_steps)}")
        if self.changed_paths:
            lines.append(f"- files touched last attempt: {', '.join(self.changed_paths)}")
        if self.trajectory_id:
            lines.append(
                f"- trajectory `{self.trajectory_id}` "
                f"(`hsai replay {self.trajectory_id}` for the full run)"
            )
        if self.agent_error:
            lines.append(f"- agent error excerpt:\n```\n{self.agent_error}\n```")
        return "\n".join(lines)


def post(repo: str, ticket: int, handoff: Handoff, *, runner: Runner = run) -> None:
    """Record ``handoff`` as a comment on ``ticket`` - the durable escalation trail."""
    add_issue_comment(repo, ticket, handoff.render_comment(), runner=runner)


def _parse(body: str) -> Handoff | None:
    """Best-effort parse of one comment body; ``None`` if it isn't a valid handoff."""
    if HEADING not in body:
        return None
    match = _JSON_BLOCK.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Handoff(
            attempt=int(data["attempt"]),
            tier=str(data["tier"]),
            model=str(data["model"]),
            remote_ci=str(data["remote_ci"]),
            failing_steps=tuple(data.get("failing_steps") or ()),
            agent_error=str(data.get("agent_error") or ""),
            changed_paths=tuple(data.get("changed_paths") or ()),
            trajectory_id=data.get("trajectory_id"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def read_latest(repo: str, ticket: int, *, runner: Runner = run) -> Handoff | None:
    """Parse the most recent handoff comment on ``ticket``.

    Tolerates a ticket with no comments, comments that aren't handoffs, and a
    handoff comment mangled by manual editing - all degrade to ``None`` rather
    than raising, so a corrupted comment can never break the loop.
    """
    comments = list_issue_comments(repo, ticket, runner=runner)
    for body in reversed(comments):
        parsed = _parse(body)
        if parsed is not None:
            return parsed
    return None
