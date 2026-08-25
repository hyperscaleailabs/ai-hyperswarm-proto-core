"""The reference-practice registry: durable, cited memory of what this loop has
already learned from the reference set (G1's traceability claim, made real).

Until this module existed, "every improvement traces back to a field
observation" (G1) lived only in free-text PR prose and module docstrings -
nothing indexed it, and nothing stopped the planner from re-deriving ground
already covered (the recurring "chore: refresh reference-set snapshot and
extract one practice" ticket is the visible symptom). A :class:`Practice` is
one committed, Obsidian-ready note per studied practice: which reference
project it came from, along which ``reference_set.learn_from`` DIMENSION, and
the evidence (a commit subject, a workflow filename, a README anchor) that
proves it was observed rather than invented.

A practice moves through three states, and the state IS the audit trail:

``observed``
    :mod:`hsai.synthesis` named this repo in a filed ticket's rationale. The
    note carries the ticket number.
``adopted``
    the iteration for that ticket merged; :mod:`hsai.orchestrator` stamped the
    PR number and the lesson note onto it.
``rejected`` / ``partial``
    considered and dropped, or landed only in part - still a field observation
    worth remembering, so the planner does not re-derive it.

Two read paths close the loop. :func:`coverage_map` answers "which of the ten
pinned projects have we actually learned from, and about what?" as a repo x
dimension matrix, and :func:`least_covered` turns the thin cells of that matrix
into a targeting instruction for the next cycle's rotation. :func:`load` /
:class:`PracticeRegistry` are the whole read/write surface; :mod:`hsai.knowledge`
composes the notes into a "Practices MOC".
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PRACTICES_DIR_DEFAULT = "knowledge/practices"

# The kinds of field observation a practice can come from - core.yaml's
# ``reference_set.learn_from``, duplicated here as the fallback so this module
# stays usable without a CoreConfig (see :func:`learn_from`).
DIMENSIONS = (
    "source_code",
    "commit_history",
    "ci_cd",
    "issue_history",
    "harness_design",
    "readme",
)

# ``observed`` is what synthesis writes when it files a ticket citing a repo;
# ``adopted`` is what the orchestrator flips it to once that ticket's PR merges.
# The pair is what makes "proposed" distinguishable from "shipped".
STATUSES = ("observed", "adopted", "partial", "rejected")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def normalize_title(title: str) -> str:
    """Whitespace-collapsed, case-folded title - what the duplicate check compares."""
    return re.sub(r"\s+", " ", title.strip().lower())


def make_id(source_repo: str, title: str) -> str:
    """Stable identifier for a practice - also its note filename (deterministic:
    the same (source repo, title) pair always yields the same id, so a
    resumed backfill or a re-run of `hsai practices add` never renames a note).
    """
    return f"{_slugify(source_repo)}--{_slugify(title)}"


def _split_sections(text: str) -> dict[str, str]:
    parts = _SECTION_RE.split(text)
    sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lower()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections[heading] = body.strip()
    return sections


@dataclass(frozen=True)
class Practice:
    """One practice this loop has observed in (or adopted from) a reference project.

    ``dimension`` is one of core.yaml's ``reference_set.learn_from`` values -
    what KIND of field observation taught this, not just which project. It is
    the second axis of :func:`coverage_map`, so getting it right is what makes
    "we have never looked at anyone's issue history" a question the loop can
    answer about itself.

    ``ticket`` and ``adopted_pr`` are the join keys the slug exists to carry:
    ticket at ``observed`` time, PR (plus ``lesson_note``) at ``adopted`` time.
    """

    id: str
    title: str
    source_repo: str
    dimension: str
    evidence: str  # commit subject, workflow filename, README anchor, or URL
    adopted_pr: int | None = None
    adopted_date: str = ""
    status: str = "adopted"  # see STATUSES
    notes: str = ""
    related: tuple[str, ...] = ()  # wikilinked note names (lessons, whitepapers)
    ticket: int | None = None      # the ticket whose rationale cited this repo
    lesson_note: str = ""          # the lesson note written by the adopting iteration

    def note_name(self) -> str:
        return self.id


class DuplicatePracticeError(ValueError):
    """Raised when a (source_repo, title) pair is already registered."""


def build_practice(
    *,
    title: str,
    source_repo: str,
    dimension: str,
    evidence: str,
    status: str = "adopted",
    adopted_pr: int | None = None,
    adopted_date: str = "",
    notes: str = "",
    related: tuple[str, ...] = (),
    ticket: int | None = None,
    lesson_note: str = "",
) -> Practice:
    """Convenience constructor: derives the id, defaults the date to today."""
    return Practice(
        id=make_id(source_repo, title),
        title=title,
        source_repo=source_repo,
        dimension=dimension,
        evidence=evidence,
        adopted_pr=adopted_pr,
        adopted_date=adopted_date or _today(),
        status=status,
        notes=notes,
        related=related,
        ticket=ticket,
        lesson_note=lesson_note,
    )


def practices_dir(root: str | Path, cfg: Any = None) -> Path:
    """The registry directory, created if absent."""
    if cfg is not None:
        rel = (cfg.knowledge or {}).get("practices_dir", PRACTICES_DIR_DEFAULT)
    else:
        rel = PRACTICES_DIR_DEFAULT
    path = Path(root) / rel
    path.mkdir(parents=True, exist_ok=True)
    return path


def practice_notes(root: str | Path, cfg: Any = None) -> list[str]:
    return sorted(p.stem for p in practices_dir(root, cfg).glob("*.md"))


def render(practice: Practice) -> str:
    """Obsidian-ready markdown: YAML frontmatter + wikilinks to its MOC.

    Every field the parser needs lives in the frontmatter, so a note is
    machine-readable without prose heuristics; the table and sections below it
    exist for the human reading the vault.
    """
    fm: dict[str, Any] = {
        "tags": [
            "practice",
            f"status/{practice.status}",
            f"source/{_slugify(practice.source_repo)}",
            f"dimension/{practice.dimension}",
        ],
        "created": practice.adopted_date or _today(),
        "slug": practice.id,
        "source_repo": practice.source_repo,
        "dimension": practice.dimension,
        "status": practice.status,
        "evidence": practice.evidence,
    }
    if practice.ticket:
        fm["ticket"] = practice.ticket
    if practice.adopted_pr:
        fm["pr"] = practice.adopted_pr
    if practice.lesson_note:
        fm["lesson_note"] = practice.lesson_note
    fm_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    related = "\n".join(f"- [[{r}]]" for r in practice.related) or "- _(none linked yet)_"
    pr = f"#{practice.adopted_pr}" if practice.adopted_pr else "_(none)_"
    ticket = f"#{practice.ticket}" if practice.ticket else "_(none)_"
    lesson = f"[[{practice.lesson_note}]]" if practice.lesson_note else "_(none)_"
    return f"""---
{fm_text}
---

