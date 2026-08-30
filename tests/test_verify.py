import re

from hsai.knowledge import KnowledgeBase, Lesson, Whitepaper
from hsai.ledger import LedgerRecord, append_record
from hsai.verify import (
    CHECK_ARTICLES,
    CHECK_LEDGER,
    CHECK_LESSON_FRONTMATTER,
    CHECK_MOCS,
    CHECK_WIKILINKS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    Finding,
    VerifyReport,
    check_articles,
    check_ledger,
    check_lesson_frontmatter,
    check_mocs,
    check_wikilinks,
    verify_repo,
)


def _lesson(**overrides) -> Lesson:
    fields = dict(
        title="implement: add widget",
        outcome="pass",
        kind="implement",
        context="ctx",
        what_happened="did the thing",
        lesson="kept it small",
    )
    fields.update(overrides)
    return Lesson(**fields)


# --- VerifyReport --------------------------------------------------------------

def test_verify_report_ok_iff_no_error_finding():
    clean = VerifyReport([Finding(CHECK_WIKILINKS, SEVERITY_WARNING, "a.md", "meh")])
    assert clean.ok is True

    broken = VerifyReport([Finding(CHECK_WIKILINKS, SEVERITY_ERROR, "a.md", "boom")])
    assert broken.ok is False


def test_verify_report_groups_and_renders():
    report = VerifyReport([
        Finding(CHECK_WIKILINKS, SEVERITY_ERROR, "a.md", "dangling"),
        Finding(CHECK_LEDGER, SEVERITY_WARNING, "iterations.jsonl:3", "odd"),
    ])
    assert report.errors() == [report.findings[0]]
    assert report.warnings() == [report.findings[1]]
    grouped = report.by_check()
    assert set(grouped) == {CHECK_WIKILINKS, CHECK_LEDGER}
    text = report.render()
    assert "wikilinks" in text and "ledger" in text
    assert "1 error(s), 1 warning(s)" in text


def test_verify_report_render_clean():
    assert "OK" in VerifyReport([]).render()


# --- (a) wikilinks ---------------------------------------------------------------

def test_check_wikilinks_passes_when_every_link_resolves(tmp_path):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "a.md").write_text("# A\n\n[[b]]\n")
    (lessons / "b.md").write_text("# B\n\n[[a|Alias]] and [[a#Section]]\n")
    assert check_wikilinks(tmp_path) == []


def test_check_wikilinks_flags_a_dangling_link(tmp_path):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    broken = lessons / "a.md"
    broken.write_text("# A\n\n[[does-not-exist]]\n")

    findings = check_wikilinks(tmp_path)

    assert len(findings) == 1
    assert findings[0].check == CHECK_WIKILINKS
    assert findings[0].severity == SEVERITY_ERROR
    assert findings[0].path == "knowledge/lessons/a.md"
    assert "does-not-exist" in findings[0].message


def test_check_wikilinks_is_a_noop_without_a_knowledge_dir(tmp_path):
    assert check_wikilinks(tmp_path) == []


# --- (b) MOCs --------------------------------------------------------------------

def test_check_mocs_passes_when_committed_mocs_are_freshly_reindexed(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()

    assert check_mocs(tmp_path) == []


def test_check_mocs_flags_a_stale_moc(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()

    # A second lesson lands, but nobody re-ran `hsai reindex`.
    kb.write_lesson(_lesson(title="implement: add gadget"))

    findings = check_mocs(tmp_path)

    stale_paths = {f.path for f in findings}
    assert "knowledge/MOCs/Lessons MOC.md" in stale_paths
    for f in findings:
        assert f.check == CHECK_MOCS
        assert f.severity == SEVERITY_ERROR
        assert "stale" in f.message


def test_check_mocs_flags_a_missing_moc(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()
    (kb.mocs_dir / "Lessons MOC.md").unlink()

    findings = check_mocs(tmp_path)

    assert any(
        f.path == "knowledge/MOCs/Lessons MOC.md" and "missing" in f.message for f in findings
    )


def test_check_mocs_ignores_the_updated_timestamp_line(tmp_path):
    """Regenerating on a different day than the last commit must not, by
    itself, look like drift - only the `updated:` line may legitimately
    differ between two reindexes of an unchanged vault."""
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()

    path = kb.mocs_dir / "Lessons MOC.md"
    text = re.sub(r"^updated: \d{4}-\d{2}-\d{2}$", "updated: 1999-01-01", path.read_text(),
                  flags=re.MULTILINE)
    path.write_text(text)

    assert check_mocs(tmp_path) == []


# --- (c) lesson frontmatter --------------------------------------------------------

def test_check_lesson_frontmatter_passes_a_well_formed_lesson(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson())
    assert check_lesson_frontmatter(tmp_path) == []


def test_check_lesson_frontmatter_flags_a_missing_outcome_tag(tmp_path):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "bad.md").write_text(
        "---\ntags:\n  - lesson\n  - kind/implement\n---\n\n"
        "# Bad lesson\n\n## Context\nc\n\n## What happened\nw\n\n## Lesson learned\nl\n"
    )

    findings = check_lesson_frontmatter(tmp_path)

    assert len(findings) == 1
    assert findings[0].check == CHECK_LESSON_FRONTMATTER
    assert findings[0].severity == SEVERITY_ERROR
    assert findings[0].path == "knowledge/lessons/bad.md"
    assert "outcome" in findings[0].message


def test_check_lesson_frontmatter_flags_a_missing_section(tmp_path):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "bad.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n---\n\n"
        "# Bad lesson\n\n## Context\nc\n\n## Lesson learned\nl\n"
    )

    findings = check_lesson_frontmatter(tmp_path)

    assert any(
        "## What happened" in f.message and f.path == "knowledge/lessons/bad.md"
        for f in findings
    )


# --- (d) ledger ------------------------------------------------------------------

def test_check_ledger_passes_well_formed_lines(tmp_path):
    path = tmp_path / "knowledge" / "ledger" / "iterations.jsonl"
    append_record(path, LedgerRecord(
        iteration=1, block=1, ticket=1, kind="implement", tier="standard",
        model="sonnet", wall_clock_seconds=1.0, attempts=1, outcome="merged",
    ))
    assert check_ledger(tmp_path) == []


def test_check_ledger_reports_a_malformed_line_instead_of_raising(tmp_path):
    path = tmp_path / "knowledge" / "ledger" / "iterations.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"iteration": 1, "block": 1, "ticket": 1, "kind": "implement", '
        '"tier": "standard", "model": "sonnet", "wall_clock_seconds": 1.0, '
        '"attempts": 1, "outcome": "merged"}\n'
        "not even json\n"
        '{"iteration": 2}\n'
    )

    findings = check_ledger(tmp_path)

    assert len(findings) == 2
    assert all(f.check == CHECK_LEDGER and f.severity == SEVERITY_ERROR for f in findings)
    assert findings[0].path == "knowledge/ledger/iterations.jsonl:2"
    assert "invalid JSON" in findings[0].message
    assert findings[1].path == "knowledge/ledger/iterations.jsonl:3"
    assert "LedgerRecord" in findings[1].message


