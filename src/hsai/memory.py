"""Retrieval memory: what this repo already learned, joined with what it cost.

:mod:`hsai.recall` answers "which notes are relevant to this task". This module
answers a different question - "how did attempts like this one actually end, and
what did they cost" - by joining each lesson with its :mod:`hsai.ledger` record
on the ticket number. A memory therefore carries both halves of a field
observation: the conclusion, and the price paid to reach it.

Three consumers:

* **workers** - :func:`for_task` renders a budget-capped prompt section where
  ``outcome/fail`` memories become explicit AVOID warnings and ``outcome/pass``
  memories become precedent (see :func:`hsai.orchestrator._task_prompt`);
* **the synthesis planner** - :func:`adopted_digest` lists practices already
  merged, so settled work is not re-proposed;
* **the architect** - ``hsai memory "<query>"``.

Scoring is TF-IDF-style term overlap over the same vocabulary the whitepaper
synthesizer uses (:data:`hsai.knowledge._WORD_RE` / ``_STOPWORDS``), with a
recency tiebreak. No embeddings and no new dependency: at a corpus of a few
dozen notes a vector store would be novelty theater, and lexical scoring stays
deterministic enough for tests to assert exact orderings.

Provenance is the write half: every merged iteration appends one line to
``knowledge/registry/practices.jsonl`` (ticket, PR, reference repos, lesson
note), so G1's claim that each improvement traces back to a field observation
becomes queryable instead of buried in prose.

Synthesis: run-llama/llama_index (an owned corpus retrieved into generation),
assafelovic/gpt-researcher (citation discipline as a first-class artifact),
FoundationAgents/MetaGPT (prior procedure carried into the next agent's context).
"""
from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ledger
from .config import CoreConfig
from .knowledge import _STOPWORDS, _WORD_RE, LessonRecord, parse_note, split_sections

DEFAULT_PRACTICES_FILE = "knowledge/registry/practices.jsonl"
DEFAULT_MAX_PROMPT_CHARS = 2000

# Score multiplier applied when a memory's outcome matches ``prefer_outcome``.
_PREFER_BOOST = 1.5

# Fields the lesson renderer writes into its summary table / sections.
_TICKET_RE = re.compile(r"^\|\s*ticket\s*\|\s*#(\d+)\s*\|", re.MULTILINE)
_PR_RE = re.compile(r"^\|\s*pull request\s*\|\s*#(\d+)\s*\|", re.MULTILINE)
_REF_RE = re.compile(r"^-\s+`([^`]+)`", re.MULTILINE)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

# Serializes appends so concurrent workers never interleave a partial line.
_PRACTICES_LOCK = threading.Lock()

HEADING = "Prior outcomes for similar work"
_PREAMBLE = (
    f"{HEADING} (this repo's own lessons, joined with what each attempt cost). "
    "AVOID lines are failures - do not repeat them:"
)
ADOPTED_INTRO = "Practices already adopted (merged) here - title, then the reference repos cited:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terms(text: str) -> frozenset[str]:
    """The scoreable vocabulary of a piece of text - the whitepaper tokenizer."""
    return frozenset({w.lower() for w in _WORD_RE.findall(text)} - _STOPWORDS)


def _date_key(created: str) -> int:
    """``YYYY-MM-DD`` as a sortable int (0 when the name carries no date)."""
    match = _DATE_RE.match(created)
    return int("".join(match.groups())) if match else 0


def _first_line(text: str) -> str:
    """The first line that reads as a sentence - not a heading, table or fence."""
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*> ").strip()
        if line and not line.startswith(("#", "|", "```", "---")):
            return " ".join(line.split())
    return ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


