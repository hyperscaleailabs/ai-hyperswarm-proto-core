"""Unit tests for :mod:`hsai.proc` - the single choke point every shell-out
goes through, including the environment contract that keeps the subscription-
only guarantee real (see :func:`hsai.ai._sanitized_env` and the module
docstring of :mod:`hsai.proc`).

These tests intercept at ``subprocess.run`` itself rather than injecting a
fake ``Runner`` - the whole point is to exercise the REAL :func:`hsai.proc.run`
implementation, since a fake runner would never reproduce the merge-vs-replace
bug this module fixes.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from hsai.proc import Proc, run


def _capture_env(monkeypatch) -> dict:
    """Patch subprocess.run to record the env it was actually invoked with."""
    captured: dict = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    return captured


def test_run_with_no_env_inherits_os_environ(monkeypatch):
    monkeypatch.setenv("HSAI_TEST_INHERITED", "yes")
    captured = _capture_env(monkeypatch)

    run(["true"])

    assert captured["env"]["HSAI_TEST_INHERITED"] == "yes"


def test_run_env_is_used_verbatim_as_the_complete_child_environment(monkeypatch):
    captured = _capture_env(monkeypatch)

    run(["true"], env={"FOO": "1", "BAR": "2"})

    assert captured["env"] == {"FOO": "1", "BAR": "2"}


def test_env_remove_strips_a_variable_that_would_otherwise_be_inherited(monkeypatch):
    """The direct regression test for the leak: `env_remove` must guarantee
    absence, not merely omission from an override map."""
    monkeypatch.setenv("HSAI_TEST_SECRET", "leak-me")
    captured = _capture_env(monkeypatch)

    run(["true"], env_remove=["HSAI_TEST_SECRET"])

    assert "HSAI_TEST_SECRET" not in captured["env"]
    # Everything else the child would normally see is untouched.
    assert captured["env"].get("PATH") == os.environ.get("PATH")


def test_env_remove_applies_on_top_of_an_explicit_env_mapping(monkeypatch):
    captured = _capture_env(monkeypatch)

    run(["true"], env={"HSAI_TEST_SECRET": "leak-me", "FOO": "1"},
        env_remove=["HSAI_TEST_SECRET"])

    assert captured["env"] == {"FOO": "1"}


def test_env_remove_of_an_absent_key_is_a_noop(monkeypatch):
    captured = _capture_env(monkeypatch)

    run(["true"], env={"FOO": "1"}, env_remove=["NEVER_SET_ANYWHERE"])

    assert captured["env"] == {"FOO": "1"}


def test_a_given_env_replaces_rather_than_merges_with_os_environ(monkeypatch):
    """The actual bug this ticket fixes: an override map used to be merged
    onto a fresh `os.environ` copy, so a key deliberately absent from the
    override still reappeared from the parent process. `env` is now the
    authoritative child environment, matching subprocess.Popen's own contract.
    """
    monkeypatch.setenv("HSAI_TEST_SECRET", "leak-me")
    captured = _capture_env(monkeypatch)

    # A caller building a full replacement env (as `ai._sanitized_env` does)
    # simply omits the key - no merge should ever bring it back.
    run(["true"], env={"PATH": os.environ.get("PATH", "")})

    assert "HSAI_TEST_SECRET" not in captured["env"]


# --- pre-existing behavior: every Runner-injection test keeps working -------

def test_run_returns_a_proc_with_captured_output():
    proc = run([sys.executable, "-c", "import sys; sys.stdout.write('hi')"])
    assert isinstance(proc, Proc)
    assert proc.ok is True
    assert proc.stdout == "hi"


def test_run_never_raises_on_a_missing_binary():
    proc = run(["hsai-definitely-not-a-real-binary-xyz"])
    assert proc.ok is False
    assert proc.code == 127


def test_run_never_raises_on_non_zero_exit():
    proc = run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert proc.ok is False
    assert proc.code == 3


def test_run_honors_timeout():
    proc = run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
    assert proc.code == 124


def test_run_passes_input_text_to_stdin():
    proc = run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        input_text="hi\n",
    )
    assert proc.stdout == "HI\n"


def test_run_uses_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    proc = run(
        [sys.executable, "-c", "import os; print(sorted(os.listdir('.')))"], cwd=str(tmp_path)
    )
    assert "marker.txt" in proc.stdout


@pytest.mark.parametrize("runner", [run])
def test_run_is_a_valid_runner_callable(runner):
    """Sanity check that `run` still satisfies the loose `Runner` protocol every
    fake-runner test in the rest of the suite relies on."""
    proc = runner(["true"], cwd=None, env=None, timeout=None, input_text=None)
    assert isinstance(proc, Proc)
