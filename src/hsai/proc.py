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

    ``env``, when given, is the complete child environment (matching
    :class:`subprocess.Popen`'s own convention) rather than a patch merged onto
    a fresh copy of ``os.environ`` - the previous merge-only behavior could add
    or override a variable but could never make one absent, which is exactly
    the shape of bug that let a stripped ``ANTHROPIC_API_KEY`` reappear from
    ``os.environ`` on its way to the child. ``env_remove`` covers the common
    case of "inherit everything except these": it applies on top of whatever
    ``env`` resolves to (the given mapping, or ``os.environ`` when ``env`` is
    ``None``), so a caller never has to hand-copy the whole environment just to
    guarantee one variable's absence.
    """
    full_env = dict(env) if env is not None else dict(os.environ)
    if env_remove:
        for key in env_remove:
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
