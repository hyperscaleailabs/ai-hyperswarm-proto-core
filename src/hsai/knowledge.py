"""The knowledge base: lessons, whitepapers, reference field notes, and MOCs.

Everything written here is Obsidian-ready:
- YAML frontmatter with tags,
- ``[[wikilinks]]`` between notes and up to their MOCs,
so that cloning the repo and opening it as a vault yields a connected graph.

Lessons and whitepapers record what THIS loop did. Reference field notes
(``knowledge/reference/``) record what the reference set does: one append-only
note per project, each observation dated, citing the artifact it came from, and
addressable by a stable ``practice_id`` - so a later cycle can ask "what have we
already learned from crewAI, and did we adopt it?" instead of re-deriving it.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .config import CoreConfig

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TAG_RE = re.compile(r"^\s*-\s+(\S.*)$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z-]{3,}")
_STOPWORDS = {
    "this", "that", "with", "from", "have", "been", "were", "will", "which",
    "their", "there", "these", "those", "about", "would", "could", "should",
    "being", "into", "when", "what", "while", "where", "before", "after",
    "between", "over", "under", "more", "most", "some", "such", "than",
    "then", "them", "they", "also", "each", "only", "just", "like", "need",
    "needs", "keep", "keeps", "make", "makes", "made", "gets",
    "using", "used", "uses", "here", "isnt", "doesnt", "wasnt",
    "very", "even", "still", "much", "many", "both", "either", "without",
    "through", "because", "same", "part", "kind", "lesson", "lessons",
}


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Lesson:
    title: str
    outcome: str  # "pass" | "fail"
    kind: str  # heal | implement | improve
    context: str
    what_happened: str
    lesson: str
    iteration: int = 0
    ticket: int | None = None
    pr: int | None = None
    model: str = ""
    references: tuple[str, ...] = ()  # reference-set repos that informed the work
    practices: tuple[str, ...] = ()   # practice_ids from knowledge/reference field notes
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)
    remote_ci: str = ""  # SUCCESS | FAILURE | TIMEOUT, filled in once gh checks conclude
    repro_evidence: str = ""  # heal/bugfix only: failing-then-passing reproduction proof
    recalled: tuple[str, ...] = ()  # prior notes injected into this run's prompt
    review_verdict: str = ""  # the independent reviewer's verdict, verbatim

    def note_name(self) -> str:
        return f"{self.created}-{slugify(self.title)}"


@dataclass
class LessonRecord:
    """A lesson as parsed back off disk - the read-side counterpart of `Lesson`."""

    note_name: str
    title: str
    outcome: str
    kind: str
    tags: tuple[str, ...]
    lesson_text: str
    what_happened: str = ""
    body: str = ""  # everything after the frontmatter; what the recall index reads
    practices: tuple[str, ...] = ()  # practice_ids this note claims an outcome for


def split_sections(text: str) -> dict[str, str]:
    """Map lowercased ``## headings`` to their bodies."""
    parts = _SECTION_RE.split(text)
    # parts[0] is the preamble; the rest alternates heading, body, heading, body...
    sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lower()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections[heading] = body.strip()
    return sections


def _frontmatter_list(fm: str, key: str) -> tuple[str, ...]:
    """List items under one frontmatter key only.

    Frontmatter holds several lists (``tags:``, ``recalled:``, ``practices:``),
    so a blanket "every ``- item`` line belongs to this key" scan would file
    recalled note names as tags.
    """
    items: list[str] = []
    wanted = False
    for line in fm.splitlines():
        if line.strip() and not line.startswith((" ", "\t", "-")):
            wanted = line.strip() == f"{key}:"
            continue
        match = _TAG_RE.match(line)
        if wanted and match:
            items.append(match.group(1).strip())
    return tuple(items)


def _frontmatter_scalar(fm: str, key: str) -> str:
    """A single ``key: value`` frontmatter entry ("" when absent)."""
    for line in fm.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


