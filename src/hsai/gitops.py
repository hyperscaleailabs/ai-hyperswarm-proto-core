"""Git operations: worktrees, syncing, branching, committing, pushing.

Each worker gets its own worktree so parallel workers never share a checkout.
"""
from __future__ import annotations

from pathlib import Path

from .proc import Proc, Runner, run


def _git(args: list[str], *, cwd: str | None, runner: Runner) -> Proc:
    return runner(["git", *args], cwd=cwd)


def repo_root(cwd: str | None = None, runner: Runner = run) -> str:
    p = _git(["rev-parse", "--show-toplevel"], cwd=cwd, runner=runner)
    return p.stdout.strip()


def sync_main(default_branch: str, *, cwd: str | None = None, runner: Runner = run) -> Proc:
    """Fetch the latest default branch from origin.

    Deliberately does NOT check out or mutate the shared working tree: workers
    create their worktrees from ``origin/<default_branch>``, which is what makes
    running several workers against one clone safe.
    """
    return _git(["fetch", "origin", default_branch], cwd=cwd, runner=runner)


def create_worktree(
    worktrees_dir: str,
    branch: str,
    *,
    base: str = "origin/main",
    cwd: str | None = None,
    runner: Runner = run,
) -> tuple[Proc, str]:
    """Create a fresh worktree on a new ``branch`` off ``base``.

    Returns the process result and the worktree path.
    """
    root = repo_root(cwd=cwd, runner=runner) or (cwd or ".")
    wt_path = str(Path(root) / worktrees_dir / branch)
    proc = _git(
        ["worktree", "add", "-b", branch, wt_path, base],
        cwd=cwd,
        runner=runner,
    )
    return proc, wt_path


def remove_worktree(wt_path: str, *, cwd: str | None = None, runner: Runner = run) -> Proc:
    return _git(["worktree", "remove", "--force", wt_path], cwd=cwd, runner=runner)


def create_detached_worktree(
    worktrees_dir: str,
    name: str,
    ref: str,
    *,
    cwd: str | None = None,
    runner: Runner = run,
) -> tuple[Proc, str]:
    """Create a detached worktree pinned at ``ref``.

    Used to inspect a pre-fix (parent) state without touching any branch - the
    reproduce-before-fix guard checks the new/modified test out of the fix
    branch and runs it here to prove the bug was real.
    """
    root = repo_root(cwd=cwd, runner=runner) or (cwd or ".")
    wt_path = str(Path(root) / worktrees_dir / name)
    proc = _git(["worktree", "add", "--detach", wt_path, ref], cwd=cwd, runner=runner)
    return proc, wt_path


def merge_base(a: str, b: str, *, cwd: str | None = None, runner: Runner = run) -> str:
    p = _git(["merge-base", a, b], cwd=cwd, runner=runner)
    return p.stdout.strip()


def diff_paths(base_ref: str, *, cwd: str | None = None, runner: Runner = run) -> list[str]:
    """Paths that differ between ``base_ref`` and HEAD."""
    p = _git(["diff", "--name-only", f"{base_ref}...HEAD"], cwd=cwd, runner=runner)
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]


def diff_text(base_ref: str, *, cwd: str | None = None, runner: Runner = run) -> str:
    """The full textual diff between ``base_ref`` and HEAD.

    What the independent review gate (:mod:`hsai.review`) actually reads: paths
    alone say what was touched, not whether the change is correct.
    """
    p = _git(["diff", f"{base_ref}...HEAD"], cwd=cwd, runner=runner)
    return p.stdout


def stage_intent_to_add(*, cwd: str, runner: Runner = run) -> Proc:
    """Record *intent to add* for every untracked file (``git add -N``).

    A brand-new file is invisible to ``git diff``, so a diff taken before the
    worker's work is committed would silently omit exactly the files a new
    feature consists of. ``-N`` registers the path in the index without staging
    its content, which is enough for ``git diff`` to render it as a full
    addition. Nothing is committed, and the later ``git add -A`` in
    :func:`commit_all` is unaffected.
    """
    return _git(["add", "-N", "--", "."], cwd=cwd, runner=runner)


def worktree_diff(*, cwd: str, runner: Runner = run) -> str:
    """The full textual diff of the working tree against HEAD.

    The pre-commit counterpart of :func:`diff_text`: what the pre-PR acceptance
    audit (:mod:`hsai.audit`) reads, so a rejected change never even reaches a
    local commit. Pair with :func:`stage_intent_to_add` to include new files.
    """
    return _git(["diff", "HEAD"], cwd=cwd, runner=runner).stdout


def worktree_numstat(*, cwd: str, runner: Runner = run) -> str:
    """Raw ``git diff --numstat HEAD`` output: per-file added/deleted counts.

    A superset of :func:`diff_paths` for the working tree - it names every
    changed path *and* says how much of it changed, which is what the audit's
    diff-size band is measured against. Parsed by :func:`hsai.audit.parse_numstat`.
    """
    return _git(["diff", "--numstat", "HEAD"], cwd=cwd, runner=runner).stdout


def has_changes(*, cwd: str, runner: Runner = run) -> bool:
    p = _git(["status", "--porcelain"], cwd=cwd, runner=runner)
    return bool(p.stdout.strip())


def changed_paths(*, cwd: str, runner: Runner = run) -> list[str]:
    """Paths changed in the worktree (modified, added, or untracked)."""
    p = _git(["status", "--porcelain"], cwd=cwd, runner=runner)
    paths: list[str] = []
    for line in p.stdout.splitlines():
        entry = line[3:] if len(line) > 3 else line.strip()
        if "->" in entry:  # rename: "old -> new"
            entry = entry.split("->", 1)[1]
        entry = entry.strip().strip('"')
        if entry:
            paths.append(entry)
    return paths


def diff_pathspec(pathspec: str, *, cwd: str, runner: Runner = run) -> str:
    """Diff of the working tree against HEAD, scoped to ``pathspec``.

    Call this BEFORE :func:`restore_pathspec` discards the edits it captures -
    it exists so a change the loop is about to revert (worker edits under
    ``.github/workflows/``, which only the architect may apply - see
    :func:`hsai.orchestrator.run_once`) is not lost, just relocated into the PR
    body as a patch for the architect to apply by hand. Stages *intent to add*
    for the pathspec first (see :func:`stage_intent_to_add`) so a brand-new
    file under it renders as a full addition instead of being invisible to a
    plain ``git diff``.
    """
    _git(["add", "-N", "--", pathspec], cwd=cwd, runner=runner)
    return _git(["diff", "HEAD", "--", pathspec], cwd=cwd, runner=runner).stdout


def restore_pathspec(pathspec: str, *, cwd: str, runner: Runner = run) -> None:
    """Discard both tracked edits and new files under ``pathspec``."""
    _git(["checkout", "HEAD", "--", pathspec], cwd=cwd, runner=runner)
    _git(["clean", "-fd", pathspec], cwd=cwd, runner=runner)


def commit_all(message: str, *, cwd: str, runner: Runner = run) -> Proc:
    _git(["add", "-A"], cwd=cwd, runner=runner)
    return _git(["commit", "-m", message], cwd=cwd, runner=runner)


def push_branch(branch: str, *, cwd: str, runner: Runner = run) -> Proc:
    return _git(["push", "-u", "origin", branch], cwd=cwd, runner=runner)
