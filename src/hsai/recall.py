"""Lesson retrieval: make the knowledge base an INPUT, not just an output.

The vault under ``knowledge/`` and ``docs/adr`` accumulates everything the loop
has learned - but until a worker can read it, every lesson has to be re-encoded
as a hard-coded guard and the same mistakes recur. This module closes that
circle with a small, dependency-free BM25 index built on demand:

    corpus = Corpus.load(repo_root, cfg)
    notes = corpus.search("remote CI gate", k=3)

or, over the lessons a :class:`~hsai.knowledge.KnowledgeBase` holds:

    pack = render_pack(select(index_lessons(kb), ticket_body, k=3, max_chars=1200))

Scoring compares prose, not template: :func:`strip_boilerplate` removes the
scaffolding every note and every ticket shares before either side is tokenized,
so notes are ranked on what they concluded rather than on the fact that they
were all written by the same generator.

Two deliberate biases, both configurable under ``knowledge.recall``:

* **failures outrank successes** - a lesson tagged ``outcome/fail`` is the
  expensive knowledge, so it is up-weighted (``fail_weight``);
* **kind matches the task** - a heal worker should see heal history first
  (``kind_weight``), the scoping idea ChatDev's agent memory makes explicit.

Ranking is fully deterministic (stable tie-break on note name) and involves no
model call, so retrieval costs nothing and tests can assert exact orderings.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import CoreConfig
from .knowledge import KnowledgeBase, LessonRecord, parse_note, split_sections

# BM25 free parameters - the standard defaults; term saturation (k1) and
# length normalisation (b).
_K1 = 1.5
_B = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can do does for from had has have how in
    into is it its of on or so than that the their then there these this those
    to was were what when where which who why will with not no you your we our
    """.split()
)

# Notes that are pure indexes or scaffolding: retrieving them teaches nothing.
_SKIP_STEMS = frozenset({"TEMPLATE"})
_SKIP_SUFFIXES = (" MOC",)

# Lines that are template, not content. Every lesson this loop writes carries
# the same breadcrumb, the same metadata table, the same seven section
# headings and the same "_(none)_" placeholders; every synthesized ticket
# carries the same `- goals:`/`- size:` meta block. Scored as-is, that shared
# scaffolding is the strongest signal in the corpus and every note looks
# equally similar to every ticket. Removed before scoring, similarity is
# decided by what a note actually says.
_TEMPLATE_LINE_RE = re.compile(
    r"""^\s*(?:
        \#{1,6}\s          # section headings - the same seven in every lesson
      | >                  # the "> Part of [[Lessons MOC]]" breadcrumb
      | \|                 # metadata tables - fixed labels, values modelled as fields
      | -{3,}\s*$          # rules and frontmatter fences
      | _\(.*\)_\s*$       # "_(none cited)_", "_(pending)_", ... placeholders
      | -\s*(?:goals|size)\s*:   # a ticket's Meta block, shared by every ticket
    )""",
    re.VERBOSE,
)

# Where a note states its conclusion, best first.
_SNIPPET_SECTIONS = ("lesson learned", "summary", "decision", "what happened", "context")


@dataclass(frozen=True)
class RecallConfig:
    """The ``knowledge.recall`` block of core.yaml, with documented defaults."""

    enabled: bool = True
    k: int = 3
    max_chars: int = 1200
    fail_weight: float = 1.6
    kind_weight: float = 1.25

    @classmethod
    def from_core(cls, cfg: CoreConfig | None) -> RecallConfig:
        raw = ((cfg.knowledge if cfg else None) or {}).get("recall") or {}
        d = cls()
        return cls(
            enabled=bool(raw.get("enabled", d.enabled)),
            k=int(raw.get("k", d.k)),
            max_chars=int(raw.get("max_chars", d.max_chars)),
            fail_weight=float(raw.get("fail_weight", d.fail_weight)),
            kind_weight=float(raw.get("kind_weight", d.kind_weight)),
        )


@dataclass(frozen=True)
class RecalledNote:
    """One retrieved note, with the metadata that justified its rank."""

    note_name: str
    score: float
    outcome: str
    kind: str
    snippet: str
    source: str = "lesson"  # lesson | whitepaper | adr
    title: str = ""

    def label(self) -> str:
        """Compact provenance shown in prompts and on the CLI."""
        if self.outcome == "unknown":
            return self.source
        return f"{self.outcome}/{self.kind}"

    def render(self) -> str:
        return f"- [[{self.note_name}]] ({self.label()}) - {self.snippet}"


@dataclass(frozen=True)
class Recall:
    """What retrieval produced: the prompt text plus the audit trail behind it."""

    notes: tuple[RecalledNote, ...] = ()
    section: str = ""

    @property
    def note_names(self) -> tuple[str, ...]:
        return tuple(n.note_name for n in self.notes)

    def __bool__(self) -> bool:
        return bool(self.section)


