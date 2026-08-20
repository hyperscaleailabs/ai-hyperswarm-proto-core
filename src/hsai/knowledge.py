"""The knowledge base: lessons, whitepapers, and Maps of Content (MOCs).

Everything written here is Obsidian-ready:
- YAML frontmatter with tags,
- ``[[wikilinks]]`` between notes and up to their MOCs,
so that cloning the repo and opening it as a vault yields a connected graph.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import observatory as observatory_mod
from . import practices as practices_mod
from .config import CoreConfig, ReferenceRepo
from .observatory import ObservatoryConfig

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
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)
    remote_ci: str = ""  # SUCCESS | FAILURE | TIMEOUT, filled in once gh checks conclude
    repro_evidence: str = ""  # heal/bugfix only: failing-then-passing reproduction proof
    recalled: tuple[str, ...] = ()  # prior notes injected into this run's prompt
    review_verdict: str = ""  # the independent reviewer's verdict, verbatim
    execution_trace: str = ""  # turns/tools/tokens/exit/duration - the committed digest
    # A member of hsai.postmortem.FAILURE_CLASSES, set only when outcome=="fail"
    # (empty for a pass) - mirrored into frontmatter as a `failure/<class>` tag
    # so the Obsidian graph can filter failures by cause.
    failure_class: str = ""

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
    failure_class: str = ""  # "" when absent (pass, or a note predating this field)
    created: str = ""  # frontmatter `created:`, "" for notes that carry no date


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


def _frontmatter_tags(fm: str) -> tuple[str, ...]:
    """List items under the ``tags:`` key only.

    Frontmatter now holds a second list (``recalled:``), so a blanket "every
    ``- item`` line is a tag" scan would file recalled note names as tags.
    """
    tags: list[str] = []
    in_tags = False
    for line in fm.splitlines():
        if line.strip() and not line.startswith((" ", "\t", "-")):
            in_tags = line.strip() == "tags:"
            continue
        match = _TAG_RE.match(line)
        if in_tags and match:
            tags.append(match.group(1).strip())
    return tuple(tags)


def _frontmatter_scalar(fm: str, key: str) -> str:
    """The value of a top-level scalar frontmatter key ("" when absent).

    Deliberately not a YAML parse: frontmatter here is machine-written by
    :meth:`KnowledgeBase._frontmatter`, and a one-line reader cannot fail on a
    hand-edited note the way a strict parser would.
    """
    prefix = f"{key}:"
    for line in fm.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
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
    tags = _frontmatter_tags(fm)
    outcome = next((t.split("/", 1)[1] for t in tags if t.startswith("outcome/")), "unknown")
    kind = next((t.split("/", 1)[1] for t in tags if t.startswith("kind/")), "unknown")
    failure_class = next((t.split("/", 1)[1] for t in tags if t.startswith("failure/")), "")
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
        failure_class=failure_class,
        created=_frontmatter_scalar(fm, "created"),
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


class KnowledgeBase:
    """Filesystem-backed knowledge base rooted at the repo."""

    def __init__(
        self,
        root: str | Path,
        *,
        lessons_dir: str = "knowledge/lessons",
        whitepapers_dir: str = "knowledge/whitepapers",
        mocs_dir: str = "knowledge/MOCs",
        practices_dir: str = practices_mod.PRACTICES_DIR_DEFAULT,
        whitepaper_every: int = 10,
        reference_repos: tuple[ReferenceRepo, ...] = (),
        observatory: ObservatoryConfig | None = None,
    ) -> None:
        self.root = Path(root)
        self.lessons_dir = self.root / lessons_dir
        self.whitepapers_dir = self.root / whitepapers_dir
        self.mocs_dir = self.root / mocs_dir
        self.practices_dir = self.root / practices_dir
        self.whitepaper_every = whitepaper_every
        self.reference_repos = reference_repos
        self.observatory = observatory or ObservatoryConfig()
        self.reference_dir = self.root / self.observatory.dir
        for d in (
            self.lessons_dir, self.whitepapers_dir, self.mocs_dir,
            self.practices_dir, self.reference_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: CoreConfig, root: str | Path) -> KnowledgeBase:
        k = cfg.knowledge or {}
        return cls(
            root,
            lessons_dir=k.get("lessons_dir", "knowledge/lessons"),
            whitepapers_dir=k.get("whitepapers_dir", "knowledge/whitepapers"),
            mocs_dir=k.get("mocs_dir", "knowledge/MOCs"),
            practices_dir=k.get("practices_dir", practices_mod.PRACTICES_DIR_DEFAULT),
            whitepaper_every=int(k.get("whitepaper_every_lessons", 10)),
            reference_repos=cfg.reference_top10,
            observatory=ObservatoryConfig.from_core(cfg),
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

    # --- counting -------------------------------------------------------------
    def lesson_notes(self) -> list[str]:
        return sorted(p.stem for p in self.lessons_dir.glob("*.md"))

    def whitepaper_notes(self) -> list[str]:
        return sorted(p.stem for p in self.whitepapers_dir.glob("*.md"))

    def practice_notes(self) -> list[str]:
        return sorted(p.stem for p in self.practices_dir.glob("*.md"))

    def read_practices(self) -> list[practices_mod.Practice]:
        """Parse every practice note on disk, sorted by id (see :func:`hsai.practices.load`)."""
        return [
            practices_mod.parse(self.practices_dir / f"{name}.md")
            for name in self.practice_notes()
        ]

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
        """Rebuild every derived note from what is currently on disk.

        Reference dossiers are regenerated here, alongside the MOCs and for the
        same reason: they are shared derived files, so rebuilding them once in
        the serialized maintenance step is what keeps parallel PRs from
        colliding on them.
        """
        adopted = self.adopted_index()
        return [
            self._write_lessons_moc(),
            self._write_whitepapers_moc(),
            self._write_practices_moc(),
            *self.write_reference_dossiers(adopted=adopted),
            self._write_reference_moc(adopted),
            self._write_root_moc(),
        ]

    # --- reference dossiers ---------------------------------------------------
    def adopted_index(self) -> dict[str, tuple[observatory_mod.Citation, ...]]:
        """``reference repo -> the lessons that cite it`` (see :mod:`hsai.observatory`)."""
        return observatory_mod.adopted_index(
            self.read_lessons(), [r.repo for r in self.reference_repos]
        )

    def write_reference_dossiers(
        self, *, adopted: dict[str, tuple[observatory_mod.Citation, ...]] | None = None
    ) -> list[Path]:
        """One dossier per reference project, from the digest cache + lessons.

        Offline and deterministic: the delta each dossier reports was persisted
        by ``hsai observe`` / the synthesis cycle, so regenerating without new
        data changes nothing but the ``updated:`` stamp.
        """
        adopted = self.adopted_index() if adopted is None else adopted
        return [self._write_dossier(ref, adopted) for ref in self.reference_repos]

    def _write_dossier(
        self, ref: ReferenceRepo, adopted: dict[str, tuple[observatory_mod.Citation, ...]]
    ) -> Path:
        observation = observatory_mod.read_observation(self.reference_dir, ref.repo)
        path = self.reference_dir / f"{observatory_mod.dossier_name(ref.repo)}.md"
        path.write_text(self._render_dossier(ref, observation, adopted.get(ref.repo, ())))
        return path

    @staticmethod
    def _dossier_questions(
        ref: ReferenceRepo,
        observation: observatory_mod.Observation | None,
        citations: tuple[observatory_mod.Citation, ...],
    ) -> str:
        """What this dossier does not yet answer - derived, never invented."""
        lines: list[str] = []
        if observation is None:
            lines.append(
                "- Never observed. Run `hsai observe --refresh` to record a baseline for "
                f"`{ref.repo}`."
            )
        elif observation.delta.new_commits:
            lines.append(
                f"- Do the {len(observation.delta.new_commits)} new commit subject(s) above "
                "point at a practice worth adopting?"
            )
        if not citations:
            lines.append(
                "- Nothing here has been adopted yet - which of its practices should the "
                "next synthesis cycle mine first?"
            )
        lines.append(
            "- Which of its CI workflows has no counterpart in this repo's "
            "`.github/workflows`?"
        )
        return "\n".join(lines)

    def _render_dossier(
        self,
        ref: ReferenceRepo,
        observation: observatory_mod.Observation | None,
        citations: tuple[observatory_mod.Citation, ...],
    ) -> str:
        fm = self._frontmatter(
            ("reference", "dossier", f"source/{slugify(ref.repo)}"),
            {"repo": ref.repo, "updated": _today()},
        )
        digest = observation.digest if observation else None
        observed = (
            f"{digest.fetched_at} (head `{digest.head_sha[:7] or '?'}` "
            f"on `{digest.default_branch or '?'}`)"
            if digest and digest.fetched_at
            else "_never observed_"
        )
        adopted = (
            "\n".join(f"- [[{c.note_name}]] - {c.title}" for c in citations)
            or "_Nothing adopted from this project yet._"
        )
        delta = (
            observation.delta.render(max_commits=self.observatory.delta_commits)
            if observation
            else "_No observation on record - nothing to diff against yet._"
        )
        workflows = (
            ", ".join(f"`{w}`" for w in digest.workflows) if digest and digest.workflows
            else "_(none recorded)_"
        )
        return f"""{fm}

