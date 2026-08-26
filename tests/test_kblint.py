"""hsai.kblint: the Obsidian knowledge base's integrity gate.

Builds a small temp vault by hand (not through `KnowledgeBase.write_*`, so the
fixture stays legible as exactly what it is: one deliberate defect per code)
and asserts `lint()` finds exactly one of each. The CLI-level behavior
(exit codes, `--strict`, printing) and the reindex round-trip live here too.
"""
from __future__ import annotations

from hsai import kblint
from hsai.cli import main
from hsai.knowledge import KnowledgeBase, Lesson


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _seed_broken_vault(root):
    """One deliberate defect per code, each isolated so it triggers exactly
    one finding (see the module docstring)."""
    k = root / "knowledge"

    # A clean lesson, linked from the Lessons MOC - the baseline every other
    # note is compared against; must contribute zero findings.
    _write(
        k / "lessons" / "good-lesson.md",
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n---\n\n"
        "# Good lesson\n\n> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]\n\n"
        "| ticket | #12 |\n\n## Lesson learned\nKeep the loop simple.\n",
    )

    # KB004: valid frontmatter and ticket ref, but a placeholder lesson body.
    _write(
        k / "lessons" / "placeholder-lesson.md",
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n---\n\n"
        "# Placeholder lesson\n\n> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]\n\n"
        "| ticket | #5 |\n\n## Lesson learned\n_(none)_\n",
    )

    # KB003: a lesson that exists on disk but no MOC links to it.
    _write(
        k / "lessons" / "orphan-lesson.md",
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n---\n\n"
        "# Orphan lesson\n\n| ticket | #9 |\n\n## Lesson learned\nReal content.\n",
    )

    # KB001: valid frontmatter, linked from its MOC, but cites a note that
    # does not exist anywhere in the vault.
    _write(
        k / "whitepapers" / "dangling-paper.md",
        "---\ntags:\n  - whitepaper\n---\n\n# Dangling paper\n\n"
        "> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]\n\n"
        "See [[does-not-exist]] for background.\n",
    )

    # KB002: linked from its MOC, no dangling links, but missing every
    # required tag for its kind.
    _write(
        k / "articles" / "bad-article.md",
        "---\ntags:\n  - misc\n---\n\n# Bad article\n\nNo persona tag, no article tag.\n",
    )

    _write(
        k / "MOCs" / "Knowledge Base MOC.md",
        "---\ntags:\n  - moc\n  - index\n---\n\n# Knowledge Base MOC\n\n"
        "- [[Lessons MOC]]\n- [[Whitepapers MOC]]\n- [[Articles MOC]]\n",
    )
    _write(
        k / "MOCs" / "Lessons MOC.md",
        "---\ntags:\n  - moc\n  - lessons\n---\n\n# Lessons MOC\n\n"
        "- [[good-lesson]]\n- [[placeholder-lesson]]\n",
    )
    _write(
        k / "MOCs" / "Whitepapers MOC.md",
        "---\ntags:\n  - moc\n  - whitepapers\n---\n\n# Whitepapers MOC\n\n"
        "- [[dangling-paper]]\n",
    )
    _write(
        k / "MOCs" / "Articles MOC.md",
        "---\ntags:\n  - moc\n  - articles\n---\n\n# Articles MOC\n\n"
        "- [[bad-article]]\n",
    )


def test_lint_reports_exactly_one_finding_per_defect(tmp_path):
    _seed_broken_vault(tmp_path)

    findings = kblint.lint(tmp_path)
    by_code = {f.code: f for f in findings}

    assert len(findings) == 4
    assert set(by_code) == {"KB001", "KB002", "KB003", "KB004"}

    assert by_code["KB001"].path == "knowledge/whitepapers/dangling-paper.md"
    assert "does-not-exist" in by_code["KB001"].message
    assert by_code["KB001"].severity == "error"

    assert by_code["KB002"].path == "knowledge/articles/bad-article.md"
    assert "article" in by_code["KB002"].message
    assert by_code["KB002"].severity == "warn"

    assert by_code["KB003"].path == "knowledge/lessons/orphan-lesson.md"
    assert by_code["KB003"].severity == "error"

    assert by_code["KB004"].path == "knowledge/lessons/placeholder-lesson.md"
    assert "Lesson learned" in by_code["KB004"].message
    assert by_code["KB004"].severity == "warn"