def parse_note(path: str | Path) -> LessonRecord:
    """Parse any Obsidian note in the vault into a :class:`LessonRecord`.

    Lessons carry ``outcome/*`` and ``kind/*`` frontmatter tags; whitepapers and
    ADRs do not, and come back as ``unknown``. This is the single place those
    tags are interpreted - both :meth:`KnowledgeBase.read_lessons` and the
    :mod:`hsai.recall` index read notes through it.
    """
    path = Path(path)
    text = path.read_text()
    fm_match = _FRONTMATTER_RE.match(text)
    fm = fm_match.group(1) if fm_match else ""
    body = text[fm_match.end():] if fm_match else text
    tags = _frontmatter_list(fm, "tags")
    outcome = next((t.split("/", 1)[1] for t in tags if t.startswith("outcome/")), "unknown")
    kind = next((t.split("/", 1)[1] for t in tags if t.startswith("kind/")), "unknown")
    title_match = _TITLE_RE.search(text)
    title = title_match.group(1).strip() if title_match else path.stem
    sections = split_sections(text)
    return LessonRecord(
        note_name=path.stem,
        title=title,
        outcome=outcome,
        kind=kind,
        tags=tags,
        lesson_text=sections.get("lesson learned", ""),
        what_happened=sections.get("what happened", ""),
        body=body.strip(),
        practices=_frontmatter_list(fm, "practices"),
    )


@dataclass
class Whitepaper:
    title: str
    summary: str
    body: str
    covers_lessons: tuple[str, ...] = ()  # note names
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)

    def note_name(self) -> str:
        return f"{self.created}-{slugify(self.title)}"


def reference_note_name(repo: str) -> str:
    """Stable note stem for a reference project: ``owner/repo`` -> ``owner-repo``."""
    return slugify(repo.replace("/", "-"))


@dataclass(frozen=True)
class Observation:
    """One dated, artifact-citing thing the miner saw in a reference project.

    ``practice_id`` is the addressable key: tickets cite it, lessons record its
    outcome, and the synthesizer's adoption index is keyed on it. ``digest``
    identifies the *content* observed, which is what makes appending idempotent
    - re-mining an unchanged artifact adds nothing, while a changed artifact
    appends a new dated entry beside (never over) the old one.
    """

    practice_id: str
    artifact: str          # the concrete thing observed: a path, a query, a listing
    detail: str            # bounded excerpt of what was actually there
    observed: str = field(default_factory=_today)

    def digest(self) -> str:
        payload = f"{self.practice_id}\n{self.artifact}\n{self.detail}".encode()
        return hashlib.sha256(payload).hexdigest()[:12]

    def render(self) -> str:
        return (
            f"### {self.observed} - `{self.practice_id}`\n"
            f"- artifact: `{self.artifact}`\n"
            f"- digest: `{self.digest()}`\n\n"
            f"{self.detail.strip()}\n"
        )


@dataclass(frozen=True)
class FieldNote:
    """A reference field note as parsed back off disk."""

    note_name: str
    repo: str
    practice_ids: tuple[str, ...]
    digests: frozenset[str] = frozenset()

    @property
    def observations(self) -> int:
        return len(self.practice_ids)


