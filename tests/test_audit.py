"""The pre-PR acceptance audit: deterministic hygiene, fail-open semantics.

Every credential-shaped fixture in this file is ASSEMBLED at runtime rather
than written as a literal. A literal would put a real deny-list match into this
repo's own source, and the gate under test would then (correctly) refuse the
next change that touched this file.
"""
import json
from dataclasses import replace

from hsai import audit, ledger
from hsai.config import load_config
from hsai.proc import Proc

# A real ticket body, in the shape hsai.tickets.TicketSpec renders.
TICKET_BODY = """## Problem
The widget is missing.

## Proposal
Build it.

## Prior art
- [[2026-01-01-a-note]] (fail) - widgets are hard

## Acceptance criteria
- [ ] the widget builds
- [x] the widget is tested

## Verification plan
- [ ] pytest green

## Meta
- goals: G4
- size: M
"""

CLEAN_DIFF = """diff --git a/src/hsai/widget.py b/src/hsai/widget.py
new file mode 100644
--- /dev/null
+++ b/src/hsai/widget.py
@@ -0,0 +1,2 @@
+def widget() -> str:
+    return "widget"
"""
CLEAN_NUMSTAT = "2\t0\tsrc/hsai/widget.py\n9\t0\ttests/test_widget.py\n"

PYPROJECT_DEP_DIFF = """diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -13,6 +13,7 @@ license = { text = "Apache-2.0" }
 dependencies = [
   "PyYAML>=6.0",
+  "requests>=2.31",
 ]
"""

# The same file, but the added line is a keyword rather than a requirement.
PYPROJECT_KEYWORD_DIFF = """diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -10,4 +10,5 @@ authors = [{ name = "hyperscaleailabs" }]
 keywords = [
   "ai",
+  "audit",
 ]
"""

# An added requirement too far from its array header to keep it in context: the
# version specifier is what still gives it away.
PYPROJECT_PINNED_FAR_DIFF = """diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -20,3 +20,4 @@ dev = [
   "pytest>=8.0",
+  "httpx>=0.27",
 ]
"""

PERMISSION_RAISE_DIFF = """diff --git a/.ai-swarm/core.yaml b/.ai-swarm/core.yaml
--- a/.ai-swarm/core.yaml
+++ b/.ai-swarm/core.yaml
@@ -89,1 +89,1 @@ execution:
-  permission_mode: acceptEdits
+  permission_mode: bypassPermissions
"""

PERMISSION_LOWER_DIFF = """diff --git a/.ai-swarm/core.yaml b/.ai-swarm/core.yaml
--- a/.ai-swarm/core.yaml
+++ b/.ai-swarm/core.yaml
@@ -89,1 +89,1 @@ execution:
-  permission_mode: acceptEdits
+  permission_mode: default
"""

CONSTRAINTS_DIFF = """diff --git a/.ai-swarm/core.yaml b/.ai-swarm/core.yaml
--- a/.ai-swarm/core.yaml
+++ b/.ai-swarm/core.yaml
@@ -195,4 +195,4 @@ publish:
-  subscription_only: true
+  subscription_only: false
"""

CLEAN_VERDICT = """I read the diff against the ticket.

```json
{"criteria": [
  {"criterion": "the widget builds", "status": "satisfied",
   "evidence": "src/hsai/widget.py:widget"},
  {"criterion": "the widget is tested", "status": "unmet",
   "evidence": "no test in this diff names widget()"}
], "verdict": "fail", "rationale": "One criterion has no test proving it."}
```
"""


def _envelope(text: str, *, tokens: tuple[int, int] = (300, 40)) -> str:
    """A `claude -p --output-format json` envelope wrapping ``text``."""
    return json.dumps(
        {
            "type": "result",
            "result": text,
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
        }
    )


# --- criterion extraction ------------------------------------------------------

