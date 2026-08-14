"""The knowledge base: lessons, whitepapers, reference field notes, and MOCs.

Everything written here is Obsidian-ready:
- YAML frontmatter with tags,
- ``[[wikilinks]]`` between notes and up to their MOCs,
so that cloning the repo and opening it as a vault yields a connected graph.

Three note families live here:

- **lessons** - one per iteration, pass or fail;
- **whitepapers** - periodic syntheses of accumulated lessons;
- **reference field notes** - one per reference-set project, an *append-only*
  log of dated observations, each citing the concrete artifact it came from and
  carrying a stable ``practice_id``. Field notes are what makes "what did we
  already learn from crewAI in July?" answerable at all: a mining pass adds a
  new dated entry and never rewrites an existing one.
"""
from __future__ import annotations

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


def practice_id(repo: str, practice: str) -> str:
    """The stable key one observed practice is addressed by, forever.

    Namespaced by the repo it was observed in, so two projects can each have a
    "duplicate triage" practice without colliding, and so a bare id read off a
    ticket or a lesson still says where it came from.
    """
    return f"{slugify(repo)}--{slugify(practice)}"


def field_note_name(repo: str) -> str:
    """``owner/repo`` -> the field note's stem, casing preserved for traceability."""
    return repo.replace("/", "-")


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
    practices: tuple[str, ...] = ()  # practice_ids this change adopted (see field notes)
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)
    remote_ci: str = ""  # SUCCESS | FAILURE | TIMEOUT, filled in once gh checks conclude
    repro_evidence: str = ""  # heal/bugfix only: failing-then-passing reproduction proof
    recalled: tuple[str, ...] = ()  # prior notes injected into this run's prompt
    review_verdict: str = ""  # the independent reviewer's verdict, verbatim
    execution_trace: str = ""  # turns/tools/tokens/exit/duration - the committed digest

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
    practices: tuple[str, ...] = ()  # practice_ids from the `practices:` frontmatter key


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
    """List items under exactly one frontmatter key.

    Frontmatter holds several lists (``tags:``, ``recalled:``, ``practices:``),
    so a blanket "every ``- item`` line belongs to me" scan would file recalled
    note names and practice ids as tags.
    """
    items: list[str] = []
    inside = False
    for line in fm.splitlines():
        if line.strip() and not line.startswith((" ", "\t", "-")):
            inside = line.strip() == f"{key}:"
            continue
        match = _TAG_RE.match(line)
        if inside and match:
            items.append(match.group(1).strip())
    return tuple(items)


def _frontmatter_value(fm: str, key: str) -> str:
    """A scalar ``key: value`` from frontmatter ("" when absent)."""
    for line in fm.splitlines():
        head, sep, tail = line.partition(":")
        if sep and head.strip() == key and not head.startswith((" ", "\t", "-")):
            return tail.strip()
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


# --- reference field notes ----------------------------------------------------

OBSERVATIONS_HEADING = "## Observations"

# One entry header, e.g. "### 2026-08-14 - `crewaiinc-crewai--docs-freeze`".
_OBSERVATION_RE = re.compile(
    r"^###\s+(\d{4}-\d{2}-\d{2})\s+-\s+`([^`]+)`\s*$", re.MULTILINE
)


@dataclass(frozen=True)
class Observation:
    """One dated, artifact-citing thing we noticed about a reference project.

    ``artifact`` is mandatory in spirit as well as in signature: an observation
    that cannot name the file, workflow, or commit it came from is folklore,
    not evidence, and G1/G2 jointly demand evidence.
    """

    practice: str  # human-readable name of the practice
    artifact: str  # the concrete thing observed: a path, a workflow, a commit convention
    what: str  # what that artifact actually does
    why: str = ""  # why it matters for this repo
    observed: str = field(default_factory=_today)
    practice_id: str = ""  # defaults to practice_id(repo, practice) at write time

    def render(self, repo: str) -> str:
        pid = self.practice_id or practice_id(repo, self.practice)
        why = f"\n- **why it matters here**: {self.why}" if self.why else ""
        return (
            f"### {self.observed} - `{pid}`\n"
            f"- **practice**: {self.practice}\n"
            f"- **artifact**: {self.artifact}\n"
            f"- **what it does**: {self.what}{why}"
        )


