"""Protected-surface policy: guard integrity and the self-modification gate.

The loop already learned once that a worker can change the rules it is judged
by (a worker slipped mypy into CI; ``orchestrator.run_once`` grew a hard-coded
revert of any ``.github/workflows`` edit). That fix was one string check
covering one path. Nothing stopped a worker from raising the budget ceilings
in ``.ai-swarm/core.yaml``, weakening the well-formedness rules in
``tickets.py``, deleting tests to turn a red build green, or editing this
very module to disable itself.

``protected_surfaces`` in ``core.yaml`` is the declared allowlist of what a
diff may touch (OpenBMB/ChatDev's "validate untrusted input against a
declared allowlist" applied to our own agent's diff - the untrusted input
here is the PR). :func:`evaluate` is the pure decision core: given the facts
of one diff (changed paths, a test-count delta, the ticket's labels, and the
policy itself) it returns a verdict. It performs no I/O so it is trivially
unit-testable; the small amount of I/O needed to gather those facts (reading
git refs, walking a worktree) lives in the functions below it, mirroring how
:mod:`hsai.repro` separates its pure ``classify_pr_title`` from its I/O-bound
``check_repro``.

:func:`evaluate` is driven from two places: ``orchestrator.run_once`` (the
loop's own guard, replacing the old ad-hoc workflow revert) and the
``hsai policy-check`` CLI command (the CI gate that binds human PRs too).
"""
from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .proc import Runner, run

# Modes a protected surface can be declared with.
REVERT = "revert"
REQUIRE_LABEL = "require_label"
DENY = "deny"
_VALID_MODES = (REVERT, REQUIRE_LABEL, DENY)

# The architect's escape hatch: apply this label to a ticket to approve a
# require_label surface being touched (or a net test-count decrease).
GUARDS_APPROVED_LABEL = "guards-approved"

# Not a real path on disk - the synthetic "surface" a test-count regression is
# reported against, so it renders through the same Violation shape as a
# glob-matched one.
TEST_INTEGRITY_SURFACE = "<test-function-count>"


@dataclass(frozen=True)
class ProtectedSurface:
    """One declared entry from ``protected_surfaces`` in core.yaml."""

    glob: str
    mode: str
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"protected surface {self.glob!r}: unknown mode {self.mode!r} "
                f"(must be one of {_VALID_MODES})"
            )


@dataclass(frozen=True)
class Violation:
    """One protected surface a diff failed to satisfy."""

    glob: str
    mode: str
    paths: tuple[str, ...]
    rationale: str
    reason: str

    def describe(self) -> str:
        where = ", ".join(f"`{p}`" for p in self.paths) if self.paths else "(no path)"
        tail = f" - {self.rationale}" if self.rationale else ""
        return f"[{self.mode}] {self.glob}: {where} - {self.reason}{tail}"


@dataclass(frozen=True)
class PolicyVerdict:
    """The result of grading one diff against the protected-surfaces policy."""

    allowed: bool
    violations: tuple[Violation, ...] = ()
    actions: tuple[ProtectedSurface, ...] = ()  # revert-mode surfaces that matched

    def summary(self) -> str:
        if not self.violations:
            return "no violations"
        return "; ".join(v.describe() for v in self.violations)


def _as_surface(item: ProtectedSurface | dict) -> ProtectedSurface:
    if isinstance(item, ProtectedSurface):
        return item
    return ProtectedSurface(
        glob=item["glob"],
        mode=item.get("mode", REQUIRE_LABEL),
        rationale=item.get("rationale", ""),
    )


def matches(path: str, glob: str) -> bool:
    """Does ``path`` fall under a declared glob (``**`` included)?"""
    return fnmatchcase(path, glob)


def revert_pathspec(glob: str) -> str:
    """Turn a directory-style glob into a pathspec ``gitops.restore_pathspec``
    can restore (e.g. ``".github/workflows/**"`` -> ``".github/workflows"``).
    """
    return glob.rstrip("/*") or "."