# {ref.repo}

> Part of [[Reference Set MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| repo | `{ref.repo}` |
| rank | {ref.rank} |
| stars | {ref.stars} |
| license | {ref.license or "_(unknown)_"} |
| last observed | {observed} |
| CI workflows | {workflows} |

## What it is
{ref.note or "_(no note recorded in core.yaml)_"}

## What we have adopted
{adopted}

## What changed last cycle
{delta}

## Open questions
{self._dossier_questions(ref, observation, citations)}
"""

    def _write_reference_moc(
        self, adopted: dict[str, tuple[observatory_mod.Citation, ...]]
    ) -> Path:
        """Index of the ten dossiers, with how long ago each was observed.

        Ordered by the rank pinned in core.yaml, so the MOC reads as the
        reference set rather than as an alphabetical accident.
        """
        rows: list[str] = []
        for ref in sorted(self.reference_repos, key=lambda r: (r.rank, r.repo)):
            observation = observatory_mod.read_observation(self.reference_dir, ref.repo)
            observed = observation.digest.fetched_at[:10] if observation else "never"
            rows.append(
                f"| {ref.rank} | [[{observatory_mod.dossier_name(ref.repo)}]] | "
                f"`{ref.repo}` | {observed} | {len(adopted.get(ref.repo, ()))} |"
            )
        body = "\n".join(rows) or "| - | _(none pinned)_ | - | - | 0 |"
        fm = self._frontmatter(("moc", "reference"), {"updated": _today()})
        content = f"""{fm}

# Reference Set MOC

Up: [[Knowledge Base MOC]]

One dossier per pinned reference project: what it is, what this loop has
adopted from it, and what changed since it was last studied. Digests are cached
under `{self.observatory.dir}/` by `hsai observe` and refreshed by every
synthesis cycle.

| rank | dossier | repo | last observed | adopted |
| --- | --- | --- | --- | --- |
{body}
"""
        path = self.mocs_dir / "Reference Set MOC.md"
        path.write_text(content)
        return path

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
        # Only for failed iterations, and only a real classification - keeps a
        # passing note byte-for-byte identical to before this field existed.
        if lesson.outcome == "fail" and lesson.failure_class:
            tags = (*tags, f"failure/{lesson.failure_class}")
        extra: dict[str, str | tuple[str, ...]] = {
            "created": lesson.created,
            "iteration": str(lesson.iteration),
        }
        # Only present when retrieval actually fired, so a run with recall
        # disabled renders byte-for-byte as it did before recall existed.
        if lesson.recalled:
            extra["recalled"] = lesson.recalled
        fm = self._frontmatter(tags, extra)
        refs = "\n".join(f"- `{r}`" for r in lesson.references) or "- _(none cited)_"
        ticket = f"#{lesson.ticket}" if lesson.ticket else "_(none)_"
        pr = f"#{lesson.pr}" if lesson.pr else "_(none)_"
        repro = lesson.repro_evidence or "_(not applicable: not a heal/bugfix ticket)_"
        # Who checked the work, not just who wrote it (G2).
        review = lesson.review_verdict or "_(no independent review recorded)_"
        # Same "fail only" rule as the tag above, so a pass row set is
        # byte-for-byte unchanged (no extra line, no stray whitespace either).
        failure_row = (
            f"\n| failure class | `{lesson.failure_class}` |"
            if lesson.outcome == "fail" and lesson.failure_class
            else ""
        )
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
| remote CI | {lesson.remote_ci or "_(pending)_"} |{failure_row}

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

    def _write_practices_moc(self) -> Path:
        """Adopted-practice registry, grouped by source project.

        Deterministic on every run: :meth:`read_practices` already sorts by
        id, and the project groups are sorted here too, so `hsai reindex` run
        twice in a row on an unchanged registry produces byte-identical output.
        """
        records = self.read_practices()
        fm = self._frontmatter(("moc", "practices"), {"updated": _today()})
        if records:
            groups: dict[str, list[practices_mod.Practice]] = {}
            for p in records:
                groups.setdefault(p.source_project, []).append(p)
            sections = []
            for project in sorted(groups):
                lines = "\n".join(
                    f"- [[{p.note_name()}]] - {p.title} ({p.status})"
                    for p in sorted(groups[project], key=lambda x: x.title)
                )
                sections.append(f"### `{project}`\n{lines}")
            body = "\n\n".join(sections)
        else:
            body = "_No practices recorded yet._"
        content = f"""{fm}

# Practices MOC

Up: [[Knowledge Base MOC]]

Practices adopted (or rejected) from the reference set, grouped by source
project - the durable record behind G1's traceability claim. Total: **{len(records)}**.

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
        n_refs = len(self.reference_repos)
        content = f"""{fm}

# Knowledge Base MOC

The living memory of **ai-hyperswarm-proto-core**. Open this repo as an Obsidian
vault and use the graph view to explore how lessons connect.

## Maps
- [[Lessons MOC]] - {n_lessons} lesson(s)
- [[Whitepapers MOC]] - {n_papers} whitepaper(s)
- [[Practices MOC]] - {n_practices} practice(s)
- [[Reference Set MOC]] - {n_refs} reference project dossier(s)

## How this is maintained
- Each PR the [[hsai]] loop opens contributes exactly one lesson.
- Every {self.whitepaper_every} lessons, a whitepaper synthesizes the themes.
- Every synthesized ticket that adds or extends a practice is recorded in the
  practices registry, indexed by [[Practices MOC]].
- Each reference project keeps a dossier of what changed since it was last
  studied and what this loop has adopted from it, refreshed by `hsai observe`.
- These MOCs are regenerated by `hsai reindex` after each iteration.
"""
        path = self.mocs_dir / "Knowledge Base MOC.md"
        path.write_text(content)
        return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> date:
    return datetime.now(timezone.utc).date()