def test_check_ledger_is_a_noop_without_a_ledger_file(tmp_path):
    assert check_ledger(tmp_path) == []


# --- (e) persona articles ---------------------------------------------------------

def test_check_articles_passes_when_the_whitepaper_exists(tmp_path):
    kb = KnowledgeBase(tmp_path)
    paper = kb.write_whitepaper(Whitepaper(title="synthesis", summary="s", body="b"))
    articles = tmp_path / "knowledge" / "articles"
    articles.mkdir(parents=True)
    (articles / f"{paper.stem}-cto.md").write_text(
        "---\ntags:\n  - article\n  - persona/cto\n---\n\n# For the CTO\n\nBody.\n"
    )

    assert check_articles(tmp_path) == []


def test_check_articles_flags_a_missing_whitepaper(tmp_path):
    articles = tmp_path / "knowledge" / "articles"
    articles.mkdir(parents=True)
    (articles / "2026-01-01-ghost-cto.md").write_text(
        "---\ntags:\n  - article\n  - persona/cto\n---\n\n# For the CTO\n\nBody.\n"
    )

    findings = check_articles(tmp_path)

    assert len(findings) == 1
    assert findings[0].check == CHECK_ARTICLES
    assert findings[0].severity == SEVERITY_ERROR
    assert findings[0].path == "knowledge/articles/2026-01-01-ghost-cto.md"
    assert "2026-01-01-ghost" in findings[0].message


def test_check_articles_flags_a_missing_persona_tag(tmp_path):
    articles = tmp_path / "knowledge" / "articles"
    articles.mkdir(parents=True)
    (articles / "2026-01-01-untagged.md").write_text("# No tag\n\nBody.\n")

    findings = check_articles(tmp_path)

    assert len(findings) == 1
    assert "persona" in findings[0].message


# --- the aggregate + allowlist -----------------------------------------------------

def test_verify_repo_downgrades_the_documented_legacy_template_link(tmp_path):
    """The whitepaper template's placeholder [[lesson-note-name]] link is a
    known, documented pre-existing violation (see LEGACY_ALLOWLIST) - it must
    report as a warning, not fail the build."""
    kb = KnowledgeBase(tmp_path)
    # The root MOC template wikilinks [[hsai]] - the real repo carries that
    # note at knowledge/hsai.md; a synthetic vault needs it too or the root
    # MOC itself would dangle before reindex_mocs() is even at fault.
    (tmp_path / "knowledge" / "hsai.md").write_text("# hsai\n\nThe loop.\n")
    kb.reindex_mocs()  # empty vault: committed MOCs already match a fresh reindex

    templates = tmp_path / "knowledge" / "templates"
    templates.mkdir(parents=True)
    (templates / "whitepaper.md").write_text(
        "---\ntags:\n  - whitepaper\n---\n\n# {{title}}\n\n"
        "## Lessons synthesized\n- [[lesson-note-name]]\n"
    )

    report = verify_repo(tmp_path)

    assert report.ok is True
    matching = [f for f in report.findings if f.path == "knowledge/templates/whitepaper.md"]
    assert len(matching) == 1
    assert matching[0].severity == SEVERITY_WARNING
    assert "allowlisted" in matching[0].message


def test_verify_repo_aggregates_every_check(tmp_path):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "a.md").write_text("# A\n\n[[does-not-exist]]\n")

    report = verify_repo(tmp_path)

    assert report.ok is False
    assert any(f.check == CHECK_WIKILINKS for f in report.errors())