# {practice.title}

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `{practice.source_repo}` |
| dimension | {practice.dimension} |
| status | **{practice.status}** |
| ticket | {ticket} |
| adopted PR | {pr} |
| lesson | {lesson} |
| created | {practice.adopted_date or "_(none)_"} |

## Evidence
{practice.evidence or "_(none recorded)_"}

## Notes
{practice.notes or "_(none)_"}

## Related
{related}
"""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", False) else None
    except (TypeError, ValueError):
        return None


# What :func:`render` writes where a field is empty. Reading them back as text
# would make render->parse lossy, so the parser maps them to "" again - a note
# that round-trips is the only kind the registry can safely rewrite in place
# (which is exactly what promoting observed -> adopted does).
_PLACEHOLDERS = frozenset({"_(none)_", "_(none recorded)_", "- _(none linked yet)_"})


def _unplaceholder(text: str) -> str:
    return "" if text.strip() in _PLACEHOLDERS else text


def parse(path: str | Path) -> Practice:
    """Parse a practice note back off disk - the read-side counterpart of :func:`render`.

    Legacy keys (``practice_id``/``source_project``/``source_artifact``/
    ``adopted_pr``/``adopted_date``) are still honoured so notes written before
    the registry adopted core.yaml's own vocabulary keep loading unchanged; a
    rewrite through :func:`render` migrates them.
    """
    path = Path(path)
    text = path.read_text()
    fm_match = _FRONTMATTER_RE.match(text)
    fm: dict[str, Any] = (yaml.safe_load(fm_match.group(1)) or {}) if fm_match else {}
    body = text[fm_match.end():] if fm_match else text
    title_match = _TITLE_RE.search(text)
    title = title_match.group(1).strip() if title_match else path.stem
    sections = _split_sections(body)
    related = tuple(_WIKILINK_RE.findall(sections.get("related", "")))
    evidence = fm.get("evidence")
    if evidence is None:
        evidence = sections.get("evidence", "")
    return Practice(
        id=str(fm.get("slug") or fm.get("practice_id") or path.stem),
        title=title,
        source_repo=str(fm.get("source_repo", fm.get("source_project", ""))),
        dimension=str(fm.get("dimension", fm.get("source_artifact", ""))),
        evidence=_unplaceholder(str(evidence)),
        adopted_pr=_int_or_none(fm.get("pr", fm.get("adopted_pr"))),
        adopted_date=str(fm.get("adopted_date", fm.get("created", ""))),
        status=str(fm.get("status", "adopted")),
        notes=_unplaceholder(sections.get("notes", "")),
        related=related,
        ticket=_int_or_none(fm.get("ticket")),
        lesson_note=str(fm.get("lesson_note", "")),
    )


def load(root: str | Path, cfg: Any = None) -> list[Practice]:
    """Every practice on disk, sorted by id (deterministic - no dependence on
    filesystem iteration order, so `hsai reindex` never produces a spurious diff)."""
    d = practices_dir(root, cfg)
    return [parse(d / f"{name}.md") for name in practice_notes(root, cfg)]


def is_duplicate(practices: list[Practice], source_repo: str, title: str) -> Practice | None:
    """Would `(source_repo, title)` duplicate an existing entry?

    Keyed on normalized title (whitespace-collapsed, case-folded) and
    case-insensitive source repo, so "LangChain" vs "langchain-ai/langchain"
    with an identically-worded title cannot both be filed. Returns the
    conflicting record, or ``None``.
    """
    norm_title = normalize_title(title)
    norm_repo = source_repo.strip().lower()
    for p in practices:
        if p.source_repo.strip().lower() == norm_repo and normalize_title(p.title) == norm_title:
            return p
    return None


def append(root: str | Path, practice: Practice, *, cfg: Any = None) -> Path:
    """Write `practice` as a new note - refuses a (source_repo, title) duplicate."""
    existing = load(root, cfg)
    dup = is_duplicate(existing, practice.source_repo, practice.title)
    if dup is not None:
        raise DuplicatePracticeError(
            f"a practice for source_repo={practice.source_repo!r} "
            f"title={practice.title!r} is already recorded as [[{dup.note_name()}]]"
        )
    path = practices_dir(root, cfg) / f"{practice.note_name()}.md"
    path.write_text(render(practice))
    return path


class PracticeRegistry:
    """Directory-backed view of ``knowledge/practices/``.

    Everything here is one repo root plus the note format; there is no index
    file to keep in step and no state beyond what is on disk, so two workers
    writing different practices never conflict on a shared artifact (the same
    reason lessons are one-file-per-note).
    """

    def __init__(self, root: str | Path, cfg: Any = None) -> None:
        self.root = Path(root)
        self.cfg = cfg

    @property
    def dir(self) -> Path:
        return practices_dir(self.root, self.cfg)

    def read(self) -> list[Practice]:
        """Every practice on disk, sorted by id."""
        return load(self.root, self.cfg)

    def get(self, practice_id: str) -> Practice | None:
        path = self.dir / f"{practice_id}.md"
        return parse(path) if path.is_file() else None

    def write(self, practice: Practice) -> Path:
        """Create or overwrite `practice`'s note - the idempotent write path.

        Unlike :func:`append` this does not refuse a duplicate: re-writing the
        same (repo, title) is exactly how a practice is promoted from
        ``observed`` to ``adopted``.
        """
        path = self.dir / f"{practice.note_name()}.md"
        path.write_text(render(practice))
        return path

    def append(self, practice: Practice) -> Path:
        """Write a NEW practice, refusing a (source_repo, title) duplicate."""
        return append(self.root, practice, cfg=self.cfg)

    def for_ticket(self, ticket: int) -> list[Practice]:
        """Every practice filed against `ticket` - the join synthesis set up."""
        return [p for p in self.read() if p.ticket == ticket]

    def adopt(
        self, ticket: int, *, pr: int, lesson_note: str = "", date: str = ""
    ) -> list[Practice]:
        """Flip `ticket`'s observed practices to ``adopted``, stamping PR + lesson.

        Only ``observed`` notes move: a practice already adopted (or explicitly
        rejected) keeps the PR that first proved it, so re-running an iteration
        can never rewrite settled provenance.
        """
        adopted: list[Practice] = []
        for practice in self.for_ticket(ticket):
            if practice.status != "observed":
                continue
            updated = replace(
                practice,
                status="adopted",
                adopted_pr=pr,
                adopted_date=date or _today(),
                lesson_note=lesson_note or practice.lesson_note,
                related=tuple(dict.fromkeys(practice.related + ((lesson_note,) if lesson_note else ()))),
            )
            self.write(updated)
            adopted.append(updated)
        return adopted


# --- the reference set: what we could have learned, vs what we did ----------

def learn_from(cfg: Any) -> tuple[str, ...]:
    """core.yaml's ``reference_set.learn_from`` dimensions, or the defaults."""
    ref = (getattr(cfg, "raw", None) or {}).get("reference_set") or {}
    dims = tuple(str(d) for d in (ref.get("learn_from") or []))
    return dims or DIMENSIONS


