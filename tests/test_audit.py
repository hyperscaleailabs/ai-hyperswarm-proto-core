"""hsai audit: wikilinks / orphans / MOC freshness / frontmatter, plus the
GitHub-dependent closure + consistency checks and the idempotent drift ticket.
"""
import json

from hsai import audit, ledger
from hsai.cli import main
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson
from hsai.proc import Proc


def _lesson(title, **kw):
    kw.setdefault("outcome", "pass")
    kw.setdefault("kind", "implement")
    kw.setdefault("context", "c")
    kw.setdefault("what_happened", "w")
    kw.setdefault("lesson", "l")
    return Lesson(title=title, **kw)


# --- check (a): wikilink resolution --------------------------------------------


def test_check_wikilinks_flags_a_dangling_link(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson("a", lesson="See [[does-not-exist]] for details."))

    result = audit.check_wikilinks(tmp_path, [])

    assert result.name == "wikilinks"
    assert result.ok is False
    assert len(result.findings) == 1
    assert "does-not-exist" in result.findings[0].detail


def test_check_wikilinks_passes_when_every_link_resolves(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson("a"))
    kb.reindex_mocs()

    result = audit.check_wikilinks(tmp_path, [])
    assert result.ok is True


# --- check (b): orphan detection -----------------------------------------------


def test_check_orphans_flags_a_note_not_reachable_from_any_moc(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson("linked"))
    kb.write_lesson(_lesson("orphan"))
    kb.reindex_mocs()

    notes = kb.lesson_notes()
    linked_name = next(n for n in notes if n.endswith("-linked"))
    orphan_name = next(n for n in notes if n.endswith("-orphan"))

    # Simulate drift: the orphan's link is dropped from the committed MOC.
    moc_path = kb.mocs_dir / "Lessons MOC.md"
    moc_path.write_text(moc_path.read_text().replace(f"- [[{orphan_name}]]\n", ""))

    result = audit.check_orphans(tmp_path, kb, [])

    assert result.ok is False
    assert {f.target for f in result.findings} == {orphan_name}
    assert linked_name not in {f.target for f in result.findings}


def test_check_orphans_passes_on_an_empty_vault(tmp_path):
    kb = KnowledgeBase(tmp_path)
    result = audit.check_orphans(tmp_path, kb, [])
    assert result.ok is True


# --- check (c): MOC freshness --------------------------------------------------


def test_check_moc_freshness_flags_a_stale_moc(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson("a"))
    kb.reindex_mocs()
    assert audit.check_moc_freshness(kb, []).ok is True

    # A second lesson lands without a reindex: the committed MOCs are stale.
    kb.write_lesson(_lesson("b"))
    result = audit.check_moc_freshness(kb, [])

    assert result.ok is False
    targets = {f.target for f in result.findings}
    assert str(kb.mocs_dir / "Lessons MOC.md") in targets
    assert str(kb.mocs_dir / "Whitepapers MOC.md") not in targets


# --- check (d): frontmatter/schema validity ------------------------------------


def test_check_frontmatter_flags_a_missing_required_field(tmp_path):
    kb = KnowledgeBase(tmp_path)
    path = kb.lessons_dir / "2026-01-01-bad.md"
    path.write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n---\n\n# bad\n"
    )

    result = audit.check_frontmatter(tmp_path, kb, [])

    assert result.ok is False
    details = {f.detail for f in result.findings}
    assert any("created" in d for d in details)
    assert any("iteration" in d for d in details)


def test_check_frontmatter_passes_on_a_well_formed_lesson(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson("a"))
    result = audit.check_frontmatter(tmp_path, kb, [])
    assert result.ok is True


# --- known exceptions -----------------------------------------------------------


def test_load_known_exceptions_round_trips(tmp_path):
    path = tmp_path / "known_exceptions.yaml"
    path.write_text("- check: orphans\n  target: some-note\n  reason: pre-invariant\n")

    exceptions = audit.load_known_exceptions(path)

    assert exceptions == [
        audit.KnownException(check="orphans", target="some-note", reason="pre-invariant")
    ]
    assert audit.load_known_exceptions(tmp_path / "missing.yaml") == []


def test_known_exception_suppresses_a_finding(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson("a", lesson="See [[missing]] here."))
    note = kb.lesson_notes()[0]
    exceptions = [
        audit.KnownException(
            check="wikilinks", target=f"knowledge/lessons/{note}.md:missing", reason="test"
        )
    ]

    result = audit.check_wikilinks(tmp_path, exceptions)
    assert result.ok is True


# --- run_audit: vault-local vs full ---------------------------------------------