@dataclass(frozen=True)
class MemoryConfig:
    """The ``knowledge.memory`` block of core.yaml, with documented defaults."""

    enabled: bool = True
    k: int = 3
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    practices_file: str = DEFAULT_PRACTICES_FILE

    @classmethod
    def from_core(cls, cfg: CoreConfig | None) -> MemoryConfig:
        raw = ((cfg.knowledge if cfg else None) or {}).get("memory") or {}
        d = cls()
        return cls(
            enabled=bool(raw.get("enabled", d.enabled)),
            k=int(raw.get("k", d.k)),
            max_prompt_chars=int(raw.get("max_prompt_chars", d.max_prompt_chars)),
            practices_file=str(raw.get("practices_file", d.practices_file)),
        )


@dataclass(frozen=True)
class Record:
    """One memory: what was learned, plus what learning it cost."""

    note_name: str
    title: str
    kind: str
    outcome: str
    lesson_text: str
    references: tuple[str, ...] = ()
    ticket: int | None = None
    pr: int | None = None
    created: str = ""
    terms: frozenset[str] = frozenset()
    # Joined in from the quota ledger by ticket number; all zero/empty when the
    # ledger has no record for this ticket (an older or hand-written lesson).
    attempts: int = 0
    wall_clock_seconds: float = 0.0
    model: str = ""
    ledger_outcome: str = ""

    @property
    def is_failure(self) -> bool:
        return self.outcome == "fail"

    def cost(self) -> str:
        """Compact "what it cost" clause; empty when the ledger had no match."""
        bits: list[str] = []
        if self.attempts:
            bits.append(f"{self.attempts} attempt(s)")
        if self.wall_clock_seconds:
            bits.append(f"{self.wall_clock_seconds:.0f}s")
        if self.model:
            bits.append(f"`{self.model}`")
        return ", ".join(bits)

    def label(self) -> str:
        """Provenance shown in prompts and on the CLI: outcome, ticket, cost."""
        parts = [f"{self.outcome}/{self.kind}"]
        if self.ticket:
            parts.append(f"#{self.ticket}")
        cost = self.cost()
        if cost:
            parts.append(cost)
        return ", ".join(parts)

    def summary(self, limit: int = 160) -> str:
        return _clip(_first_line(self.lesson_text) or self.title, limit)

    def render(self) -> str:
        marker = "AVOID" if self.is_failure else "PRECEDENT"
        return f"- {marker} [[{self.note_name}]] ({self.label()}): {self.summary()}"


@dataclass(frozen=True)
class Hit:
    """One retrieved memory and the score that ranked it."""

    record: Record
    score: float


@dataclass(frozen=True)
class MemorySection:
    """Rendered prompt text plus the audit trail of what actually got injected."""

    note_names: tuple[str, ...] = ()
    section: str = ""

    def __bool__(self) -> bool:
        return bool(self.section)


def _to_record(lesson: LessonRecord, costs: dict[int, ledger.LedgerRecord]) -> Record:
    """Parse one lesson note into a memory, joined with its ledger record."""
    sections = split_sections(lesson.body)
    refs_body = next(
        (body for name, body in sections.items() if name.startswith("references")), ""
    )
    ticket_match = _TICKET_RE.search(lesson.body)
    pr_match = _PR_RE.search(lesson.body)
    ticket = int(ticket_match.group(1)) if ticket_match else None
    cost = costs.get(ticket) if ticket is not None else None
    return Record(
        note_name=lesson.note_name,
        title=lesson.title,
        kind=lesson.kind,
        outcome=lesson.outcome,
        lesson_text=lesson.lesson_text,
        references=tuple(_REF_RE.findall(refs_body)),
        ticket=ticket,
        pr=int(pr_match.group(1)) if pr_match else None,
        # Note names are date-prefixed by construction, so recency needs no
        # extra parsing - and an undated note simply sorts oldest.
        created=lesson.note_name[:10] if _DATE_RE.match(lesson.note_name) else "",
        # The conclusion and the narrative, not the whole note: a lesson's
        # boilerplate table would otherwise dominate a short query's overlap.
        terms=_terms(
            f"{lesson.title}\n{lesson.lesson_text}\n{lesson.what_happened}\n"
            f"{' '.join(lesson.tags)}"
        ),
        attempts=cost.attempts if cost else 0,
        wall_clock_seconds=cost.wall_clock_seconds if cost else 0.0,
        model=cost.model if cost else "",
        ledger_outcome=cost.outcome if cost else "",
    )