@dataclass
class FieldNote:
    """A reference project's durable field notes - one file, appended forever."""

    repo: str
    stars: int = 0
    license: str = ""
    snapshot_date: str = ""
    observations: tuple[Observation, ...] = ()

    def note_name(self) -> str:
        return field_note_name(self.repo)


@dataclass(frozen=True)
class FieldNoteRecord:
    """A field note as parsed back off disk - what the adoption index reads."""

    note_name: str
    repo: str
    practice_ids: tuple[str, ...] = ()
    observed_dates: tuple[str, ...] = ()


def parse_field_note(path: str | Path) -> FieldNoteRecord:
    path = Path(path)
    text = path.read_text()
    fm_match = _FRONTMATTER_RE.match(text)
    fm = fm_match.group(1) if fm_match else ""
    entries = _OBSERVATION_RE.findall(text)
    return FieldNoteRecord(
        note_name=path.stem,
        repo=_frontmatter_value(fm, "repo") or path.stem,
        # Dedupe while keeping first-seen order: the same practice legitimately
        # recurs across dated entries, but the index wants it once.
        practice_ids=tuple(dict.fromkeys(pid for _, pid in entries)),
        observed_dates=tuple(day for day, _ in entries),
    )


class KnowledgeBase:
    """Filesystem-backed knowledge base rooted at the repo."""

    def __init__(
        self,
        root: str | Path,
        *,
        lessons_dir: str = "knowledge/lessons",
        whitepapers_dir: str = "knowledge/whitepapers",
        mocs_dir: str = "knowledge/MOCs",
        reference_dir: str = "knowledge/reference",
        whitepaper_every: int = 10,
    ) -> None:
        self.root = Path(root)
        self.lessons_dir = self.root / lessons_dir
        self.whitepapers_dir = self.root / whitepapers_dir
        self.mocs_dir = self.root / mocs_dir
        self.reference_dir = self.root / reference_dir
        self.whitepaper_every = whitepaper_every
        for d in (self.lessons_dir, self.whitepapers_dir, self.mocs_dir, self.reference_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: CoreConfig, root: str | Path) -> KnowledgeBase:
        k = cfg.knowledge or {}
        return cls(
            root,
            lessons_dir=k.get("lessons_dir", "knowledge/lessons"),
            whitepapers_dir=k.get("whitepapers_dir", "knowledge/whitepapers"),
            mocs_dir=k.get("mocs_dir", "knowledge/MOCs"),
            reference_dir=k.get("reference_dir", "knowledge/reference"),
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

    def append_field_note(self, note: FieldNote) -> Path:
        """Append this mining pass's observations to a project's field note.

        **Append-only, by construction.** When the note already exists, the file
        on disk is used verbatim as the prefix of the new content: existing
        bytes are never re-rendered, so a later change to the header template
        (or to an observation's wording) can never silently rewrite history.
        Each pass adds its own dated entries - the crewAI ``[docs-freeze]``
        discipline - rather than overwriting the previous snapshot.

        A pass that observed nothing writes nothing: an empty entry would be
        noise in a log whose whole value is that every line cites an artifact.
        """
        path = self.reference_dir / f"{note.note_name()}.md"
        entries = "\n\n".join(o.render(note.repo) for o in note.observations)
        if not entries:
            return path
        if path.exists():
            prior = path.read_text()
            if not prior.endswith("\n"):
                prior += "\n"
            path.write_text(f"{prior}\n{entries}\n")
        else:
            path.write_text(f"{self._render_field_note_header(note)}\n{entries}\n")
        return path

    # --- counting -------------------------------------------------------------
    def lesson_notes(self) -> list[str]:
        return sorted(p.stem for p in self.lessons_dir.glob("*.md"))

    def whitepaper_notes(self) -> list[str]:
        return sorted(p.stem for p in self.whitepapers_dir.glob("*.md"))

    def reference_notes(self) -> list[str]:
        return sorted(p.stem for p in self.reference_dir.glob("*.md"))

    def should_write_whitepaper(self) -> bool:
        n = len(self.lesson_notes())
        return n > 0 and n % self.whitepaper_every == 0

    # --- reading ----------------------------------------------------------------
    def read_lessons(self) -> list[LessonRecord]:
        """Parse every lesson note on disk back into structured records, oldest first."""
        return [self._parse_lesson(name) for name in self.lesson_notes()]

    def _parse_lesson(self, note_name: str) -> LessonRecord:
        return parse_note(self.lessons_dir / f"{note_name}.md")

    def read_field_notes(self) -> list[FieldNoteRecord]:
        """Parse every reference field note on disk, alphabetically by repo."""
        return [
            parse_field_note(self.reference_dir / f"{name}.md")
            for name in self.reference_notes()
        ]

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
        # The practices this change adopted, addressable by id. This is what
        # turns "we looked at crewAI" into "we adopted crewAI's dated-snapshot
        # discipline, and here is the field note that recorded it".
        if lesson.practices:
            extra["practices"] = lesson.practices
        fm = self._frontmatter(tags, extra)
        refs = "\n".join(f"- `{r}`" for r in lesson.references) or "- _(none cited)_"
        practices = (
            "\n".join(f"- `{p}`" for p in lesson.practices)
            or "- _(no practice_id cited - the ticket named none)_"
        )
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

## Execution trace
{lesson.execution_trace or "_(no model run this iteration)_"}

## Independent review
{review}

## Reproduction evidence
{repro}

## References (reference-set evidence)
{refs}

### Practices adopted
{practices}
"""

    def _render_field_note_header(self, note: FieldNote) -> str:
        """The immutable head of a field note - written once, never re-rendered."""
        tags = ("reference", "field-notes")
        extra: dict[str, str | tuple[str, ...]] = {"repo": note.repo}
        if note.stars:
            extra["stars"] = str(note.stars)
        if note.license:
            extra["license"] = note.license
        extra["snapshot_date"] = note.snapshot_date or _today()
        fm = self._frontmatter(tags, extra)
        return f"""{fm}

# {note.repo} - field notes

> Part of [[Reference MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| repo | https://github.com/{note.repo} |
| stars | {note.stars or "_(not recorded)_"} |
| license | {note.license or "_(not recorded)_"} |
| snapshot | {note.snapshot_date or _today()} |

Append-only. Every mining pass adds a dated entry below; entries already here
are never rewritten, so "what did we know about this project in July?" stays
answerable. Each entry cites the artifact it came from and carries a stable
`practice_id` that tickets and lessons reference back.

{OBSERVATIONS_HEADING}
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
        notes = self.reference_notes()
        records = self.read_field_notes()
        observations = sum(len(r.observed_dates) for r in records)
        fm = self._frontmatter(("moc", "reference"), {"updated": _today()})
        links = (
            "\n".join(
                f"- [[{r.note_name}]] - {r.repo}, "
                f"{len(r.practice_ids)} practice(s) over {len(r.observed_dates)} entry(ies)"
                for r in records
            )
            or "- _No reference field notes yet - run a synthesis cycle._"
        )
        content = f"""{fm}

# Reference MOC

Up: [[Knowledge Base MOC]]

Durable field notes on the reference set (G1). One append-only note per project,
each entry dated, citing the artifact it came from, and keyed by a stable
`practice_id` that tickets and lessons cite back. Total: **{len(notes)}** note(s),
**{observations}** observation(s).

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
- [[Reference MOC]] - {n_refs} reference field note(s)

## How this is maintained
- Each PR the [[hsai]] loop opens contributes exactly one lesson.
- Every {self.whitepaper_every} lessons, a whitepaper synthesizes the themes.
- Every synthesis cycle appends dated observations to the field notes it mined.
- These MOCs are regenerated by `hsai reindex` after each iteration.
"""
        path = self.mocs_dir / "Knowledge Base MOC.md"
        path.write_text(content)
        return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> date:
    return datetime.now(timezone.utc).date()
