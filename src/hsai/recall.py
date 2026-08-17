"""Lesson retrieval: make the knowledge base an INPUT, not just an output.

The vault under ``knowledge/`` and ``docs/adr`` accumulates everything the loop
has learned - but until a worker can read it, every lesson has to be re-encoded
as a hard-coded guard and the same mistakes recur. This module closes that
circle with a small, dependency-free BM25 index built on demand:

    corpus = Corpus.load(repo_root, cfg)
    notes = corpus.search("remote CI gate", k=3)

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
from dataclasses import dataclass
from pathlib import Path

from . import github, ledger
from .config import CoreConfig
from .knowledge import LessonRecord, parse_note, split_sections
from .proc import Runner, run

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
    source: str = "lesson"  # lesson | whitepaper | adr | issue | ledger
    title: str = ""
    # Citation form for sources that are not vault notes (``#123`` for an issue,
    # ``ledger/block-41335`` for a ledger aggregate). Empty means "a note", which
    # is cited as an Obsidian wikilink.
    ref: str = ""

    def label(self) -> str:
        """Compact provenance shown in prompts and on the CLI."""
        if self.outcome == "unknown":
            return self.source
        return f"{self.outcome}/{self.kind}"

    def citation(self) -> str:
        """The stable source ref a ticket must quote to cite this artifact."""
        return self.ref or f"[[{self.note_name}]]"

    def render(self) -> str:
        return f"- {self.citation()} ({self.label()}) - {self.snippet}"


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
    ref: str = ""  # see RecalledNote.ref


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


def _is_indexable(path: Path) -> bool:
    return path.stem not in _SKIP_STEMS and not path.stem.endswith(_SKIP_SUFFIXES)


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


def _to_document(path: Path, source: str) -> Document:
    record = parse_note(path)
    return Document(
        note_name=record.note_name,
        title=record.title,
        outcome=record.outcome,
        kind=record.kind,
        source=source,
        snippet=_snippet(record),
        # The whole note is indexed - a lesson's value is often in what
        # happened, not only in its one-line conclusion.
        tokens=tuple(tokenize(f"{record.title}\n{' '.join(record.tags)}\n{record.body}")),
    )


def note_documents(root: str | Path, cfg: CoreConfig | None = None) -> list[Document]:
    """Index documents for every Markdown note in the vault.

    Missing directories are simply skipped, so a fresh worktree (or a
    ``tmp_path`` in a test) yields an empty list rather than an error.
    """
    root = Path(root)
    knowledge = (cfg.knowledge if cfg else None) or {}
    governance = (cfg.governance if cfg else None) or {}
    sources = (
        ("lesson", knowledge.get("lessons_dir", "knowledge/lessons")),
        ("whitepaper", knowledge.get("whitepapers_dir", "knowledge/whitepapers")),
        ("adr", governance.get("adr_dir", "docs/adr")),
    )
    documents: list[Document] = []
    for source, rel in sources:
        directory = root / rel
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if _is_indexable(path):
                documents.append(_to_document(path, source))
    return documents


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
    def load(cls, root: str | Path, cfg: CoreConfig | None = None) -> Corpus:
        """Build the index from what is on disk under ``root``.

        Missing directories are simply skipped, so a fresh worktree (or a
        tmp_path in a test) yields an empty corpus rather than an error.
        """
        return cls(note_documents(root, cfg), RecallConfig.from_core(cfg))

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
        query_terms = set(tokenize(query))
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
                    ref=doc.ref,
                )
            )
        scored.sort(key=lambda n: (-n.score, n.note_name))
        return scored[:limit]


HEADING = "Prior lessons from this repo"
_PREAMBLE = (
    f"{HEADING} (retrieved from the knowledge base - failures first). "
    "Read them before you start; do not repeat what already failed:"
)


def render(notes: list[RecalledNote] | tuple[RecalledNote, ...], max_chars: int) -> Recall:
    """Render at most ``max_chars`` of prompt text, dropping whole notes to fit.

    The returned :class:`Recall` reports only the notes that survived the
    budget, so the audit trail can never claim more than was actually injected.
    """
    if not notes or max_chars <= 0:
        return Recall()
    if len(_PREAMBLE) > max_chars:
        return Recall()
    kept: list[RecalledNote] = []
    lines = [_PREAMBLE]
    used = len(_PREAMBLE)
    for note in notes:
        line = note.render()
        if used + 1 + len(line) > max_chars:
            break
        lines.append(line)
        used += 1 + len(line)
        kept.append(note)
    if not kept:
        return Recall()
    return Recall(notes=tuple(kept), section="\n".join(lines))


def for_task(
    root: str | Path, cfg: CoreConfig, *, title: str, body: str = "", kind: str = ""
) -> Recall:
    """Retrieve prior notes relevant to one task, ready to paste into a prompt.

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


