"""Executable assertions of the safety invariants the protected-invariants gate
(:mod:`hsai.guard`) defends.

These run under the EXISTING pytest step, so remote enforcement of the gate
needs no new CI workflow - which matters because the guard itself would
revert any ``.github/workflows/`` edit meant to add one.
"""
import pytest

from hsai import invariants
from hsai.config import load_config


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.mark.parametrize("inv", invariants.INVARIANTS, ids=lambda inv: inv.name)
def test_invariant_holds(inv, cfg):
    inv.check(cfg)


def test_at_least_five_invariants_are_checked():
    assert len(invariants.INVARIANTS) >= 5


def test_run_all_reports_a_failure_when_api_key_stripping_is_removed(cfg, monkeypatch):
    """Directly regresses the scenario the ticket calls out: if the
    ANTHROPIC_API_KEY stripping is removed from ai._sanitized_env, run_all
    (and therefore `hsai verify-invariants`) must go red."""
    from hsai import ai as ai_module

    def unsafe_sanitized_env(_cfg):
        import os

        return dict(os.environ)  # no stripping at all

    monkeypatch.setattr(ai_module, "_sanitized_env", unsafe_sanitized_env)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")

    results = invariants.run_all(cfg)
    by_name = {r.name: r for r in results}
    assert by_name["ai._sanitized_env strips ANTHROPIC_API_KEY"].ok is False
