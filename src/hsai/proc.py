"""Thin, testable subprocess wrapper.

Every shell-out in hsai goes through :func:`run`. A ``Runner`` is any callable
with the same signature, which lets tests inject a fake instead of executing
real commands.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass


@dataclass
class Proc:
    """Result of a completed subprocess."""

    cmd: Sequence[str]
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(
    cmd: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    env_remove: Iterable[str] | None = None,
    timeout: float | None = None,
    input_text: str | None = None,
) -> Proc:
    """Run ``cmd`` and capture output. Never raises on non-zero exit.

    ``env`` overlays additional or overridden variables on top of the current
    process environment - a merge, not a replace, so a caller can add a
    handful of keys without reconstructing the whole environment. That merge
    is exactly why ``env`` alone cannot GUARANTEE a variable's absence: a key
    simply omitted from ``env`` is still inherited from ``os.environ`` because
    ``dict.update`` can only add or overwrite keys, never delete one back out.
    ``env_remove`` is the explicit fix for that asymmetry - every key named
    there is popped from the merged result, so it is genuinely absent from the
    child process regardless of whether the parent process has it set. This is
    what makes hsai's subscription-only guard (ANTHROPIC_API_KEY must never
    reach ``claude -p``) an enforced invariant instead of a best-effort one.
    """
    full_env = dict(os.environ)
    if env is not None:
        full_env.update(env)
    for key in env_remove or ():
        full_env.pop(key, None)
    try:
        completed = subprocess.run(  # noqa: S603 - cmd is a list, never shell=True
            list(cmd),
            cwd=cwd,
            env=full_env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return Proc(cmd, completed.returncode, completed.stdout, completed.stderr)
    except FileNotFoundError as exc:
        return Proc(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return Proc(cmd, 124, exc.stdout or "", f"timeout after {timeout}s")


# A Runner is anything call-compatible with `run`.
Runner = Callable[..., Proc]