# --- prior art: retrieval over the loop's OWN record ---------------------------
# `for_task` above answers "what should this worker read first?". The planner
# needs a wider and differently-shaped answer: not only vault notes, but also
# what shipped (closed tickets), what failed (fail lessons + blocked tickets),
# and what the block currently costs (the quota ledger). Same BM25 index, four
# sources, one hard character budget.

PRIOR_ART_HEADING = "Prior art from this loop's own record"

_PRIOR_ART_PREAMBLE = (
    f"{PRIOR_ART_HEADING} (retrieved from our lessons, whitepapers, quota ledger "
    "and closed/blocked tickets). Every ticket you file MUST quote at least one "
    "of the refs below verbatim in its `prior_art` field:"
)
_SHIPPED_HEADING = "Already shipped or decided:"
_FAILED_HEADING = "Tried and FAILED - do not re-propose blindly:"
_COST_HEADING = "Current cost pressure (quota ledger):"

_GROUP_ORDER = (
    ("shipped", _SHIPPED_HEADING),
    ("failed", _FAILED_HEADING),
    ("cost", _COST_HEADING),
)

DEFAULT_PRIOR_ART_MAX_CHARS = 2500
DEFAULT_PRIOR_ART_K = 12
DEFAULT_PRIOR_ART_LEDGER_BLOCKS = 3
DEFAULT_PRIOR_ART_CLOSED_LIMIT = 30


@dataclass(frozen=True)
class PriorArt:
    """Ranked internal evidence, rendered under a hard character budget."""

    items: tuple[RecalledNote, ...] = ()
    section: str = ""

    @property
    def refs(self) -> tuple[str, ...]:
        """The stable citation refs a filed ticket may quote."""
        return tuple(i.citation() for i in self.items)

    def __bool__(self) -> bool:
        return bool(self.section)


def _issue_document(issue: github.Issue, *, outcome: str, kind: str) -> Document:
    """Index one ticket. The title identifies it; the lead line is the excerpt."""
    lead = _first_prose_line(issue.body)
    snippet = _clip(f"{issue.title} - {lead}" if lead else issue.title, 200)
    return Document(
        note_name=f"issue-{issue.number}",
        title=issue.title,
        outcome=outcome,
        kind=kind,
        source="issue",
        snippet=snippet,
        tokens=tuple(tokenize(f"{issue.title}\n{issue.body}")),
        ref=f"#{issue.number}",
    )


def issue_documents(
    cfg: CoreConfig, *, runner: Runner = run, closed_limit: int | None = None
) -> list[Document]:
    """Closed tickets (what shipped) and blocked ones (what defeated us).

    Degrades to ``[]`` when `gh` is missing or unauthenticated - prior art is
    additive evidence, so it must never be able to abort synthesis.
    """
    if closed_limit is None:
        closed_limit = int(
            cfg.synthesis.get("prior_art_closed_limit", DEFAULT_PRIOR_ART_CLOSED_LIMIT)
        )
    try:
        closed = github.list_closed_issues(cfg.repo_slug, limit=closed_limit, runner=runner)
    except (OSError, ValueError):
        closed = []
    try:
        blocked = [i for i in github.list_open_issues(cfg.repo_slug, runner=runner) if i.is_blocked]
    except (OSError, ValueError):
        blocked = []
    return [_issue_document(i, outcome="pass", kind="closed") for i in closed] + [
        _issue_document(i, outcome="fail", kind="blocked") for i in blocked
    ]


def ledger_documents(
    root: str | Path, cfg: CoreConfig, *, blocks: int | None = None
) -> list[Document]:
    """Per-block quota aggregates, newest block first.

    An absent or half-written ledger yields ``[]`` rather than raising: the
    planner losing its cost signal is bad, losing the whole synthesis is worse.
    """
    if blocks is None:
        blocks = int(
            cfg.synthesis.get("prior_art_ledger_blocks", DEFAULT_PRIOR_ART_LEDGER_BLOCKS)
        )
    try:
        records = ledger.read_records(ledger.ledger_path(cfg, root))
    except (OSError, ValueError, TypeError):
        return []
    documents: list[Document] = []
    for block in sorted({r.block for r in records}, reverse=True)[: max(0, blocks)]:
        summary = ledger.aggregate_block(records, block).summary()
        documents.append(
            Document(
                note_name=f"ledger-block-{block}",
                title=f"Block {block} cost",
                outcome="unknown",
                kind="ledger",
                source="ledger",
                snippet=_clip(f"block {block}: {summary}", 200),
                # Seeded with the vocabulary a cost-shaped query uses, so a
                # planner asking about quota/budget retrieves these by rank.
                tokens=tuple(tokenize(f"quota cost budget spend ledger block {block} {summary}")),
                ref=f"ledger/block-{block}",
            )
        )
    return documents


