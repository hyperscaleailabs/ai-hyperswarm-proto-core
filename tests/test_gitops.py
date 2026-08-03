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


def test_staged_diff_compares_the_index_against_the_base():
    runner = _fake("+++ b/src/hsai/x.py\n")
    out = gitops.staged_diff("basesha", cwd="/repo", runner=runner)
    assert out == "+++ b/src/hsai/x.py\n"
    assert runner.calls[0][0] == ["git", "diff", "--cached", "basesha"]


def test_commit_all_stages_everything_first():
    runner = _fake()
    gitops.commit_all("msg", cwd="/repo", runner=runner)
    assert [c[0] for c in runner.calls] == [
        ["git", "add", "-A"], ["git", "commit", "-m", "msg"]
    ]


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
