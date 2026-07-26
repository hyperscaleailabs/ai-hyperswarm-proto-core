"""Reproduce-before-fix guard for heal and bugfix tickets.

The methodology says "reproduce the bug in an E2E setting first" (see
CLAUDE.md and run-llama/llama_index's fix-stream discipline: regression tests
that pin the exact broken behavior). This module enforces that contract in
code: a heal or bugfix PR must add or modify a test that FAILS against the
pre-fix (parent) tree and PASSES on the fix branch. Without that transition,
a "fix" may be green for the wrong reason.

:func:`check_repro` is the local guard the orchestrator runs before a PR is
opened. :func:`evaluate_pr` is the remote counterpart driven by the
``hsai repro-check`` CLI command from CI, so the same contract is enforced as
a pre-merge gate on GitHub, not only inside the loop.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import gitops
from .proc import Runner, run

# Ticket kinds exempt from the contract regardless of loop-kind (docs/chore
# work may legitimately ship without a regression test).
_EXEMPT_TITLE_PREFIXES = ("docs:", "chore:")
_BUGFIX_TITLE_PREFIXES = ("fix:",)

# Matches a loop-filed PR title of the form "{kind}: {ticket title}".
_PR_TITLE_RE = re.compile(r"^(heal|implement|improve):\s*(.*)$", re.IGNORECASE)


def requires_repro_guard(kind: str, ticket_title: str) -> bool:
    """Is this ticket subject to the reproduce-before-fix contract?

    ``heal`` tickets (the loop's own auto-heal path) and ``fix:``-titled
    bugfix tickets must prove the bug was real. ``docs:``/``chore:`` tickets
    are exempt regardless of loop-kind.
    """
    lowered = ticket_title.strip().lower()
    if lowered.startswith(_EXEMPT_TITLE_PREFIXES):
        return False
    if kind == "heal":
        return True
    return lowered.startswith(_BUGFIX_TITLE_PREFIXES)


def _is_test_file(path: str) -> bool:
    p = Path(path)
    if p.suffix != ".py":
        return False
    if p.name.startswith("test_"):
        return True
    return "tests" in p.parts


def changed_test_files(paths: Iterable[str]) -> list[str]:
    """Filter changed paths down to test files (added or modified)."""
    return [p for p in paths if _is_test_file(p)]


@dataclass
class ReproResult:
    ok: bool
    reason: str
    test_files: tuple[str, ...] = ()
    fix_ok: bool | None = None
    parent_ok: bool | None = None
    log: str = ""


def check_repro(
    *,
    repo_root: str,
    wt: str,
    base_ref: str,
    test_files: list[str],
    worktrees_dir: str,
    runner: Runner = run,
) -> ReproResult:
    """Prove ``test_files`` reproduce a real bug.

    Runs the (new/modified) ``test_files`` against the fix branch - they must
    pass - then checks out ``base_ref`` (the pre-fix / parent state) into a
    throwaway detached worktree, overlays the same test files on top of that
    old source tree, and runs them there - they must fail. That failing-then-
    passing transition is the reproduction evidence.
    """
    if not test_files:
        return ReproResult(
            ok=False,
            reason=(
                "no test file added or modified; heal/bugfix PRs must add or "
                "modify a regression test that proves the bug"
            ),
        )

    fix_run = runner(["pytest", *test_files], cwd=wt)
    if not fix_run.ok:
        return ReproResult(
            ok=False,
            reason="the new/modified test does not pass on the fix branch",
            test_files=tuple(test_files),
            fix_ok=False,
            log=fix_run.stdout + fix_run.stderr,
        )

    _, parent_wt = gitops.create_detached_worktree(
        worktrees_dir, f"repro-check-{uuid4().hex[:8]}", base_ref,
        cwd=repo_root, runner=runner,
    )
    try:
        for rel in test_files:
            src = Path(wt) / rel
            dst = Path(parent_wt) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text())
        parent_run = runner(["pytest", *test_files], cwd=parent_wt)
    finally:
        gitops.remove_worktree(parent_wt, cwd=repo_root, runner=runner)

    combined_log = fix_run.stdout + fix_run.stderr + parent_run.stdout + parent_run.stderr
    if parent_run.ok:
        return ReproResult(
            ok=False,
            reason=(
                "the new/modified test also PASSES on the pre-fix tree - it "
                "does not reproduce a real bug"
            ),
            test_files=tuple(test_files),
            fix_ok=True,
            parent_ok=True,
            log=combined_log,
        )

    return ReproResult(
        ok=True,
        reason="reproduced: fails on the pre-fix tree, passes on the fix branch",
        test_files=tuple(test_files),
        fix_ok=True,
        parent_ok=False,
        log=combined_log,
    )


def render_evidence(result: ReproResult) -> str:
    """Structured markdown recording the failing-then-passing transition."""
    if not result.test_files:
        return f"_(not applicable: {result.reason})_"
    files = ", ".join(f"`{f}`" for f in result.test_files)
    parent = "FAIL (reproduces the bug)" if result.parent_ok is False else "PASS (did not reproduce)"
    fix = "PASS" if result.fix_ok else "FAIL"
    return (
        f"- regression test(s): {files}\n"
        f"- pre-fix (parent) run: {parent}\n"
        f"- fix-branch run: {fix}\n"
        f"- verdict: **{'reproduced' if result.ok else 'blocked'}** - {result.reason}"
    )


def classify_pr_title(pr_title: str) -> tuple[str, str]:
    """Split a loop-filed PR title (``"{kind}: {ticket title}"``) into its parts."""
    m = _PR_TITLE_RE.match(pr_title.strip())
    if not m:
        return "implement", pr_title.strip()
    return m.group(1).lower(), m.group(2).strip()


def evaluate_pr(
    *,
    pr_title: str,
    repo_dir: str,
    base_ref: str,
    worktrees_dir: str,
    runner: Runner = run,
) -> ReproResult:
    """Remote-CI entry point: mirrors the orchestrator's local guard exactly.

    ``repo_dir`` is the PR branch checkout CI already has on disk (the "fix"
    state); ``base_ref`` (e.g. ``origin/main``) is fetched separately by the
    CI job before this runs.
    """
    kind, ticket_title = classify_pr_title(pr_title)
    if not requires_repro_guard(kind, ticket_title):
        return ReproResult(ok=True, reason="exempt: not a heal/bugfix ticket")
    test_files = changed_test_files(
        gitops.diff_paths(base_ref, cwd=repo_dir, runner=runner)
    )
    return check_repro(
        repo_root=repo_dir, wt=repo_dir, base_ref=base_ref,
        test_files=test_files, worktrees_dir=worktrees_dir, runner=runner,
    )
