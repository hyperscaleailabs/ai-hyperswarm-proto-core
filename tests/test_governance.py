import json

from hsai.config import load_config
from hsai.governance import (
    NOTES_END,
    NOTES_START,
    BlockReport,
    preserved_notes,
    render_brief,
    render_direction,
)
from hsai.ledger import BlockAggregate
from hsai.proc import Proc


def _issues_runner(cmd, **kwargs):
    if cmd[:3] == ["gh", "issue", "list"]:
        return Proc(cmd, 0, json.dumps([
            {"number": 1, "title": "feat: x", "labels": [{"name": "priority:P1"}],
             "assignees": [], "body": ""},
            {"number": 2, "title": "feat: y", "labels": [{"name": "priority:P2"},
             {"name": "blocked"}], "assignees": [], "body": ""},
        ]), "")
    return Proc(cmd, 0, "", "")


def test_direction_has_three_layers_and_issue_map(tmp_path):
    cfg = load_config()
    text = render_direction(cfg, repo_root=tmp_path, runner=_issues_runner)
    assert "## Now (current state)" in text
    assert "## Issues Map" in text
    assert "## Direction (where we are going)" in text
    assert "#1 feat: x" in text
    assert "BLOCKED" in text  # blocked tickets visibly flagged
    assert NOTES_START in text and NOTES_END in text


def test_architect_notes_survive_regeneration(tmp_path):
    cfg = load_config()
    doc = tmp_path / "governance" / "DIRECTION.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(f"junk\n{NOTES_START}\nKEEP ME - human wrote this\n{NOTES_END}\n")
    text = render_direction(cfg, repo_root=tmp_path, runner=_issues_runner)
    assert "KEEP ME - human wrote this" in text


def test_brief_links_everything(tmp_path):
    cfg = load_config()
    report = BlockReport(
        cycle_index=7, synthesized=[31, 32], iterations=["iteration(kind=implement...)"],
        merged_prs=[40], recovered_prs=[41], whitepaper="2026-07-26-synthesis",
        articles=["knowledge/articles/2026-07-26-synthesis-cto.md"],
    )
    body = render_brief(cfg, report)
    assert "#31" in body and "#32" in body
    assert "pull/40" in body and "pull/41" in body
    assert "2026-07-26-synthesis" in body
    assert "/review-next" in body


def test_preserved_notes_default_when_missing(tmp_path):
    assert "never overwritten" in preserved_notes(tmp_path / "nope.md")


def test_brief_renders_block_cost_summary():
    cfg = load_config()
    cost = BlockAggregate(
        block=7, iterations=3, heavy_iterations=2, total_seconds=180.0,
        total_attempts=4, tier_counts={"heavy": 2, "standard": 1},
    )
    report = BlockReport(cycle_index=7, cost=cost)
    body = render_brief(cfg, report)
    assert "## Cost this block (quota ledger)" in body
    assert "heavy-tier=2" in body
    assert "180s wall-clock" in body


def test_brief_cost_note_when_no_ledger_records():
    cfg = load_config()
    body = render_brief(cfg, BlockReport(cycle_index=7))
    assert "_no ledger records for this block_" in body


def test_brief_reports_tokens_per_merged_pr():
    """Quota spent per unit of delivered work - the number G4 steers on."""
    cfg = load_config()
    cost = BlockAggregate(
        block=7, iterations=3, heavy_iterations=1, merged_iterations=2,
        total_seconds=180.0, total_attempts=3,
        input_tokens=9000, output_tokens=1000,
    )
    body = render_brief(cfg, BlockReport(cycle_index=7, cost=cost))
    assert "5000 tokens per merged PR" in body
    assert "10000 tokens / 2 merged" in body


def test_brief_says_when_tokens_per_merged_pr_is_unavailable():
    cfg = load_config()
    cost = BlockAggregate(block=7, iterations=2, total_seconds=10.0)
    body = render_brief(cfg, BlockReport(cycle_index=7, cost=cost))
    assert "tokens per merged PR: _not available_" in body


def test_brief_shows_the_slowest_stage_line():
    cfg = load_config()
    line = "slowest stage: `agent_run` - 42.0s total across 3 run(s) (max 20.0s)"
    body = render_brief(cfg, BlockReport(cycle_index=7, slowest_stage=line))
    assert line in body


def test_brief_notes_when_no_stage_timing_was_recorded():
    cfg = load_config()
    body = render_brief(cfg, BlockReport(cycle_index=7))
    assert "no stage timing recorded for this block" in body
