import json
from pathlib import Path

from hsai.config import load_config
from hsai.governance import (
    NOTES_END,
    NOTES_START,
    BlockReport,
    preserved_notes,
    render_brief,
    render_direction,
)
from hsai.knowledge import KnowledgeBase
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


def test_governance_artifacts_latest_whitepaper_has_all_personas():
    """Verify the latest whitepaper has persona articles for all configured personas.

    This test enforces that block governance artifacts are complete:
    - Whitepaper synthesized ✓
    - Persona articles for each configured persona ✓
    - MOCs reindexed (verified elsewhere)
    - DIRECTION refreshed (verified elsewhere)
    """
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, Path("."))
    papers = kb.whitepaper_notes()
    assert papers, "No whitepapers found - knowledge base is empty"

    latest_paper = papers[-1]
    articles = kb.persona_articles(latest_paper)
    personas = cfg.personas or []

    for persona in personas:
        persona_id = persona.get("id")
        assert (
            persona_id in articles
        ), f"Missing persona article for {persona_id} in whitepaper {latest_paper}. " \
           f"Found: {list(articles.keys())}, Expected: {[p.get('id') for p in personas]}"


def test_mocs_include_all_lessons():
    """Verify all lesson notes are referenced in the Lessons MOC."""
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, Path("."))
    moc_path = kb.mocs_dir / "Lessons MOC.md"
    assert moc_path.exists(), "Lessons MOC not found"

    moc_content = moc_path.read_text()
    lessons = kb.lesson_notes()

    for lesson_note in lessons:
        assert (
            f"[[{lesson_note}]]" in moc_content
        ), f"Lesson {lesson_note} not linked in Lessons MOC"


def test_mocs_include_all_whitepapers():
    """Verify all whitepaper notes are referenced in the Whitepapers MOC."""
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, Path("."))
    moc_path = kb.mocs_dir / "Whitepapers MOC.md"
    assert moc_path.exists(), "Whitepapers MOC not found"

    moc_content = moc_path.read_text()
    papers = kb.whitepaper_notes()

    for paper_note in papers:
        assert (
            f"[[{paper_note}]]" in moc_content
        ), f"Whitepaper {paper_note} not linked in Whitepapers MOC"


def test_direction_doc_exists_and_is_recent():
    """Verify DIRECTION.md exists and references the current goals."""
    cfg = load_config()
    direction_path = Path(".") / cfg.governance.get("direction_doc", "governance/DIRECTION.md")
    assert direction_path.exists(), f"DIRECTION doc not found at {direction_path}"

    content = direction_path.read_text()
    assert "## Now (current state)" in content
    assert "## Issues Map" in content
    assert "## Direction (where we are going)" in content

    # Verify all goals are referenced
    for goal in cfg.goals:
        goal_id = goal.get("id")
        assert f"- **{goal_id}**" in content, f"Goal {goal_id} not found in DIRECTION"
