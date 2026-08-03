"""Executable assertions of the safety invariants the protected-invariants gate
(:mod:`hsai.guard`) exists to defend.

Each :class:`Invariant` is a name plus a ``check(cfg)`` callable that raises
``AssertionError`` on failure. ``tests/test_invariants.py`` and the
``hsai verify-invariants`` CLI command both run :data:`INVARIANTS`, so CI and
local pre-flight can never drift onto different definitions of "safe" - and
because these run under the EXISTING pytest step, remote enforcement needs no
new CI workflow (which the guard would itself revert).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace

from . import ai, ci
from .config import CoreConfig
from .models import ModelChoice
from .orchestrator import build_pr_body
from .proc import Proc


def _check_env_strips_api_key(cfg: CoreConfig) -> None:
    backup = dict(os.environ)
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-should-be-stripped"
        env = ai._sanitized_env(cfg)
        assert "ANTHROPIC_API_KEY" not in env, (
            "ai._sanitized_env must strip ANTHROPIC_API_KEY"
        )
    finally:
        os.environ.clear()
        os.environ.update(backup)


def _check_preflight_raises_on_unlisted_key(cfg: CoreConfig) -> None:
    cfg = replace(cfg, constraints={**cfg.constraints, "forbid_env": []})
    backup = dict(os.environ)
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        try:
            ai.preflight(cfg)
        except ai.SubscriptionGuardError:
            return
        raise AssertionError(
            "ai.preflight must raise when ANTHROPIC_API_KEY is set and unlisted"
        )
    finally:
        os.environ.clear()
        os.environ.update(backup)


def _check_pr_body_requires_ticket(cfg: CoreConfig) -> None:
    choice = ModelChoice(tier="standard", model="sonnet", rationale="x")
    try:
        build_pr_body(
            ticket=0, choice=choice, lesson_note="n",
            lesson_summary="s", ci_summary="CI green",
        )
    except ValueError:
        return
    raise AssertionError("build_pr_body must raise without a ticket")


def _check_ci_runs_ruff_and_pytest(cfg: CoreConfig) -> None:
    calls: list[list[str]] = []

    def fake_runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        calls.append(list(cmd))
        return Proc(cmd, 0, "", "")

    ci.run_local(runner=fake_runner)
    assert any(c[:2] == ["ruff", "check"] for c in calls), "ci.run_local must invoke ruff"
    assert any(c[:1] == ["pytest"] for c in calls), "ci.run_local must invoke pytest"


def _check_core_yaml_constraints(cfg: CoreConfig) -> None:
    required = (
        "subscription_only", "require_green_ci_to_merge",
        "require_ticket_per_pr", "require_lesson_per_pr",
    )
    missing = [k for k in required if k not in cfg.constraints]
    assert not missing, f"core.yaml constraints missing: {missing}"

    budget_keys = ("max_heavy_iterations_per_block", "max_seconds_per_block", "soft_ratio")
    missing_budget = [k for k in budget_keys if k not in cfg.budget]
    assert not missing_budget, f"core.yaml budget missing: {missing_budget}"


@dataclass(frozen=True)
class Invariant:
    name: str
    check: Callable[[CoreConfig], None]


INVARIANTS: tuple[Invariant, ...] = (
    Invariant("ai._sanitized_env strips ANTHROPIC_API_KEY", _check_env_strips_api_key),
    Invariant("ai.preflight raises on an unlisted API key", _check_preflight_raises_on_unlisted_key),
    Invariant("build_pr_body requires a ticket", _check_pr_body_requires_ticket),
    Invariant("ci.run_local invokes ruff and pytest", _check_ci_runs_ruff_and_pytest),
    Invariant("core.yaml declares required constraints/budget keys", _check_core_yaml_constraints),
)


@dataclass
class InvariantResult:
    name: str
    ok: bool
    error: str = ""


def run_all(cfg: CoreConfig) -> list[InvariantResult]:
    results: list[InvariantResult] = []
    for inv in INVARIANTS:
        try:
            inv.check(cfg)
        except Exception as exc:  # noqa: BLE001 - each invariant gets its own pass/fail row
            results.append(InvariantResult(inv.name, False, str(exc)))
        else:
            results.append(InvariantResult(inv.name, True))
    return results