def reference_repos(cfg: Any) -> tuple[str, ...]:
    """The pinned reference set, in rank order."""
    return tuple(r.repo for r in cfg.reference_top10)


# Bare project names too generic to count as a citation on their own: this repo
# calls ITSELF an "AI-swarm harness", so the word "swarm" in a rationale is not
# evidence that openai/swarm was studied. Its full slug still counts.
_GENERIC_NAMES = frozenset({"swarm"})


def _aliases(repo: str) -> tuple[str, ...]:
    """How a rationale may legitimately name `repo`: full slug, or bare name."""
    slug = repo.strip().lower()
    name = slug.rsplit("/", 1)[-1]
    if name in _GENERIC_NAMES or len(name) < 4:
        return (slug,)
    return (slug, name)


def repos_named_in(text: str, cfg: Any) -> tuple[str, ...]:
    """Which PINNED repos this text actually names, in rank order.

    The evidence gate behind :func:`hsai.synthesis.parse_ticket_specs`: a
    rationale that claims to combine three projects but names none of the ten
    in ``reference_set.top10`` is unfalsifiable prose, not a citation. Matching
    is word-bounded and case-insensitive, and accepts either the full
    ``owner/name`` slug or the bare project name (see :data:`_GENERIC_NAMES`).
    """
    haystack = (text or "").lower()
    named: list[str] = []
    for repo in reference_repos(cfg):
        for alias in _aliases(repo):
            if re.search(rf"(?<![\w/-]){re.escape(alias)}(?![\w-])", haystack):
                named.append(repo)
                break
    return tuple(named)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def citation_for(text: str, repo: str) -> str:
    """The sentence of `text` that names `repo` - a practice note's evidence anchor.

    Keeping the sentence rather than the whole rationale is what makes the note
    reviewable: the reader sees the specific claim made about THIS project, not
    a paragraph about four of them.
    """
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        lowered = sentence.lower()
        for alias in _aliases(repo):
            if re.search(rf"(?<![\w/-]){re.escape(alias)}(?![\w-])", lowered):
                return sentence.strip()
    return ""


