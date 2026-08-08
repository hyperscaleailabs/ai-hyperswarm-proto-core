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


def test_block_41345_governance_artifacts_exist():
    """Verify that governance artifacts for block 41345 have been created."""
    import pathlib

    # Verify lesson files exist
    lesson_41344 = pathlib.Path("knowledge/lessons/2026-08-08-implement-skill-learned-model-selection-heuristic-v2-calibrated-from-lessons.md")
    lesson_41345 = pathlib.Path("knowledge/lessons/2026-08-08-implement-feat-adversarial-acceptance-criteria-review-gate.md")
    lesson_governance = pathlib.Path("knowledge/lessons/2026-08-08-implement-chore-governance-artifacts-for-block-41345.md")
    assert lesson_41344.exists(), f"Lesson file for block 41344 not found: {lesson_41344}"
    assert lesson_41345.exists(), f"Lesson file for block 41345 not found: {lesson_41345}"
    assert lesson_governance.exists(), f"Lesson file for governance artifacts not found: {lesson_governance}"

    # Verify whitepaper exists
    whitepaper = pathlib.Path("knowledge/whitepapers/2026-08-08-synthesis-after-22-lessons.md")
    assert whitepaper.exists(), f"Whitepaper not found: {whitepaper}"

    # Verify persona articles exist
    architect_article = pathlib.Path("knowledge/articles/2026-08-08-synthesis-after-22-lessons-architect.md")
    devops_article = pathlib.Path("knowledge/articles/2026-08-08-synthesis-after-22-lessons-devops.md")
    assert architect_article.exists(), f"Architect article not found: {architect_article}"
    assert devops_article.exists(), f"DevOps article not found: {devops_article}"


def test_block_41345_lesson_files_have_correct_metadata():
    """Verify that lesson files have correct metadata and format."""
    import pathlib
    import yaml

    lesson_41344 = pathlib.Path("knowledge/lessons/2026-08-08-implement-skill-learned-model-selection-heuristic-v2-calibrated-from-lessons.md")
    content_41344 = lesson_41344.read_text()

    # Extract YAML front matter
    parts = content_41344.split("---")
    assert len(parts) >= 3, "Lesson file missing YAML front matter"

    metadata = yaml.safe_load(parts[1])
    assert "lesson" in metadata.get("tags", []), "Lesson missing 'lesson' tag"
    assert "outcome/pass" in metadata.get("tags", []), "Lesson should have outcome/pass"
    assert metadata.get("created") == "2026-08-08", "Lesson should be dated 2026-08-08"
    assert metadata.get("iteration") == 4134701, "Lesson should reference iteration 4134701"


def test_block_41345_moc_counts_updated():
    """Verify that MOCs have been updated with correct lesson and whitepaper counts."""
    import pathlib

    lessons_moc = pathlib.Path("knowledge/MOCs/Lessons MOC.md")
    content = lessons_moc.read_text()
    assert "Total: **23**" in content, "Lessons MOC should show 23 lessons"
    assert "[[2026-08-08-implement-skill-learned-model-selection-heuristic-v2-calibrated-from-lessons]]" in content
    assert "[[2026-08-08-implement-feat-adversarial-acceptance-criteria-review-gate]]" in content
    assert "[[2026-08-08-implement-chore-governance-artifacts-for-block-41345]]" in content

    whitepapers_moc = pathlib.Path("knowledge/MOCs/Whitepapers MOC.md")
    content = whitepapers_moc.read_text()
    assert "Total: **6**" in content, "Whitepapers MOC should show 6 whitepapers"
    assert "[[2026-08-08-synthesis-after-22-lessons]]" in content


def test_block_41345_direction_updated():
    """Verify that DIRECTION.md reflects the new state."""
    import pathlib

    direction = pathlib.Path("governance/DIRECTION.md")
    content = direction.read_text()
    assert "23 lessons" in content, "DIRECTION should mention 23 lessons"
    assert "6 whitepapers" in content, "DIRECTION should mention 6 whitepapers"
    assert "Learned model selection" in content, "DIRECTION should reference learned model selection"
    assert "adversarial acceptance-criteria" in content, "DIRECTION should reference adversarial gate"
