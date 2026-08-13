"""The knowledge base: lessons, practices, whitepapers, and Maps of Content.

Everything written here is Obsidian-ready:
- YAML frontmatter with tags,
- ``[[wikilinks]]`` between notes and up to their MOCs,
so that cloning the repo and opening it as a vault yields a connected graph.

The *practice registry* (``knowledge/practices``) is the evidence half of goal
G1: one note per practice actually observed in a reference project, naming the
artifact it was observed in and what this repo did instead. Tickets cite
practices by id (``practice:<id>``); the orchestrator threads those ids into the
PR and the lesson, so "which practice came from where" is answerable from the
vault rather than asserted.
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
_FM_SCALAR_RE = re.compile(r"^([a-z_]+):[ \t]*(\S.*)$", re.MULTILINE)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)\]\]")
# How a ticket cites a practice registry entry. Deliberately narrow: it must be
# greppable, so a citation that does not resolve is a hard, checkable error.
PRACTICE_REF_RE = re.compile(r"practice:([a-z0-9][a-z0-9-]*)")
PRACTICE_SECTION = "Practice evidence"
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


def _yaml_scalar(value: str) -> str:
    """Render a frontmatter value that stays valid YAML.

    Practice artifacts are free text a model wrote ("workflow.yml: the gate"),
    and a bare ``: `` in a plain scalar breaks the whole block - which in a vault
    means the note silently loses its tags.
    """
    text = str(value).replace("\n", " ").strip()
    if not text:
        return '""'
    if ": " in text or text.endswith(":") or text[0] in "\"'#&*?|-<>=!%@`{[":
        return '"' + text.replace('"', "'") + '"'
    return text


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def extract_practice_ids(text: str) -> tuple[str, ...]:
    """Practice ids cited as ``practice:<id>`` in a ticket or PR body.

    Order-preserving and deduped, so the evidence threaded onto a PR reads the
    way the ticket wrote it.
    """
    seen: dict[str, None] = {}
    for match in PRACTICE_REF_RE.finditer(text or ""):
        seen.setdefault(match.group(1).strip("-"), None)
    return tuple(seen)


def cited_practices(note_body: str) -> tuple[str, ...]:
    """Practice notes wikilinked from a note's ``## Practice evidence`` section."""
    section = split_sections(note_body).get(PRACTICE_SECTION.lower(), "")
    seen: dict[str, None] = {}
    for match in _WIKILINK_RE.finditer(section):
        seen.setdefault(match.group(1).strip(), None)
    return tuple(seen)


@dataclass
class Practice:
    """One practice observed in a reference project and adapted here.

    ``artifact`` is what was actually looked at - a file path, a workflow name,
    a PR or commit URL. A practice without one is a claim, not evidence.
    """

    id: str  # kebab slug; the note name and the citation key
    source_repo: str
    artifact: str
    observation: str  # what the reference project actually does
    adaptation: str  # what hsai did instead
    adopted_by: tuple[str, ...] = ()  # ticket / PR references, e.g. "#203"
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)

    def note_name(self) -> str:
        return slugify(self.id)

    def citation(self) -> str:
        return f"practice:{self.note_name()}"


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
    references: tuple[str, ...] = ()  # practice ids that actually informed the work
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


def parse_practice(path: str | Path) -> Practice:
    """Read a practice note back off disk into a :class:`Practice`."""
    path = Path(path)
    text = path.read_text()
    fm_match = _FRONTMATTER_RE.match(text)
    fm = fm_match.group(1) if fm_match else ""
    fields = {k: _unquote(v) for k, v in _FM_SCALAR_RE.findall(fm)}
    sections = split_sections(text)
    adopted = tuple(
        line.strip()[2:].strip()
        for line in sections.get("adopted by", "").splitlines()
        if line.strip().startswith("- ") and not line.strip().startswith("- _(")
    )
    return Practice(
        id=fields.get("id", path.stem).strip(),
        source_repo=fields.get("source_repo", "").strip(),
        artifact=fields.get("artifact", "").strip(),
        observation=sections.get("observation", ""),
        adaptation=sections.get("adaptation", ""),
        adopted_by=adopted,
        tags=_frontmatter_tags(fm),
        created=fields.get("created", "").strip(),
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
        practices_dir: str = "knowledge/practices",
        whitepapers_dir: str = "knowledge/whitepapers",
        mocs_dir: str = "knowledge/MOCs",
        whitepaper_every: int = 10,
    ) -> None:
        self.root = Path(root)
        self.lessons_dir = self.root / lessons_dir
        self.practices_dir = self.root / practices_dir
        self.whitepapers_dir = self.root / whitepapers_dir
        self.mocs_dir = self.root / mocs_dir
        self.whitepaper_every = whitepaper_every
        for d in (self.lessons_dir, self.practices_dir, self.whitepapers_dir, self.mocs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: CoreConfig, root: str | Path) -> KnowledgeBase:
        k = cfg.knowledge or {}
        return cls(
            root,
            lessons_dir=k.get("lessons_dir", "knowledge/lessons"),
            practices_dir=k.get("practices_dir", "knowledge/practices"),
            whitepapers_dir=k.get("whitepapers_dir", "knowledge/whitepapers"),
            mocs_dir=k.get("mocs_dir", "knowledge/MOCs"),
            whitepaper_every=int(k.get("whitepaper_every_lessons", 10)),
        )

    # --- writing --------------------------------------------------------------
    def write_lesson(self, lesson: Lesson) -> Path:
        path = self.lessons_dir / f"{lesson.note_name()}.md"
        path.write_text(self._render_lesson(lesson))
        return path

    def write_practice(self, practice: Practice) -> Path:
        """Write (or refresh) one practice registry note."""
        path = self.practices_dir / f"{practice.note_name()}.md"
        path.write_text(self._render_practice(practice))
        return path

    def write_whitepaper(self, paper: Whitepaper) -> Path:
        path = self.whitepapers_dir / f"{paper.note_name()}.md"
        path.write_text(self._render_whitepaper(paper))
        return path

    # --- counting -------------------------------------------------------------
    def lesson_notes(self) -> list[str]:
        return sorted(p.stem for p in self.lessons_dir.glob("*.md"))

    def practice_notes(self) -> list[str]:
        return sorted(p.stem for p in self.practices_dir.glob("*.md"))

    def whitepaper_notes(self) -> list[str]:
        return sorted(p.stem for p in self.whitepapers_dir.glob("*.md"))

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
        """Every practice registry note on disk, by id."""
        return [parse_practice(self.practices_dir / f"{n}.md") for n in self.practice_notes()]

    def practice_ids(self) -> set[str]:
        """The ids a ticket may legitimately cite (what the evidence guard checks)."""
        return {p.id for p in self.read_practices()} | set(self.practice_notes())

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

        practice_lines = self._practices_adopted_lines(covered)

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
{theme_lines}