def _ledger_index(root: Path, cfg: CoreConfig | None) -> dict[int, ledger.LedgerRecord]:
    """Ledger records keyed by ticket, keeping each ticket's LAST attempt.

    The last one is the outcome that stuck; the earlier rows are the retries it
    already paid for, which ``attempts`` on that final record reports anyway.
    """
    rel = ((cfg.knowledge if cfg else None) or {}).get(
        "ledger_file", ledger.DEFAULT_LEDGER_FILE
    )
    try:
        records = ledger.read_records(root / rel)
    except (OSError, ValueError, TypeError):
        return {}
    return {r.ticket: r for r in records if r.ticket is not None}


class Corpus:
    """A TF-IDF index over the repo's own lessons, joined with the cost ledger."""

    def __init__(self, records: list[Record]) -> None:
        self.records = records
        self._df: Counter[str] = Counter()
        for record in records:
            self._df.update(record.terms)

    @classmethod
    def load(cls, root: str | Path, cfg: CoreConfig | None = None) -> Corpus:
        """Build the index from what is on disk under ``root``.

        A missing lessons directory yields an empty corpus rather than an error,
        so a fresh worktree (or a ``tmp_path`` in a test) is a supported state.
        """
        root = Path(root)
        knowledge = (cfg.knowledge if cfg else None) or {}
        lessons_dir = root / knowledge.get("lessons_dir", "knowledge/lessons")
        if not lessons_dir.is_dir():
            return cls([])
        costs = _ledger_index(root, cfg)
        return cls([_to_record(parse_note(p), costs) for p in sorted(lessons_dir.glob("*.md"))])

    def __len__(self) -> int:
        return len(self.records)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1 + len(self.records) / df) if df else 0.0

    def score(self, query: str | frozenset[str], record: Record) -> float:
        """TF-IDF-style overlap between ``query`` and one memory.

        Normalised by note length so a long, rambling lesson cannot outrank a
        short precise one, and rounded so float noise can never reorder ties.
        """
        terms = query if isinstance(query, frozenset) else _terms(query)
        overlap = terms & record.terms
        if not overlap:
            return 0.0
        return round(sum(self._idf(t) for t in overlap) / math.sqrt(len(record.terms)), 6)

    def retrieve(
        self, query: str, k: int = 3, *, prefer_outcome: str | None = None
    ) -> list[Hit]:
        """Rank memories against ``query``, best first.

        ``prefer_outcome`` up-weights one outcome - workers pass ``"fail"``,
        because the expensive knowledge is what already went wrong. Ties break
        on recency, then note name, so the ordering is total and reproducible.
        """
        terms = _terms(query)
        if not terms or k <= 0:
            return []
        hits: list[Hit] = []
        for record in self.records:
            base = self.score(terms, record)
            if base <= 0:
                continue
            if prefer_outcome and record.outcome == prefer_outcome:
                base = round(base * _PREFER_BOOST, 6)
            hits.append(Hit(record=record, score=base))
        hits.sort(key=lambda h: (-h.score, -_date_key(h.record.created), h.record.note_name))
        return hits[:k]


