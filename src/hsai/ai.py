"""Invoke Claude Code headless (`claude -p`).

All model work goes through here so a single place enforces the
subscription-only constraint: the metered API is never used, which means any
``ANTHROPIC_API_KEY`` in the environment is stripped before ``claude`` runs
(otherwise the CLI would silently route to - and bill - the API).
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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
    usage: dict[str, Any] | None = None  # token counts, when the CLI exposes them
    payload: dict[str, Any] | None = None  # parsed JSON envelope (None = plain text)

    @property
    def text(self) -> str:
        """The agent's final message, unwrapped from the JSON envelope."""
        if isinstance(self.payload, dict) and isinstance(self.payload.get("result"), str):
            return self.payload["result"]
        return self.output

    @property
    def session_id(self) -> str:
        """The CLI's per-run session id, when the envelope exposes one."""
        if isinstance(self.payload, dict):
            return str(self.payload.get("session_id") or "")
        return ""


def parse_output(stdout: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Split a ``claude -p --output-format json`` stdout into ``(payload, usage)``.

    A pure parse of text the CLI already printed - never a metered call. Any
    output that is not a JSON object (an older ``claude`` binary, or a crash
    that printed plain text) degrades to ``(None, None)`` so the loop keeps
    running on the plain-text path instead of breaking.
    """
    text = (stdout or "").strip()
    if not text.startswith("{"):
        return None, None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    usage = data.get("usage")
    return data, usage if isinstance(usage, dict) else None


def _sanitized_env(cfg: CoreConfig) -> tuple[dict[str, str], tuple[str, ...]]:
    """Build the child environment for ``claude -p``: ``(overrides, removals)``.

    ``removals`` is returned alongside the override map - not just applied to
    it - because :func:`hsai.proc.run` cannot honor an omission: its ``env``
    parameter is an overlay merged on top of a fresh ``os.environ`` read, and
    ``dict.update`` can only add or overwrite a key, never delete one back
    out. A key simply absent from ``overrides`` is still inherited from the
    parent process. Passing ``removals`` through as ``env_remove`` is what
    actually enforces the subscription-only guard at the real subprocess
    boundary.
    """
    removals = set(cfg.forbidden_env)
    # Belt and suspenders: never let a stray key leak in, even if the config
    # forgot to list it (constraints.forbid_env is also validated separately).
    if cfg.subscription_only:
        removals.add("ANTHROPIC_API_KEY")

    overrides: dict[str, str] = {}
    if cfg.subscription_only:
        # Signal to the CLI to prefer subscription auth where supported.
        overrides["CLAUDE_CODE_SUBSCRIPTION_ONLY"] = "1"
    return overrides, tuple(sorted(removals))


def build_command(
    prompt: str,
    choice: ModelChoice,
    cfg: CoreConfig,
    *,
    permission_mode: str | None = None,
) -> list[str]:
    """Construct the ``claude -p`` argument vector (no execution).

    The structured envelope is what makes a run auditable afterwards (token
    usage for the quota ledger, the step stream for the trajectory store; see
    :mod:`hsai.trajectory`), so ``execution.output_format`` defaults to
    ``json``. It stays config-driven rather than hardcoded because the loop
    must not be brickable by a CLI flag change: setting it to ``text`` (or
    empty) drops the flag and falls back to the plain-text path, which every
    consumer already tolerates.
    """
    mode = permission_mode or cfg.permission_mode
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        choice.model,
        "--permission-mode",
        mode,
    ]
    fmt = (cfg.output_format or "").strip()
    if fmt and fmt != "text":
        cmd += ["--output-format", fmt]
        # The CLI refuses `stream-json` under `-p` unless `--verbose` is set.
        if fmt == "stream-json":
            cmd.append("--verbose")
    return cmd


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


def check_child_env(cfg: CoreConfig, *, runner: Runner = run) -> tuple[bool, str]:
    """Spawn a real child process with the exact env :func:`run_agent` would
    use, and read back what that child actually saw.

    `hsai doctor`'s subscription guard used to only inspect config (would
    ``preflight`` raise?) and never asked the question a real leak needs
    answered: does the forbidden variable actually reach a spawned process?
    This is the live counterpart - it is the one check that would have caught
    :func:`hsai.proc.run`'s env-merge asymmetry (``dict.update`` cannot
    remove a key) before it shipped. Uses the current Python interpreter as
    the probe so it needs no external binary.
    """
    env, removals = _sanitized_env(cfg)
    if not removals:
        return True, "no forbidden variables configured (constraints.forbid_env is empty)"
    probe = "import os,sys;sys.stdout.write(','.join(k for k in sys.argv[1:] if os.environ.get(k)))"
    result = runner(
        [sys.executable, "-c", probe, *removals],
        env=env, env_remove=removals, timeout=10,
    )
    leaked = result.stdout.strip()
    if leaked:
        return False, f"leaked into a real spawned child process: {leaked}"
    return True, f"{', '.join(removals)} confirmed absent from a real spawned child process"


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
    env, env_remove = _sanitized_env(cfg)
    proc: Proc = runner(cmd, cwd=cwd, env=env, env_remove=env_remove, timeout=timeout)
    payload, usage = parse_output(proc.stdout)
    return AIResult(
        ok=proc.ok,
        model=choice.model,
        output=proc.stdout,
        error=proc.stderr,
        cmd=cmd,
        usage=usage,
        payload=payload,
    )