# Which learn_from dimension a piece of evidence came from, in precedence
# order: the most specific artifact wins, so "the sync-docs.yml workflow" is
# ci_cd rather than source_code even though a workflow is also a file in the
# tree. Falls through to source_code, the dimension you get by reading a repo.
_DIMENSION_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ci_cd", ("workflow", ".yml", ".yaml", "github action", "ci/cd", "ci pipeline", "ci gate")),
    ("commit_history", ("commit", "changelog", "git log", "git history")),
    ("issue_history", ("issue tracker", "issue history", "bug report", "issue thread")),
    ("readme", ("readme", "docs page", "documentation site")),
    ("harness_design", ("harness", "orchestrat", "architecture", "sop", "design doc")),
)


def _cue_pattern(cue: str) -> re.Pattern[str]:
    """A cue matched at a word START, and - for short cues - at a word END too.

    Every cue must begin a word, so ``sop`` cannot fire inside "aesop". Only
    cues shorter than five characters must also END one, which is what stops
    ``sop`` matching "sophisticated" while still letting the longer cues match
    their own inflections ("commit" -> "commits", "orchestrat" ->
    "orchestration"). Boundaries are asserted only on ends that are word
    characters, so ``.yml`` and ``ci/cd`` keep matching where they appear.
    """
    left = r"(?<!\w)" if cue[:1].isalnum() else ""
    right = r"(?!\w)" if (len(cue) < 5 and cue[-1:].isalnum()) else ""
    return re.compile(left + re.escape(cue) + right)


_DIMENSION_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (dimension, tuple(_cue_pattern(c) for c in cues)) for dimension, cues in _DIMENSION_CUES
)


def infer_dimension(text: str) -> str:
    """Best-effort ``learn_from`` dimension for a free-text piece of evidence.

    Deterministic and cue-based rather than model-driven: the coverage matrix
    is only useful if the same rationale always lands in the same cell.
    """
    haystack = (text or "").lower()
    for dimension, patterns in _DIMENSION_PATTERNS:
        if any(p.search(haystack) for p in patterns):
            return dimension
    return "source_code"


