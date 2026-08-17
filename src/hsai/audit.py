"""End-to-end traceability and vault-integrity audit (``hsai audit``).

The repo's central claim is three traceability invariants (ticket-linked PRs,
model recorded, a lesson per PR) plus an Obsidian-ready knowledge base. CI
only greps the *open* PR's body, so nothing checks any of this after a merge:
a dangling ``[[wikilink]]``, an orphaned note, a stale MOC, a lesson that
names a ticket/PR that no longer resolves, or a PR body whose recorded tier
disagrees with the ledger, can all drift silently. This module is that check.

Six independent, individually testable checks:

  a. ``wikilinks``            - every ``[[target]]`` under knowledge/ resolves
  b. ``orphans``               - every lesson/whitepaper/article is reachable
                                  from the Knowledge Base MOC
  c. ``moc_freshness``         - regenerating MOCs in-memory yields no diff
                                  against what is committed (see
                                  :meth:`hsai.knowledge.KnowledgeBase.moc_drift`)
  d. ``frontmatter``           - every note's YAML frontmatter is parseable
                                  and carries the fields its kind requires
  e. ``lesson_ticket_pr``      - each lesson names a ticket + PR that exist
                                  and are closed/merged (GitHub-dependent)
  f. ``merged_prs_have_lessons``/``model_consistency`` - each merged PR since
                                  ``--since`` has a lesson naming it, and its
                                  recorded tier matches the ledger
                                  (GitHub-dependent)

Checks a-d are vault-local: no network, no ``gh`` call, safe on every PR.
Checks e/f only run when ``--since REF`` is given (they shell out to `gh`,
so they need network + a token) - that is what the daily
``audit-periodic.yml`` workflow supplies; the per-PR ``ci.yml`` job never
passes ``--since``.

A `.ai-swarm/known_exceptions.yaml` list lets pre-invariant history (recorded
before a given check existed) pass without either lying about the past or
blocking the gate forever - see :func:`load_known_exceptions`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import github, ledger
from .config import CoreConfig
from .knowledge import KnowledgeBase
from .proc import Runner, run

KNOWN_EXCEPTIONS_PATH = ".ai-swarm/known_exceptions.yaml"

DRIFT_LABEL = "audit-drift"

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TICKET_ROW_RE = re.compile(r"\|\s*ticket\s*\|\s*#(\d+)\s*\|", re.IGNORECASE)
_PR_ROW_RE = re.compile(r"\|\s*pull request\s*\|\s*#(\d+)\s*\|", re.IGNORECASE)
_TIER_RE = re.compile(r"tier:\s*`([a-zA-Z0-9_-]+)`")
_CLOSES_RE = re.compile(r"closes\s+#(\d+)", re.IGNORECASE)

_ROOT_MOC = "Knowledge Base MOC"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- known exceptions ----------------------------------------------------------


@dataclass(frozen=True)
class KnownException:
    """One documented, pre-invariant exemption from a named check.

    Matching is coarse (check + target): once a note/PR is excepted for a
    check, every finding that check would raise about that target is
    suppressed. That is deliberate - the point is "this predates the
    invariant", not "this predates one specific symptom of it".
    """

    check: str
    target: str
    reason: str = ""


def load_known_exceptions(path: str | Path) -> list[KnownException]:
    """Load ``.ai-swarm/known_exceptions.yaml`` (empty list if absent/malformed)."""
    path = Path(path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        return []
    out: list[KnownException] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        check = str(item.get("check", "")).strip()
        target = str(item.get("target", "")).strip()
        if check and target:
            out.append(KnownException(check=check, target=target, reason=str(item.get("reason", ""))))
    return out


def _is_excepted(exceptions: list[KnownException], check: str, target: str) -> bool:
    return any(e.check == check and e.target == target for e in exceptions)


# --- report shape ----------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    check: str
    target: str
    detail: str


@dataclass
class CheckResult:
    name: str
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class AuditReport:
    checks: list[CheckResult] = field(default_factory=list)
    since: str = ""
    generated_at: str = field(default_factory=_now_iso)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "since": self.since or None,
            "ok": self.ok,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "skipped": c.skipped,
                    "skip_reason": c.skip_reason,
                    "findings": [
                        {"target": f.target, "detail": f.detail} for f in c.findings
                    ],
                }
                for c in self.checks
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def render(self) -> str:
        lines = [f"hsai audit - {'PASS' if self.ok else 'FAIL'} ({self.generated_at})"]
        for c in self.checks:
            if c.skipped:
                lines.append(f"  - {c.name}: SKIPPED ({c.skip_reason})")
                continue
            status = "pass" if c.ok else f"FAIL ({len(c.findings)} finding(s))"
            lines.append(f"  - {c.name}: {status}")
            for f in c.findings:
                lines.append(f"      {f.target}: {f.detail}")
        return "\n".join(lines)

    def oneline(self) -> str:
        failed = [c.name for c in self.checks if not c.ok]
        if not failed:
            return f"PASS ({len(self.checks)} check(s))"
        return f"FAIL - {', '.join(failed)}"


# --- vault scanning helpers ----------------------------------------------------


def _iter_vault_notes(root: Path) -> list[Path]:
    """Every real note under ``knowledge/`` and ``docs/adr`` (templates excluded).

    Templates use ``{{placeholder}}`` frontmatter and example wikilinks - they
    are not notes, and would only ever show up as false positives.
    """
    notes: list[Path] = []
    knowledge_dir = root / "knowledge"
    if knowledge_dir.is_dir():
        for path in sorted(knowledge_dir.rglob("*.md")):
            if "templates" in path.relative_to(root).parts:
                continue
            notes.append(path)
    adr_dir = root / "docs" / "adr"
    if adr_dir.is_dir():
        for path in sorted(adr_dir.glob("*.md")):
            if path.stem == "TEMPLATE":
                continue
            notes.append(path)
    return notes


# --- check (a): wikilink resolution --------------------------------------------


def check_wikilinks(root: Path, exceptions: list[KnownException]) -> CheckResult:
    notes = _iter_vault_notes(root)
    valid = {p.stem for p in notes}
    findings: list[Finding] = []
    for path in notes:
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        for m in _WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            if target in valid:
                continue
            key = f"{rel}:{target}"
            if _is_excepted(exceptions, "wikilinks", key):
                continue
            findings.append(Finding("wikilinks", rel, f"dangling link [[{target}]]"))
    return CheckResult(name="wikilinks", ok=not findings, findings=findings)


# --- check (b): orphan detection -----------------------------------------------


def check_orphans(
    root: Path, kb: KnowledgeBase, exceptions: list[KnownException]
) -> CheckResult:
    universe = {*kb.lesson_notes(), *kb.whitepaper_notes(), *kb.article_notes()}
    if not universe:
        return CheckResult(name="orphans", ok=True, findings=[])

    note_text: dict[str, str] = {}
    for directory in (kb.lessons_dir, kb.whitepapers_dir, kb.articles_dir, kb.mocs_dir):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.md"):
            note_text[path.stem] = path.read_text(encoding="utf-8")

    seen: set[str] = set()
    stack = [_ROOT_MOC]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for m in _WIKILINK_RE.finditer(note_text.get(name, "")):
            target = m.group(1).strip()
            if target not in seen:
                stack.append(target)

    findings: list[Finding] = []
    for name in sorted(universe):
        if name in seen:
            continue
        if _is_excepted(exceptions, "orphans", name):
            continue
        findings.append(
            Finding("orphans", name, f"not reachable from [[{_ROOT_MOC}]]")
        )
    return CheckResult(name="orphans", ok=not findings, findings=findings)


# --- check (c): MOC freshness --------------------------------------------------


def check_moc_freshness(kb: KnowledgeBase, exceptions: list[KnownException]) -> CheckResult:
    drift = kb.moc_drift()
    findings = [
        Finding("moc_freshness", path, f"{reason}; run `hsai reindex`")
        for path, reason in sorted(drift.items())
        if not _is_excepted(exceptions, "moc_freshness", path)
    ]
    return CheckResult(name="moc_freshness", ok=not findings, findings=findings)


# --- check (d): frontmatter/schema validity ------------------------------------

# (directory, required tag, extra fields required when that tag is present)
_SCHEMA = (
    ("lessons_dir", "lesson", ("created", "iteration")),
    ("whitepapers_dir", "whitepaper", ("created",)),
    ("mocs_dir", "moc", ("updated",)),
    ("articles_dir", "article", ()),
)


def check_frontmatter(
    root: Path, kb: KnowledgeBase, exceptions: list[KnownException]
) -> CheckResult:
    findings: list[Finding] = []
    for attr, expected_tag, required_fields in _SCHEMA:
        directory: Path = getattr(kb, attr)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            rel = str(path.relative_to(root))
            if _is_excepted(exceptions, "frontmatter", rel):
                continue
            text = path.read_text(encoding="utf-8")
            m = _FRONTMATTER_RE.match(text)
            if not m:
                findings.append(Finding("frontmatter", rel, "missing YAML frontmatter block"))
                continue
            try:
                data = yaml.safe_load(m.group(1))
            except yaml.YAMLError as exc:
                findings.append(Finding("frontmatter", rel, f"invalid YAML frontmatter: {exc}"))
                continue
            if not isinstance(data, dict):
                findings.append(Finding("frontmatter", rel, "frontmatter is not a mapping"))
                continue
            tags = [str(t) for t in (data.get("tags") or [])]
            if expected_tag not in tags:
                findings.append(Finding("frontmatter", rel, f"missing '{expected_tag}' tag"))
                continue
            for missing in required_fields:
                if missing not in data:
                    findings.append(Finding("frontmatter", rel, f"missing '{missing}' field"))
            if expected_tag == "lesson":
                if not any(t.startswith("outcome/") for t in tags):
                    findings.append(Finding("frontmatter", rel, "missing 'outcome/*' tag"))
                if not any(t.startswith("kind/") for t in tags):
                    findings.append(Finding("frontmatter", rel, "missing 'kind/*' tag"))
            if expected_tag == "article" and not any(t.startswith("persona/") for t in tags):
                findings.append(Finding("frontmatter", rel, "missing 'persona/*' tag"))
    return CheckResult(name="frontmatter", ok=not findings, findings=findings)


# --- git/github helpers (only used by the --since-gated checks) ---------------


def _ref_timestamp(root: Path, ref: str, *, runner: Runner) -> str:
    """The committer-date (ISO-8601) of ``ref``, or ``""`` if it does not resolve."""
    p = runner(["git", "log", "-1", "--format=%cI", ref], cwd=str(root))
    return p.stdout.strip() if p.ok else ""


# --- check (e): lesson<->ticket<->PR closure -----------------------------------


def check_lesson_ticket_pr(
    root: Path,
    cfg: CoreConfig,
    kb: KnowledgeBase,
    exceptions: list[KnownException],
    *,
    runner: Runner,
) -> CheckResult:
    findings: list[Finding] = []
    repo = cfg.repo_slug
    for name in kb.lesson_notes():
        if _is_excepted(exceptions, "lesson_ticket_pr", name):
            continue
        text = (kb.lessons_dir / f"{name}.md").read_text(encoding="utf-8")
        tm = _TICKET_ROW_RE.search(text)
        if not tm:
            findings.append(Finding("lesson_ticket_pr", name, "lesson names no ticket"))
        else:
            ticket_num = int(tm.group(1))
            state = github.issue_state(repo, ticket_num, runner=runner)
            if state != "CLOSED":
                findings.append(
                    Finding(
                        "lesson_ticket_pr", name,
                        f"ticket #{ticket_num} is not closed (state={state or 'missing'})",
                    )
                )
        pm = _PR_ROW_RE.search(text)
        if not pm:
            findings.append(Finding("lesson_ticket_pr", name, "lesson names no pull request"))
            continue
        pr_num = int(pm.group(1))
        pr = github.get_pr(repo, pr_num, runner=runner)
        if pr is None or not pr.merged:
            findings.append(
                Finding("lesson_ticket_pr", name, f"PR #{pr_num} is not merged")
            )
    return CheckResult(name="lesson_ticket_pr", ok=not findings, findings=findings)


# --- check (f/1): every merged PR since --since has a lesson -------------------


def check_merged_prs_have_lessons(
    root: Path,
    cfg: CoreConfig,
    kb: KnowledgeBase,
    since: str,
    exceptions: list[KnownException],
    *,
    runner: Runner,
) -> CheckResult:
    since_iso = _ref_timestamp(root, since, runner=runner)
    if not since_iso:
        return CheckResult(
            name="merged_prs_have_lessons", ok=True, findings=[],
            skipped=True, skip_reason=f"could not resolve --since ref {since!r}",
        )
    known_prs: set[int] = set()
    for name in kb.lesson_notes():
        text = (kb.lessons_dir / f"{name}.md").read_text(encoding="utf-8")
        pm = _PR_ROW_RE.search(text)
        if pm:
            known_prs.add(int(pm.group(1)))

    findings: list[Finding] = []
    for pr in github.list_merged_prs_since(cfg.repo_slug, since_iso, runner=runner):
        if pr.number in known_prs:
            continue
        if _is_excepted(exceptions, "merged_prs_have_lessons", str(pr.number)):
            continue
        findings.append(
            Finding(
                "merged_prs_have_lessons", str(pr.number),
                f"merged PR #{pr.number} ({pr.title}) has no lesson naming it",
            )
        )
    return CheckResult(name="merged_prs_have_lessons", ok=not findings, findings=findings)


# --- check (f/2): model-record consistency -------------------------------------


def check_model_consistency(
    root: Path,
    cfg: CoreConfig,
    since: str,
    exceptions: list[KnownException],
    *,
    runner: Runner,
) -> CheckResult:
    since_iso = _ref_timestamp(root, since, runner=runner)
    if not since_iso:
        return CheckResult(
            name="model_consistency", ok=True, findings=[],
            skipped=True, skip_reason=f"could not resolve --since ref {since!r}",
        )
    records = ledger.read_records(ledger.ledger_path(cfg, root))
    by_ticket: dict[int, list[ledger.LedgerRecord]] = {}
    for r in records:
        if r.ticket is not None:
            by_ticket.setdefault(r.ticket, []).append(r)

    findings: list[Finding] = []
    for pr in github.list_merged_prs_since(cfg.repo_slug, since_iso, runner=runner):
        if _is_excepted(exceptions, "model_consistency", str(pr.number)):
            continue
        cm = _CLOSES_RE.search(pr.body)
        tm = _TIER_RE.search(pr.body)
        if not cm or not tm:
            continue  # not a ticket-implementing PR (e.g. a governance-artifact PR)
        ticket_num = int(cm.group(1))
        pr_tier = tm.group(1)
        candidates = by_ticket.get(ticket_num, [])
        merged_records = [r for r in candidates if r.outcome == "merged"] or candidates
        if not merged_records:
            findings.append(
                Finding(
                    "model_consistency", str(pr.number),
                    f"no ledger record for ticket #{ticket_num}",
                )
            )
            continue
        ledger_tier = merged_records[-1].tier
        if ledger_tier != pr_tier:
            findings.append(
                Finding(
                    "model_consistency", str(pr.number),
                    f"PR tier `{pr_tier}` != ledger tier `{ledger_tier}` "
                    f"for ticket #{ticket_num}",
                )
            )
    return CheckResult(name="model_consistency", ok=not findings, findings=findings)


# --- entrance ------------------------------------------------------------------


def run_audit(
    cfg: CoreConfig,
    root: str | Path,
    *,
    since: str | None = None,
    runner: Runner = run,
) -> AuditReport:
    """Run the audit. Vault-local checks (a-d) always run; the GitHub-dependent
    closure/consistency checks (e/f) only run when ``since`` is given."""
    root = Path(root)
    kb = KnowledgeBase.from_config(cfg, root)
    exceptions = load_known_exceptions(root / KNOWN_EXCEPTIONS_PATH)

    checks = [
        check_wikilinks(root, exceptions),
        check_orphans(root, kb, exceptions),
        check_moc_freshness(kb, exceptions),
        check_frontmatter(root, kb, exceptions),
    ]
    if since:
        checks.append(check_lesson_ticket_pr(root, cfg, kb, exceptions, runner=runner))
        checks.append(
            check_merged_prs_have_lessons(root, cfg, kb, since, exceptions, runner=runner)
        )
        checks.append(check_model_consistency(root, cfg, since, exceptions, runner=runner))
    return AuditReport(checks=checks, since=since or "")


# --- drift ticket (scheduled workflow) ------------------------------------------


def _drift_ticket_body(report: AuditReport) -> str:
    lines = [
        "Automated by `hsai audit` (scheduled). This body is replaced verbatim",
        "on every run while drift persists - do not hand-edit it.",
        "",
        f"## Summary\n{report.oneline()} - generated {report.generated_at}"
        + (f" (since {report.since})" if report.since else ""),
        "",
    ]
    for c in report.checks:
        if c.ok:
            continue
        lines.append(f"## {c.name}")
        for f in c.findings:
            lines.append(f"- `{f.target}`: {f.detail}")
        lines.append("")
    lines.append(
        "This ticket is filed/updated idempotently - the next scheduled audit "
        "either updates this same ticket (drift persists) or leaves it for a "
        "human to close (drift cleared)."
    )
    return "\n".join(lines)


def file_or_update_drift_ticket(
    cfg: CoreConfig, report: AuditReport, *, runner: Runner = run
) -> int:
    """File the single open ``audit-drift`` ticket, or update it if one exists.

    Idempotent by construction: at most one open issue ever carries the
    ``audit-drift`` label, so a second (or hundredth) run against the same
    unresolved drift edits that issue instead of filing a duplicate.
    """
    repo = cfg.repo_slug
    open_issues = github.list_open_issues(repo, runner=runner)
    existing = next((i for i in open_issues if DRIFT_LABEL in i.labels), None)
    failed = [c.name for c in report.checks if not c.ok]
    title = f"chore: audit drift ({', '.join(failed) or 'unknown'})"
    body = _drift_ticket_body(report)
    if existing:
        github.update_issue_body(repo, existing.number, body, runner=runner)
        github.comment_issue(
            repo, existing.number,
            f"Drift persists as of {report.generated_at}: {report.oneline()}",
            runner=runner,
        )
        return existing.number
    return github.create_issue(
        repo, title, body, ["priority:P1", DRIFT_LABEL], runner=runner
    )