def evaluate(
    changed_paths: Iterable[str],
    test_delta: int,
    ticket_labels: Iterable[str],
    policy: Iterable[ProtectedSurface | dict],
) -> PolicyVerdict:
    """Grade one diff against the declared protected surfaces.

    Pure: every input is an already-gathered fact (changed paths, the
    test-function-count delta, the ticket's labels, and the policy itself) -
    no git, no filesystem, no GitHub. An empty ``policy`` allows everything
    (as long as ``test_delta`` is not a regression).

    - ``revert`` surfaces never block: a match is returned in ``.actions`` for
      the caller to restore (preserves the pre-existing workflow-revert
      behaviour exactly).
    - ``require_label`` surfaces block unless ``ticket_labels`` carries
      ``guards-approved``.
    - ``deny`` surfaces always block, regardless of labels - they are
      append-only invariants, not something even the architect waives inline.
    - A net decrease in test-function count (``test_delta < 0``) is itself a
      violation unless ``guards-approved`` is present, independent of which
      files changed - it is the guard against "delete the test to turn a red
      build green".
    """
    surfaces = [_as_surface(s) for s in (policy or ())]
    paths = list(changed_paths or ())
    approved = GUARDS_APPROVED_LABEL in set(ticket_labels or ())

    violations: list[Violation] = []
    actions: list[ProtectedSurface] = []

    for surface in surfaces:
        matched = tuple(p for p in paths if matches(p, surface.glob))
        if not matched:
            continue
        if surface.mode == REVERT:
            actions.append(surface)
        elif surface.mode == DENY:
            violations.append(
                Violation(
                    glob=surface.glob, mode=DENY, paths=matched,
                    rationale=surface.rationale,
                    reason="deny-mode surface; it cannot be edited by a worker",
                )
            )
        elif surface.mode == REQUIRE_LABEL and not approved:
            violations.append(
                Violation(
                    glob=surface.glob, mode=REQUIRE_LABEL, paths=matched,
                    rationale=surface.rationale,
                    reason=f"missing '{GUARDS_APPROVED_LABEL}' label on the ticket",
                )
            )

    if test_delta < 0 and not approved:
        violations.append(
            Violation(
                glob=TEST_INTEGRITY_SURFACE, mode=REQUIRE_LABEL, paths=(),
                rationale="tests must not be deleted to turn a red build green",
                reason=f"net test-function count decreased by {-test_delta}",
            )
        )

    return PolicyVerdict(allowed=not violations, violations=tuple(violations), actions=tuple(actions))


# --- AST-based test-function counting (I/O helpers around a pure core) ------


def function_names(source: str) -> set[str]:
    """Every ``test*`` function/method defined anywhere in ``source``.

    Parsed by AST, not collected by running pytest, so it works against a
    base ref's tree without installing it. Malformed/unparsable source counts
    as zero rather than raising - a syntax error is a different guard's job.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }


def count_test_functions(sources: Iterable[str]) -> int:
    """Total ``test*`` function count across a collection of source files."""
    return sum(len(function_names(src)) for src in sources)


def test_function_delta(base_sources: Iterable[str], pr_sources: Iterable[str]) -> int:
    """PR total minus base total, across whatever sources the caller gathered.

    Pure: ``sources`` are plain strings, gathered however the caller likes.
    Comparing the WHOLE test corpus (not per file) is what makes a pure
    rename or a file move net zero - the function moves, but nothing is lost,
    so the total on each side is unchanged. Only an actual deletion (no
    matching addition anywhere else) moves the total down.
    """
    return count_test_functions(pr_sources) - count_test_functions(base_sources)


def _is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    if p.suffix != ".py":
        return False
    return p.name.startswith("test_") or "tests" in p.parts


def _test_sources_at_ref(ref: str, *, cwd: str, runner: Runner) -> list[str]:
    """Read every tracked test file's content as of ``ref`` (I/O: shells out
    to git; not part of the pure core above)."""
    listing = runner(["git", "ls-tree", "-r", "--name-only", ref], cwd=cwd)
    sources: list[str] = []
    for path in listing.stdout.splitlines():
        path = path.strip()
        if not path or not _is_test_path(path):
            continue
        shown = runner(["git", "show", f"{ref}:{path}"], cwd=cwd)
        if shown.ok:
            sources.append(shown.stdout)
    return sources


def _test_sources_in_worktree(cwd: str, *, runner: Runner) -> list[str]:
    """Read every test file's content currently on disk in ``cwd``.

    ``--others --exclude-standard`` alongside the default tracked (cached)
    listing so a freshly-added, not-yet-``git add``-ed test file still counts
    - otherwise it would look invisible to the PR side and bias the delta
    toward a false "decrease".
    """
    listing = runner(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=cwd
    )
    sources: list[str] = []
    for path in listing.stdout.splitlines():
        path = path.strip()
        if not path or not _is_test_path(path):
            continue
        try:
            sources.append((Path(cwd) / path).read_text())
        except OSError:
            continue
    return sources


def test_function_delta_for_tree(
    *,
    base_ref: str,
    repo_dir: str,
    worktree: str,
    runner: Runner = run,
) -> int:
    """I/O glue: gather the base-ref and PR-tree test sources, then diff them
    through the pure :func:`test_function_delta`."""
    base_sources = _test_sources_at_ref(base_ref, cwd=repo_dir, runner=runner)
    pr_sources = _test_sources_in_worktree(worktree, runner=runner)
    return test_function_delta(base_sources, pr_sources)
