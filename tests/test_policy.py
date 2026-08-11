"""Protected-surface policy: evaluate(), glob matching, and the AST-based
test-integrity guard.
"""
import pytest

from hsai import policy
from hsai.policy import (
    DENY,
    REQUIRE_LABEL,
    REVERT,
    PolicyVerdict,
    ProtectedSurface,
    count_test_functions,
    evaluate,
    function_names,
    matches,
    revert_pathspec,
    test_function_delta,
)
from hsai.proc import Proc


# --- ProtectedSurface / glob matching -----------------------------------------

def test_protected_surface_rejects_unknown_mode():
    with pytest.raises(ValueError):
        ProtectedSurface(glob="x", mode="allow-anything")


def test_matches_supports_double_star_globs():
    assert matches(".github/workflows/ci.yml", ".github/workflows/**")
    assert matches(".github/workflows/sub/deploy.yml", ".github/workflows/**")
    assert not matches("src/hsai/ci.py", ".github/workflows/**")


def test_matches_exact_path():
    assert matches(".ai-swarm/core.yaml", ".ai-swarm/core.yaml")
    assert not matches(".ai-swarm/other.yaml", ".ai-swarm/core.yaml")


def test_revert_pathspec_strips_trailing_glob():
    assert revert_pathspec(".github/workflows/**") == ".github/workflows"
    assert revert_pathspec("knowledge/ledger/**") == "knowledge/ledger"
    assert revert_pathspec(".ai-swarm/core.yaml") == ".ai-swarm/core.yaml"


# --- evaluate(): the empty-policy case -----------------------------------------

def test_empty_policy_allows_everything():
    verdict = evaluate(
        ["anything.py", ".ai-swarm/core.yaml", ".github/workflows/ci.yml"], 0, (), [],
    )
    assert verdict.allowed is True
    assert verdict.violations == ()
    assert verdict.actions == ()


# --- evaluate(): revert mode ---------------------------------------------------

def test_revert_mode_never_blocks_but_is_reported_as_an_action():
    policy_cfg = [ProtectedSurface(glob=".github/workflows/**", mode=REVERT)]
    verdict = evaluate([".github/workflows/ci.yml", "src/hsai/foo.py"], 0, (), policy_cfg)
    assert verdict.allowed is True
    assert verdict.violations == ()
    assert len(verdict.actions) == 1
    assert verdict.actions[0].glob == ".github/workflows/**"


def test_revert_mode_no_match_produces_no_action():
    policy_cfg = [ProtectedSurface(glob=".github/workflows/**", mode=REVERT)]
    verdict = evaluate(["src/hsai/foo.py"], 0, (), policy_cfg)
    assert verdict.allowed is True
    assert verdict.actions == ()


# --- evaluate(): require_label mode --------------------------------------------

def test_require_label_blocks_without_the_label():
    policy_cfg = [ProtectedSurface(glob=".ai-swarm/core.yaml", mode=REQUIRE_LABEL, rationale="budget")]
    verdict = evaluate([".ai-swarm/core.yaml"], 0, ("priority:P2",), policy_cfg)
    assert verdict.allowed is False
    assert len(verdict.violations) == 1
    v = verdict.violations[0]
    assert v.mode == REQUIRE_LABEL
    assert v.paths == (".ai-swarm/core.yaml",)
    assert "guards-approved" in v.reason
    assert "budget" in v.describe()


def test_require_label_passes_with_the_guards_approved_label():
    policy_cfg = [ProtectedSurface(glob=".ai-swarm/core.yaml", mode=REQUIRE_LABEL)]
    verdict = evaluate(
        [".ai-swarm/core.yaml"], 0, ("priority:P2", "guards-approved"), policy_cfg,
    )
    assert verdict.allowed is True
    assert verdict.violations == ()


def test_require_label_ignores_unmatched_paths():
    policy_cfg = [ProtectedSurface(glob=".ai-swarm/core.yaml", mode=REQUIRE_LABEL)]
    verdict = evaluate(["src/hsai/foo.py"], 0, (), policy_cfg)
    assert verdict.allowed is True


# --- evaluate(): deny mode ------------------------------------------------------

