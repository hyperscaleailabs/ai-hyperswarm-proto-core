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

:func:`build_prior_art` widens the same index for the *planner*: besides the
vault it indexes the cost ledger's per-block aggregates and the closed/blocked
tickets fetched via ``gh``, then renders a character-capped, citable digest of
what this loop already shipped, what it already failed at, and what a block
currently costs. Every external source degrades to nothing on its own failure -
a thin prior-art section beats no synthesis at all.
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


def vault_documents(root: str | Path, cfg: CoreConfig | None = None) -> list[Document]:
    """Index documents for every Markdown note in the vault.

    Missing directories are simply skipped, so a fresh worktree (or a tmp_path
    in a test) yields an empty list rather than an error.
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
        return cls(vault_documents(root, cfg), RecallConfig.from_core(cfg))

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


# --- prior art: the planner's view of our own record ---------------------------
# `for_task` answers "what did we learn about this task?" for a worker. A planner
# needs a wider and more citable answer - "what have we already shipped, what did
# we already fail at, and what does a block cost right now?" - so the same index
# is extended with two non-Markdown sources, both optional:
#
# * the cost ledger, folded per block (an economic constraint has no note), and
# * closed and blocked tickets read through `gh` (work that is already done, or
#   already given up on, leaves no vault note until its lesson lands).
#
# Everything here is best-effort by construction: a missing ledger or an absent
# `gh` removes a source, never raises, and never blocks synthesis.

PRIOR_ART_HEADING = "Prior art from this loop's own record"
DEFAULT_PRIOR_ART_CHARS = 2500
DEFAULT_PRIOR_ART_K = 6
DEFAULT_PRIOR_ART_CLOSED_LIMIT = 30

_PRIOR_ART_PREAMBLE = (
    f"{PRIOR_ART_HEADING} - retrieved from our lessons, whitepapers, ADRs, the "
    "quota ledger, and closed/blocked tickets. This is what we already shipped, "
    "what already failed, and what a block currently costs. Every ticket you "
    "file MUST cite at least one of these refs verbatim:"
)

# Ledger aggregates carry numbers, not prose, so a query about cost would never
# match them on their own words. These terms are what a planner actually asks
# about when it reasons about spend, and they are indexed alongside the figures.
_LEDGER_INDEX_TERMS = (
    "ledger cost quota budget spend spent economics efficiency throughput "
    "tokens heavy tier iterations wall-clock ceiling pressure"
)


@dataclass(frozen=True)
class PriorArtItem:
    """One retrieved internal artifact, with the ref a ticket must cite."""

    ref: str          # "[[note-name]]" | "#123" | "`ledger:block-41339`"
    source: str       # lesson | whitepaper | adr | ledger | issue
    score: float
    excerpt: str
    detail: str = ""  # "outcome/fail", "closed", "blocked", ...

    def render(self) -> str:
        tag = f" ({self.detail})" if self.detail else ""
        return f"- {self.ref}{tag} - {self.excerpt}"


@dataclass(frozen=True)
class PriorArt:
    """What retrieval produced for the planner, plus the audit trail behind it.

    ``section`` is the only thing that reaches the prompt, and it is capped;
    ``items`` lists exactly the artifacts that survived that cap, so the audit
    trail can never claim more than was actually injected.
    """

    items: tuple[PriorArtItem, ...] = ()
    cost_pressure: str = ""
    section: str = ""

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(i.ref for i in self.items)

    def __bool__(self) -> bool:
        return bool(self.section)


def _issue_snippet(body: str, title: str, limit: int = 160) -> str:
    """One line saying what a ticket was about (its Problem, then its Proposal)."""
    sections = split_sections(body)
    for name in ("problem", "proposal"):
        line = _first_prose_line(sections.get(name, ""))
        if line:
            return _clip(line, limit)
    return _clip(_first_prose_line(body) or title, limit)


def ledger_documents(root: str | Path, cfg: CoreConfig | None = None) -> list[Document]:
    """One indexable document per block in the quota ledger (empty if unreadable)."""
    if cfg is None:
        return []
    try:
        records = ledger.read_records(ledger.ledger_path(cfg, root))
    except (OSError, ValueError):
        return []
    documents: list[Document] = []
    for block in sorted({r.block for r in records}):
        summary = ledger.aggregate_block(records, block).summary()
        documents.append(
            Document(
                note_name=f"ledger:block-{block}",
                title=f"Quota ledger, block {block}",
                outcome="unknown",
                kind="ledger",
                source="ledger",
                snippet=summary,
                tokens=tuple(tokenize(f"{_LEDGER_INDEX_TERMS} block {block} {summary}")),
            )
        )
    return documents


def issue_documents(
    cfg: CoreConfig | None = None, *, limit: int | None = None, runner: Runner = run
) -> list[Document]:
    """Closed and blocked tickets as indexable documents.

    Closed tickets are work already delivered; blocked ones are work already
    given up on. Both are prior art the planner must not silently re-propose.
    Returns ``[]`` - never raises - when `gh` is missing or answers with
    anything unparseable, so retrieval degrades instead of failing synthesis.
    """
    if cfg is None:
        return []
    limit = DEFAULT_PRIOR_ART_CLOSED_LIMIT if limit is None else limit
    try:
        closed = github.list_closed_issues(cfg.repo_slug, limit=limit, runner=runner)
        blocked = [i for i in github.list_open_issues(cfg.repo_slug, runner=runner) if i.is_blocked]
    except (OSError, ValueError):
        return []
    documents: list[Document] = []
    for issue, state in [(i, "closed") for i in closed] + [(i, "blocked") for i in blocked]:
        documents.append(
            Document(
                note_name=f"#{issue.number}",
                title=issue.title,
                outcome="unknown",
                kind=state,
                source="issue",
                snippet=_issue_snippet(issue.body, issue.title),
                tokens=tuple(tokenize(f"{issue.title}\n{issue.body}")),
            )
        )
    return documents


def cost_pressure(root: str | Path, cfg: CoreConfig | None = None) -> str:
    """The most recent block's spend, graded against ``cfg.budget``.

    This is the constraint the planner should optimise against: it says what a
    block of work has actually been costing and how close that is to the
    ceiling that halts new work. Empty string when the ledger says nothing.
    """
    if cfg is None:
        return ""
    try:
        records = ledger.read_records(ledger.ledger_path(cfg, root))
    except (OSError, ValueError):
        return ""
    if not records:
        return ""
    block = max(r.block for r in records)
    agg = ledger.aggregate_block(records, block)
    decision = ledger.evaluate_budget(agg, cfg.budget)
    ceilings = ", ".join(
        f"{key}={cfg.budget[key]}"
        for key in ("max_heavy_iterations_per_block", "max_seconds_per_block")
        if cfg.budget.get(key) is not None
    ) or "none configured"
    return (
        f"Cost pressure - latest ledger block {block}: {agg.summary()}. "
        f"Budget verdict: {decision.status} ({decision.reason}). Ceilings: {ceilings}."
    )


def _to_prior_art_item(note: RecalledNote) -> PriorArtItem:
    """Turn a ranked hit into a citable item, ref formatted per source."""
    if note.source == "issue":
        return PriorArtItem(
            ref=note.note_name, source=note.source, score=note.score,
            excerpt=note.snippet, detail=note.kind,
        )
    if note.source == "ledger":
        return PriorArtItem(
            ref=f"`{note.note_name}`", source=note.source, score=note.score,
            excerpt=note.snippet, detail="ledger",
        )
    detail = note.source if note.outcome == "unknown" else f"outcome/{note.outcome}"
    return PriorArtItem(
        ref=f"[[{note.note_name}]]", source=note.source, score=note.score,
        excerpt=note.snippet, detail=detail,
    )


def render_prior_art(
    items: list[PriorArtItem] | tuple[PriorArtItem, ...],
    budget_chars: int,
    *,
    cost: str = "",
) -> PriorArt:
    """Render at most ``budget_chars`` of prompt text, dropping whole items to fit.

    The preamble and the cost line come first because they are the framing the
    planner needs even when only one artifact survives the budget; items are
    then appended while they fit, never truncated mid-line.
    """
    if budget_chars <= 0 or len(_PRIOR_ART_PREAMBLE) > budget_chars:
        return PriorArt()
    lines = [_PRIOR_ART_PREAMBLE]
    used = len(_PRIOR_ART_PREAMBLE)
    kept_cost = ""
    if cost and used + 2 + len(cost) <= budget_chars:
        lines.extend(["", cost])
        used += 2 + len(cost)
        kept_cost = cost
    kept: list[PriorArtItem] = []
    for item in items:
        line = item.render()
        if used + 1 + len(line) > budget_chars:
            break
        lines.append(line)
        used += 1 + len(line)
        kept.append(item)
    return PriorArt(items=tuple(kept), cost_pressure=kept_cost, section="\n".join(lines))


def build_prior_art(
    query: str,
    budget_chars: int = DEFAULT_PRIOR_ART_CHARS,
    *,
    root: str | Path = ".",
    cfg: CoreConfig | None = None,
    k: int = DEFAULT_PRIOR_ART_K,
    runner: Runner = run,
) -> PriorArt:
    """Rank this loop's own artifacts against ``query`` and render them, capped.

    One BM25 index spans all four sources so their scores are comparable (the
    IDF is computed over the union, not per source). The rendered section never
    exceeds ``budget_chars``; when every source is unavailable - no vault, no
    ledger, no `gh` - the result is empty and synthesis continues without it.
    """
    documents = vault_documents(root, cfg)
    documents += ledger_documents(root, cfg)
    documents += issue_documents(cfg, runner=runner)
    if not documents:
        return PriorArt()
    hits = Corpus(documents, RecallConfig.from_core(cfg)).search(query, k)
    return render_prior_art(
        [_to_prior_art_item(h) for h in hits], budget_chars, cost=cost_pressure(root, cfg)
    )