_OBSERVATION_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2}) - `([^`]+)`\s*$", re.MULTILINE)
_DIGEST_RE = re.compile(r"^- digest: `([0-9a-f]+)`\s*$", re.MULTILINE)


def parse_field_note(path: str | Path) -> FieldNote:
    """Read a field note's repo and the practice_ids/digests already recorded."""
    path = Path(path)
    text = path.read_text()
    fm_match = _FRONTMATTER_RE.match(text)
    fm = fm_match.group(1) if fm_match else ""
    return FieldNote(
        note_name=path.stem,
        repo=_frontmatter_scalar(fm, "repo") or path.stem,
        practice_ids=tuple(pid for _, pid in _OBSERVATION_RE.findall(text)),
        digests=frozenset(_DIGEST_RE.findall(text)),
    )


class KnowledgeBase:
    """Filesystem-backed knowledge base rooted at the repo."""

    def __init__(
        self,
        root: str | Path,
        *,
        lessons_dir: str = "knowledge/lessons",
        whitepapers_dir: str = "knowledge/whitepapers",
        reference_dir: str = "knowledge/reference",
        mocs_dir: str = "knowledge/MOCs",
        whitepaper_every: int = 10,
    ) -> None:
        self.root = Path(root)
        self.lessons_dir = self.root / lessons_dir
        self.whitepapers_dir = self.root / whitepapers_dir
        self.reference_dir = self.root / reference_dir
        self.mocs_dir = self.root / mocs_dir
        self.whitepaper_every = whitepaper_every
        for d in (self.lessons_dir, self.whitepapers_dir, self.reference_dir, self.mocs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: CoreConfig, root: str | Path) -> KnowledgeBase:
        k = cfg.knowledge or {}
        return cls(
            root,
            lessons_dir=k.get("lessons_dir", "knowledge/lessons"),
            whitepapers_dir=k.get("whitepapers_dir", "knowledge/whitepapers"),
            reference_dir=k.get("reference_dir", "knowledge/reference"),
            mocs_dir=k.get("mocs_dir", "knowledge/MOCs"),
            whitepaper_every=int(k.get("whitepaper_every_lessons", 10)),
        )

    # --- writing --------------------------------------------------------------
    def write_lesson(self, lesson: Lesson) -> Path:
        path = self.lessons_dir / f"{lesson.note_name()}.md"
        path.write_text(self._render_lesson(lesson))
        return path

    def write_whitepaper(self, paper: Whitepaper) -> Path:
        path = self.whitepapers_dir / f"{paper.note_name()}.md"
        path.write_text(self._render_whitepaper(paper))
        return path

    def append_observations(
        self,
        repo: str,
        observations: list[Observation],
        *,
        stars: int = 0,
        license: str = "",
        snapshot_date: str = "",
    ) -> tuple[Path, list[Observation]]:
        """Append new observations to ``repo``'s field note, creating it if needed.

        Strictly append-only: the header is written exactly once and every byte
        already on disk is preserved. An observation whose content digest is
        already recorded is skipped, so re-mining an unchanged artifact is a
        no-op and only real drift produces a new dated entry.

        Returns the note path and the observations actually appended.
        """
        path = self.reference_dir / f"{reference_note_name(repo)}.md"
        if not path.exists():
            path.write_text(
                self._render_field_note_header(
                    repo, stars=stars, license=license, snapshot_date=snapshot_date
                )
            )
        existing = parse_field_note(path).digests
        fresh: list[Observation] = []
        seen = set(existing)
        for obs in observations:
            if obs.digest() in seen:
                continue
            seen.add(obs.digest())
            fresh.append(obs)
        if fresh:
            with path.open("a") as fh:
                fh.write("\n".join(obs.render() for obs in fresh) + "\n")
        return path, fresh

    # --- counting -------------------------------------------------------------
    def lesson_notes(self) -> list[str]:
        return sorted(p.stem for p in self.lessons_dir.glob("*.md"))

    def whitepaper_notes(self) -> list[str]:
        return sorted(p.stem for p in self.whitepapers_dir.glob("*.md"))

    def reference_notes(self) -> list[str]:
        return sorted(p.stem for p in self.reference_dir.glob("*.md"))

    def read_field_notes(self) -> list[FieldNote]:
        """Every reference field note on disk, by note name."""
        return [
            parse_field_note(self.reference_dir / f"{name}.md")
            for name in self.reference_notes()
        ]

    def field_note_of(self, practice_id: str) -> str:
        """Which field note owns a practice_id ("" when none does).

        Practice ids are minted as ``<note stem>-<artifact>``, so the owning
        note is the longest note stem the id starts with.
        """
        candidates = [n for n in self.reference_notes() if practice_id.startswith(f"{n}-")]
        return max(candidates, key=len) if candidates else ""

    def should_write_whitepaper(self) -> bool:
        n = len(self.lesson_notes())
        return n > 0 and n % self.whitepaper_every == 0

    # --- reading ----------------------------------------------------------------
    def read_lessons(self) -> list[LessonRecord]:
        """Parse every lesson note on disk back into structured records, oldest first."""
        return [self._parse_lesson(name) for name in self.lesson_notes()]

    def _parse_lesson(self, note_name: str) -> LessonRecord:
        return parse_note(self.lessons_dir / f"{note_name}.md")

    def synthesize_whitepaper(self, n: int | None = None) -> Whitepaper:
        """Synthesize a whitepaper by grouping the last `n` lessons by outcome/kind
        and surfacing themes that recur across more than one of them.
        """
        window = n if n is not None else self.whitepaper_every
        all_lessons = self.read_lessons()
        covered = all_lessons[-window:] if window else all_lessons

        outcome_counts = Counter(r.outcome for r in covered)
        kind_counts = Counter(r.kind for r in covered)
        failures = [r for r in covered if r.outcome == "fail"]

        word_sources: dict[str, set[str]] = {}
        for r in covered:
            words = {w.lower() for w in _WORD_RE.findall(r.lesson_text)} - _STOPWORDS
            for w in words:
                word_sources.setdefault(w, set()).add(r.note_name)
        recurring_themes = sorted(
            (w for w, notes in word_sources.items() if len(notes) >= 2),
            key=lambda w: (-len(word_sources[w]), w),
        )[:5]

        outcome_table = "\n".join(f"| {k} | {v} |" for k, v in sorted(outcome_counts.items())) or "| _(none)_ | 0 |"
        kind_table = "\n".join(f"| {k} | {v} |" for k, v in sorted(kind_counts.items())) or "| _(none)_ | 0 |"

        if failures:
            failure_lines = "\n".join(
                f"- [[{r.note_name}]] ({r.kind}): "
                f"{r.lesson_text.splitlines()[0] if r.lesson_text else '_(no lesson text recorded)_'}"
                for r in failures
            )
        else:
            failure_lines = "_No failures in this window - the loop stayed green throughout._"

        if recurring_themes:
            theme_lines = "\n".join(
                f"- **{w}** - appears in {len(word_sources[w])} lessons" for w in recurring_themes
            )
        else:
            theme_lines = "_Not enough repeated vocabulary yet to call out a theme._"

        body = f"""## Outcomes in this window
| outcome | count |
| --- | --- |
{outcome_table}

## Work by kind
| kind | count |
| --- | --- |
{kind_table}

## Recurring failures
{failure_lines}

## Recurring themes
{theme_lines}"""

        summary = (
            f"Synthesis of the last {len(covered)} lesson(s): "
            f"{outcome_counts.get('pass', 0)} pass / {outcome_counts.get('fail', 0)} fail, "
            f"across kinds {', '.join(sorted(kind_counts)) or '(none)'}."
        )
        return Whitepaper(
            title=f"Synthesis after {len(all_lessons)} lessons",
            summary=summary,
            body=body,
            covers_lessons=tuple(r.note_name for r in covered),
        )

    # --- indexing -------------------------------------------------------------
    def reindex_mocs(self) -> list[Path]:
        """Rebuild the MOC files from what is currently on disk."""
        written = [
            self._write_lessons_moc(),
            self._write_whitepapers_moc(),
            self._write_reference_moc(),
            self._write_root_moc(),
        ]
        return written

    # --- rendering ------------------------------------------------------------
    @staticmethod
    def _frontmatter(
        tags: tuple[str, ...], extra: dict[str, str | tuple[str, ...]] | None = None
    ) -> str:
        lines = ["---", "tags:"]
        for t in tags:
            lines.append(f"  - {t}")
        for key, value in (extra or {}).items():
            if isinstance(value, (list, tuple)):
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
            else:
                lines.append(f"{key}: {value}")
        lines.append("---")
        return "\n".join(lines)

    def _practice_line(self, practice_id: str) -> str:
        note = self.field_note_of(practice_id)
        return f"- `{practice_id}` - see [[{note}]]" if note else f"- `{practice_id}`"

    def _render_field_note_header(
        self, repo: str, *, stars: int, license: str, snapshot_date: str
    ) -> str:
        """The part of a field note written exactly once, at creation."""
        owner = repo.split("/", 1)[0]
        tags = ("reference", f"reference/{slugify(owner)}")
        fm = self._frontmatter(
            tags,
            {
                "repo": repo,
                "stars": str(stars),
                "license": license or "unknown",
                "snapshot_date": snapshot_date or _today(),
            },
        )
        return f"""{fm}

# {repo} - field notes

> Part of [[Reference MOC]] - [[Knowledge Base MOC]]

Durable field notes on a reference-set project. **Append-only**: every mining
pass adds dated observations below and rewrites nothing above them, so "what
did we learn from this project, and when" stays answerable. Each observation
cites the artifact it came from and carries a stable `practice_id` that tickets
and lessons refer back to.

## Observations

"""

    def _render_lesson(self, lesson: Lesson) -> str:
        tags = ("lesson", f"outcome/{lesson.outcome}", f"kind/{lesson.kind}", *lesson.tags)
        extra: dict[str, str | tuple[str, ...]] = {
            "created": lesson.created,
            "iteration": str(lesson.iteration),
        }
        # Only present when retrieval actually fired, so a run with recall
        # disabled renders byte-for-byte as it did before recall existed.
        if lesson.recalled:
            extra["recalled"] = lesson.recalled
        # The named practices this change adopted, so a merged PR traces back to
        # a concrete observed practice rather than to a bare repo slug (G1/G2).
        if lesson.practices:
            extra["practices"] = lesson.practices
        fm = self._frontmatter(tags, extra)
        refs = "\n".join(f"- `{r}`" for r in lesson.references) or "- _(none cited)_"
        if lesson.practices:
            practices = "\n".join(self._practice_line(p) for p in lesson.practices)
            refs += f"\n\n### Practices adopted\n{practices}"
        ticket = f"#{lesson.ticket}" if lesson.ticket else "_(none)_"
        pr = f"#{lesson.pr}" if lesson.pr else "_(none)_"
        repro = lesson.repro_evidence or "_(not applicable: not a heal/bugfix ticket)_"
        # Who checked the work, not just who wrote it (G2).
        review = lesson.review_verdict or "_(no independent review recorded)_"
        return f"""{fm}

# {lesson.title}

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **{lesson.outcome}** |
| kind | {lesson.kind} |
| iteration | {lesson.iteration} |
| ticket | {ticket} |
| pull request | {pr} |
| model | `{lesson.model}` |
| remote CI | {lesson.remote_ci or "_(pending)_"} |

## Context
{lesson.context}

## What happened
{lesson.what_happened}

## Lesson learned
{lesson.lesson}

## Independent review
{review}

## Reproduction evidence
{repro}

## References (reference-set evidence)
{refs}
"""

    def _render_whitepaper(self, paper: Whitepaper) -> str:
        tags = ("whitepaper", *paper.tags)
        fm = self._frontmatter(tags, {"created": paper.created})
        covered = "\n".join(f"- [[{n}]]" for n in paper.covers_lessons) or "- _(none)_"
        return f"""{fm}

# {paper.title}

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
{paper.summary}

{paper.body}

## Lessons synthesized
{covered}
"""

    def _write_lessons_moc(self) -> Path:
        notes = self.lesson_notes()
        fm = self._frontmatter(("moc", "lessons"), {"updated": _today()})
        links = "\n".join(f"- [[{n}]]" for n in notes) or "- _No lessons recorded yet._"
        content = f"""{fm}

# Lessons MOC

Up: [[Knowledge Base MOC]]

Every hsai iteration leaves a lesson here - pass or fail. Total: **{len(notes)}**.

{links}
"""
        path = self.mocs_dir / "Lessons MOC.md"
        path.write_text(content)
        return path

    def _write_whitepapers_moc(self) -> Path:
        notes = self.whitepaper_notes()
        fm = self._frontmatter(("moc", "whitepapers"), {"updated": _today()})
        links = "\n".join(f"- [[{n}]]" for n in notes) or "- _No whitepapers yet._"
        content = f"""{fm}

# Whitepapers MOC

Up: [[Knowledge Base MOC]]

Periodic syntheses of accumulated lessons. Total: **{len(notes)}**.

{links}
"""
        path = self.mocs_dir / "Whitepapers MOC.md"
        path.write_text(content)
        return path

    def _write_reference_moc(self) -> Path:
        notes = self.read_field_notes()
        fm = self._frontmatter(("moc", "reference"), {"updated": _today()})
        links = "\n".join(
            f"- [[{n.note_name}]] - `{n.repo}` ({n.observations} observation(s))"
            for n in notes
        ) or "- _No field notes mined yet._"
        total = sum(n.observations for n in notes)
        content = f"""{fm}

# Reference MOC

Up: [[Knowledge Base MOC]]

Append-only field notes on the top-10 reference set (G1). Each note accumulates
dated, artifact-citing observations; each observation carries a `practice_id`
that tickets cite and lessons record an outcome for. Projects: **{len(notes)}**,
observations: **{total}**.

{links}
"""
        path = self.mocs_dir / "Reference MOC.md"
        path.write_text(content)
        return path

    def _write_root_moc(self) -> Path:
        fm = self._frontmatter(("moc", "index"), {"updated": _today()})
        n_lessons = len(self.lesson_notes())
        n_papers = len(self.whitepaper_notes())
        n_refs = len(self.reference_notes())
        content = f"""{fm}

# Knowledge Base MOC

The living memory of **ai-hyperswarm-proto-core**. Open this repo as an Obsidian
vault and use the graph view to explore how lessons connect.

## Maps
- [[Lessons MOC]] - {n_lessons} lesson(s)
- [[Whitepapers MOC]] - {n_papers} whitepaper(s)
- [[Reference MOC]] - {n_refs} reference project(s) under field notes

## How this is maintained
- Each PR the [[hsai]] loop opens contributes exactly one lesson.
- Every {self.whitepaper_every} lessons, a whitepaper synthesizes the themes.
- Every synthesis pass appends dated observations to the reference field notes.
- These MOCs are regenerated by `hsai reindex` after each iteration.
"""
        path = self.mocs_dir / "Knowledge Base MOC.md"
        path.write_text(content)
        return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> date:
    return datetime.now(timezone.utc).date()
