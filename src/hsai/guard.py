"""Protected-invariants gate: classify changed paths as allowed or protected.

The loop's safety guarantees (subscription-only enforcement, the green-CI
merge gate, the ticket/lesson traceability requirements, workflow parity) are
themselves enforced by code and config that a worker running under
``permission_mode=acceptEdits`` is otherwise free to edit. A diff that quietly
weakens one of those surfaces still reports a green build, so nothing else in
the loop would ever notice. :func:`classify` is the single place that decides
whether a diff touches a protected surface; :func:`orchestrator.run_once`
reverts what it flags unless the claimed ticket carries
``ESCAPE_HATCH_LABEL``, in which case the edit is allowed to stand and the
caller records that the gate was consciously opened.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .config import CoreConfig

ESCAPE_HATCH_LABEL = "approved:invariant-change"

DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    ".github/workflows/",
    ".ai-swarm/core.yaml",
    "src/hsai/ai.py",
    "src/hsai/ci.py",
    "src/hsai/guard.py",
)


@dataclass(frozen=True)
class GuardResult:
    """Outcome of classifying one iteration's changed paths."""

    protected: tuple[str, ...]  # changed paths that matched a protected prefix
    escaped: bool               # escape-hatch label present, so protected edits stand

    @property
    def should_revert(self) -> bool:
        return bool(self.protected) and not self.escaped


def protected_paths_for(cfg: CoreConfig) -> tuple[str, ...]:
    """Read ``constraints.protected_paths`` from core.yaml, defaulting if unset."""
    raw = cfg.constraints.get("protected_paths")
    if not raw:
        return DEFAULT_PROTECTED_PATHS
    return tuple(raw)


def classify(
    changed_paths: Sequence[str],
    protected_paths: Sequence[str],
    labels: Sequence[str] = (),
) -> GuardResult:
    """Split ``changed_paths`` into protected vs allowed.

    A path is protected if it starts with one of ``protected_paths``. The
    escape hatch only fires when a protected path was actually touched - a
    ticket merely carrying the label changes nothing on its own.
    """
    protected = tuple(
        p for p in changed_paths
        if any(p.startswith(prefix) for prefix in protected_paths)
    )
    escaped = bool(protected) and ESCAPE_HATCH_LABEL in labels
    return GuardResult(protected=protected, escaped=escaped)
