"""The knowledge base: practices, lessons, whitepapers, and Maps of Content.

Everything written here is Obsidian-ready:
- YAML frontmatter with tags,
- ``[[wikilinks]]`` between notes and up to their MOCs,
so that cloning the repo and opening it as a vault yields a connected graph.

The *practice registry* (:class:`Practice`, ``knowledge/practices/``) is the
evidence half of goal G1. A practice note records one thing a reference project
actually does - naming the artifact where it can be seen - and what this repo did
instead. Tickets, PRs and lessons cite practices by id, and a citation that does
not resolve to a note here is caught by the orchestrator's evidence guard, so
"which practice came from where" has a machine-checkable answer rather than a
plausible-looking list of repo names.
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
# A practice id is a kebab slug of >= 2 segments, cited either as an inline
# `code` span (GitHub, which does not render wikilinks) or as a [[wikilink]]
# (the vault, where the link is the graph edge). The >= 2 requirement is what
# keeps ordinary inline code in the same section (`gh`, `pytest`, a repo slug
# like `openai/swarm`) out of the evidence trail.
_SLUG_ID = r"[a-z0-9]+(?:-[a-z0-9]+)+"
_PRACTICE_ID_RE = re.compile(rf"`({_SLUG_ID})`|\[\[({_SLUG_ID})\]\]")
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
    references: tuple[str, ...] = ()  # practice ids (see Practice) that informed the work
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

    Frontmatter holds several lists (``tags:``, ``recalled:``, ``adopted_by:``),
    so a blanket "every ``- item`` line belongs to me" scan would file recalled
    note names as tags.
    """
    items: list[str] = []
    in_key = False
    for line in fm.splitlines():
        if line.strip() and not line.startswith((" ", "\t", "-")):
            in_key = line.strip() == f"{key}:"
            continue
        match = _TAG_RE.match(line)
        if in_key and match:
            items.append(match.group(1).strip())
    return tuple(items)


def _frontmatter_scalar(fm: str, key: str) -> str:
    """The value of a single-line ``key: value`` frontmatter entry ("" if absent)."""
    for line in fm.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def _frontmatter_tags(fm: str) -> tuple[str, ...]:
    return _frontmatter_list(fm, "tags")


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
    tags = _frontmatter_tags(fm)
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
    )


# Where a ticket lists the practices it is built on, and where a lesson lists
# the ones that informed it. Both are read by :func:`cited_practice_ids`.
PRACTICES_HEADING = "Practices cited"
LESSON_REFERENCES_HEADING = "References (reference-set evidence)"


@dataclass
class Practice:
    """One practice observed in a reference project, and what hsai did with it.

    The unit of G1 evidence. ``artifact`` must point at something a reader can
    open - a file path, a workflow name, a PR or commit URL - because "MetaGPT
    does roles well" is an impression, while ``metagpt/roles/engineer.py`` is a
    citation.
    """

    id: str  # kebab slug, >= 2 segments; also the note name
    source_repo: str  # e.g. "FoundationAgents/MetaGPT"
    artifact: str  # file path, workflow name, PR or commit URL
    observation: str  # what that project actually does
    adaptation: str  # what hsai did instead
    adopted_by: tuple[str, ...] = ()  # e.g. ("ticket #203", "PR #204")
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)

    def note_name(self) -> str:
        return self.id


def parse_practice(path: str | Path) -> Practice:
    """Read a practice note back off disk - the read side of :class:`Practice`."""
    path = Path(path)
    text = path.read_text()
    fm_match = _FRONTMATTER_RE.match(text)
    fm = fm_match.group(1) if fm_match else ""
    sections = split_sections(text)
    return Practice(
        id=path.stem,
        source_repo=_frontmatter_scalar(fm, "source_repo"),
        artifact=_frontmatter_scalar(fm, "artifact"),
        observation=sections.get("observation", ""),
        adaptation=sections.get("adaptation", ""),
        adopted_by=_frontmatter_list(fm, "adopted_by"),
        # The generated tags are dropped so a note round-trips through
        # parse -> write unchanged instead of accumulating duplicates.
        tags=tuple(
            t for t in _frontmatter_tags(fm)
            if t != "practice" and not t.startswith("source/")
        ),
        created=_frontmatter_scalar(fm, "created"),
    )