def test_criteria_are_extracted_from_a_real_ticket_body():
    assert audit.acceptance_criteria(TICKET_BODY) == [
        "the widget builds",
        "the widget is tested",
    ]
    # The verification plan's checkboxes belong to a different section...
    assert "pytest green" not in audit.acceptance_criteria(TICKET_BODY)
    # ...and a body with no acceptance section yields nothing, never a crash.
    assert audit.acceptance_criteria("just a sentence") == []


# --- Layer A: the deterministic checks -----------------------------------------

def test_parse_numstat_reads_line_churn_and_tolerates_binary_files():
    stats = audit.parse_numstat("2\t0\tsrc/a.py\n-\t-\tdocs/logo.png\nnot a row\n")
    assert [(s.added, s.deleted, s.path) for s in stats] == [
        (2, 0, "src/a.py"),
        (0, 0, "docs/logo.png"),
    ]
    assert stats[0].changed == 2
    assert audit.parse_numstat("") == []


def test_added_lines_and_file_hunks_split_a_unified_diff():
    assert audit.added_lines(CLEAN_DIFF) == ["def widget() -> str:", '    return "widget"']
    hunks = audit.file_hunks(CLEAN_DIFF)
    assert list(hunks) == ["src/hsai/widget.py"]
    assert "+def widget() -> str:" in hunks["src/hsai/widget.py"]


def test_title_prefix_check_fires_only_on_a_non_conventional_title():
    assert audit.check_title("feat: add widget") == ""
    assert audit.check_title("fix(core): stop the leak") == ""
    assert audit.check_title("chore: refresh the snapshot") == ""
    assert "conventional-commit prefix" in audit.check_title("add widget")


def test_diff_size_is_banded_against_the_tickets_size_label():
    bands = {"S": 300, "M": 800, "L": 2000}
    small = [audit.FileStat(added=10, deleted=5, path="src/a.py")]
    big = [audit.FileStat(added=900, deleted=200, path="src/a.py")]

    assert audit.diff_size_band("S", small, bands) == ""
    assert audit.diff_size_band("L", big, bands) == ""
    assert "over the size:S ceiling of 300" in audit.diff_size_band("S", big, bands)
    assert "over the size:M ceiling of 800" in audit.diff_size_band("M", big, bands)
    # An unknown band is graded as M - an unlabeled ticket is ordinary, never
    # unbounded.
    assert "size:XL ceiling of 800" in audit.diff_size_band("XL", big, bands)
    assert audit.diff_size_band("M", []) == ""


def test_added_dependencies_are_flagged_and_a_keyword_is_not():
    assert "`requests`" in audit.check_new_dependencies(PYPROJECT_DEP_DIFF)
    assert "`httpx`" in audit.check_new_dependencies(PYPROJECT_PINNED_FAR_DIFF)
    assert audit.check_new_dependencies(PYPROJECT_KEYWORD_DIFF) == ""
    assert audit.check_new_dependencies(CLEAN_DIFF) == ""


def test_config_escalation_fires_on_a_raised_permission_mode_but_not_a_lowered_one():
    raised = audit.check_config_escalation(PERMISSION_RAISE_DIFF)
    assert "permission_mode" in raised
    assert audit.check_config_escalation(PERMISSION_LOWER_DIFF) == ""
    assert audit.check_config_escalation(CLEAN_DIFF) == ""


def test_config_escalation_fires_on_any_edit_to_the_constraints_block():
    detail = audit.check_config_escalation(CONSTRAINTS_DIFF)
    assert "`constraints:`" in detail
    assert "permission_mode" not in detail


def test_the_deny_list_reports_the_pattern_and_never_the_secret():
    secret = "sk-ant-" + "abcdef0123456789"
    diff = f'diff --git a/src/x.py b/src/x.py\n+++ b/src/x.py\n+KEY = "{secret}"\n'

    hits = audit.scan_deny_list(diff)

    assert len(hits) == 1
    assert "sk-ant-" in hits[0]          # the PATTERN is named...
    assert secret not in hits[0]         # ...the matched value never is
    assert "value withheld" in hits[0]