def test_deny_mode_blocks_even_with_the_guards_approved_label():
    policy_cfg = [ProtectedSurface(glob="knowledge/ledger/**", mode=DENY, rationale="append-only")]
    verdict = evaluate(
        ["knowledge/ledger/iterations.jsonl"], 0, ("guards-approved",), policy_cfg,
    )
    assert verdict.allowed is False
    v = verdict.violations[0]
    assert v.mode == DENY
    assert "append-only" in v.describe()


# --- evaluate(): multiple surfaces, dict form, mixed outcomes ------------------

def test_evaluate_accepts_raw_dicts_as_policy_entries():
    policy_cfg = [{"glob": ".ai-swarm/core.yaml", "mode": "require_label", "rationale": "r"}]
    verdict = evaluate([".ai-swarm/core.yaml"], 0, (), policy_cfg)
    assert verdict.allowed is False


def test_evaluate_names_every_violated_surface():
    policy_cfg = [
        ProtectedSurface(glob=".ai-swarm/core.yaml", mode=REQUIRE_LABEL),
        ProtectedSurface(glob="knowledge/ledger/**", mode=DENY),
        ProtectedSurface(glob=".github/workflows/**", mode=REVERT),
    ]
    verdict = evaluate(
        [
            ".ai-swarm/core.yaml",
            "knowledge/ledger/iterations.jsonl",
            ".github/workflows/ci.yml",
            "src/hsai/unrelated.py",
        ],
        0, (), policy_cfg,
    )
    assert verdict.allowed is False
    globs = {v.glob for v in verdict.violations}
    assert globs == {".ai-swarm/core.yaml", "knowledge/ledger/**"}
    assert len(verdict.actions) == 1


# --- evaluate(): test-integrity guard ------------------------------------------

def test_test_count_decrease_without_label_is_a_violation():
    verdict = evaluate(["tests/test_foo.py"], -1, (), [])
    assert verdict.allowed is False
    assert "decreased by 1" in verdict.violations[0].reason


def test_test_count_decrease_with_guards_approved_is_allowed():
    verdict = evaluate(["tests/test_foo.py"], -1, ("guards-approved",), [])
    assert verdict.allowed is True


def test_test_count_increase_or_unchanged_is_not_a_violation():
    assert evaluate([], 0, (), []).allowed is True
    assert evaluate([], 3, (), []).allowed is True


def test_policy_verdict_summary_lists_every_violation():
    policy_cfg = [ProtectedSurface(glob=".ai-swarm/core.yaml", mode=REQUIRE_LABEL)]
    verdict = evaluate([".ai-swarm/core.yaml"], -2, (), policy_cfg)
    assert verdict.summary().count("[") == 2  # two distinct violations rendered
    assert PolicyVerdict(allowed=True).summary() == "no violations"


# --- AST-based test-function counting (pure) -----------------------------------

def test_function_names_finds_test_functions_only():
    src = """
def test_a():
    pass

def helper():
    pass

class TestThing:
    def test_b(self):
        pass

    def not_a_test(self):
        pass

async def test_c():
    pass
"""
    assert function_names(src) == {"test_a", "test_b", "test_c"}


def test_function_names_on_unparsable_source_is_empty():
    assert function_names("def broken(:\n") == set()


def test_count_test_functions_sums_across_sources():
    a = "def test_a():\n    pass\n"
    b = "def test_b():\n    pass\ndef test_c():\n    pass\n"
    assert count_test_functions([a, b]) == 3
    assert count_test_functions([]) == 0


def test_function_delta_flags_a_real_deletion():
    base = ["def test_a():\n    pass\ndef test_b():\n    pass\n"]
    pr = ["def test_a():\n    pass\n"]
    assert test_function_delta(base, pr) == -1


def test_function_delta_is_zero_for_a_pure_rename():
    # same file, function renamed: one name disappears, one appears - net zero.
    base = ["def test_old_name():\n    pass\n"]
    pr = ["def test_new_name():\n    pass\n"]
    assert test_function_delta(base, pr) == 0


def test_function_delta_is_zero_for_a_file_move():
    # the function moves to a different file (a different source string in the
    # collection) but the total across the whole corpus is unchanged.
    base = [
        "def test_a():\n    pass\n",             # tests/test_one.py
        "",                                        # tests/test_two.py (empty)
    ]
    pr = [
        "",                                        # tests/test_one.py (emptied)
        "def test_a():\n    pass\n",              # tests/test_two.py (moved here)
    ]
    assert test_function_delta(base, pr) == 0