def _group_of(item: RecalledNote) -> str:
    if item.source == "ledger":
        return "cost"
    return "failed" if item.outcome == "fail" else "shipped"


def render_prior_art(
    items: list[RecalledNote] | tuple[RecalledNote, ...], budget_chars: int
) -> PriorArt:
    """Render at most ``budget_chars``, grouped into shipped / failed / cost.

    Items are *selected* round-robin across the three groups so a long run of
    shipped work can never starve the failure history or the cost line, then
    *rendered* grouped so the planner reads one coherent block. Whole items are
    dropped to fit - never a truncated half-excerpt - and the returned
    :class:`PriorArt` lists exactly what survived, so the audit trail can never
    claim more than was actually injected.
    """
    if not items or budget_chars <= 0 or len(_PRIOR_ART_PREAMBLE) > budget_chars:
        return PriorArt()

    queues: dict[str, list[RecalledNote]] = {name: [] for name, _ in _GROUP_ORDER}
    for item in items:
        queues[_group_of(item)].append(item)

    chosen: dict[str, list[RecalledNote]] = {name: [] for name, _ in _GROUP_ORDER}
    used = len(_PRIOR_ART_PREAMBLE)
    progress = True
    while progress:
        progress = False
        for name, heading in _GROUP_ORDER:
            queue = queues[name]
            i = len(chosen[name])
            if i >= len(queue):
                continue
            # A line costs its own length plus the newline joining it; the first
            # item of a group also pays for the blank line and the group heading.
            cost = 1 + len(queue[i].render())
            if not chosen[name]:
                cost += 2 + len(heading)
            if used + cost > budget_chars:
                continue
            used += cost
            chosen[name].append(queue[i])
            progress = True

    lines = [_PRIOR_ART_PREAMBLE]
    kept: list[RecalledNote] = []
    for name, heading in _GROUP_ORDER:
        if not chosen[name]:
            continue
        lines += ["", heading]
        for item in chosen[name]:
            lines.append(item.render())
            kept.append(item)
    if not kept:
        return PriorArt()
    return PriorArt(items=tuple(kept), section="\n".join(lines))


def build_prior_art(
    root: str | Path,
    cfg: CoreConfig,
    *,
    query: str,
    budget_chars: int | None = None,
    k: int | None = None,
    runner: Runner = run,
) -> PriorArt:
    """Rank the loop's own evidence for ``query`` and render it under budget.

    Four sources share one BM25 index (lessons + whitepapers + ADRs, closed and
    blocked tickets, per-block ledger aggregates) so their scores are directly
    comparable. Every source degrades independently to nothing, so a missing
    `gh`, an empty vault or an absent ledger thins the section instead of
    failing synthesis.
    """
    synthesis = cfg.synthesis or {}
    if budget_chars is None:
        budget_chars = int(synthesis.get("prior_art_max_chars", DEFAULT_PRIOR_ART_MAX_CHARS))
    if k is None:
        k = int(synthesis.get("prior_art_k", DEFAULT_PRIOR_ART_K))

    ledger_docs = ledger_documents(root, cfg)
    documents = note_documents(root, cfg) + issue_documents(cfg, runner=runner) + ledger_docs
    if not documents:
        return PriorArt()

    ranked = Corpus(documents, RecallConfig.from_core(cfg)).search(query, k)
    if ledger_docs and not any(n.source == "ledger" for n in ranked):
        # Cost pressure is a CONSTRAINT, not a search hit: the planner must see
        # the newest block's spend even when the query says nothing about cost.
        newest = ledger_docs[0]
        ranked.append(
            RecalledNote(
                note_name=newest.note_name, score=0.0, outcome=newest.outcome,
                kind=newest.kind, snippet=newest.snippet, source=newest.source,
                title=newest.title, ref=newest.ref,
            )
        )
    return render_prior_art(ranked, budget_chars)