def test_the_deny_list_catches_a_generic_long_credential_assignment():
    line = "password = " + '"' + "A" * 20 + '"'
    assert audit.scan_deny_list(f"diff --git a/src/x.py b/src/x.py\n+{line}\n")


def test_the_deny_list_stays_quiet_on_an_ordinary_diff_and_survives_a_bad_pattern():
    assert audit.scan_deny_list(CLEAN_DIFF) == []
    broken = audit.scan_deny_list(CLEAN_DIFF, patterns=("[unclosed",))
    assert broken and "not a valid regex" in broken[0]


def test_hygiene_findings_resolve_severity_from_config():
    stats = [audit.FileStat(added=5000, deleted=0, path="src/a.py")]

    findings = audit.hygiene_findings(
        ticket_title="add widget", size="M", stats=stats, diff=CLEAN_DIFF
    )
    by_check = {f.check: f for f in findings}

    # A title the loop did not author is advisory; an oversized diff is not.
    assert by_check[audit.TITLE_PREFIX].blocking is False
    assert by_check[audit.DIFF_SIZE].blocking is True
    assert "**BLOCK**" in by_check[audit.DIFF_SIZE].render()

    # ...and the architect can retune either one without a code change.
    relaxed = audit.hygiene_findings(
        ticket_title="add widget", size="M", stats=stats, diff=CLEAN_DIFF,
        settings={"severity": {audit.DIFF_SIZE: audit.WARN}, "size_bands": {"M": 10}},
    )
    assert relaxed and all(not f.blocking for f in relaxed)


def test_a_clean_change_produces_no_hygiene_findings_at_all():
    assert audit.hygiene_findings(
        ticket_title="feat: add widget",
        size="M",
        stats=audit.parse_numstat(CLEAN_NUMSTAT),
        diff=CLEAN_DIFF,
    ) == []


# --- Layer B: parsing, and the fail-open contract ------------------------------

def test_parse_audit_json_reads_per_criterion_status_and_evidence():
    verdict = audit.parse_audit_json(CLEAN_VERDICT)

    assert verdict.error == "" and verdict.usable is True
    assert [c.status for c in verdict.criteria] == [audit.SATISFIED, audit.UNMET]
    assert verdict.criteria[0].evidence == "src/hsai/widget.py:widget"
    assert verdict.criteria[0].met is True
    assert verdict.overall == "fail"
    assert [c.criterion for c in verdict.gaps()] == ["the widget is tested"]
    assert verdict.gap_list() == "unmet: the widget is tested"

    rendered = verdict.render()
    assert "| the widget builds | `satisfied` | src/hsai/widget.py:widget |" in rendered
    assert "One criterion has no test proving it." in rendered


def test_parse_audit_json_is_fail_open_on_garbage_and_on_silence():
    """The mirror image of the reviewer's fail-closed contract: this auditor
    only annotates, so anything unreadable must block nothing."""
    for output in (
        "looks good to me",
        "",
        "   ",
        "```json\n{not json at all}\n```",
        "```json\n[1, 2]\n```",
        '{"criteria": [], "verdict": "pass"}',
    ):
        verdict = audit.parse_audit_json(output)
        assert verdict.error, output
        assert verdict.usable is False
        assert verdict.gaps() == []
        assert "not treated as a failure" in verdict.render()


def test_an_unrecognised_status_reads_as_unmet_rather_than_as_a_pass():
    verdict = audit.parse_audit_json(
        '{"criteria": [{"criterion": "c1", "status": "probably fine"}], "verdict": "pass"}'
    )
    assert verdict.criteria[0].status == audit.UNMET
    assert verdict.gap_list() == "unmet: c1"


# --- run_audit: the gate end to end --------------------------------------------

