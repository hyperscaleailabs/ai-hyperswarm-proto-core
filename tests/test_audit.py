"""hsai audit: independent, individually-testable vault + traceability checks.

Each check gets its own temporary-vault fixture (built through the real
``KnowledgeBase`` so notes are byte-identical to what the loop actually
writes), plus a handful of integration tests through :func:`audit.run_audit`
and the idempotent drift-ticket path.
"""
import json

from hsai import audit, ledger
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson, Whitepaper
from hsai.ledger import LedgerRecord
from hsai.proc import Proc

CFG = load_config()


def _kb(tmp_path) -> KnowledgeBase:
    return KnowledgeBase.from_config(CFG, tmp_path)


def _lesson(**overrides) -> Lesson:
    fields = dict(
        title="implement: add widget", outcome="pass", kind="implement",
        context="c", what_happened="w", lesson="l",
    )
    fields.update(overrides)
    return Lesson(**fields)


# --- (a) wikilink resolution --------------------------------------------------

def test_wikilinks_passes_on_a_clean_vault(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()

    result = audit.check_wikilinks(CFG, tmp_path)
    assert result.ok and result.findings == []


def test_wikilinks_fails_on_a_dangling_link(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()
    (kb.lessons_dir / "2026-01-01-bad-link.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n---\n\n"
        "# Bad link\n\nSee [[does-not-exist]] for details.\n"
    )

    result = audit.check_wikilinks(CFG, tmp_path)

    assert not result.ok
    assert any("does-not-exist" in f and "2026-01-01-bad-link" in f for f in result.findings)


# --- (b) orphan detection -------------------------------------------------------

def test_orphans_passes_when_everything_is_reachable_from_a_moc(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.write_whitepaper(Whitepaper(title="synthesis", summary="s", body="b"))
    kb.reindex_mocs()

    result = audit.check_orphans(CFG, tmp_path)
    assert result.ok and result.findings == []


def test_orphans_fails_on_a_note_no_moc_links_to(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()                                  # Lessons MOC lists only the above
    orphan = kb.write_lesson(_lesson(title="implement: orphaned widget", created="2026-01-02"))

    result = audit.check_orphans(CFG, tmp_path)

    assert not result.ok
    assert any(orphan.stem in f and "not reachable" in f for f in result.findings)


# --- (c) MOC freshness -----------------------------------------------------------

def test_moc_freshness_passes_right_after_reindex(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()

    result = audit.check_moc_freshness(CFG, tmp_path)
    assert result.ok and result.findings == []


def test_moc_freshness_fails_when_a_note_is_added_after_reindex(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()
    kb.write_lesson(_lesson(title="implement: a second widget", created="2026-01-02"))

    result = audit.check_moc_freshness(CFG, tmp_path)

    assert not result.ok
    assert any("Lessons MOC.md" in f for f in result.findings)


def test_moc_freshness_ignores_the_updated_timestamp_alone(tmp_path):
    """A MOC whose only difference from what would be regenerated is its
    `updated:` date is NOT drift - that line is expected to move every run."""
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()
    lessons_moc = kb.mocs_dir / "Lessons MOC.md"
    stamped = lessons_moc.read_text().replace("updated: ", "updated: 2000-01-01  # was ", 1)
    lessons_moc.write_text(stamped)

    result = audit.check_moc_freshness(CFG, tmp_path)
    assert result.ok, result.findings


# --- (f) frontmatter / schema validity --------------------------------------------

def test_frontmatter_passes_on_well_formed_notes(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.write_whitepaper(Whitepaper(title="synthesis", summary="s", body="b"))
    kb.reindex_mocs()

    result = audit.check_frontmatter(CFG, tmp_path)
    assert result.ok and result.findings == []


def test_frontmatter_fails_on_missing_frontmatter_block(tmp_path):
    kb = _kb(tmp_path)
    (kb.lessons_dir / "2026-01-01-no-frontmatter.md").write_text("# Just a title\n\nNo frontmatter.\n")

    result = audit.check_frontmatter(CFG, tmp_path)

    assert not result.ok
    assert any("no-frontmatter" in f and "missing frontmatter" in f for f in result.findings)


def test_frontmatter_fails_when_a_lesson_is_missing_its_kind_tags(tmp_path):
    kb = _kb(tmp_path)
    (kb.lessons_dir / "2026-01-01-untagged.md").write_text(
        "---\ntags:\n  - lesson\n---\n\n# Untagged\n\nBody.\n"
    )

    result = audit.check_frontmatter(CFG, tmp_path)

    assert not result.ok
    assert any("outcome/*" in f for f in result.findings)
    assert any("kind/*" in f for f in result.findings)


# --- (d) / (e) GitHub-dependent checks --------------------------------------------

def _closure_runner(cmd, **kwargs):
    cmd = list(cmd)
    if cmd[:3] == ["gh", "issue", "view"]:
        return Proc(cmd, 0, json.dumps({
            "number": 7, "title": "feat: x", "labels": [], "assignees": [], "body": "",
            "state": "CLOSED",
        }), "")
    if cmd[:3] == ["gh", "pr", "view"]:
        return Proc(cmd, 0, json.dumps({
            "number": 8, "title": "implement: x", "body": "Closes #7", "state": "MERGED",
            "merged": True,
        }), "")
    if cmd[:3] == ["gh", "pr", "list"]:
        return Proc(cmd, 0, json.dumps([{
            "number": 8, "title": "implement: x",
            "body": "Closes #7\n\n## Model used\n- **model**: `sonnet` (tier: `standard`)\n",
            "headRefName": "",
        }]), "")
    return Proc(cmd, 0, "", "")


def test_lesson_ticket_pr_closure_passes_when_everything_lines_up(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson(ticket=7, pr=8, model="sonnet"))

    result = audit.check_lesson_ticket_pr_closure(CFG, tmp_path, runner=_closure_runner)
    assert result.ok and result.findings == []


def test_lesson_ticket_pr_closure_flags_a_ticket_that_is_still_open(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson(ticket=7, pr=8, model="sonnet"))

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return Proc(cmd, 0, json.dumps({
                "number": 7, "title": "feat: x", "labels": [], "assignees": [], "body": "",
                "state": "OPEN",
            }), "")
        return _closure_runner(cmd, **kwargs)

    result = audit.check_lesson_ticket_pr_closure(CFG, tmp_path, runner=runner)

    assert not result.ok
    assert any("is not closed" in f for f in result.findings)


def test_lesson_ticket_pr_closure_flags_a_merged_pr_with_no_lesson(tmp_path):
    # No lessons at all - the reverse direction of the invariant.
    result = audit.check_lesson_ticket_pr_closure(CFG, tmp_path, runner=_closure_runner)

    assert not result.ok
    assert any("merged PR #8" in f and "no matching lesson" in f for f in result.findings)


def test_lesson_ticket_pr_closure_exempts_pre_invariant_history_via_since(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson(ticket=7, pr=8, model="sonnet"))

    def runner(cmd, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, "[]", "")   # no merged PRs at all above the cutoff
        return Proc(cmd, 0, "", "")         # would 404 the ticket/PR lookups if ever called

    result = audit.check_lesson_ticket_pr_closure(CFG, tmp_path, since=100, runner=runner)
    assert result.ok and result.findings == []


def test_model_record_consistency_passes_when_the_ledger_backs_the_claim(tmp_path):
    ledger.append_record(
        ledger.ledger_path(CFG, tmp_path),
        LedgerRecord(
            iteration=1, block=0, ticket=7, kind="implement", tier="standard",
            model="sonnet", wall_clock_seconds=1.0, attempts=1, outcome="merged",
        ),
    )

    result = audit.check_model_record_consistency(CFG, tmp_path, runner=_closure_runner)
    assert result.ok and result.findings == []


def test_model_record_consistency_flags_a_tier_mismatch(tmp_path):
    ledger.append_record(
        ledger.ledger_path(CFG, tmp_path),
        LedgerRecord(
            iteration=1, block=0, ticket=7, kind="implement", tier="light",
            model="haiku", wall_clock_seconds=1.0, attempts=1, outcome="merged",
        ),
    )

    result = audit.check_model_record_consistency(CFG, tmp_path, runner=_closure_runner)

    assert not result.ok
    assert any("claims tier `standard`" in f and "['light']" in f for f in result.findings)


# --- run_audit orchestration -------------------------------------------------------

def test_run_audit_offline_skips_github_checks(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson(ticket=1, pr=2, model="sonnet"))
    kb.reindex_mocs()

    report = audit.run_audit(CFG, tmp_path, offline=True)

    assert report.ok, report.human()
    by_name = {c.name: c for c in report.checks}
    assert by_name["lesson_ticket_pr_closure"].skipped
    assert by_name["model_record_consistency"].skipped
    assert {c.name for c in report.checks} == {
        "wikilinks", "orphans", "moc_freshness", "frontmatter",
        "lesson_ticket_pr_closure", "model_record_consistency",
    }


def test_run_audit_json_report_has_a_per_check_breakdown(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    kb.reindex_mocs()

    payload = json.loads(audit.run_audit(CFG, tmp_path, offline=True).to_json())

    assert payload["ok"] is True
    assert payload["offline"] is True
    assert len(payload["checks"]) == 6
    for check in payload["checks"]:
        assert set(check) == {"name", "ok", "findings", "skipped", "skip_reason"}


def test_known_exceptions_silence_a_documented_finding(tmp_path):
    kb = _kb(tmp_path)
    kb.write_lesson(_lesson())
    (kb.lessons_dir / "2026-01-01-bad-link.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n---\n\n"
        "# Bad link\n\nSee [[does-not-exist]] for details.\n"
    )
    kb.reindex_mocs()   # both lessons indexed and fresh; only the dangling link is a problem
    exceptions_path = tmp_path / ".ai-swarm" / "audit_known_exceptions.yaml"
    exceptions_path.parent.mkdir(parents=True, exist_ok=True)
    exceptions_path.write_text(
        "known_exceptions:\n"
        "  - check: wikilinks\n"
        "    match: 'does-not-exist'\n"
        "    reason: pre-invariant test fixture\n"
    )

    report = audit.run_audit(CFG, tmp_path, offline=True)

    assert report.ok, report.human()
    assert any("does-not-exist" in e for e in report.excepted)


# --- idempotent drift ticket -------------------------------------------------------

class _DriftGhRunner:
    """Simulates `gh` across two `file_or_update_drift_ticket` calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.number: int | None = None

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            if self.number is None:
                return Proc(cmd, 0, "[]", "")
            return Proc(cmd, 0, json.dumps([{
                "number": self.number, "title": audit.DRIFT_TITLE,
                "labels": [{"name": "audit-drift"}, {"name": "priority:P1"}],
                "assignees": [], "body": "",
            }]), "")
        if cmd[:3] == ["gh", "issue", "create"]:
            self.number = 555
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self.number}\n", "")
        if cmd[:3] == ["gh", "issue", "edit"]:
            return Proc(cmd, 0, "", "")
        return Proc(cmd, 0, "", "")

    def count(self, *prefix: str) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == list(prefix))


def _failing_report() -> audit.AuditReport:
    return audit.AuditReport(checks=[
        audit.CheckResult("wikilinks", ok=False, findings=["some/note.md: dangling [[x]]"]),
    ])


def test_file_or_update_drift_ticket_files_once_then_updates(tmp_path):
    gh = _DriftGhRunner()

    first = audit.file_or_update_drift_ticket(CFG, _failing_report(), runner=gh)
    assert first == 555
    assert gh.count("gh", "issue", "create") == 1
    assert gh.count("gh", "issue", "edit") == 0

    second = audit.file_or_update_drift_ticket(CFG, _failing_report(), runner=gh)
    assert second == 555                                 # same ticket, not a new one
    assert gh.count("gh", "issue", "create") == 1         # still exactly one ever filed
    assert gh.count("gh", "issue", "edit") == 1            # the second run updated it


def test_file_or_update_drift_ticket_does_nothing_when_the_audit_is_clean():
    gh = _DriftGhRunner()
    number = audit.file_or_update_drift_ticket(
        CFG, audit.AuditReport(checks=[audit.CheckResult("wikilinks", ok=True)]), runner=gh
    )
    assert number == 0
    assert gh.calls == []
