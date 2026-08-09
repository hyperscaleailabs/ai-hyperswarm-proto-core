"""The reference-practice registry: what was learned, from where, and whether it shipped.

Goal G1 says every improvement must trace back to something observed in the
field, and G2 says that trace has to survive after the fact. A *practice* is the
unit of that trace: one named artifact in one reference project (a commit
convention, a workflow, a file), extracted once, adopted at most once.

Each practice is a durable Obsidian note under ``knowledge/practices/``, written
with the same frontmatter + ``[[wikilink]]`` conventions as :mod:`hsai.knowledge`:

- synthesis files a ``queued`` note when it cites a practice in a ticket,
- the ticket body declares it under ``## Practices adopted``,
- the PR body's reference section is derived from that declaration - never invented,
- the merge flips the note to ``adopted`` with its PR and lesson.

The registry is therefore the single answer to "which of the top-10 have we
actually learned from, and where is the receipt?".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import CoreConfig
from .knowledge import slugify, today_stamp

QUEUED = "queued"
ADOPTED = "adopted"
REJECTED = "rejected"
STATUSES = (QUEUED, ADOPTED, REJECTED)

PRACTICES_HEADING = "## Practices adopted"
NONE_DECLARED = "_(none declared)_"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_FIELD_RE = re.compile(r"^([a-z_]+):[ \t]*(.*)$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^## Summary\n(.*?)(?=\n## |\Z)", re.DOTALL | re.MULTILINE)
# A declared line: "- crewAIInc/crewAI -> commits a provenance artifact per change"
_DECLARED_RE = re.compile(r"^\s*-\s+(\S+/\S+)\s*->\s*(\S.*)$")
_SECTION_START_RE = re.compile(r"^#{2,3}\s+\S")


@dataclass(frozen=True)
class PracticeRef:
    """A practice as *declared* on a ticket: which project, and what was taken."""

    source_repo: str
    practice: str

    def render(self) -> str:
        return f"- {self.source_repo} -> {self.practice}"

    def practice_id(self) -> str:
        return practice_id(self.source_repo, self.practice)


@dataclass
class Practice:
    """A practice as *recorded* in the registry - the durable, auditable form."""

    id: str
    source_repo: str
    artifact: str  # commit / workflow / file path or URL the practice was read off
    summary: str
    status: str = QUEUED
    adopted_by_ticket: int | None = None
    adopted_by_pr: int | None = None
    lesson_note: str = ""
    created: str = field(default_factory=today_stamp)

    def note_name(self) -> str:
        return self.id


def practice_id(source_repo: str, summary: str) -> str:
    """Stable slug for a practice, so the same citation never files two notes."""
    return f"{slugify(source_repo)}-{slugify(summary)}"[:100].strip("-")


def render_practices_section(refs: tuple[PracticeRef, ...]) -> str:
    """The ``## Practices adopted`` block a ticket carries (its provenance claim)."""
    lines = "\n".join(r.render() for r in refs) or NONE_DECLARED
    return f"{PRACTICES_HEADING}\n{lines}\n"


def parse_practices(body: str) -> tuple[PracticeRef, ...]:
    """Read the practices a ticket declares. Anything unparseable yields ``()``.

    This is the provenance the PR body is built from, so it fails closed: a
    missing or malformed section means "nothing declared", never a default list.
    """
    lines = body.splitlines()
    try:
        start = next(
            i for i, ln in enumerate(lines)
            if ln.strip().lower() == PRACTICES_HEADING.lower()
        )
    except StopIteration:
        return ()
    refs: list[PracticeRef] = []
    for ln in lines[start + 1:]:
        if _SECTION_START_RE.match(ln):
            break
        m = _DECLARED_RE.match(ln)
        if m:
            refs.append(PracticeRef(source_repo=m.group(1), practice=m.group(2).strip()))
    return tuple(refs)