class _AuditRunner:
    """Answers git/claude for the gate; records every command it was asked."""

    def __init__(
        self,
        *,
        diff: str = CLEAN_DIFF,
        numstat: str = CLEAN_NUMSTAT,
        output: str | None = None,
        ok: bool = True,
    ) -> None:
        self.diff = diff
        self.numstat = numstat
        self.output = _envelope(CLEAN_VERDICT) if output is None else output
        self.ok = ok
        self.calls: list[list[str]] = []

    def __call__(
        self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
    ) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:2] == ["git", "add"]:
            return Proc(cmd, 0, "", "")
        if cmd[:2] == ["git", "diff"]:
            return Proc(cmd, 0, self.numstat if "--numstat" in cmd else self.diff, "")
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0 if self.ok else 1, self.output, "" if self.ok else "boom")
        raise AssertionError(f"unexpected command {cmd!r}")

    @property
    def claude_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:1] == ["claude"]]


def _audit(cfg, root, runner, *, title: str = "feat: add widget", labels=("size:M",)):
    return audit.run_audit(
        cfg,
        repo_root=str(root), wt=str(root),
        ticket_title=title, ticket_body=TICKET_BODY, labels=labels,
        iteration=3, block=0, ticket=7,
        runner=runner, ai_runner=runner,
    )


def test_a_clean_change_passes_and_the_audit_is_metered(tmp_path):
    cfg = load_config()
    runner = _AuditRunner()

    report = _audit(cfg, tmp_path, runner)

    assert report.ok is True and report.status == "pass"
    assert report.findings == []
    assert report.files_changed == 2 and report.changed_lines == 11
    assert report.auditor_tier == "light" and report.auditor_model
    # New files are made diffable before the tree is read, or a feature's diff
    # (mostly new files) would look empty.
    assert ["git", "add", "-N", "--", "."] in runner.calls

    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert [(r.kind, r.outcome) for r in records] == [("audit", "pass")]
    assert records[0].ticket == 7 and records[0].iteration == 3
    assert records[0].input_tokens == 300 and records[0].output_tokens == 40
    assert records[0].model == report.auditor_model


def test_the_auditor_prompt_is_built_from_the_ticket_and_the_diff():
    prompt = audit.build_prompt(
        ticket_title="feat: add widget",
        ticket_body=TICKET_BODY,
        criteria=audit.acceptance_criteria(TICKET_BODY),
        paths=["src/hsai/widget.py"],
        diff=CLEAN_DIFF,
    )
    assert audit.PROMPT_MARKER in prompt
    assert "feat: add widget" in prompt
    assert "1. the widget builds" in prompt
    assert "2. the widget is tested" in prompt
    assert "src/hsai/widget.py" in prompt
    assert "+def widget() -> str:" in prompt
    # The auditor is told to judge only what the diff contains.
    assert "do not credit intent" in prompt


def test_a_blocking_hygiene_finding_hard_fails_the_audit(tmp_path):
    cfg = load_config()
    runner = _AuditRunner(
        diff=PERMISSION_RAISE_DIFF, numstat="1\t1\t.ai-swarm/core.yaml\n"
    )

    report = _audit(cfg, tmp_path, runner)

    assert report.ok is False and report.status == "fail"
    assert [f.check for f in report.blocking] == [audit.CONFIG_ESCALATION]
    assert "permission_mode" in report.failure_detail()
    # The next attempt gets the auditor's concrete gap list alongside it.
    assert "unmet criteria - unmet: the widget is tested" in report.failure_detail()
    assert "**FAILED**" in report.render()
    assert [r.outcome for r in ledger.read_records(ledger.ledger_path(cfg, tmp_path))] == [
        "fail"
    ]


def test_an_unparseable_auditor_reply_annotates_but_never_blocks(tmp_path):
    cfg = load_config()
    runner = _AuditRunner(output=_envelope("Looks fine to me, shipping it."))

    report = _audit(cfg, tmp_path, runner)

    assert report.ok is True and report.status == "pass"
    assert report.verdict.error == audit.UNPARSEABLE
    assert "not treated as a failure" in report.render()