def clamp(text: str, max_chars: int) -> str:
    """Deterministically cut ``text`` to ``max_chars``, dropping whole lines first.

    The last resort is a hard character cut, so the return value is *always*
    within budget however long a single line turns out to be.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = len(line) + (1 if kept else 0)
        if used + cost > max_chars:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept) if kept else text[:max_chars]


def render(hits: list[Hit] | tuple[Hit, ...], max_chars: int) -> MemorySection:
    """Render at most ``max_chars`` of prompt text, dropping whole memories to fit.

    The returned section reports only the memories that survived the budget, so
    the audit trail can never claim more than was actually injected.
    """
    if not hits or max_chars <= 0 or len(_PREAMBLE) > max_chars:
        return MemorySection()
    kept: list[Record] = []
    lines = [_PREAMBLE]
    used = len(_PREAMBLE)
    for hit in hits:
        line = hit.record.render()
        if used + 1 + len(line) > max_chars:
            break
        lines.append(line)
        used += 1 + len(line)
        kept.append(hit.record)
    if not kept:
        return MemorySection()
    return MemorySection(note_names=tuple(r.note_name for r in kept), section="\n".join(lines))


def for_task(
    root: str | Path,
    cfg: CoreConfig,
    *,
    title: str,
    body: str = "",
    kind: str = "",
    exclude: tuple[str, ...] = (),
) -> MemorySection:
    """Retrieve prior outcomes for one task, ready to paste into a prompt.

    ``exclude`` drops notes another retriever already put in the same prompt, so
    the worker is never told the same thing twice.
    """
    mcfg = MemoryConfig.from_core(cfg)
    if not mcfg.enabled or mcfg.k <= 0:
        return MemorySection()
    corpus = Corpus.load(root, cfg)
    if not len(corpus):
        return MemorySection()
    # Over-fetch by the exclusion count so filtering cannot shrink the result.
    hits = corpus.retrieve(
        f"{title}\n{body}\n{kind}", mcfg.k + len(exclude), prefer_outcome="fail"
    )
    hits = [h for h in hits if h.record.note_name not in exclude][: mcfg.k]
    return render(hits, mcfg.max_prompt_chars)


# --- provenance registry ------------------------------------------------------


@dataclass
class PracticeRecord:
    """One merged practice and the evidence behind it - the unit of provenance."""

    ticket: int | None
    pr: int | None
    title: str
    reference_repos: tuple[str, ...] = ()
    lesson_note: str = ""
    note: str = ""  # the lesson's one-line conclusion
    created: str = field(default_factory=_now)

    def to_json(self) -> str:
        data = asdict(self)
        data["reference_repos"] = list(self.reference_repos)
        return json.dumps(data, sort_keys=True)


def practices_path(cfg: CoreConfig | None, repo_root: str | Path) -> Path:
    """Resolve the append-only provenance JSONL under the repo's knowledge base."""
    return Path(repo_root) / MemoryConfig.from_core(cfg).practices_file


def append_practice(path: str | Path, record: PracticeRecord) -> Path:
    """Append one record as a single JSON line (append-only, never rewrites)."""
    path = Path(path)
    line = record.to_json() + "\n"
    with _PRACTICES_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return path


def read_practices(path: str | Path) -> list[PracticeRecord]:
    """Parse every record back off disk (empty list if the registry is absent).

    Malformed lines are skipped rather than raised: this feeds a planner prompt,
    and one bad line must not be able to stop a synthesis cycle.
    """
    path = Path(path)
    if not path.exists():
        return []
    records: list[PracticeRecord] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            data["reference_repos"] = tuple(data.get("reference_repos", ()))
            records.append(PracticeRecord(**data))
        except (ValueError, TypeError):
            continue
    return records


def adopted_digest(
    cfg: CoreConfig | None, *, root: str | Path = ".", max_practices: int = 25
) -> str:
    """What this repo has already merged, with the reference repos each cited.

    Deduplicated by title (a ticket retried twice is still one practice) and
    ordered newest first, so the planner sees the current frontier at the top.
    """
    records = read_practices(practices_path(cfg, root))
    lines: list[str] = []
    seen: set[str] = set()
    for record in reversed(records):
        title = record.title.strip()
        if not title or title in seen:
            continue
        seen.add(title)
        refs = ", ".join(f"`{r}`" for r in record.reference_repos) or "_(none cited)_"
        pr = f" (PR #{record.pr})" if record.pr else ""
        lines.append(f"- {title}{pr} - {refs}")
        if len(lines) >= max_practices:
            break
    return f"{ADOPTED_INTRO}\n" + "\n".join(lines) if lines else ""