def extract_practice_refs(rationale: str, known_repos: tuple[str, ...]) -> tuple[PracticeRef, ...]:
    """Mine a synthesis rationale for the reference projects it actually cites.

    Matching is on the full ``owner/name`` slug and only against repos the
    harness pinned: a bare project name ("swarm", "camel") is too easy to hit by
    accident, and a citation nobody can check is exactly the fabricated
    provenance this registry exists to stop. The practice text is the sentence
    the model wrote, so the declaration quotes the reasoning rather than
    asserting something new.
    """
    refs: list[PracticeRef] = []
    sentences = [s.strip() for s in re.split(r"(?<=[.;])\s+", rationale) if s.strip()]
    for repo in dict.fromkeys(known_repos):
        hit = next((s for s in sentences if repo.lower() in s.lower()), "")
        if hit:
            refs.append(PracticeRef(source_repo=repo, practice=_condense(hit)))
    return tuple(refs)


def _condense(text: str, limit: int = 240) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class Coverage:
    """Per-reference-project extraction counts."""

    source_repo: str
    queued: int = 0
    adopted: int = 0
    rejected: int = 0

    @property
    def total(self) -> int:
        return self.queued + self.adopted + self.rejected


class PracticeRegistry:
    """Filesystem-backed practice notes, one Obsidian file per practice."""

    def __init__(
        self, root: str | Path, *, practices_dir: str | Path = "knowledge/practices"
    ) -> None:
        self.root = Path(root)
        self.dir = self.root / practices_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: CoreConfig, root: str | Path) -> PracticeRegistry:
        k = cfg.knowledge or {}
        return cls(root, practices_dir=k.get("practices_dir", "knowledge/practices"))

    # --- reading ---------------------------------------------------------------
    def note_names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.md"))

    def read(self, practice_id: str) -> Practice | None:
        path = self.dir / f"{practice_id}.md"
        if not path.is_file():
            return None
        return self._parse(path)

    def read_all(self) -> list[Practice]:
        return [self._parse(self.dir / f"{n}.md") for n in self.note_names()]

    def coverage(
        self, *, repo: str | None = None, status: str | None = None
    ) -> list[Coverage]:
        """Extraction counts per source project, alphabetical."""
        counts: dict[str, dict[str, int]] = {}
        for p in self.read_all():
            if repo and p.source_repo != repo:
                continue
            if status and p.status != status:
                continue
            counts.setdefault(p.source_repo, dict.fromkeys(STATUSES, 0))
            counts[p.source_repo][p.status] = counts[p.source_repo].get(p.status, 0) + 1
        return [
            Coverage(source_repo=r, queued=c[QUEUED], adopted=c[ADOPTED], rejected=c[REJECTED])
            for r, c in sorted(counts.items())
        ]

    # --- writing ---------------------------------------------------------------
    def write(self, practice: Practice) -> Path:
        path = self.dir / f"{practice.note_name()}.md"
        path.write_text(self._render(practice))
        return path

    def queue(self, practice: Practice) -> Path:
        """Record a newly-cited practice, without ever demoting one already adopted."""
        existing = self.read(practice.id)
        if existing is not None and existing.status != QUEUED:
            return self.dir / f"{existing.note_name()}.md"
        if existing is not None:
            # Keep the original note's provenance; refresh only what the new
            # citation knows (a later ticket may be the one that ships it).
            practice = Practice(
                id=existing.id,
                source_repo=existing.source_repo,
                artifact=practice.artifact or existing.artifact,
                summary=practice.summary or existing.summary,
                status=QUEUED,
                adopted_by_ticket=practice.adopted_by_ticket or existing.adopted_by_ticket,
                lesson_note=existing.lesson_note,
                created=existing.created,
            )
        return self.write(practice)

    def mark_adopted(
        self,
        refs: tuple[PracticeRef, ...],
        *,
        ticket: int | None,
        pr: int | None,
        lesson_note: str = "",
    ) -> list[Path]:
        """Flip every declared practice to ``adopted``, filing any note that is missing.

        A merged PR is the receipt, so the registry records the practice even if
        synthesis never queued it (a hand-written ticket, say).
        """
        written: list[Path] = []
        for ref in refs:
            pid = ref.practice_id()
            current = self.read(pid)
            written.append(self.write(Practice(
                id=pid,
                source_repo=ref.source_repo,
                artifact=current.artifact if current else "",
                summary=current.summary if current else ref.practice,
                status=ADOPTED,
                adopted_by_ticket=ticket or (current.adopted_by_ticket if current else None),
                adopted_by_pr=pr,
                lesson_note=lesson_note or (current.lesson_note if current else ""),
                created=current.created if current else today_stamp(),
            )))
        return written

    # --- rendering / parsing ----------------------------------------------------
    @staticmethod
    def _render(practice: Practice) -> str:
        tags = (
            "practice",
            f"status/{practice.status}",
            f"source/{slugify(practice.source_repo)}",
        )
        fields = {
            "created": practice.created,
            "source_repo": practice.source_repo,
            "artifact": practice.artifact or "(unrecorded)",
            "status": practice.status,
            "adopted_by_ticket": str(practice.adopted_by_ticket or ""),
            "adopted_by_pr": str(practice.adopted_by_pr or ""),
            "lesson_note": practice.lesson_note,
        }
        fm = "\n".join(
            ["---", "tags:", *(f"  - {t}" for t in tags),
             *(f"{k}: {v}".rstrip() for k, v in fields.items()), "---"]
        )
        ticket_link = f"[[ticket-{practice.adopted_by_ticket}]]" if practice.adopted_by_ticket else "_(none)_"
        lesson_link = f"[[{practice.lesson_note}]]" if practice.lesson_note else "_(none yet)_"
        pr = f"#{practice.adopted_by_pr}" if practice.adopted_by_pr else "_(none)_"
        return f"""{fm}

# Practice: {practice.source_repo}

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `{practice.source_repo}` |
| artifact | `{practice.artifact or "(unrecorded)"}` |
| status | **{practice.status}** |
| ticket | {ticket_link} |
| pull request | {pr} |
| lesson | {lesson_link} |

## Summary
{practice.summary}
"""

    @staticmethod
    def _parse(path: Path) -> Practice:
        text = path.read_text()
        fm_match = _FRONTMATTER_RE.match(text)
        fields = dict(_FIELD_RE.findall(fm_match.group(1))) if fm_match else {}
        summary_match = _SUMMARY_RE.search(text)

        def _int(key: str) -> int | None:
            raw = fields.get(key, "").strip()
            return int(raw) if raw.isdigit() else None

        status = fields.get("status", QUEUED).strip() or QUEUED
        return Practice(
            id=path.stem,
            source_repo=fields.get("source_repo", "").strip(),
            artifact=fields.get("artifact", "").strip(),
            summary=summary_match.group(1).strip() if summary_match else "",
            status=status if status in STATUSES else QUEUED,
            adopted_by_ticket=_int("adopted_by_ticket"),
            adopted_by_pr=_int("adopted_by_pr"),
            lesson_note=fields.get("lesson_note", "").strip(),
            created=fields.get("created", "").strip() or today_stamp(),
        )


def render_coverage_table(rows: list[Coverage]) -> str:
    """The table `hsai practices` prints (and the Practices MOC embeds)."""
    header = "| source repo | queued | adopted | rejected | total |\n| --- | --- | --- | --- | --- |"
    if not rows:
        return f"{header}\n| _(no practices recorded yet)_ | 0 | 0 | 0 | 0 |"
    body = "\n".join(
        f"| {c.source_repo} | {c.queued} | {c.adopted} | {c.rejected} | {c.total} |"
        for c in rows
    )
    return f"{header}\n{body}"