def test_a_crashed_or_timed_out_auditor_annotates_but_never_blocks(tmp_path):
    cfg = load_config()
    runner = _AuditRunner(output="", ok=False)

    report = _audit(cfg, tmp_path, runner)

    assert report.ok is True
    assert "auditor run failed" in report.verdict.error
    assert "boom" in report.verdict.error


def test_layer_a_still_hard_fails_when_layer_b_is_unavailable(tmp_path):
    """Only the deterministic layer decides; it does not need the other one."""
    cfg = load_config()
    runner = _AuditRunner(
        diff=PERMISSION_RAISE_DIFF, numstat="1\t1\t.ai-swarm/core.yaml\n",
        output="", ok=False,
    )

    report = _audit(cfg, tmp_path, runner)

    assert report.ok is False
    assert report.verdict.error and report.verdict.criteria == []


def test_a_hard_budget_breach_skips_layer_b_without_spending_anything(tmp_path):
    """A budget-exhausted block must still be able to finish its in-flight PR."""
    cfg = load_config()
    path = ledger.ledger_path(cfg, tmp_path)
    for i in range(cfg.budget["max_heavy_iterations_per_block"]):
        ledger.append_record(path, ledger.LedgerRecord(
            iteration=i, block=0, ticket=1, kind="implement", tier="heavy",
            model="opus", wall_clock_seconds=1.0, attempts=1, outcome="merged",
        ))
    runner = _AuditRunner()

    report = _audit(cfg, tmp_path, runner)

    assert report.ok is True
    assert "hard budget breach" in report.verdict.error
    assert runner.claude_calls == []                      # it spent nothing to say so
    assert [r.kind for r in ledger.read_records(path)] == ["implement"] * 3
    # ...and Layer A still ran: the deterministic half costs no quota.
    assert report.files_changed == 2


def test_the_size_band_is_measured_against_the_tickets_own_label(tmp_path):
    cfg = load_config()
    numstat = "900\t200\tsrc/hsai/widget.py\n"

    over = _audit(cfg, tmp_path, _AuditRunner(numstat=numstat), labels=("size:M",))
    assert over.ok is False
    assert [f.check for f in over.blocking] == [audit.DIFF_SIZE]

    within = _audit(cfg, tmp_path, _AuditRunner(numstat=numstat), labels=("size:L",))
    assert within.ok is True


def test_disabling_the_gate_skips_it_entirely(tmp_path):
    cfg = replace(load_config(), audit={"enabled": False})
    runner = _AuditRunner()

    report = _audit(cfg, tmp_path, runner)

    assert report.skipped is True and report.ok is True and report.status == "skipped"
    assert runner.calls == []
    assert ledger.read_records(ledger.ledger_path(cfg, tmp_path)) == []
    assert report.render() == "_(not run: acceptance audit disabled in cfg.audit)_"
    assert report.summary().startswith("skipped (")


# --- configuration -------------------------------------------------------------

def test_the_gate_is_configured_and_enabled_in_core_yaml():
    cfg = load_config()
    assert audit.is_enabled(cfg) is True
    assert cfg.audit["tier"] in cfg.tiers
    assert int(cfg.audit["timeout_seconds"]) > 0
    bands = cfg.audit["size_bands"]
    assert bands["S"] < bands["M"] < bands["L"]
    assert cfg.audit["deny_list"]
    assert set(cfg.audit["severity"]) == set(audit.CHECKS)


def test_audit_defaults_to_enabled_when_the_block_is_absent(tmp_path):
    core = tmp_path / ".ai-swarm"
    core.mkdir()
    (core / "core.yaml").write_text(
        "identity:\n  owner: someone\n"
        "models:\n  tiers:\n    standard:\n      model: sonnet\n"
        "  default_tier: standard\n"
    )
    cfg = load_config(core / "core.yaml")

    assert cfg.audit == {}
    assert audit.is_enabled(cfg) is True       # additive by default, opt-out only
    # ...and an unconfigured tier falls back to one this repo actually has.
    assert audit.select_auditor(cfg).tier == "standard"