def cited_practice_ids(text: str) -> tuple[str, ...]:
    """Practice ids cited by a ticket body or a lesson note, in order of appearance.

    One reader serves both because both list ids the same way: as inline `code`
    spans under a heading that says they are evidence (``## Practices cited`` on
    a ticket, ``## References ...`` on a lesson). Prose elsewhere in the note is
    ignored - a citation only counts where the note claims to be citing.
    """
    ids: list[str] = []
    for heading, body in split_sections(text).items():
        if heading != PRACTICES_HEADING.lower() and not heading.startswith("references"):
            continue
        for match in _PRACTICE_ID_RE.finditer(body):
            pid = match.group(1) or match.group(2)
            if pid not in ids:
                ids.append(pid)
    return tuple(ids)


@dataclass
class Whitepaper:
    title: str
    summary: str
    body: str
    covers_lessons: tuple[str, ...] = ()  # note names
    cites_practices: tuple[str, ...] = ()  # practice ids adopted in this window
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)

    def note_name(self) -> str:
        return f"{self.created}-{slugify(self.title)}"


class KnowledgeBase:
    """Filesystem-backed knowledge base rooted at the repo."""

    def __init__(
        self,
        root: str | Path,
        *,
        lessons_dir: str = "knowledge/lessons",
        whitepapers_dir: str = "knowledge/whitepapers",
        practices_dir: str = "knowledge/practices",
        mocs_dir: str = "knowledge/MOCs",
        whitepaper_every: int = 10,
    ) -> None:
        self.root = Path(root)
        self.lessons_dir = self.root / lessons_dir
        self.whitepapers_dir = self.root / whitepapers_dir
        self.practices_dir = self.root / practices_dir
        self.mocs_dir = self.root / mocs_dir
        self.whitepaper_every = whitepaper_every
        for d in (self.lessons_dir, self.whitepapers_dir, self.practices_dir, self.mocs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: CoreConfig, root: str | Path) -> KnowledgeBase:
        k = cfg.knowledge or {}
        return cls(
            root,
            lessons_dir=k.get("lessons_dir", "knowledge/lessons"),
            whitepapers_dir=k.get("whitepapers_dir", "knowledge/whitepapers"),
            practices_dir=k.get("practices_dir", "knowledge/practices"),
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

    def write_practice(self, practice: Practice) -> Path:
        path = self.practices_dir / f"{practice.note_name()}.md"
        path.write_text(self._render_practice(practice))
        return path

    # --- counting -------------------------------------------------------------
    def lesson_notes(self) -> list[str]:
        return sorted(p.stem for p in self.lessons_dir.glob("*.md"))

    def whitepaper_notes(self) -> list[str]:
        return sorted(p.stem for p in self.whitepapers_dir.glob("*.md"))

    def practice_notes(self) -> list[str]:
        return sorted(p.stem for p in self.practices_dir.glob("*.md"))

    def practice_ids(self) -> set[str]:
        """Every id a PR, ticket or lesson is allowed to cite as evidence."""
        return set(self.practice_notes())

    def should_write_whitepaper(self) -> bool:
        n = len(self.lesson_notes())
        return n > 0 and n % self.whitepaper_every == 0

    # --- reading ----------------------------------------------------------------
    def read_lessons(self) -> list[LessonRecord]:
        """Parse every lesson note on disk back into structured records, oldest first."""
        return [self._parse_lesson(name) for name in self.lesson_notes()]

    def _parse_lesson(self, note_name: str) -> LessonRecord:
        return parse_note(self.lessons_dir / f"{note_name}.md")

    def read_practices(self) -> list[Practice]:
        """Parse the whole practice registry back off disk, ordered by id."""
        return [
            parse_practice(self.practices_dir / f"{name}.md")
            for name in self.practice_notes()
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
            cites_practices=self._practices_cited_by(covered),
        )

    def _practices_cited_by(self, records: list[LessonRecord]) -> tuple[str, ...]:
        """Which registry practices the lessons in a window actually cited.

        Only ids that resolve to a note are kept: a whitepaper that linked to a
        practice which does not exist would break the very lesson -> practice ->
        MOC path it is meant to demonstrate.
        """
        known = self.practice_ids()
        cited = {
            pid
            for record in records
            for pid in cited_practice_ids(record.body)
            if pid in known
        }
        return tuple(sorted(cited))

    # --- indexing -------------------------------------------------------------
    def reindex_mocs(self) -> list[Path]:
        """Rebuild the MOC files from what is currently on disk."""
        written = [
            self._write_lessons_moc(),
            self._write_whitepapers_moc(),
            self._write_practices_moc(),
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
        fm = self._frontmatter(tags, extra)
        # Practice ids, wikilinked so lesson -> practice -> [[Practices MOC]] is
        # a real edge in the graph. Nothing cited stays nothing cited: an
        # invented list of repo names is worse than an honest blank.
        refs = "\n".join(f"- [[{r}]]" for r in lesson.references) or "- _(none cited)_"
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

## {LESSON_REFERENCES_HEADING}
{refs}
"""

    def _render_practice(self, practice: Practice) -> str:
        tags = ("practice", f"source/{slugify(practice.source_repo)}", *practice.tags)
        extra: dict[str, str | tuple[str, ...]] = {
            "created": practice.created,
            "source_repo": practice.source_repo,
            "artifact": practice.artifact,
        }
        if practice.adopted_by:
            extra["adopted_by"] = practice.adopted_by
        fm = self._frontmatter(tags, extra)
        adopted = "\n".join(f"- {a}" for a in practice.adopted_by) or "- _(not yet adopted)_"
        return f"""{fm}

# {practice.id}

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `{practice.source_repo}` |
| artifact | `{practice.artifact}` |

## Observation
{practice.observation}

## Adaptation
{practice.adaptation}

## Adopted by
{adopted}
"""

    def _render_whitepaper(self, paper: Whitepaper) -> str:
        tags = ("whitepaper", *paper.tags)
        fm = self._frontmatter(tags, {"created": paper.created})
        covered = "\n".join(f"- [[{n}]]" for n in paper.covers_lessons) or "- _(none)_"
        # Registry lookup, so the whitepaper says which PROJECT each adopted
        # practice came from rather than only its id.
        index = {p.id: p for p in self.read_practices()}
        adopted = "\n".join(
            f"- [[{pid}]] - `{index[pid].source_repo}`" if pid in index else f"- [[{pid}]]"
            for pid in paper.cites_practices
        ) or "_No lesson in this window cited a practice from the registry._"
        return f"""{fm}

# {paper.title}

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
{paper.summary}

{paper.body}

## Practices adopted in this window
{adopted}

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

    def _write_practices_moc(self) -> Path:
        """Index the registry by the project each practice was taken from.

        Grouping by source repo is what turns the registry into an answer to
        "what have we actually learned from MetaGPT?" - the question G1 exists
        to make answerable.
        """
        practices = self.read_practices()
        by_repo: dict[str, list[Practice]] = {}
        for practice in practices:
            by_repo.setdefault(practice.source_repo or "_(unattributed)_", []).append(practice)
        groups: list[str] = []
        for repo in sorted(by_repo):
            links = "\n".join(
                f"- [[{p.id}]] - `{p.artifact}`"
                for p in sorted(by_repo[repo], key=lambda x: x.id)
            )
            groups.append(f"### {repo}\n{links}")
        body = "\n\n".join(groups) or "_No practices recorded yet._"
        fm = self._frontmatter(("moc", "practices"), {"updated": _today()})
        content = f"""{fm}

# Practices MOC

Up: [[Knowledge Base MOC]]

Every practice this repo adopted from the reference set, grouped by the project
it came from. Tickets, PRs and lessons cite these by id; a citation that does not
resolve to a note here is refused by the orchestrator's evidence guard.
Total: **{len(practices)}** across {len(by_repo)} project(s).

{body}
"""
        path = self.mocs_dir / "Practices MOC.md"
        path.write_text(content)
        return path

    def _write_root_moc(self) -> Path:
        fm = self._frontmatter(("moc", "index"), {"updated": _today()})
        n_lessons = len(self.lesson_notes())
        n_papers = len(self.whitepaper_notes())
        n_practices = len(self.practice_notes())
        content = f"""{fm}

# Knowledge Base MOC

The living memory of **ai-hyperswarm-proto-core**. Open this repo as an Obsidian
vault and use the graph view to explore how lessons connect.

## Maps
- [[Lessons MOC]] - {n_lessons} lesson(s)
- [[Whitepapers MOC]] - {n_papers} whitepaper(s)
- [[Practices MOC]] - {n_practices} practice(s) adopted from the reference set

## How this is maintained
- Each PR the [[hsai]] loop opens contributes exactly one lesson.
- Every improvement cites the practices it came from; each id resolves to a note
  under `knowledge/practices/`.
- Every {self.whitepaper_every} lessons, a whitepaper synthesizes the themes.
- These MOCs are regenerated by `hsai reindex` after each iteration.
"""
        path = self.mocs_dir / "Knowledge Base MOC.md"
        path.write_text(content)
        return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> date:
    return datetime.now(timezone.utc).date()
