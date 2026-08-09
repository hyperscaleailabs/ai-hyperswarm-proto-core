"""The reference-practice registry: what was learned, from where, and whether it shipped.

Goal G1 says every improvement must trace back to something observed in the
field; goal G2 says that trace must be auditable end to end. This module is the
durable record that makes both true rather than merely claimed:

- a :class:`PracticeRef` is a practice *declared on a ticket* (``source_repo ->
  practice``), rendered into and parsed back out of the ticket body, so the
  provenance the PR reports is the provenance the ticket asked for;
- a :class:`Practice` is the durable note for it under
  ``knowledge/practices/<id>.md``, in the same Obsidian frontmatter +
  ``[[wikilink]]`` conventions as :mod:`hsai.knowledge`;
- :func:`validate_references` is the gate: only slugs pinned in
  ``.ai-swarm/core.yaml`` (top-10 or watchlist) may ever be cited, so an
  invented reference cannot reach a PR body.

Lifecycle: synthesis files a ticket and writes one ``queued`` note per cited
practice; the orchestrator reads the declarations back off the claimed issue and
flips the notes to ``adopted`` (with PR and lesson) once the change merges.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import CoreConfig
from .knowledge import slugify, today

QUEUED = "queued"
ADOPTED = "adopted"
REJECTED = "rejected"
STATUSES = (QUEUED, ADOPTED, REJECTED)

PRACTICES_HEADING = "## Practices adopted"

_REPO_SLUG = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_PRACTICE_LINE = re.compile(
    rf"^\s*-\s+(?P<repo>{_REPO_SLUG})\s*->\s*(?P<rest>\S.*?)\s*$", re.MULTILINE
)
_ARTIFACT_SUFFIX = re.compile(r"\s*\(artifact:\s*`(?P<artifact>[^`]+)`\)\s*$")
_SECTION_SPLIT = re.compile(r"^## ", re.MULTILINE)
_FRONTMATTER = re.compile(r"^---\n(?P<body>.*?)\n---\n", re.DOTALL)
_FIELD = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>.*)$", re.MULTILINE)
_SUMMARY = re.compile(r"^## Practice\n(?P<text>.*?)(?=\n## |\Z)", re.DOTALL | re.MULTILINE)
_SENTENCE = re.compile(r"(?<=[.;])\s+")
_BACKTICKED = re.compile(r"`([^`]+)`")

# Practice summaries ride on a single ticket line, so they are capped rather
# than allowed to swallow a whole synthesis rationale.
MAX_SUMMARY = 300


def _today() -> str:
    return today().isoformat()


def practice_id(source_repo: str, summary: str) -> str:
    """Deterministic note id, so the writer and the later reader agree on it."""
    return f"{slugify(source_repo)}-{slugify(summary)[:60].strip('-')}".strip("-")


@dataclass(frozen=True)
class PracticeRef:
    """A practice as declared on a ticket: where it came from and what it is."""

    source_repo: str
    practice: str
    artifact: str = ""

    @property
    def id(self) -> str:
        return practice_id(self.source_repo, self.practice)

    def render(self) -> str:
        line = f"- {self.source_repo} -> {self.practice}"
        return f"{line} (artifact: `{self.artifact}`)" if self.artifact else line


def render_practices_section(refs: Sequence[PracticeRef]) -> str:
    """Render the ``## Practices adopted`` block of a ticket body."""
    lines = "\n".join(r.render() for r in refs) or "- _(none declared)_"
    return f"{PRACTICES_HEADING}\n{lines}\n"


def parse_practices_section(body: str) -> tuple[PracticeRef, ...]:
    """Read the practices a ticket declares back out of its rendered body.

    This is the *only* source of a PR's reference-set evidence: a body with no
    such section declares nothing, and nothing is what gets reported.
    """
    section = ""
    for chunk in _SECTION_SPLIT.split(body or ""):
        head, _, rest = chunk.partition("\n")
        if head.strip().lower() == "practices adopted":
            section = rest
            break
    if not section:
        return ()
    refs: list[PracticeRef] = []
    seen: set[str] = set()
    for m in _PRACTICE_LINE.finditer(section):
        rest = m.group("rest")
        artifact_match = _ARTIFACT_SUFFIX.search(rest)
        artifact = artifact_match.group("artifact") if artifact_match else ""
        practice = _ARTIFACT_SUFFIX.sub("", rest).strip()
        if not practice:
            continue
        ref = PracticeRef(m.group("repo"), practice, artifact)
        if ref.id in seen:
            continue
        seen.add(ref.id)
        refs.append(ref)
    return tuple(refs)


