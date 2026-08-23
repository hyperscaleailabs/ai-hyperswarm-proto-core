from hsai import gitops
from hsai.proc import Proc


def _fake(stdout: str = ""):
    calls = []

    def runner(cmd, **kwargs):
        calls.append((list(cmd), kwargs.get("cwd")))
        return Proc(cmd, 0, stdout, "")

    runner.calls = calls
    return runner


def test_merge_base_returns_trimmed_sha():
    runner = _fake("deadbeef\n")
    assert gitops.merge_base("HEAD", "origin/main", cwd="/repo", runner=runner) == "deadbeef"
    assert runner.calls[0][0] == ["git", "merge-base", "HEAD", "origin/main"]


def test_diff_paths_parses_name_only_output():
    runner = _fake("tests/test_ci.py\nsrc/hsai/ci.py\n")
    paths = gitops.diff_paths("origin/main", cwd="/repo", runner=runner)
    assert paths == ["tests/test_ci.py", "src/hsai/ci.py"]
    assert runner.calls[0][0] == ["git", "diff", "--name-only", "origin/main...HEAD"]


def test_diff_text_returns_the_branch_diff_verbatim():
    """What the review gate reads: paths say what changed, not whether it is right."""
    patch = "diff --git a/src/hsai/ci.py b/src/hsai/ci.py\n+def gate(): ...\n"
    runner = _fake(patch)
    assert gitops.diff_text("deadbeef", cwd="/repo", runner=runner) == patch
    assert runner.calls[0][0] == ["git", "diff", "deadbeef...HEAD"]


def test_worktree_diff_reads_the_uncommitted_change_against_head():
    """What the pre-PR acceptance audit reads: a refusal must cost no commit."""
    patch = "diff --git a/src/hsai/x.py b/src/hsai/x.py\n+def x(): ...\n"
    runner = _fake(patch)
    assert gitops.worktree_diff(cwd="/repo", runner=runner) == patch
    assert runner.calls[0][0] == ["git", "diff", "HEAD"]


def test_worktree_numstat_reports_per_file_line_churn():
    runner = _fake("3\t1\tsrc/hsai/x.py\n")
    assert gitops.worktree_numstat(cwd="/repo", runner=runner) == "3\t1\tsrc/hsai/x.py\n"
    assert runner.calls[0][0] == ["git", "diff", "--numstat", "HEAD"]


def test_stage_intent_to_add_makes_untracked_files_diffable():
    """Without `-N`, a brand-new file is invisible to `git diff` - and a new
    feature's diff is mostly new files."""
    runner = _fake()
    gitops.stage_intent_to_add(cwd="/repo", runner=runner)
    assert runner.calls[0][0] == ["git", "add", "-N", "--", "."]


def test_create_detached_worktree_builds_expected_path():
    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, "/repo\n", "")
        return Proc(cmd, 0, "", "")

    proc, path = gitops.create_detached_worktree(
        ".hsai/worktrees", "repro-check-abcd1234", "origin/main", cwd="/repo", runner=runner
    )
    assert proc.ok
    assert path == "/repo/.hsai/worktrees/repro-check-abcd1234"
