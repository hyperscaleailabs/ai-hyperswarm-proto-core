"""Reproduce-before-fix guard: classification, the check itself, and the
remote (CI) entry point.
"""
from pathlib import Path

from hsai import repro
from hsai.proc import Proc


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# --- classification ----------------------------------------------------------

def test_requires_repro_guard_heal_always_required():
    assert repro.requires_repro_guard("heal", "ci: main is red - auto-heal") is True


def test_requires_repro_guard_bugfix_required():
    assert repro.requires_repro_guard("implement", "fix: preserve falsy 0") is True


def test_requires_repro_guard_feature_not_required():
    assert repro.requires_repro_guard("implement", "feat: add widget") is False
    assert repro.requires_repro_guard("implement", "refactor: tidy up") is False


def test_requires_repro_guard_docs_chore_exempt_regardless_of_kind():
    assert repro.requires_repro_guard("implement", "docs: update readme") is False
    assert repro.requires_repro_guard("heal", "chore: bump dependency") is False
    assert repro.requires_repro_guard("implement", "chore: refresh snapshot") is False


def test_changed_test_files_filters_non_tests():
    paths = [
        "src/hsai/ci.py",
        "tests/test_ci.py",
        "README.md",
        "tests/sub/test_x.py",
        "src/hsai/test_helper.py",
    ]
    assert repro.changed_test_files(paths) == [
        "tests/test_ci.py", "tests/sub/test_x.py", "src/hsai/test_helper.py",
    ]


def test_classify_pr_title_parses_kind_and_ticket_title():
    assert repro.classify_pr_title("heal: ci: main is red - auto-heal") == (
        "heal", "ci: main is red - auto-heal",
    )
    assert repro.classify_pr_title("implement: fix: preserve falsy 0") == (
        "implement", "fix: preserve falsy 0",
    )


def test_classify_pr_title_defaults_to_implement_when_unprefixed():
    assert repro.classify_pr_title("some random title") == ("implement", "some random title")


# --- check_repro --------------------------------------------------------------

class FakeGit:
    """Fakes the git/pytest commands `check_repro` issues, driven against a
    real tmp_path tree - no real git repository needed."""

    def __init__(self, *, repo_root: str, fix_ok: bool, parent_ok: bool):
        self.repo_root = repo_root
        self.fix_ok = fix_ok
        self.parent_ok = parent_ok
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, f"{self.repo_root}\n", "")
        if cmd[:3] == ["git", "worktree", "add"]:
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["git", "worktree", "remove"]:
            return Proc(cmd, 0, "", "")
        if cmd[0] == "pytest":
            is_parent = bool(cwd) and "repro-check-" in cwd
            ok = self.parent_ok if is_parent else self.fix_ok
            return Proc(cmd, 0 if ok else 1, "", "" if ok else "FAILED\n")
        raise AssertionError(f"unhandled command {cmd!r}")


def test_check_repro_blocks_when_no_test_changed(tmp_path):
    result = repro.check_repro(
        repo_root=str(tmp_path), wt=str(tmp_path / "wt"), base_ref="origin/main",
        test_files=[], worktrees_dir=".hsai/worktrees",
        runner=lambda *a, **k: Proc([], 0, "", ""),
    )
    assert result.ok is False
    assert "no test file" in result.reason


def test_check_repro_passes_for_a_genuine_regression_fix(tmp_path):
    """Fixture: the bug is real - the new test fails pre-fix and passes post-fix."""
    wt = tmp_path / "wt"
    _write(wt / "tests" / "test_bug.py", "def test_bug():\n    assert True\n")
    fake = FakeGit(repo_root=str(tmp_path), fix_ok=True, parent_ok=False)

    result = repro.check_repro(
        repo_root=str(tmp_path), wt=str(wt), base_ref="origin/main",
        test_files=["tests/test_bug.py"], worktrees_dir=".hsai/worktrees", runner=fake,
    )

    assert result.ok is True
    assert result.fix_ok is True
    assert result.parent_ok is False
    assert "reproduced" in result.reason
    # the overlay actually happened: pytest ran against a real, populated dir
    assert any(c[0] == "pytest" for c in fake.calls)