def validate_references(cfg: CoreConfig, refs: Iterable[str]) -> tuple[str, ...]:
    """Drop any slug that is not pinned in the reference set (dedup, keep order).

    A citation outside ``reference_set.top10`` / ``reference_set.watchlist``
    cannot have been observed by this loop, so it is fabricated and is removed
    before it can reach a PR body.
    """
    known = set(cfg.reference_repos())
    return tuple(dict.fromkeys(r for r in refs if r in known))


def validated_practices(
    cfg: CoreConfig, refs: Iterable[PracticeRef]
) -> tuple[PracticeRef, ...]:
    """Keep only declarations that cite a pinned reference project.

    Applied where a ticket body is read back, so an unpinned slug can neither
    reach a PR body nor enter the registry.
    """
    known = set(cfg.reference_repos())
    return tuple(r for r in refs if r.source_repo in known)


def extract_practices(
    rationale: str, known_repos: Sequence[str], *, limit: int = 5
) -> tuple[PracticeRef, ...]:
    """Mine a synthesis rationale for the practices it credits.

    Deterministic on purpose: a repo slug counts only when it is one of the
    pinned reference projects, and the practice text is the sentence that names
    it - the model's own words, not a paraphrase invented here.
    """
    text = " ".join((rationale or "").split())
    if not text:
        return ()
    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()] or [text]
    refs: list[PracticeRef] = []
    seen: set[str] = set()
    for repo in known_repos:
        sentence = next((s for s in sentences if repo in s), "")
        if not sentence:
            continue
        artifacts = _BACKTICKED.findall(sentence)
        ref = PracticeRef(
            source_repo=repo,
            practice=sentence[:MAX_SUMMARY].strip(),
            artifact=artifacts[0].strip() if artifacts else "",
        )
        if ref.id in seen:
            continue
        seen.add(ref.id)
        refs.append(ref)
        if len(refs) >= limit:
            break
    return tuple(refs)


@dataclass
class Practice:
    """A practice extracted from a reference project, as stored on disk."""

    id: str
    source_repo: str
    artifact: str
    summary: str
    status: str = QUEUED
    adopted_by_ticket: int | None = None
    adopted_by_pr: int | None = None
    lesson_note: str = ""
    created: str = field(default_factory=_today)

    @classmethod
    def from_ref(cls, ref: PracticeRef, **kwargs) -> Practice:
        return cls(
            id=ref.id, source_repo=ref.source_repo,
            artifact=ref.artifact, summary=ref.practice, **kwargs,
        )

    def title(self) -> str:
        return f"Practice: {self.source_repo} - {self.summary[:80]}"


@dataclass(frozen=True)
class Coverage:
    """Per-reference-project extraction coverage."""

    repo: str
    queued: int = 0
    adopted: int = 0
    rejected: int = 0

    @property
    def total(self) -> int:
        return self.queued + self.adopted + self.rejected


