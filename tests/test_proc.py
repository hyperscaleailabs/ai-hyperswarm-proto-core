"""Direct unit tests for :func:`hsai.proc.run`'s environment contract.

`subprocess.run` itself is mocked (never a real spawn here) so these tests
pin the exact env dict proc.run hands the child - the layer where the
ANTHROPIC_API_KEY leak actually lived: `full_env = dict(os.environ)` then
`full_env.update(env)` can only add or overwrite a key, never delete one
back out, so a key omitted from `env` was silently reintroduced from the
parent process. `env_remove` is the fix - see :mod:`hsai.ai` for the caller
that wires it through.
"""
from __future__ import annotations

import subprocess

from hsai import proc


def _capture_env(monkeypatch, *, stdout: str = "", returncode: int = 0):
    captured: dict[str, dict] = {}

    def fake_subprocess_run(cmd, *, cwd, env, input, capture_output, text, timeout):
        captured["env"] = dict(env or {})
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(proc.subprocess, "run", fake_subprocess_run)
    return captured


def test_run_merges_env_overrides_on_top_of_the_parent_process(monkeypatch):
    monkeypatch.setenv("KEEP_ME", "present")
    captured = _capture_env(monkeypatch)

    proc.run(["true"], env={"NEW_VAR": "added"})

    assert captured["env"]["KEEP_ME"] == "present"   # parent env still inherited
    assert captured["env"]["NEW_VAR"] == "added"      # override applied


def test_run_env_remove_deletes_a_key_present_in_the_parent_process(monkeypatch):
    """The direct pin: `env_remove` actually removes what `env` cannot omit
    its way out of, per Acceptance criterion #2."""
    monkeypatch.setenv("SECRET", "leak-me")
    captured = _capture_env(monkeypatch)

    proc.run(["true"], env_remove=["SECRET"])

    assert "SECRET" not in captured["env"]


def test_run_env_remove_wins_even_if_env_reintroduces_the_key(monkeypatch):
    """Removal is applied AFTER the override merge, so it always has the last
    word - a caller cannot accidentally undo its own removal via `env`."""
    monkeypatch.setenv("SECRET", "leak-me")
    captured = _capture_env(monkeypatch)

    proc.run(["true"], env={"SECRET": "still-here"}, env_remove=["SECRET"])

    assert "SECRET" not in captured["env"]


def test_run_env_remove_is_a_noop_for_a_key_that_was_never_set(monkeypatch):
    captured = _capture_env(monkeypatch)

    result = proc.run(["true"], env_remove=["NEVER_SET_ANYWHERE"])

    assert "NEVER_SET_ANYWHERE" not in captured["env"]
    assert result.ok


def test_run_without_env_or_env_remove_behaves_exactly_as_before(monkeypatch):
    """Backward compatibility: every pre-existing caller that passes neither
    new parameter must see the same full-`os.environ` pass-through it always
    has."""
    monkeypatch.setenv("ORDINARY", "value")
    captured = _capture_env(monkeypatch)

    proc.run(["true"])

    assert captured["env"]["ORDINARY"] == "value"


def test_run_returns_proc_result_unaffected_by_env_handling(monkeypatch):
    _capture_env(monkeypatch, stdout="hello\n", returncode=0)
    result = proc.run(["echo", "hello"], env_remove=["ANTHROPIC_API_KEY"])
    assert result.ok
    assert result.stdout == "hello\n"