def _records(registry: PracticeRegistry | Sequence[Practice]) -> list[Practice]:
    """Accept a registry or an already-read list - coverage is pure either way."""
    if isinstance(registry, PracticeRegistry):
        return registry.read()
    return list(registry)


def coverage_map(
    cfg: Any, registry: PracticeRegistry | Sequence[Practice]
) -> dict[str, dict[str, int]]:
    """A repo x dimension count matrix over the WHOLE pinned reference set.

    Every pinned repo and every ``learn_from`` dimension appears, zeros
    included - the zeros are the point, since an absent row is exactly the
    "we have never looked at this project" answer the map exists to give.
    Practices citing an unpinned repo are ignored: the map describes coverage
    of the pinned set, not of everything ever noted.
    """
    dims = learn_from(cfg)
    matrix = {repo: dict.fromkeys(dims, 0) for repo in reference_repos(cfg)}
    for practice in _records(registry):
        row = matrix.get(practice.source_repo)
        if row is not None and practice.dimension in row:
            row[practice.dimension] += 1
    return matrix


def coverage_order(
    cfg: Any, registry: PracticeRegistry | Sequence[Practice]
) -> list[str]:
    """Pinned repos from thinnest coverage to richest.

    Ties break on the repo's position in ``reference_set.top10`` (its rank), so
    the order is a pure function of (registry contents, core.yaml) with no
    dependence on dict or filesystem iteration order.
    """
    matrix = coverage_map(cfg, registry)
    ranked = {repo: i for i, repo in enumerate(reference_repos(cfg))}
    return sorted(matrix, key=lambda repo: (sum(matrix[repo].values()), ranked[repo]))


def least_covered(
    cfg: Any, registry: PracticeRegistry | Sequence[Practice], k: int = 3
) -> list[str]:
    """The `k` pinned repos this loop has learned the least from."""
    return coverage_order(cfg, registry)[: max(0, k)]


def render_coverage(cfg: Any, registry: PracticeRegistry | Sequence[Practice]) -> str:
    """The coverage matrix as a fixed-width table - what `hsai practices --coverage` prints."""
    dims = learn_from(cfg)
    matrix = coverage_map(cfg, registry)
    repo_width = max([len("repo")] + [len(r) for r in matrix])
    widths = {d: max(len(d), 3) for d in dims}
    header = "  ".join([f"{'repo':<{repo_width}}"] + [f"{d:>{widths[d]}}" for d in dims])
    lines = [header, "-" * len(header)]
    for repo, row in matrix.items():
        lines.append(
            "  ".join(
                [f"{repo:<{repo_width}}"] + [f"{row.get(d, 0):>{widths[d]}}" for d in dims]
            )
        )
    total = sum(sum(row.values()) for row in matrix.values())
    lines.append("")
    lines.append(f"{len(matrix)} pinned repo(s) x {len(dims)} dimension(s); {total} practice(s)")
    return "\n".join(lines)


# --- prompt rendering ------------------------------------------------------

ADOPTED_HEADING = "Already adopted - do NOT re-propose"
TARGETING_HEADING = "Least-covered areas - aim here"


def render_adopted_section(practices: Iterable[Practice]) -> str:
    """The block injected into the synthesis prompt: what NOT to re-propose.

    Rejected practices are shown too (rejecting an idea is still a field
    observation worth remembering), but each carries its status so the
    planner can tell "already shipped" from "already tried and dropped".
    """
    records = list(practices)
    if not records:
        return "_(no practices recorded yet - this is an early cycle)_"
    lines = [
        f"- **{p.title}** (from `{p.source_repo}`, {p.dimension}, "
        f"status: {p.status}) [id: `{p.id}`] - {p.evidence or 'no evidence recorded'}"
        for p in records
    ]
    return "\n".join(lines)


def render_targeting_section(
    cfg: Any, registry: PracticeRegistry | Sequence[Practice], k: int = 3
) -> str:
    """Where the planner should look next: the thinnest cells of the matrix.

    Names the missing DIMENSIONS per repo, not just the repo - "we have never
    read anyone's issue history" is a more actionable instruction than "study
    microsoft/JARVIS".
    """
    matrix = coverage_map(cfg, registry)
    lines = []
    for repo in least_covered(cfg, registry, k):
        row = matrix.get(repo, {})
        missing = [d for d, n in row.items() if n == 0]
        gap = ", ".join(missing) if missing else "no untouched dimension left"
        lines.append(f"- `{repo}` ({sum(row.values())} practice(s)) - unexplored: {gap}")
    return "\n".join(lines) or "_(no reference repos pinned)_"