## Practices adopted in this window
{practice_lines}"""

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

    def _practices_adopted_lines(self, covered: list[LessonRecord]) -> str:
        """Which registry practices the lessons in this window actually cited.

        Read off the lessons themselves, not off a config list: an unresolvable
        citation is reported as such rather than quietly rendered as evidence.
        """
        registry = {p.id: p for p in self.read_practices()}
        cited: dict[str, list[str]] = {}
        for record in covered:
            for pid in cited_practices(record.body):
                cited.setdefault(pid, []).append(record.note_name)
        if not cited:
            return "_No lesson in this window cited a practice._"
        lines = []
        for pid in sorted(cited):
            by = ", ".join(f"[[{n}]]" for n in cited[pid])
            practice = registry.get(pid)
            if practice:
                lines.append(
                    f"- [[{pid}]] - from `{practice.source_repo}` "
                    f"(`{practice.artifact}`) - cited by {by}"
                )
            else:
                lines.append(f"- `{pid}` - **not in the practice registry** - cited by {by}")
        return "\n".join(lines)

    # --- indexing -------------------------------------------------------------
    def reindex_mocs(self) -> list[Path]:
        """Rebuild the MOC files from what is currently on disk."""
        written = [
            self._write_lessons_moc(),
            self._write_practices_moc(),
            self._write_whitepapers_moc(),
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
                lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
            else:
                lines.append(f"{key}: {_yaml_scalar(value)}")
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
        # Practice ids, wikilinked: lesson -> practice -> [[Practices MOC]] is a
        # path you can walk in the vault. Nothing is invented when none was cited.
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

## {PRACTICE_SECTION}
{refs}
"""

    def _render_practice(self, practice: Practice) -> str:
        # Deduped so a note read off disk and written back keeps its shape.
        tags = tuple(
            dict.fromkeys(("practice", f"source/{slugify(practice.source_repo)}", *practice.tags))
        )
        fm = self._frontmatter(
            tags,
            {
                "id": practice.note_name(),
                "source_repo": practice.source_repo,
                "artifact": practice.artifact.replace("\n", " "),
                "created": practice.created,
            },
        )
        adopted = "\n".join(f"- {a}" for a in practice.adopted_by) or "- _(not yet adopted)_"
        return f"""{fm}

# {practice.note_name()}

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `{practice.source_repo}` |
| artifact | `{practice.artifact}` |
| cite as | `{practice.citation()}` |

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

    def _write_practices_moc(self) -> Path:
        practices = self.read_practices()
        by_repo: dict[str, list[Practice]] = {}
        for p in practices:
            by_repo.setdefault(p.source_repo or "_(source not recorded)_", []).append(p)
        groups = []
        for repo in sorted(by_repo):
            entries = "\n".join(
                f"- [[{p.note_name()}]] - `{p.artifact or '(artifact not recorded)'}`"
                for p in sorted(by_repo[repo], key=lambda p: p.note_name())
            )
            groups.append(f"## {repo}\n{entries}")
        body = "\n\n".join(groups) or "_No practice has been registered yet._"
        fm = self._frontmatter(("moc", "practices"), {"updated": _today()})
        content = f"""{fm}

# Practices MOC

Up: [[Knowledge Base MOC]]

Every practice this repo adopted from the reference set, grouped by the project
it was observed in. Tickets cite these by `practice:<id>`; the orchestrator
refuses a PR whose citation does not resolve here. Total: **{len(practices)}**.

{body}
"""
        path = self.mocs_dir / "Practices MOC.md"
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

    def _write_root_moc(self) -> Path:
        fm = self._frontmatter(("moc", "index"), {"updated": _today()})
        n_lessons = len(self.lesson_notes())
        n_practices = len(self.practice_notes())
        n_papers = len(self.whitepaper_notes())
        content = f"""{fm}

# Knowledge Base MOC

The living memory of **ai-hyperswarm-proto-core**. Open this repo as an Obsidian
vault and use the graph view to explore how lessons connect.

## Maps
- [[Lessons MOC]] - {n_lessons} lesson(s)
- [[Practices MOC]] - {n_practices} practice(s) adopted from the reference set
- [[Whitepapers MOC]] - {n_papers} whitepaper(s)

## How this is maintained
- Each PR the [[hsai]] loop opens contributes exactly one lesson.
- Each lesson cites the practices that informed it, or explicitly cites none.
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