@dataclass(frozen=True)
class Document:
    """An indexed note: metadata for weighting, tokens for scoring."""

    note_name: str
    title: str
    outcome: str
    kind: str
    source: str
    snippet: str
    tokens: tuple[str, ...]


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, minus stopwords and single characters.

    Kept looser than the whitepaper synthesizer's tokenizer on purpose: short
    domain terms like ``ci``, ``pr`` and ``gh`` are exactly what a query about
    this repo is made of.
    """
    return [
        t for t in _TOKEN_RE.findall(text.lower())
        if len(t) > 1 and t not in _STOPWORDS
    ]


def strip_boilerplate(text: str) -> str:
    """Drop the scaffolding a note (or a ticket) shares with all the others.

    Applied to BOTH sides of the comparison - the indexed note and the query -
    because the ticket a worker is handed is templated too. What survives is
    prose: the context, what happened, and the lesson itself.
    """
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() and not _TEMPLATE_LINE_RE.match(line)
    )


def is_indexable(path: Path | str) -> bool:
    """Is this note worth retrieving? Templates and MOCs teach nothing.

    Accepts a path or a bare note name, so the same rule applies whether notes
    are walked off disk or arrive already parsed.
    """
    stem = Path(path).stem
    return stem not in _SKIP_STEMS and not stem.endswith(_SKIP_SUFFIXES)


def note_sources(cfg: CoreConfig | None) -> tuple[tuple[str, str], ...]:
    """``(source kind, directory)`` pairs that make up the repo's own corpus.

    One definition shared with :mod:`hsai.retrieval`, so the recall index a
    worker reads and the prior-art index the planner reads can never drift onto
    different halves of the vault.
    """
    knowledge = (cfg.knowledge if cfg else None) or {}
    governance = (cfg.governance if cfg else None) or {}
    return (
        ("lesson", knowledge.get("lessons_dir", "knowledge/lessons")),
        ("whitepaper", knowledge.get("whitepapers_dir", "knowledge/whitepapers")),
        ("adr", governance.get("adr_dir", "docs/adr")),
    )


def _snippet(record: LessonRecord, limit: int = 160) -> str:
    """One line saying what the note concluded.

    Lessons conclude under "Lesson learned", whitepapers under "Summary", ADRs
    under "Decision" - try each in turn, then fall back to the title.
    """
    sections = split_sections(record.body)
    for name in _SNIPPET_SECTIONS:
        line = _first_prose_line(sections.get(name, ""))
        if line:
            return _clip(line, limit)
    return _clip(_first_prose_line(record.body) or record.title, limit)


def _first_prose_line(text: str) -> str:
    """The first line that reads as a sentence - not a heading, table or fence."""
    for raw in text.splitlines():
        line = _WIKILINK_RE.sub(r"\1", raw).strip().lstrip("-*> ").strip()
        if line and not line.startswith(("#", "|", "```", "---")):
            return " ".join(line.split())
    return ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def to_document(record: LessonRecord, source: str = "lesson") -> Document:
    """Index one parsed note: metadata for weighting, prose tokens for scoring.

    The whole note is indexed, minus its template - a lesson's value is often in
    what happened, not only in its one-line conclusion. Frontmatter tags are
    deliberately NOT indexed: ``lesson``, ``outcome/*`` and ``kind/*`` are on
    every note (so they discriminate nothing) and already steer ranking as
    weights rather than as terms.
    """
    return Document(
        note_name=record.note_name,
        title=record.title,
        outcome=record.outcome,
        kind=record.kind,
        source=source,
        snippet=_snippet(record),
        tokens=tuple(tokenize(f"{record.title}\n{strip_boilerplate(record.body)}")),
    )


class Corpus:
    """A BM25 index over the repo's own lessons, whitepapers and ADRs."""

    def __init__(self, documents: list[Document], recall: RecallConfig | None = None) -> None:
        self.documents = documents
        self.recall = recall or RecallConfig()
        self._df = Counter()
        for doc in documents:
            self._df.update(set(doc.tokens))
        lengths = [len(d.tokens) for d in documents]
        self._avgdl = (sum(lengths) / len(lengths)) if lengths else 0.0

    @classmethod
    def from_records(
        cls,
        records: Iterable[LessonRecord],
        cfg: CoreConfig | None = None,
        *,
        source: str = "lesson",
    ) -> Corpus:
        """Index records already parsed off disk (see :func:`index_lessons`)."""
        return cls([to_document(r, source) for r in records], RecallConfig.from_core(cfg))

    @classmethod
    def load(cls, root: str | Path, cfg: CoreConfig | None = None) -> Corpus:
        """Build the index from what is on disk under ``root``.

        Missing directories are simply skipped, so a fresh worktree (or a
        tmp_path in a test) yields an empty corpus rather than an error.
        """
        root = Path(root)
        documents: list[Document] = []
        for source, rel in note_sources(cfg):
            directory = root / rel
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                if is_indexable(path):
                    documents.append(to_document(parse_note(path), source))
        return cls(documents, RecallConfig.from_core(cfg))

    def __len__(self) -> int:
        return len(self.documents)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        if not df:
            return 0.0
        n = len(self.documents)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def _bm25(self, doc: Document, query_terms: set[str]) -> float:
        """Classic Robertson BM25 over the DISTINCT terms of the query.

        Distinct, not counted: a query is a whole ticket body here, and letting
        one word repeated eight times outweigh three precise ones made the
        ranking follow ticket boilerplate instead of ticket substance.
        """
        if not doc.tokens:
            return 0.0
        counts = Counter(doc.tokens)
        dl = len(doc.tokens)
        norm = _K1 * (1 - _B + _B * dl / self._avgdl) if self._avgdl else _K1
        score = 0.0
        for term in query_terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            score += self._idf(term) * (tf * (_K1 + 1)) / (tf + norm)
        return score

    def search(self, query: str, k: int | None = None, *, kind: str = "") -> list[RecalledNote]:
        """Rank notes against ``query``, best first.

        Ties break on ``note_name`` so the ordering is total and reproducible.
        """
        limit = self.recall.k if k is None else k
        query_terms = set(tokenize(strip_boilerplate(query)))
        if not query_terms or limit <= 0:
            return []
        scored: list[RecalledNote] = []
        for doc in self.documents:
            base = self._bm25(doc, query_terms)
            if base <= 0:
                continue
            weight = 1.0
            if doc.outcome == "fail":
                weight *= self.recall.fail_weight
            if kind and doc.kind == kind:
                weight *= self.recall.kind_weight
            scored.append(
                RecalledNote(
                    note_name=doc.note_name,
                    # Rounded so float noise can never reorder equal matches.
                    score=round(base * weight, 6),
                    outcome=doc.outcome,
                    kind=doc.kind,
                    snippet=doc.snippet,
                    source=doc.source,
                    title=doc.title,
                )
            )
        scored.sort(key=lambda n: (-n.score, n.note_name))
        return scored[:limit]


HEADING = "Known pitfalls from prior iterations"
_PREAMBLE = (
    f"{HEADING} (retrieved from this repo's knowledge base - failures first). "
    "Read them before you start; do not repeat what already failed:"
)


def fit(notes: Sequence[RecalledNote], max_chars: int) -> list[RecalledNote]:
    """The longest prefix of ``notes`` that still renders within ``max_chars``.

    Whole notes are dropped rather than one truncated: half a lesson is worse
    than no lesson, and a clipped ``[[wikilink]]`` would not resolve.
    """
    if not notes or max_chars <= 0 or len(_PREAMBLE) > max_chars:
        return []
    kept: list[RecalledNote] = []
    used = len(_PREAMBLE)
    for note in notes:
        line = note.render()
        if used + 1 + len(line) > max_chars:
            break
        used += 1 + len(line)
        kept.append(note)
    return kept


def render_pack(notes: Sequence[RecalledNote]) -> str:
    """The bounded pitfalls block a worker sees, or "" for nothing to say.

    Size is decided by :func:`fit`, never here: this renders exactly the notes
    it is given, so what a caller records as injected is what was injected.
    """
    if not notes:
        return ""
    return "\n".join([_PREAMBLE, *(n.render() for n in notes)])


def render(notes: Sequence[RecalledNote], max_chars: int) -> Recall:
    """Fit ``notes`` to the budget and render them, with the audit trail attached.

    The returned :class:`Recall` reports only the notes that survived the
    budget, so the audit trail can never claim more than was actually injected.
    """
    kept = fit(notes, max_chars)
    return Recall(notes=tuple(kept), section=render_pack(kept))


def index_lessons(kb: KnowledgeBase) -> list[LessonRecord]:
    """Every lesson in ``kb`` that is worth retrieving, oldest first.

    The read half of the knowledge base, expressed against the same
    :class:`~hsai.knowledge.KnowledgeBase` the loop writes through - so the
    corpus a worker recalls from is by construction the corpus the loop wrote.
    """
    return [r for r in kb.read_lessons() if is_indexable(r.note_name)]


def select(
    records: Iterable[LessonRecord],
    task: str,
    k: int = RecallConfig.k,
    max_chars: int = RecallConfig.max_chars,
    *,
    kind: str = "",
    cfg: CoreConfig | None = None,
) -> list[RecalledNote]:
    """Rank ``records`` against ``task`` and keep what fits the character budget.

    Returns at most ``k`` notes, and never more than :func:`render_pack` can
    render inside ``max_chars`` - the two limits a prompt budget actually cares
    about, enforced in one place.
    """
    corpus = Corpus.from_records(records, cfg)
    return fit(corpus.search(task, k, kind=kind), max_chars)


def for_task(
    root: str | Path, cfg: CoreConfig, *, title: str, body: str = "", kind: str = ""
) -> Recall:
    """Retrieve prior notes relevant to one task, ready to paste into a prompt.

    Indexes lessons, whitepapers and ADRs alike (see :func:`note_sources`) -
    broader than :func:`select`, which ranks a lessons-only record list.

    Returns an empty :class:`Recall` when recall is disabled or the corpus is
    empty - callers then render nothing at all.
    """
    rcfg = RecallConfig.from_core(cfg)
    if not rcfg.enabled:
        return Recall()
    corpus = Corpus.load(root, cfg)
    if not len(corpus):
        return Recall()
    notes = corpus.search(f"{title}\n{body}\n{kind}", rcfg.k, kind=kind)
    return render(notes, rcfg.max_chars)
