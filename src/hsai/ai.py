"""Invoke Claude Code headless (`claude -p`).

All model work goes through here so a single place enforces the
subscription-only constraint: the metered API is never used, which means any
``ANTHROPIC_API_KEY`` in the environment is stripped before ``claude`` runs
(otherwise the CLI would silently route to - and bill - the API).
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .config import CoreConfig
from .models import ModelChoice
from .proc import Proc, Runner, run
from .trajectory import Recorder


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


def run_agent(
    prompt: str,
    choice: ModelChoice,
    cfg: CoreConfig,
    *,
    cwd: str | None = None,
    permission_mode: str | None = None,
    timeout: float | None = None,
    runner: Runner = run,
    recorder: Recorder | None = None,
    phase: str = "agent",
) -> AIResult:
    """Run a headless Claude Code agent for one task.

    When ``recorder`` is given, the invocation appends exactly one event to the
    iteration's trajectory. It is recorded *here* rather than by the recorder's
    generic runner wrapper because this is the only place that knows the tier,
    the model, the prompt and the token counts - the fields a replay and a
    post-mortem actually need.
    """
    preflight(cfg)
    cmd = build_command(prompt, choice, cfg, permission_mode=permission_mode)
    started = time.monotonic()
    proc: Proc = runner(cmd, cwd=cwd, env=_sanitized_env(cfg), timeout=timeout)
    duration = time.monotonic() - started
    payload, usage = parse_output(proc.stdout)
    if recorder is not None:
        recorder.record(
            cmd,
            exit_code=proc.code,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=duration,
            phase=phase,
            tier=choice.tier,
            model=choice.model,
            prompt=prompt,
            input_tokens=(usage or {}).get("input_tokens"),
            output_tokens=(usage or {}).get("output_tokens"),
        )
    return AIResult(
        ok=proc.ok,
        model=choice.model,
        output=proc.stdout,
        error=proc.stderr,
        cmd=cmd,
        usage=usage,
        payload=payload,
    )