def test_check_repro_blocks_a_no_repro_fix(tmp_path):
    """Fixture: the 'fix' adds a test that passes even without the fix - no bug proven."""
    wt = tmp_path / "wt"
    _write(wt / "tests" / "test_bug.py", "def test_bug():\n    assert True\n")
    fake = FakeGit(repo_root=str(tmp_path), fix_ok=True, parent_ok=True)

    result = repro.check_repro(
        repo_root=str(tmp_path), wt=str(wt), base_ref="origin/main",
        test_files=["tests/test_bug.py"], worktrees_dir=".hsai/worktrees", runner=fake,
    )

    assert result.ok is False
    assert "does not reproduce a real bug" in result.reason


def test_check_repro_blocks_when_fix_branch_test_itself_fails(tmp_path):
    wt = tmp_path / "wt"
    _write(wt / "tests" / "test_bug.py", "def test_bug():\n    assert False\n")
    fake = FakeGit(repo_root=str(tmp_path), fix_ok=False, parent_ok=False)

    result = repro.check_repro(
        repo_root=str(tmp_path), wt=str(wt), base_ref="origin/main",
        test_files=["tests/test_bug.py"], worktrees_dir=".hsai/worktrees", runner=fake,
    )

    assert result.ok is False
    assert "does not pass on the fix branch" in result.reason
    # never even attempted the parent-tree check
    assert not any("worktree" in " ".join(c) for c in fake.calls if c[0] == "git")


# --- render_evidence -----------------------------------------------------------

def test_render_evidence_for_reproduced_result():
    result = repro.ReproResult(
        ok=True, reason="reproduced: fails on the pre-fix tree, passes on the fix branch",
        test_files=("tests/test_bug.py",), fix_ok=True, parent_ok=False,
    )
    text = repro.render_evidence(result)
    assert "tests/test_bug.py" in text
    assert "FAIL (reproduces the bug)" in text
    assert "PASS" in text
    assert "**reproduced**" in text


def test_render_evidence_not_applicable_when_no_test_files():
    result = repro.ReproResult(ok=False, reason="no test file added or modified; ...")
    text = repro.render_evidence(result)
    assert "not applicable" in text


# --- evaluate_pr (remote / CI entry point) --------------------------------------

def test_evaluate_pr_exempt_for_docs_ticket():
    def boom(*a, **k):
        raise AssertionError("must not shell out for an exempt ticket")

    result = repro.evaluate_pr(
        pr_title="implement: docs: update readme", repo_dir=".", base_ref="origin/main",
        worktrees_dir=".hsai/worktrees", runner=boom,
    )
    assert result.ok is True
    assert "exempt" in result.reason


def test_evaluate_pr_exempt_for_feature_ticket():
    def boom(*a, **k):
        raise AssertionError("must not shell out for a non heal/bugfix ticket")

    result = repro.evaluate_pr(
        pr_title="implement: feat: add widget", repo_dir=".", base_ref="origin/main",
        worktrees_dir=".hsai/worktrees", runner=boom,
    )
    assert result.ok is True


def test_evaluate_pr_runs_the_guard_for_a_bugfix_pr(tmp_path):
    _write(tmp_path / "tests" / "test_bug.py", "def test_bug():\n    assert True\n")

    def fake(cmd, **kwargs):
        cmd = list(cmd)
        cwd = kwargs.get("cwd")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return Proc(cmd, 0, "tests/test_bug.py\nsrc/hsai/foo.py\n", "")
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, f"{tmp_path}\n", "")
        if cmd[:3] == ["git", "worktree", "add"]:
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["git", "worktree", "remove"]:
            return Proc(cmd, 0, "", "")
        if cmd[0] == "pytest":
            is_parent = bool(cwd) and "repro-check-" in cwd
            ok = not is_parent
            return Proc(cmd, 0 if ok else 1, "", "")
        raise AssertionError(f"unhandled command {cmd!r}")

    result = repro.evaluate_pr(
        pr_title="implement: fix: something broken", repo_dir=str(tmp_path),
        base_ref="origin/main", worktrees_dir=".hsai/worktrees", runner=fake,
    )

    assert result.ok is True
    assert result.test_files == ("tests/test_bug.py",)


def test_evaluate_pr_blocks_bugfix_pr_with_no_regression_test():
    def fake(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return Proc(cmd, 0, "src/hsai/foo.py\n", "")
        raise AssertionError(f"unhandled command {cmd!r}")

    result = repro.evaluate_pr(
        pr_title="implement: fix: something broken", repo_dir=".",
        base_ref="origin/main", worktrees_dir=".hsai/worktrees", runner=fake,
    )

    assert result.ok is False
    assert "no test file" in result.reason
