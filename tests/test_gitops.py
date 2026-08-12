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