def test_run_audit_passes_on_a_clean_fresh_vault(tmp_path):
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    kb.write_lesson(_lesson("a"))
    kb.reindex_mocs()

    report = audit.run_audit(cfg, tmp_path)

    assert report.ok is True
    assert [c.name for c in report.checks] == [
        "wikilinks", "orphans", "moc_freshness", "frontmatter",
    ]
    assert json.loads(report.to_json())["ok"] is True


def test_run_audit_only_adds_github_checks_when_since_is_given(tmp_path):
    cfg = load_config()

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:2] == ["git", "log"]:
            return Proc(cmd, 0, "2026-01-01T00:00:00+00:00\n", "")
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, "[]", "")
        return Proc(cmd, 0, "", "")

    without_since = audit.run_audit(cfg, tmp_path, runner=runner)
    assert [c.name for c in without_since.checks] == [
        "wikilinks", "orphans", "moc_freshness", "frontmatter",
    ]

    with_since = audit.run_audit(cfg, tmp_path, since="HEAD~5", runner=runner)
    assert [c.name for c in with_since.checks] == [
        "wikilinks", "orphans", "moc_freshness", "frontmatter",
        "lesson_ticket_pr", "merged_prs_have_lessons", "model_consistency",
    ]
    assert with_since.ok is True


# --- check (e): lesson<->ticket<->PR closure (GitHub-dependent) ----------------


def test_check_lesson_ticket_pr_flags_an_open_ticket_and_an_unmerged_pr(tmp_path):
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    kb.write_lesson(_lesson("a", ticket=7, pr=8))

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return Proc(cmd, 0, json.dumps({"state": "OPEN"}), "")
        if cmd[:3] == ["gh", "pr", "view"]:
            return Proc(
                cmd, 0,
                json.dumps({
                    "number": 8, "state": "OPEN", "mergedAt": None, "body": "", "title": "",
                }),
                "",
            )
        return Proc(cmd, 0, "", "")

    result = audit.check_lesson_ticket_pr(tmp_path, cfg, kb, [], runner=runner)

    assert result.ok is False
    details = " ".join(f.detail for f in result.findings)
    assert "ticket #7" in details and "not closed" in details
    assert "PR #8" in details and "not merged" in details


def test_check_lesson_ticket_pr_flags_a_lesson_with_no_pr(tmp_path):
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    kb.write_lesson(_lesson("a", ticket=7))  # pr left unset

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return Proc(cmd, 0, json.dumps({"state": "CLOSED"}), "")
        return Proc(cmd, 0, "", "")

    result = audit.check_lesson_ticket_pr(tmp_path, cfg, kb, [], runner=runner)

    assert result.ok is False
    assert any("no pull request" in f.detail for f in result.findings)


def test_check_lesson_ticket_pr_respects_known_exceptions(tmp_path):
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    kb.write_lesson(_lesson("a", ticket=7))
    note = kb.lesson_notes()[0]

    def runner(cmd, **kwargs):
        return Proc(list(cmd), 0, "", "")

    exceptions = [audit.KnownException(check="lesson_ticket_pr", target=note, reason="pre-invariant")]
    result = audit.check_lesson_ticket_pr(tmp_path, cfg, kb, exceptions, runner=runner)
    assert result.ok is True


# --- check (f/1): merged PRs since --since have a lesson -----------------------


def test_check_merged_prs_have_lessons_flags_a_pr_with_no_lesson(tmp_path):
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    kb.write_lesson(_lesson("a", ticket=1, pr=10))

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:2] == ["git", "log"]:
            return Proc(cmd, 0, "2026-01-01T00:00:00+00:00\n", "")
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, json.dumps([
                {"number": 10, "title": "x", "body": "", "headRefName": "",
                 "mergedAt": "2026-01-02T00:00:00Z"},
                {"number": 11, "title": "y", "body": "", "headRefName": "",
                 "mergedAt": "2026-01-03T00:00:00Z"},
            ]), "")
        return Proc(cmd, 0, "", "")

    result = audit.check_merged_prs_have_lessons(tmp_path, cfg, kb, "HEAD~5", [], runner=runner)

    assert result.ok is False
    assert {f.target for f in result.findings} == {"11"}


def test_check_merged_prs_have_lessons_skips_when_since_does_not_resolve(tmp_path):
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)

    def runner(cmd, **kwargs):
        return Proc(list(cmd), 0, "", "")  # `git log` prints nothing: unresolved ref

    result = audit.check_merged_prs_have_lessons(tmp_path, cfg, kb, "not-a-ref", [], runner=runner)
    assert result.ok is True
    assert result.skipped is True


# --- check (f/2): model-record consistency -------------------------------------


