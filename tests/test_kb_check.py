from pathlib import Path

from hsai.cli import main
from hsai.kb_check import check_vault, link_targets

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed_vault(root: Path, *, lessons: dict[str, str] | None = None) -> Path:
    vault = root / "knowledge"
    (vault / "lessons").mkdir(parents=True)
    (vault / "MOCs").mkdir(parents=True)
    (vault / "whitepapers").mkdir(parents=True)
    lessons = lessons or {"2026-08-04-a-lesson": "# a lesson\n\n> [[Lessons MOC]]\n"}
    for name, text in lessons.items():
        (vault / "lessons" / f"{name}.md").write_text(text)
    links = "\n".join(f"- [[{name}]]" for name in lessons)
    (vault / "MOCs" / "Lessons MOC.md").write_text(
        f"# Lessons MOC\n\nUp: [[Knowledge Base MOC]]\n\nTotal: **{len(lessons)}**.\n\n{links}\n"
    )
    (vault / "MOCs" / "Whitepapers MOC.md").write_text(
        "# Whitepapers MOC\n\nUp: [[Knowledge Base MOC]]\n\nTotal: **0**.\n"
    )
    (vault / "MOCs" / "Knowledge Base MOC.md").write_text(
        "# Knowledge Base MOC\n\n## Maps\n"
        f"- [[Lessons MOC]] - {len(lessons)} lesson(s)\n"
        "- [[Whitepapers MOC]] - 0 whitepaper(s)\n"
    )
    return vault


def test_link_targets_strips_aliases_and_headings():
    assert link_targets("see [[A]] and [[B|the b]] and [[C#section]]") == ["A", "B", "C"]
    assert link_targets("no links here") == []


def test_clean_vault_passes(tmp_path):
    _seed_vault(tmp_path)
    report = check_vault(tmp_path)
    assert report.ok is True
    assert report.errors == [] and report.warnings == []
    assert report.notes_scanned == 4
    assert "PASS" in report.render()


def test_dangling_wikilink_is_an_error(tmp_path):
    _seed_vault(
        tmp_path,
        lessons={"2026-08-04-a-lesson": "# a lesson\n\nsee [[2026-01-01-never-written]]\n"},
    )
    report = check_vault(tmp_path)
    assert report.ok is False
    assert len(report.errors) == 1
    assert report.errors[0].kind == "dangling wikilink"
    assert "2026-01-01-never-written" in report.errors[0].detail
    assert "FAIL" in report.render()


def test_kb_check_command_exits_nonzero_on_a_dangling_link(tmp_path, capsys):
    _seed_vault(
        tmp_path, lessons={"2026-08-04-a-lesson": "# a lesson\n\n[[nowhere]]\n"}
    )
    rc = main(["kb-check", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "dangling wikilink" in out and "[[nowhere]]" in out


def test_orphan_lessons_and_moc_drift_are_warnings_not_errors(tmp_path, capsys):
    """A worker PR adds a lesson; the MOCs are rebuilt once per block by
    `hsai reindex`. That window must not fail an honest PR - but `--strict`
    (run right after a reindex, when the index is meant to be current) does."""
    vault = _seed_vault(tmp_path)
    (vault / "lessons" / "2026-08-05-unindexed.md").write_text("# later lesson\n")

    report = check_vault(tmp_path)
    assert report.ok is True                       # not an error
    kinds = sorted({f.kind for f in report.warnings})
    assert kinds == ["MOC drift", "orphan lesson note"]

    assert main(["kb-check", "--root", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["kb-check", "--root", str(tmp_path), "--strict"]) == 1
    assert "orphan lesson note" in capsys.readouterr().out


def test_templates_are_exempt_from_link_checking(tmp_path):
    """Templates carry placeholder links on purpose (`[[lesson-note-name]]`)."""
    vault = _seed_vault(tmp_path)
    (vault / "templates").mkdir()
    (vault / "templates" / "whitepaper.md").write_text("- [[lesson-note-name]]\n")
    assert check_vault(tmp_path).ok is True


def test_the_committed_vault_has_no_dangling_links():
    """The gate CI runs, over the real knowledge base in this repo."""
    report = check_vault(REPO_ROOT)
    assert report.ok is True, report.render()
    assert report.notes_scanned > 0 and report.links_scanned > 0
