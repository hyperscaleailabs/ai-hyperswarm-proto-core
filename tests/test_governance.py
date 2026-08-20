import json

from hsai.calibrate import disagreement, fit
from hsai.config import load_config
from hsai.governance import (
    NOTES_END,
    NOTES_START,
    BlockReport,
    preserved_notes,
    render_brief,
    render_direction,
)
from hsai.ledger import BlockAggregate, LedgerRecord
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


# --- routing calibration section (see hsai.calibrate) ------------------------

def _routing_records():
    """Enough labelled records to clear the sample floor, with a clear optimum."""
    records = []
    for i in range(12):
        records.append(LedgerRecord(
            iteration=100 + i, block=7, ticket=1, kind="implement", tier="heavy",
            model="opus", wall_clock_seconds=100.0, attempts=1,
            outcome="merged" if i < 6 else "recovered",
            complexity_score=6, est_files=3, heavy_signals=1, light_signals=0,
            size_label="", demoted=False, strategy="heuristic-v1",
            shadow_tier="standard", shadow_strategy="heuristic-v2",
        ))
    for i in range(12):
        records.append(LedgerRecord(
            iteration=200 + i, block=7, ticket=1, kind="implement", tier="standard",
            model="sonnet", wall_clock_seconds=50.0, attempts=1, outcome="merged",
            complexity_score=3, est_files=3, heavy_signals=1, light_signals=0,
            size_label="", demoted=False, strategy="heuristic-v1",
            shadow_tier="standard", shadow_strategy="heuristic-v2",
        ))
    return records


def test_brief_has_a_routing_calibration_section_with_the_fit():
    cfg = load_config()
    records = _routing_records()
    report = BlockReport(
        cycle_index=7,
        routing_fit=fit(records),
        routing_disagreement=disagreement(records),
    )
    body = render_brief(cfg, report)

    assert "## Routing calibration" in body
    assert "Active strategy: `heuristic-v1`" in body
    assert "heavy>=7, light<=-3" in body           # the fitted recommendation
    assert "Shadow disagreement: 12/24 (50%)" in body
    assert "models.calibration" in body            # the human step is spelled out
    # It reads before the failure taxonomy, next to the other economics.
    assert body.index("## Cost this block") < body.index("## Routing calibration")
    assert body.index("## Routing calibration") < body.index("## Failure taxonomy")


def test_brief_states_insufficient_data_rather_than_a_fitted_guess():
    cfg = load_config()
    report = BlockReport(cycle_index=7, routing_fit=fit(_routing_records()[:4]))
    body = render_brief(cfg, report)

    assert "## Routing calibration" in body
    assert "Insufficient data" in body
    assert "No recommendation" in body
    assert "heavy>=" not in body


def test_brief_routing_section_when_no_fit_was_computed():
    body = render_brief(load_config(), BlockReport(cycle_index=7))
    assert "## Routing calibration" in body
    assert "no routing calibration computed for this block" in body
    assert "hsai calibrate" in body


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


# --- practices adopted this block --------------------------------------------

def test_brief_reports_practices_adopted_this_block():
    cfg = load_config()
    report = BlockReport(
        cycle_index=7,
        practices_adopted=[
            {
                "id": "openbmb-chatdev--session-durability",
                "title": "session durability",
                "source_project": "OpenBMB/ChatDev",
                "source_artifact": "harness_design",
                "status": "adopted",
                "evidence": "PR #104",
            }
        ],
    )
    body = render_brief(cfg, report)
    assert "## Practices adopted this block" in body
    assert "session durability" in body
    assert "OpenBMB/ChatDev" in body
    assert "PR #104" in body


def test_brief_reports_no_practices_adopted_when_none():
    cfg = load_config()
    body = render_brief(cfg, BlockReport(cycle_index=7))
    assert "## Practices adopted this block" in body
    assert "_none this block_" in body