def test_check_model_consistency_flags_a_tier_mismatch(tmp_path):
    cfg = load_config()
    ledger_file = ledger.ledger_path(cfg, tmp_path)
    ledger.append_record(
        ledger_file,
        ledger.LedgerRecord(
            iteration=1, block=0, ticket=42, kind="implement",
            tier="standard", model="sonnet", wall_clock_seconds=1.0,
            attempts=1, outcome="merged",
        ),
    )
    pr_body = "Closes #42\n\n## Model used\n- **model**: `haiku` (tier: `light`)\n"

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:2] == ["git", "log"]:
            return Proc(cmd, 0, "2026-01-01T00:00:00+00:00\n", "")
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, json.dumps([
                {"number": 99, "title": "x", "body": pr_body, "headRefName": "",
                 "mergedAt": "2026-01-02T00:00:00Z"},
            ]), "")
        return Proc(cmd, 0, "", "")

    result = audit.check_model_consistency(tmp_path, cfg, "HEAD~5", [], runner=runner)

    assert result.ok is False
    assert "light" in result.findings[0].detail and "standard" in result.findings[0].detail


def test_check_model_consistency_ignores_prs_that_do_not_reference_a_tier(tmp_path):
    cfg = load_config()

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:2] == ["git", "log"]:
            return Proc(cmd, 0, "2026-01-01T00:00:00+00:00\n", "")
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, json.dumps([
                {"number": 99, "title": "x", "body": "Closes #1\n\ngovernance artifacts",
                 "headRefName": "", "mergedAt": "2026-01-02T00:00:00Z"},
            ]), "")
        return Proc(cmd, 0, "", "")

    result = audit.check_model_consistency(tmp_path, cfg, "HEAD~5", [], runner=runner)
    assert result.ok is True


# --- idempotent audit-drift ticket ----------------------------------------------


def test_file_or_update_drift_ticket_never_files_a_duplicate(tmp_path):
    cfg = load_config()
    report = audit.AuditReport(
        checks=[audit.CheckResult(
            name="wikilinks", ok=False,
            findings=[audit.Finding("wikilinks", "x.md", "dangling [[y]]")],
        )]
    )
    calls = {"create": 0, "edit": 0, "list": 0, "comment": 0}
    state = {"issues": []}

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            calls["list"] += 1
            return Proc(cmd, 0, json.dumps(state["issues"]), "")
        if cmd[:3] == ["gh", "issue", "create"]:
            calls["create"] += 1
            return Proc(cmd, 0, "https://github.com/o/r/issues/501\n", "")
        if cmd[:3] == ["gh", "issue", "edit"]:
            calls["edit"] += 1
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["gh", "issue", "comment"]:
            calls["comment"] += 1
            return Proc(cmd, 0, "", "")
        return Proc(cmd, 0, "", "")

    first = audit.file_or_update_drift_ticket(cfg, report, runner=runner)
    assert first == 501
    assert calls["create"] == 1 and calls["edit"] == 0

    # A second run against the same unresolved drift: the ticket is now open,
    # so it must be updated in place - never a second, duplicate ticket.
    state["issues"] = [{
        "number": 501, "title": "chore: audit drift (wikilinks)",
        "labels": [{"name": "priority:P1"}, {"name": "audit-drift"}],
        "assignees": [], "body": "",
    }]
    second = audit.file_or_update_drift_ticket(cfg, report, runner=runner)

    assert second == 501
    assert calls["create"] == 1  # still just one ticket ever filed
    assert calls["edit"] == 1


# --- CLI ---------------------------------------------------------------------


def test_audit_command_json_and_strict_flag(tmp_path, capsys):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "2026-01-01-a.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n"
        "created: 2026-01-01\niteration: 0\n---\n\n# a\n\n## Lesson learned\nfine\n"
    )

    rc = main(["audit", "--root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False  # no MOC ever regenerated: orphan + stale
    assert rc == 0  # --strict not passed: report only, clean exit

    rc = main(["audit", "--root", str(tmp_path), "--strict"])
    capsys.readouterr()
    assert rc == 1


def test_audit_command_passes_after_a_reindex(tmp_path, capsys):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "2026-01-01-a.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n"
        "created: 2026-01-01\niteration: 0\n---\n\n# a\n\n## Lesson learned\nfine\n"
    )
    assert main(["reindex", "--root", str(tmp_path)]) == 0
    capsys.readouterr()

    rc = main(["audit", "--root", str(tmp_path), "--strict", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True


def test_reindex_check_flags_drift_without_writing_any_moc(tmp_path, capsys):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "2026-01-01-a.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n"
        "created: 2026-01-01\niteration: 0\n---\n\n# a\n\n## Lesson learned\nfine\n"
    )

    rc = main(["reindex", "--root", str(tmp_path), "--check"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "stale" in out
    assert not (tmp_path / "knowledge" / "MOCs" / "Lessons MOC.md").exists()

    assert main(["reindex", "--root", str(tmp_path)]) == 0
    capsys.readouterr()

    rc = main(["reindex", "--root", str(tmp_path), "--check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no drift" in out
