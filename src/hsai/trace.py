"""Iteration trajectories: the ordered decision record `run_once` leaves behind.

This is a different artifact from :mod:`hsai.trajectory`, which records ONE
agent invocation (a single ``claude -p`` run: prompt, steps, usage). This
module records the WHOLE iteration - every decision point ``run_once`` passes
through on its way from a fresh worktree to a merged (or recovered) PR: which
model was chosen, what each guard decided, what local and remote CI said, and
how the iteration ended. Before this module that sequence existed only as
prose scattered across a lesson note, a ledger row, and a PR body; nothing
captured it as one machine-readable artifact a future run could be replayed
against.

A :class:`Trajectory` is an ordered list of :class:`TraceEvent` - each with a
monotonic index, the elapsed seconds since the iteration started, and a
JSON-safe payload. It is written once per non-dry-run iteration to
``knowledge/trajectories/iter-<iteration>-<branch>.json`` - committed (unlike
``.hsai/traj/``, which stays local) so ``hsai replay`` can rerun a recorded
cassette through ``run_once`` and diff the trajectory it produces against this
golden one, turning a real production run into a regression test.

Synthesis: SWE-agent (a ``.traj`` file per run, replayed against recorded
tool output), langchain (cassette-recorded interactions replayed
deterministically in CI), and MetaGPT (explicit per-phase SOP artifacts as
the unit of auditability).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

TRAJECTORY_DIR = "knowledge/trajectories"

# Canonical event kinds, roughly in the order `run_once` can emit them. Kept as
# plain string constants (not an enum) so a trajectory JSON stays trivially
# readable without importing this module.
WORKTREE_CREATED = "worktree_created"
CI_LOCAL = "ci_local"
TICKET_CLAIMED = "ticket_claimed"
MODEL_SELECTED = "model_selected"
AGENT_INVOKED = "agent_invoked"
GUARD_VERDICT = "guard_verdict"
CI_REMOTE = "ci_remote"
PR_OPENED = "pr_opened"
MERGED = "merged"
RECOVERED = "recovered"

# Fields that are expected to vary between an original run and a later replay
# of the same cassette (wall-clock derived, or a filesystem path unique to
# this process) - excluded from :meth:`Trajectory.diff` so a byte-identical
# decision sequence compares equal even when it was captured on a different
# machine or at a different time.
_VOLATILE_PAYLOAD_KEYS = frozenset({"worktree"})


@dataclass
class TraceEvent:
    """One decision point: a monotonic index, elapsed time, and its payload."""

    index: int
    kind: str
    elapsed_seconds: float
    payload: dict[str, Any] = field(default_factory=dict)

    def _comparable_payload(self) -> dict[str, Any]:
        return {k: v for k, v in self.payload.items() if k not in _VOLATILE_PAYLOAD_KEYS}


@dataclass
class Trajectory:
    """One iteration's ordered decision record - the unit this module persists."""

    iteration: int
    branch: str
    block: int = 0
    ticket: int | None = None
    kind: str = ""
    events: list[TraceEvent] = field(default_factory=list)
    outcome: str = ""

    def add_event(self, kind: str, *, elapsed_seconds: float, **payload: Any) -> TraceEvent:
        """Append the next event, stamped with a monotonic index."""
        event = TraceEvent(
            index=len(self.events) + 1,
            kind=kind,
            elapsed_seconds=round(max(0.0, elapsed_seconds), 3),
            payload=payload,
        )
        self.events.append(event)
        return event

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    def diff(self, golden: "Trajectory") -> list[str]:
        """Compare this trajectory's decision sequence against a golden one.

        Ignores wall-clock-derived fields (``elapsed_seconds``, volatile
        payload keys) so two runs of the SAME cassette compare equal even
        though real time passed differently between them. Returns a list of
        human-readable divergences; empty means the two trajectories made the
        exact same sequence of decisions.
        """
        diffs: list[str] = []
        if self.outcome != golden.outcome:
            diffs.append(f"outcome: {self.outcome!r} != {golden.outcome!r}")
        if self.kind != golden.kind:
            diffs.append(f"kind: {self.kind!r} != {golden.kind!r}")
        if self.ticket != golden.ticket:
            diffs.append(f"ticket: {self.ticket!r} != {golden.ticket!r}")

        a, b = self.events, golden.events
        for i in range(max(len(a), len(b))):
            ea = a[i] if i < len(a) else None
            eb = b[i] if i < len(b) else None
            if ea is None:
                diffs.append(f"event {i + 1}: missing (golden has {eb.kind!r})")
                continue
            if eb is None:
                diffs.append(f"event {i + 1}: unexpected {ea.kind!r} (golden has none)")
                continue
            if ea.kind != eb.kind:
                diffs.append(f"event {i + 1}: kind {ea.kind!r} != {eb.kind!r}")
                continue
            pa, pb = ea._comparable_payload(), eb._comparable_payload()
            if pa != pb:
                diffs.append(f"event {i + 1} ({ea.kind}): payload {pa!r} != {pb!r}")
        return diffs


def _safe_branch(branch: str) -> str:
    """A branch name flattened to a single path component.

    Branch names carry a namespace prefix (``hsai/iter-...``); keeping the
    literal ``/`` would nest the trajectory file under an extra directory
    instead of writing one flat, greppable file per iteration.
    """
    return branch.replace("/", "-")


def path_for(repo_root: str | Path, iteration: int, branch: str) -> Path:
    return Path(repo_root) / TRAJECTORY_DIR / f"iter-{iteration}-{_safe_branch(branch)}.json"


def write(traj: Trajectory, repo_root: str | Path) -> Path:
    """Persist (or refresh) one iteration's trajectory as a single JSON file."""
    path = path_for(repo_root, traj.iteration, traj.branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(traj.to_json() + "\n", encoding="utf-8")
    return path


def read(path: str | Path) -> Trajectory:
    """Parse a trajectory back off disk."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    events = [TraceEvent(**e) for e in data.pop("events", []) or []]
    return Trajectory(events=events, **data)


def load(repo_root: str | Path, iteration: int, branch: str) -> Trajectory:
    """Resolve and read one iteration's trajectory by its identity."""
    return read(path_for(repo_root, iteration, branch))
