import pytest

from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.orchestrator import (
    HEAL,
    IMPLEMENT,
    IMPROVE,
    build_pr_body,
    decide_path,
    run_once,
)


def test_decide_path():
    assert decide_path(ci_green=False, has_tickets=True) == HEAL
    assert decide_path(ci_green=False, has_tickets=False) == HEAL
    assert decide_path(ci_green=True, has_tickets=True) == IMPLEMENT
    assert decide_path(ci_green=True, has_tickets=False) == IMPROVE


def test_build_pr_body_requires_ticket():
    choice = ModelChoice(tier="standard", model="sonnet", rationale="x")
    with pytest.raises(ValueError):
        build_pr_body(
            ticket=0, choice=choice, lesson_note="n",
            lesson_summary="s", ci_summary="CI green",
        )


def test_build_pr_body_contains_traceability():
    choice = ModelChoice(tier="heavy", model="opus", rationale="score=4 -> heavy")
    body = build_pr_body(
        ticket=42, choice=choice, lesson_note="2026-07-25-do-thing",
        lesson_summary="kept it small", ci_summary="CI green (ruff=pass, pytest=pass)",
        references=("openai/swarm", "SWE-agent/SWE-agent"),
    )
    assert "Closes #42" in body          # ticket linkage
    assert "`opus`" in body              # model recorded
    assert "kept it small" in body       # lesson present
    assert "[[2026-07-25-do-thing]]" in body
    assert "openai/swarm" in body


def test_run_once_dry_run_is_side_effect_free(tmp_path):
    cfg = load_config()
    result = run_once(cfg, repo_dir=str(tmp_path), dry_run=True, iteration=1)
    assert result.kind == IMPROVE          # no CI run, no tickets -> self-improve
    assert result.ci_before is True
    assert result.lesson_path                # a lesson was still recorded
    assert (tmp_path / "knowledge" / "lessons").exists()
    # dry-run never opens a PR
    assert result.pr is None
    assert result.merged is False