def test_function_delta_flags_a_net_decrease_even_with_other_additions():
    base = ["def test_a():\n    pass\ndef test_b():\n    pass\n"]
    pr = ["def test_a():\n    pass\ndef test_c():\n    pass\n"]  # b deleted, c added: net 0
    assert test_function_delta(base, pr) == 0
    pr_regression = ["def test_a():\n    pass\n"]  # both b and its "replacement" gone
    assert test_function_delta(base, pr_regression) == -1


# --- I/O glue: test_function_delta_for_tree ------------------------------------

class _FakePolicyGit:
    """Fakes the git plumbing `test_function_delta_for_tree` shells out to."""

    def __init__(
        self,
        *,
        base_files: dict[str, str],
        base_listing: list[str] | None = None,
        worktree_files: list[str] | None = None,
    ):
        self.base_files = base_files  # path -> content, as of the base ref
        # What `git ls-tree` reports - defaults to exactly `base_files`' keys,
        # but a test can list an extra path with no readable content to
        # simulate `git show` failing on it.
        self.base_listing = base_listing if base_listing is not None else list(base_files)
        self.worktree_files = worktree_files or []  # paths `git ls-files` reports
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["git", "ls-tree", "-r"]:
            listing = "\n".join(self.base_listing) + ("\n" if self.base_listing else "")
            return Proc(cmd, 0, listing, "")
        if cmd[:2] == ["git", "show"]:
            _, path = cmd[2].split(":", 1)
            content = self.base_files.get(path)
            if content is None:
                return Proc(cmd, 1, "", "fatal: path not in tree")
            return Proc(cmd, 0, content, "")
        if cmd[:2] == ["git", "ls-files"]:
            names = "\n".join(self.worktree_files)
            return Proc(cmd, 0, names + ("\n" if names else ""), "")
        raise AssertionError(f"unhandled command {cmd!r}")


def test_test_function_delta_for_tree_reads_base_via_git_and_pr_from_disk(tmp_path):
    (tmp_path / "test_thing.py").write_text("def test_a():\n    pass\n")
    fake = _FakePolicyGit(
        base_files={"test_thing.py": "def test_a():\n    pass\ndef test_b():\n    pass\n"},
        worktree_files=["test_thing.py"],
    )
    delta = policy.test_function_delta_for_tree(
        base_ref="origin/main", repo_dir=str(tmp_path), worktree=str(tmp_path), runner=fake,
    )
    assert delta == -1  # test_b existed at base and is gone from the PR tree
    assert any(c[:3] == ["git", "ls-tree", "-r"] for c in fake.calls)
    assert any(c[:2] == ["git", "ls-files"] for c in fake.calls)


def test_test_function_delta_for_tree_ignores_unreadable_show(tmp_path):
    # `git show` failing on a listed path (e.g. a submodule boundary) must not
    # raise - it is simply excluded from the base-side count.
    fake = _FakePolicyGit(base_files={}, base_listing=["tests/test_gone.py"])
    delta = policy.test_function_delta_for_tree(
        base_ref="origin/main", repo_dir=str(tmp_path), worktree=str(tmp_path), runner=fake,
    )
    assert delta == 0
    assert any(c[:2] == ["git", "show"] for c in fake.calls)


def test_test_function_delta_for_tree_is_zero_for_a_file_move(tmp_path):
    # The test moves from tests/test_one.py to tests/test_two.py; the fake
    # ls-tree only reports the OLD path (it no longer exists at HEAD's base
    # comparison point isn't relevant here - base still has the old path),
    # and the worktree only has the NEW path. Net count is unchanged.
    (tmp_path / "test_two.py").write_text("def test_a():\n    pass\n")
    fake = _FakePolicyGit(
        base_files={"test_one.py": "def test_a():\n    pass\n"},
        worktree_files=["test_two.py"],
    )
    delta = policy.test_function_delta_for_tree(
        base_ref="origin/main", repo_dir=str(tmp_path), worktree=str(tmp_path), runner=fake,
    )
    assert delta == 0
