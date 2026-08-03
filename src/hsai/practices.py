"""The practice registry: a durable, status-carrying record of practices
extracted from the reference set.

Every practice observed in the field (G1) becomes one Obsidian-ready note
under ``knowledge/practices/`` with a ``status`` that tracks its lifecycle:

    proposed -> adopted | rejected | superseded

Synthesis reads this registry (alongside open/closed GitHub issues) so it
never re-proposes work that has already been decided, and the block cycle
reconciles ``proposed`` entries against their linked ticket's outcome once the
implementation block concludes.

Deliberately independent of :mod:`hsai.knowledge` (no import either way) so
:mod:`hsai.knowledge` can depend on this module for the Practices MOC without
a cycle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import github
from .config import CoreConfig
from .proc import Runner, run

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TAG_RE = re.compile(r"^\s*-\s+(\S.*)$", re.MULTILINE)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_FIELD_RE = re.compile(r"^([a-z_]+):\s*(.*)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

STATUSES = ("proposed", "adopted", "rejected", "superseded")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Practice:
    """A practice extracted from a reference-set project, ready to write."""

    title: str
    source_repo: str
    summary: str
    status: str = "proposed"  # proposed | adopted | rejected | superseded
    ticket: int | None = None
    pr: int | None = None
    lessons: tuple[str, ...] = ()  # lesson note names, wikilinked
    created: str = field(default_factory=_today)

    def note_name(self) -> str:
        return f"{self.created}-{slugify(self.title)}"


@dataclass
class PracticeRecord:
    """A practice as parsed back off disk - the read-side counterpart of `Practice`."""

    note_name: str
    title: str
    source_repo: str
    status: str
    ticket: int | None
    pr: int | None
    lessons: tuple[str, ...]
    summary: str


class PracticeRegistry:
    """Filesystem-backed practice registry rooted at the repo."""

    def __init__(self, root: str | Path, *, practices_dir: str = "knowledge/practices") -> None:
        self.root = Path(root)
        self.practices_dir = self.root / practices_dir
        self.practices_dir.mkdir(parents=True, exist_ok=True)

    # --- writing ----------------------------------------------------------
    def write(self, practice: Practice) -> Path:
        path = self.practices_dir / f"{practice.note_name()}.md"
        path.write_text(self._render(practice))
        return path

    def set_status(self, note_name: str, status: str, *, pr: int | None = None) -> Path:
        """Flip a practice's status in place (used by cycle-block reconciliation)."""
        if status not in STATUSES:
            raise ValueError(f"unknown practice status: {status!r}")
        rec = self._parse(note_name)
        practice = Practice(
            title=rec.title,
            source_repo=rec.source_repo,
            summary=rec.summary,
            status=status,
            ticket=rec.ticket,
            pr=pr if pr is not None else rec.pr,
            lessons=rec.lessons,
            created=note_name[:10],
        )
        return self.write(practice)

    # --- reading ------------------------------------------------------------
    def notes(self) -> list[str]:
        return sorted(p.stem for p in self.practices_dir.glob("*.md"))

    def read_all(self) -> list[PracticeRecord]:
        return [self._parse(name) for name in self.notes()]

    def non_proposed(self) -> list[PracticeRecord]:
        return [r for r in self.read_all() if r.status != "proposed"]

    def _parse(self, note_name: str) -> PracticeRecord:
        text = (self.practices_dir / f"{note_name}.md").read_text()
        fm_match = _FRONTMATTER_RE.match(text)
        fm = fm_match.group(1) if fm_match else ""
        tags = tuple(m.group(1).strip() for m in _TAG_RE.finditer(fm))
        status = next((t.split("/", 1)[1] for t in tags if t.startswith("status/")), "proposed")
        fields = dict(_FIELD_RE.findall(fm))
        source_repo = fields.get("source_repo", "")
        ticket = _parse_int(fields.get("ticket"))
        pr = _parse_int(fields.get("pr"))
        title_match = _TITLE_RE.search(text)
        title = title_match.group(1).strip() if title_match else note_name
        sections = self._split_sections(text)
        summary = sections.get("summary", "")
        lessons_section = sections.get("lessons", "")
        lessons = tuple(_WIKILINK_RE.findall(lessons_section))
        return PracticeRecord(
            note_name=note_name,
            title=title,
            source_repo=source_repo,
            status=status,
            ticket=ticket,
            pr=pr,
            lessons=lessons,
            summary=summary,
        )

    @staticmethod
    def _split_sections(text: str) -> dict[str, str]:
        parts = _SECTION_RE.split(text)
        sections: dict[str, str] = {}
        for i in range(1, len(parts), 2):
            heading = parts[i].strip().lower()
            body = parts[i + 1] if i + 1 < len(parts) else ""
            sections[heading] = body.strip()
        return sections

    # --- rendering ------------------------------------------------------------
    def _render(self, practice: Practice) -> str:
        tags = ("practice", f"status/{practice.status}")
        fm_lines = ["---", "tags:"]
        for t in tags:
            fm_lines.append(f"  - {t}")
        fm_lines.append(f"source_repo: {practice.source_repo}")
        fm_lines.append(f"ticket: {practice.ticket or ''}")
        fm_lines.append(f"pr: {practice.pr or ''}")
        fm_lines.append(f"created: {practice.created}")
        fm_lines.append("---")
        fm = "\n".join(fm_lines)

        ticket_s = f"#{practice.ticket}" if practice.ticket else "_(none)_"
        pr_s = f"#{practice.pr}" if practice.pr else "_(none)_"
        lessons = "\n".join(f"- [[{n}]]" for n in practice.lessons) or "- _(none yet)_"
        return f"""{fm}

# {practice.title}

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| status | **{practice.status}** |
| source repo | `{practice.source_repo}` |
| ticket | {ticket_s} |
| pull request | {pr_s} |

## Summary
{practice.summary}

## Lessons
{lessons}
"""


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value.isdigit():
        return None
    return int(value)


def reconcile_registry(
    cfg: CoreConfig, repo_root: str | Path, *, runner: Runner = run
) -> list[str]:
    """Flip `proposed` practices to `adopted`/`rejected` from their ticket state.

    A practice's ticket closing (the only way tickets close in this loop is a
    merged PR carrying ``Closes #N``) means the practice was adopted; a ticket
    labelled ``blocked`` (exhausted retries) means it was rejected. Anything
    still open and unblocked is left `proposed` for a later block.
    """
    registry = PracticeRegistry(repo_root)
    flipped: list[str] = []
    for rec in registry.read_all():
        if rec.status != "proposed" or rec.ticket is None:
            continue
        issue = github.get_issue(cfg.repo_slug, rec.ticket, runner=runner)
        if issue is None:
            continue
        if issue.is_blocked:
            registry.set_status(rec.note_name, "rejected")
            flipped.append(f"{rec.note_name} -> rejected (ticket #{rec.ticket} blocked)")
        elif issue.closed:
            registry.set_status(rec.note_name, "adopted")
            flipped.append(f"{rec.note_name} -> adopted (ticket #{rec.ticket} closed)")
    return flipped
