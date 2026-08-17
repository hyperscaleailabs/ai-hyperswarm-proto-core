"""End-to-end traceability + vault-integrity audit (``hsai audit``).

The repo's central claim is three traceability invariants (ticket-linked PRs,
model recorded, a lesson per PR) plus an Obsidian-ready knowledge base. CI only
ever greps the *open* PR's body, so nothing after a merge checks that the vault
stays internally consistent or that the invariants held. This module is that
check, split into small, independent, individually-testable functions:

- **wikilinks** - every ``[[target]]`` under ``knowledge/`` resolves to a note
  that actually exists (:func:`check_wikilinks`).
- **orphans** - every lesson/whitepaper/article is reachable by following links
  outward from a MOC (:func:`check_orphans`).
- **moc_freshness** - regenerating the MOCs in-memory (:meth:`hsai.knowledge.
  KnowledgeBase.check_freshness`) yields no diff against what is committed
  (:func:`check_moc_freshness`).
- **frontmatter** - every note's frontmatter parses as YAML and carries the
  tags its kind requires (:func:`check_frontmatter`).
- **lesson_ticket_pr_closure** - GitHub-dependent: every lesson names a ticket
  and PR that exist and are closed/merged, and every merged PR since a start
  boundary has a matching lesson (:func:`check_lesson_ticket_pr_closure`).
- **model_record_consistency** - GitHub-dependent: the tier a merged PR claims
  was actually used by some ledger run of its ticket (:func:`
  check_model_record_consistency`).

The first four need no network and run on every PR (``--offline``); the last
two need ``gh`` and run on the daily scheduled workflow. A documented
``known_exceptions`` list (see ``.ai-swarm/audit_known_exceptions.yaml``) lets
pre-invariant history be silenced explicitly instead of being ignored
silently, so the gate starts green and stays honest.

``--since`` (and ``audit.since_pr`` in core.yaml) is a PR/ticket **number**
cutoff, not a git ref: this repo's traceability invariants are keyed off issue
and PR numbers already (``Closes #N``), and a number is a simple, stable
boundary for "pre-invariant history" without needing to walk commit ancestry
to map commits back to PRs.

Synthesis: SWE-agent/SWE-agent's paired PR/periodic link-check workflows,
langchain-ai/langchain's derived-artifact consistency gates (``check_diffs``,
``check_versions``, ``check_agents_sync``), FoundationAgents/MetaGPT's
``stale.yaml`` (acts on the backlog, not just reports), and run-llama/
llama_index's docs/index integrity practice (orphan-reachability).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import github, ledger
from .config import CoreConfig
from .knowledge import KnowledgeBase
from .proc import Runner, run

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TICKET_ROW_RE = re.compile(r"\|\s*ticket\s*\|\s*#(\d+)\s*\|", re.IGNORECASE)
_PR_ROW_RE = re.compile(r"\|\s*pull request\s*\|\s*#(\d+)\s*\|", re.IGNORECASE)
_TIER_RE = re.compile(r"\(tier:\s*`([^`]+)`\)")
_CLOSES_RE = re.compile(r"closes\s+#(\d+)", re.IGNORECASE)

DEFAULT_KNOWN_EXCEPTIONS_FILE = ".ai-swarm/audit_known_exceptions.yaml"
DRIFT_TITLE = "audit: vault-integrity drift detected"


# --- vault scanning helpers ----------------------------------------------------

def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _vault_root(cfg: CoreConfig, repo_root: Path) -> Path:
    return repo_root / cfg.knowledge.get("root", "knowledge")


def _templates_dir(cfg: CoreConfig, repo_root: Path) -> Path:
    return repo_root / cfg.knowledge.get("templates_dir", "knowledge/templates")


def _iter_notes(cfg: CoreConfig, repo_root: Path) -> list[Path]:
    """Every ``*.md`` note in the vault, excluding templates (which contain
    deliberately dangling placeholder links and are never rendered content)."""
    vault_root = _vault_root(cfg, repo_root)
    templates_dir = _templates_dir(cfg, repo_root)
    if not vault_root.exists():
        return []
    return [p for p in sorted(vault_root.rglob("*.md")) if not _is_under(p, templates_dir)]


# --- report types ---------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    ok: bool
    findings: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class KnownException:
    check: str
    match: str
    reason: str = ""


def load_known_exceptions(path: str | Path) -> list[KnownException]:
    path = Path(path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    items = data.get("known_exceptions") or []
    return [
        KnownException(
            check=str(it.get("check", "")),
            match=str(it.get("match", "")),
            reason=str(it.get("reason", "")),
        )
        for it in items
        if it.get("check") and it.get("match")
    ]


def _apply_exceptions(
    results: list[CheckResult], exceptions: list[KnownException]
) -> tuple[list[CheckResult], list[str]]:
    """Split each check's findings into 'still failing' and 'excepted'.

    A check whose every finding is excepted becomes OK again; nothing here can
    ever except a finding that is not literally present in the check's own
    output, so a `known_exceptions` entry can silence only what it names.
    """
    excepted: list[str] = []
    adjusted: list[CheckResult] = []
    for r in results:
        if r.skipped:
            adjusted.append(r)
            continue
        remaining: list[str] = []
        for finding in r.findings:
            hit = next(
                (e for e in exceptions if e.check == r.name and e.match in finding), None
            )
            if hit:
                excepted.append(f"[{r.name}] {finding} (reason: {hit.reason or 'undocumented'})")
            else:
                remaining.append(finding)
        adjusted.append(CheckResult(name=r.name, ok=not remaining, findings=remaining))
    return adjusted, excepted


@dataclass
class AuditReport:
    checks: list[CheckResult]
    excepted: list[str] = field(default_factory=list)
    since: int = 0
    offline: bool = False

    @property
    def failing(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok and not c.skipped]

    @property
    def ok(self) -> bool:
        return not self.failing

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "offline": self.offline,
                "since": self.since,
                "checks": [c.to_dict() for c in self.checks],
                "excepted": self.excepted,
            },
            indent=2,
            sort_keys=True,
        )

    def summary(self) -> str:
        marks = []
        for c in self.checks:
            if c.skipped:
                marks.append(f"{c.name}=SKIP")
            elif c.ok:
                marks.append(f"{c.name}=PASS")
            else:
                marks.append(f"{c.name}=FAIL({len(c.findings)})")
        return f"hsai audit: {'PASS' if self.ok else 'FAIL'} - " + ", ".join(marks)

    def human(self) -> str:
        lines = [self.summary(), ""]
        for c in self.checks:
            if c.skipped:
                lines.append(f"- {c.name}: SKIPPED ({c.skip_reason})")
                continue
            lines.append(f"- {c.name}: {'PASS' if c.ok else 'FAIL'}")
            lines.extend(f"    - {f}" for f in c.findings)
        if self.excepted:
            lines.append("")
            lines.append(f"known exceptions applied ({len(self.excepted)}):")
            lines.extend(f"    - {e}" for e in self.excepted)
        return "\n".join(lines)


# --- (a) wikilink resolution -----------------------------------------------------

def check_wikilinks(cfg: CoreConfig, repo_root: Path) -> CheckResult:
    notes = _iter_notes(cfg, repo_root)
    universe = {p.stem for p in notes}
    findings: list[str] = []
    for path in notes:
        text = path.read_text(encoding="utf-8")
        for m in _WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            if target and target not in universe:
                findings.append(f"{path.relative_to(repo_root)}: dangling [[{target}]]")
    return CheckResult("wikilinks", ok=not findings, findings=findings)


# --- (b) orphan detection ---------------------------------------------------------

def check_orphans(cfg: CoreConfig, repo_root: Path) -> CheckResult:
    kb = KnowledgeBase.from_config(cfg, repo_root)
    notes = _iter_notes(cfg, repo_root)
    by_stem = {p.stem: p for p in notes}

    adjacency: dict[str, set[str]] = {}
    for p in notes:
        text = p.read_text(encoding="utf-8")
        targets = {m.group(1).strip() for m in _WIKILINK_RE.finditer(text)}
        adjacency[p.stem] = {t for t in targets if t in by_stem}

    moc_stems = [p.stem for p in kb.mocs_dir.glob("*.md")] if kb.mocs_dir.exists() else []
    reached: set[str] = set()
    stack = list(moc_stems)
    while stack:
        stem = stack.pop()
        if stem in reached:
            continue
        reached.add(stem)
        stack.extend(adjacency.get(stem, ()) - reached)

    must_reach = {
        "lesson": kb.lesson_notes(),
        "whitepaper": kb.whitepaper_notes(),
        "article": kb.article_notes(),
    }
    findings = [
        f"{kind} '{name}' is not reachable from any MOC"
        for kind, names in must_reach.items()
        for name in names
        if name not in reached
    ]
    return CheckResult("orphans", ok=not findings, findings=findings)


# --- (c) MOC freshness -------------------------------------------------------------

def check_moc_freshness(cfg: CoreConfig, repo_root: Path) -> CheckResult:
    kb = KnowledgeBase.from_config(cfg, repo_root)
    stale = kb.check_freshness()
    findings = [
        f"{name} is stale (regenerating would change it - run `hsai reindex`)"
        for name in stale
    ]
    return CheckResult("moc_freshness", ok=not findings, findings=findings)


# --- (f) frontmatter / schema validity ----------------------------------------------

def check_frontmatter(cfg: CoreConfig, repo_root: Path) -> CheckResult:
    kb = KnowledgeBase.from_config(cfg, repo_root)
    findings: list[str] = []
    for p in _iter_notes(cfg, repo_root):
        text = p.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            findings.append(f"{p.relative_to(repo_root)}: missing frontmatter block")
            continue
        try:
            data = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as exc:
            findings.append(f"{p.relative_to(repo_root)}: invalid YAML frontmatter ({exc})")
            continue
        if not isinstance(data, dict) or not data.get("tags"):
            findings.append(
                f"{p.relative_to(repo_root)}: frontmatter missing a non-empty 'tags' list"
            )
            continue
        tags = [str(t) for t in data.get("tags") or []]
        rel = p.relative_to(repo_root)
        if _is_under(p, kb.lessons_dir):
            if "lesson" not in tags:
                findings.append(f"{rel}: lesson note missing the 'lesson' tag")
            if not any(t.startswith("outcome/") for t in tags):
                findings.append(f"{rel}: lesson note missing an 'outcome/*' tag")
            if not any(t.startswith("kind/") for t in tags):
                findings.append(f"{rel}: lesson note missing a 'kind/*' tag")
        elif _is_under(p, kb.whitepapers_dir):
            if "whitepaper" not in tags:
                findings.append(f"{rel}: whitepaper note missing the 'whitepaper' tag")
        elif _is_under(p, kb.mocs_dir):
            if "moc" not in tags:
                findings.append(f"{rel}: MOC note missing the 'moc' tag")
        elif _is_under(p, kb.articles_dir):
            if "article" not in tags:
                findings.append(f"{rel}: article note missing the 'article' tag")
    return CheckResult("frontmatter", ok=not findings, findings=findings)


# --- (d) lesson <-> ticket <-> PR closure (GitHub-dependent) -------------------------

@dataclass(frozen=True)
class _LessonLink:
    note_name: str
    ticket: int | None
    pr: int | None


def _parse_lesson_link(path: Path) -> _LessonLink:
    text = path.read_text(encoding="utf-8")
    ticket_m = _TICKET_ROW_RE.search(text)
    pr_m = _PR_ROW_RE.search(text)
    return _LessonLink(
        note_name=path.stem,
        ticket=int(ticket_m.group(1)) if ticket_m else None,
        pr=int(pr_m.group(1)) if pr_m else None,
    )


def check_lesson_ticket_pr_closure(
    cfg: CoreConfig, repo_root: Path, *, since: int = 0, runner: Runner = run
) -> CheckResult:
    kb = KnowledgeBase.from_config(cfg, repo_root)
    repo = cfg.repo_slug
    findings: list[str] = []
    lessoned_prs: set[int] = set()

    for name in kb.lesson_notes():
        link = _parse_lesson_link(kb.lessons_dir / f"{name}.md")
        if (link.ticket or 0) <= since and (link.pr or 0) <= since:
            continue  # pre-invariant history, exempt by the --since boundary

        if link.ticket is None:
            findings.append(f"lesson '{link.note_name}' names no ticket")
        else:
            issue = github.get_issue(repo, link.ticket, runner=runner)
            if issue is None:
                findings.append(
                    f"lesson '{link.note_name}' -> ticket #{link.ticket} does not exist"
                )
            elif issue.state and issue.state.upper() != "CLOSED":
                findings.append(
                    f"lesson '{link.note_name}' -> ticket #{link.ticket} is not closed "
                    f"(state={issue.state})"
                )

        if link.pr is None:
            findings.append(f"lesson '{link.note_name}' names no pull request")
        else:
            pr = github.get_pr(repo, link.pr, runner=runner)
            if pr is None:
                findings.append(f"lesson '{link.note_name}' -> PR #{link.pr} does not exist")
            elif not pr.merged:
                findings.append(
                    f"lesson '{link.note_name}' -> PR #{link.pr} is not merged "
                    f"(state={pr.state})"
                )
            else:
                lessoned_prs.add(link.pr)

    for pr in github.list_merged_prs(repo, runner=runner):
        if pr.number <= since:
            continue
        if pr.number not in lessoned_prs:
            findings.append(f"merged PR #{pr.number} '{pr.title}' has no matching lesson")

    return CheckResult("lesson_ticket_pr_closure", ok=not findings, findings=findings)


# --- (e) model-record consistency (GitHub-dependent) ---------------------------------

def check_model_record_consistency(
    cfg: CoreConfig, repo_root: Path, *, since: int = 0, runner: Runner = run
) -> CheckResult:
    """The tier a merged PR claims must be a tier the ledger actually recorded
    for that ticket at some point - defends the G4 cost numbers, which are
    built entirely on the ledger, from silently diverging from what shipped.
    """
    repo = cfg.repo_slug
    records = ledger.read_records(ledger.ledger_path(cfg, repo_root))
    tiers_by_ticket: dict[int, set[str]] = {}
    for r in records:
        if r.ticket is not None:
            tiers_by_ticket.setdefault(r.ticket, set()).add(r.tier)

    findings: list[str] = []
    for pr in github.list_merged_prs(repo, runner=runner):
        if pr.number <= since:
            continue
        closes = _CLOSES_RE.search(pr.body)
        tier_m = _TIER_RE.search(pr.body)
        if not closes or not tier_m:
            continue  # not a hsai-shaped PR body - nothing to cross-check
        ticket = int(closes.group(1))
        claimed_tier = tier_m.group(1)
        seen = tiers_by_ticket.get(ticket, set())
        if seen and claimed_tier not in seen:
            findings.append(
                f"PR #{pr.number} claims tier `{claimed_tier}` for ticket #{ticket}, "
                f"but the ledger only recorded {sorted(seen)} for that ticket"
            )
    return CheckResult("model_record_consistency", ok=not findings, findings=findings)


# --- orchestration ------------------------------------------------------------------

def run_audit(
    cfg: CoreConfig,
    repo_root: str | Path,
    *,
    since: int | None = None,
    offline: bool = False,
    runner: Runner = run,
) -> AuditReport:
    """Run every check and fold in the documented known-exceptions list.

    ``offline`` skips the GitHub-dependent checks (d, e) entirely - they are
    reported as ``skipped``, never silently omitted from the report.
    """
    repo_root = Path(repo_root)
    since_cutoff = since if since is not None else int(cfg.audit.get("since_pr", 0) or 0)

    checks = [
        check_wikilinks(cfg, repo_root),
        check_orphans(cfg, repo_root),
        check_moc_freshness(cfg, repo_root),
        check_frontmatter(cfg, repo_root),
    ]
    if offline:
        checks.append(CheckResult(
            "lesson_ticket_pr_closure", ok=True, skipped=True,
            skip_reason="offline mode: GitHub-dependent check skipped",
        ))
        checks.append(CheckResult(
            "model_record_consistency", ok=True, skipped=True,
            skip_reason="offline mode: GitHub-dependent check skipped",
        ))
    else:
        checks.append(
            check_lesson_ticket_pr_closure(cfg, repo_root, since=since_cutoff, runner=runner)
        )
        checks.append(
            check_model_record_consistency(cfg, repo_root, since=since_cutoff, runner=runner)
        )

    exceptions_path = repo_root / cfg.audit.get(
        "known_exceptions_file", DEFAULT_KNOWN_EXCEPTIONS_FILE
    )
    exceptions = load_known_exceptions(exceptions_path)
    adjusted, excepted = _apply_exceptions(checks, exceptions)
    return AuditReport(checks=adjusted, excepted=excepted, since=since_cutoff, offline=offline)


def offline_one_liner(cfg: CoreConfig, repo_root: str | Path) -> str:
    """A single line for DIRECTION.md / the block review brief.

    Never raises: a bad audit run (e.g. malformed known_exceptions YAML)
    degrades to a visible error string instead of blocking the governance
    rhythm that surfaces it.
    """
    try:
        return run_audit(cfg, repo_root, offline=True).summary()
    except Exception as exc:
        return f"hsai audit: ERROR ({exc})"


# --- idempotent drift ticket (periodic workflow) --------------------------------------

def file_or_update_drift_ticket(
    cfg: CoreConfig, report: AuditReport, *, runner: Runner = run
) -> int:
    """File or update the single open ``audit-drift`` ticket.

    Idempotent by design: any number of periodic runs that keep finding drift
    update the same ticket's body; a run that finds nothing does not touch it;
    a run with nothing to file returns 0.
    """
    if report.ok:
        return 0
    repo = cfg.repo_slug
    label = str(cfg.audit.get("drift_label", "audit-drift"))
    priority = str(cfg.audit.get("drift_priority_label", "priority:P1"))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        f"`hsai audit` found drift as of {stamp}.\n\n"
        f"```\n{report.human()}\n```\n\n"
        "_Filed/updated automatically by the audit-periodic workflow "
        "(see src/hsai/audit.py). One ticket per drift condition - a later run "
        "that still finds drift updates this ticket rather than filing a new one._"
    )
    existing = [i for i in github.list_open_issues(repo, runner=runner) if label in i.labels]
    if existing:
        target = existing[0]
        github.edit_issue_body(repo, target.number, body, runner=runner)
        return target.number
    return github.create_issue(
        repo, DRIFT_TITLE, body, ["hsai", label, priority], runner=runner
    )