class PracticeRegistry:
    """Filesystem-backed practice registry rooted at the repo."""

    def __init__(self, root: str | Path, *, practices_dir: str = "knowledge/practices") -> None:
        self.root = Path(root)
        self.practices_dir = self.root / practices_dir
        self.practices_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: CoreConfig, root: str | Path) -> PracticeRegistry:
        k = cfg.knowledge or {}
        return cls(root, practices_dir=k.get("practices_dir", "knowledge/practices"))

    # --- reading --------------------------------------------------------------
    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.practices_dir.glob("*.md"))

    def path_for(self, practice_id: str) -> Path:
        return self.practices_dir / f"{practice_id}.md"

    def read(self, practice_id: str) -> Practice | None:
        path = self.path_for(practice_id)
        if not path.is_file():
            return None
        return self._parse(practice_id, path.read_text())

    def read_all(self) -> list[Practice]:
        return [p for p in (self.read(i) for i in self.ids()) if p is not None]

    def adopted(self) -> list[Practice]:
        return [p for p in self.read_all() if p.status == ADOPTED]

    def coverage(self, repos: Sequence[str]) -> list[Coverage]:
        """Counts per reference project, including projects with nothing yet."""
        counts: dict[str, Counter] = {r: Counter() for r in repos}
        for p in self.read_all():
            counts.setdefault(p.source_repo, Counter())[p.status] += 1
        return [
            Coverage(
                repo=repo,
                queued=c.get(QUEUED, 0), adopted=c.get(ADOPTED, 0), rejected=c.get(REJECTED, 0),
            )
            for repo, c in counts.items()
        ]

    # --- writing --------------------------------------------------------------
    def write(self, practice: Practice) -> Path:
        path = self.path_for(practice.id)
        path.write_text(self._render(practice))
        return path

    def record_queued(
        self, refs: Sequence[PracticeRef], *, ticket: int | None = None
    ) -> list[Practice]:
        """Upsert one ``queued`` note per declared practice, linked to its ticket.

        Idempotent, and never demotes: re-filing a practice that was already
        adopted leaves that verdict (and its PR) intact.
        """
        written: list[Practice] = []
        for ref in refs:
            existing = self.read(ref.id)
            practice = existing or Practice.from_ref(ref)
            practice.artifact = practice.artifact or ref.artifact
            practice.summary = practice.summary or ref.practice
            if practice.adopted_by_ticket is None:
                practice.adopted_by_ticket = ticket
            self.write(practice)
            written.append(practice)
        return written

    def mark_adopted(
        self,
        refs: Sequence[PracticeRef],
        *,
        pr: int | None = None,
        ticket: int | None = None,
        lesson_note: str = "",
    ) -> list[Practice]:
        """Flip declared practices to ``adopted``, stamped with PR and lesson.

        Creates the note when it is missing: a practice can be declared on a
        hand-written ticket that synthesis never queued, and the record of what
        actually shipped must not depend on that.
        """
        adopted: list[Practice] = []
        for ref in refs:
            practice = self.read(ref.id) or Practice.from_ref(ref)
            practice.status = ADOPTED
            practice.artifact = practice.artifact or ref.artifact
            practice.adopted_by_ticket = ticket or practice.adopted_by_ticket
            practice.adopted_by_pr = pr or practice.adopted_by_pr
            practice.lesson_note = lesson_note or practice.lesson_note
            self.write(practice)
            adopted.append(practice)
        return adopted

    # --- rendering ------------------------------------------------------------
    @staticmethod
    def _link(prefix: str, number: int | None) -> str:
        """Wikilink to a ticket / PR, so the graph connects even off-vault."""
        return f"[[{prefix}-{number}]] (#{number})" if number else "_(none)_"

    @classmethod
    def _render(cls, p: Practice) -> str:
        ticket = cls._link("ticket", p.adopted_by_ticket)
        pr = cls._link("pr", p.adopted_by_pr)
        lesson = f"[[{p.lesson_note}]]" if p.lesson_note else "_(pending)_"
        artifact = f"`{p.artifact}`" if p.artifact else "_(none recorded)_"
        return f"""---
tags:
  - practice
  - status/{p.status}
  - source/{slugify(p.source_repo)}
source_repo: {p.source_repo}
artifact: {p.artifact}
status: {p.status}
ticket: {p.adopted_by_ticket or "-"}
pr: {p.adopted_by_pr or "-"}
lesson: {p.lesson_note or "-"}
created: {p.created}
---

# {p.title()}

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `{p.source_repo}` |
| artifact | {artifact} |
| status | **{p.status}** |
| ticket | {ticket} |
| pull request | {pr} |
| lesson | {lesson} |

## Practice
{p.summary}
"""

    @staticmethod
    def _parse(practice_id: str, text: str) -> Practice:
        fm_match = _FRONTMATTER.match(text)
        fields = {
            m.group("key"): m.group("value").strip()
            for m in _FIELD.finditer(fm_match.group("body") if fm_match else "")
        }

        def _int(key: str) -> int | None:
            raw = fields.get(key, "-")
            return int(raw) if raw.isdigit() else None

        summary_match = _SUMMARY.search(text)
        status = fields.get("status", QUEUED)
        return Practice(
            id=practice_id,
            source_repo=fields.get("source_repo", ""),
            artifact=fields.get("artifact", ""),
            summary=summary_match.group("text").strip() if summary_match else "",
            status=status if status in STATUSES else QUEUED,
            adopted_by_ticket=_int("ticket"),
            adopted_by_pr=_int("pr"),
            lesson_note="" if fields.get("lesson", "-") == "-" else fields["lesson"],
            created=fields.get("created", ""),
        )