def test_lint_on_an_empty_vault_is_clean(tmp_path):
    assert kblint.lint(tmp_path) == []


def test_dangling_wikilink_resolves_against_hsai_md(tmp_path):
    """`knowledge/hsai.md` is a valid KB001 resolution target even though it
    is not one of the five scanned directories."""
    k = tmp_path / "knowledge"
    _write(k / "hsai.md", "---\ntags:\n  - concept\n---\n\n# hsai\n\nThe loop.\n")
    _write(
        k / "MOCs" / "Knowledge Base MOC.md",
        "---\ntags:\n  - moc\n  - index\n---\n\n# Knowledge Base MOC\n\n"
        "The [[hsai]] loop.\n",
    )
    findings = kblint.lint(tmp_path)
    assert findings == []  # hsai.md resolves, and it is linked from a MOC


# --- severity / --strict ------------------------------------------------------

def test_has_errors_only_counts_error_severity_by_default(tmp_path):
    _seed_broken_vault(tmp_path)
    findings = kblint.lint(tmp_path)
    assert kblint.has_errors(findings) is True  # KB001/KB003 are already errors

    only_warnings = [f for f in findings if f.severity == "warn"]
    assert kblint.has_errors(only_warnings) is False
    assert kblint.has_errors(only_warnings, strict=True) is True


# --- the CLI --------------------------------------------------------------

def test_cli_exits_1_and_prints_path_code_message_when_errors_exist(tmp_path, capsys):
    _seed_broken_vault(tmp_path)
    rc = main(["kb-lint", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "knowledge/whitepapers/dangling-paper.md:KB001: dangling wikilink to [[does-not-exist]]" in out
    assert "knowledge/lessons/orphan-lesson.md:KB003:" in out


def test_cli_exits_0_on_a_clean_vault(tmp_path, capsys):
    rc = main(["kb-lint", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out


def test_cli_strict_promotes_warnings_to_a_nonzero_exit(tmp_path):
    # Only the KB004 warning: valid frontmatter, ticket ref present, linked
    # from its MOC, but a placeholder Lesson learned body.
    k = tmp_path / "knowledge"
    _write(
        k / "lessons" / "placeholder-lesson.md",
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n---\n\n"
        "# Placeholder lesson\n\n| ticket | #5 |\n\n## Lesson learned\n_(none)_\n",
    )
    _write(
        k / "MOCs" / "Lessons MOC.md",
        "---\ntags:\n  - moc\n  - lessons\n---\n\n# Lessons MOC\n\n- [[placeholder-lesson]]\n",
    )

    assert main(["kb-lint", "--root", str(tmp_path)]) == 0
    assert main(["kb-lint", "--strict", "--root", str(tmp_path)]) == 1


# --- reindex round-trip: the generator must satisfy its own linter -----------

def test_reindex_then_lint_stays_clean(tmp_path):
    """`KnowledgeBase.reindex_mocs` (what `hsai reindex` runs) must produce a
    vault `kblint.lint` reports zero findings against - the exact invariant
    `hsai reindex && hsai kb-lint` checks in CI."""
    kb = KnowledgeBase(tmp_path)
    # `_write_root_moc` always links [[hsai]] - the real vault's concept note
    # for the same reason `[[hsai]]` has to exist for the round-trip to stay
    # clean in this repo's own `knowledge/`.
    _write(
        tmp_path / "knowledge" / "hsai.md",
        "---\ntags:\n  - concept\n---\n\n# hsai\n\nThe loop.\n",
    )
    kb.write_lesson(Lesson(
        title="add status command",
        outcome="pass",
        kind="implement",
        context="c",
        what_happened="w",
        lesson="Keep the loop simple.",
        ticket=12,
    ))
    (kb.articles_dir / "block-1-architect.md").write_text(
        "---\ntags:\n  - article\n  - persona/architect\n---\n\n"
        "# For the architect\n\nBody.\n\n---\nPart of [[Knowledge Base MOC]].\n"
    )

    kb.reindex_mocs()

    findings = kblint.lint(tmp_path)
    assert findings == []
