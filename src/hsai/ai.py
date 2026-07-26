"""Invoke Claude Code headless (`claude -p`).

All model work goes through here so a single place enforces the
subscription-only constraint: the metered API is never used, which means any
``ANTHROPIC_API_KEY`` in the environment is stripped before ``claude`` runs
(otherwise the CLI would silently route to - and bill - the API).
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from .config import CoreConfig
from .models import ModelChoice
from .proc import Proc, Runner, run


class SubscriptionGuardError(RuntimeError):
    """Raised when we cannot guarantee subscription-only execution."""


@dataclass
class AIResult:
    ok: bool
    model: str
    output: str
    error: str
    cmd: Sequence[str]


def _sanitized_env(cfg: CoreConfig) -> dict[str, str]:
    """Return an env with forbidden (billing) variables removed."""
    env = dict(os.environ)
    for key in cfg.forbidden_env:
        env.pop(key, None)
    # Belt and suspenders: never let a stray key leak in.
    if cfg.subscription_only:
        env.pop("ANTHROPIC_API_KEY", None)
        # Signal to the CLI to prefer subscription auth where supported.
        env.setdefault("CLAUDE_CODE_SUBSCRIPTION_ONLY", "1")
    return env


def build_command(
    prompt: str,
    choice: ModelChoice,
    cfg: CoreConfig,
    *,
    permission_mode: str | None = None,
) -> list[str]:
    """Construct the ``claude -p`` argument vector (no execution)."""
    mode = permission_mode or cfg.permission_mode
    return [
        "claude",
        "-p",
        prompt,
        "--model",
        choice.model,
        "--permission-mode",
        mode,
    ]


def preflight(cfg: CoreConfig) -> None:
    """Fail fast if subscription-only cannot be honored."""
    if not cfg.subscription_only:
        return
    if os.environ.get("ANTHROPIC_API_KEY"):
        # We can strip it for the child, but refuse if the config didn't ask us to.
        if "ANTHROPIC_API_KEY" not in cfg.forbidden_env:
            raise SubscriptionGuardError(
                "ANTHROPIC_API_KEY is set but not listed in constraints.forbid_env; "
                "refusing to risk metered API usage."
            )


def run_agent(
    prompt: str,
    choice: ModelChoice,
    cfg: CoreConfig,
    *,
    cwd: str | None = None,
    permission_mode: str | None = None,
    timeout: float | None = None,
    runner: Runner = run,
) -> AIResult:
    """Run a headless Claude Code agent for one task."""
    preflight(cfg)
    cmd = build_command(prompt, choice, cfg, permission_mode=permission_mode)
    proc: Proc = runner(cmd, cwd=cwd, env=_sanitized_env(cfg), timeout=timeout)
    return AIResult(
        ok=proc.ok,
        model=choice.model,
        output=proc.stdout,
        error=proc.stderr,
        cmd=cmd,
    )
