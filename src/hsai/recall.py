"""Retrieval over the knowledge base: a pure-stdlib BM25 index of past notes.

The loop writes a lesson on every iteration, but writing is only half a memory.
This module is the read side: it ranks the notes already on disk against a query
(a ticket, or a synthesis brief) so the relevant ones can be injected into the
prompt that is about to run.

Deliberately dependency-free - standard library only, no embeddings, no vector
store, no metered API call. BM25 over a few hundred short notes is sufficient,
fast, and auditable, and it keeps the subscription-only constraint intact.

Adapted from run-llama/llama_index (index a corpus, retrieve the relevant slice
into the prompt; its node-parser discipline of clipping rather than silently
dropping an oversized leaf is why :func:`render_block` truncates in place) and
from assafelovic/gpt-researcher (retrieved evidence is fed forward into the next
reasoning step rather than discarded).

Anything with ``note_name``/``title``/``outcome``/``tags``/``lesson_text``/
``what_happened`` attributes can be indexed - :class:`hsai.knowledge.LessonRecord`
is the production case, :class:`Note` the standalone one.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_K = 3
DEFAULT_CHAR_BUDGET = 2000
HEADING = "Prior lessons from this repo's knowledge base"

# Okapi BM25 saturation / length-normalization constants (Robertson's defaults).
K1 = 1.5
B = 0.75
# Failures carry more signal than passes: a note whose outcome matches the
# caller's preference gets a modest multiplicative boost rather than a
# hard filter, so a strongly-matching pass still outranks a weak failure.
OUTCOME_BOOST = 1.25

# Field -> how many times its tokens are counted. A title term is worth three
# body terms; tags and the distilled lesson sit in between.
FIELD_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("title", 3),
    ("tags", 2),
    ("lesson_text", 2),
    ("what_happened", 1),
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "was", "were", "are",
    "has", "have", "had", "not", "but", "its", "it", "of", "to", "in", "on",
    "at", "by", "as", "is", "be", "an", "or", "a", "so", "if", "we", "our",
    "you", "your", "they", "their", "them", "then", "than", "when", "what",
    "which", "who", "how", "why", "into", "over", "under", "after", "before",
    "can", "will", "would", "should", "could", "one", "two", "all", "any",
    "each", "more", "most", "some", "such", "only", "just", "also", "very",
    "add", "added", "adding", "make", "made", "use", "used", "using", "do",
    "does", "did", "get", "got", "new", "run", "ran", "via", "per", "out",
})


def _singular(token: str) -> str:
    """Fold the trivial plural so a query for ``workflow`` matches ``workflows``."""
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords, fold plurals."""
    tokens = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        token = _singular(raw)
        if len(token) < 2 or token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


@dataclass(frozen=True)
class Note:
    """A record-shaped document, for callers that have no `LessonRecord` handy."""

    note_name: str
    title: str = ""
    outcome: str = "unknown"
    kind: str = "unknown"
    tags: tuple[str, ...] = ()
    lesson_text: str = ""
    what_happened: str = ""


def _field_text(record: Any, name: str) -> str:
    value = getattr(record, name, "") or ""
    if isinstance(value, str):
        return value
    return " ".join(str(v) for v in value)  # tags and other sequence fields


def _doc_tokens(record: Any) -> list[str]:
    tokens: list[str] = []
    for name, weight in FIELD_WEIGHTS:
        tokens.extend(tokenize(_field_text(record, name)) * weight)
    return tokens


class BM25Index:
    """Okapi BM25 over an in-memory corpus of note-shaped records."""

    def __init__(self, records: Iterable[Any]) -> None:
        self.records: list[Any] = list(records)
        self.docs: list[Counter[str]] = [Counter(_doc_tokens(r)) for r in self.records]
        self.lengths: list[int] = [sum(d.values()) for d in self.docs]
        self.avg_len: float = (sum(self.lengths) / len(self.lengths)) if self.docs else 0.0
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(doc.keys())
        n = len(self.docs)
        self.idf: dict[str, float] = {
            term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()
        }

    def __len__(self) -> int:
        return len(self.records)

    def score(self, query_terms: Iterable[str], i: int) -> float:
        doc, length = self.docs[i], self.lengths[i]
        if not length:
            return 0.0
        total = 0.0
        for term in query_terms:
            freq = doc.get(term, 0)
            if not freq:
                continue
            norm = freq + K1 * (1 - B + B * length / self.avg_len)
            total += self.idf.get(term, 0.0) * (freq * (K1 + 1)) / norm
        return total

    def search(
        self,
        query: str,
        *,
        k: int = DEFAULT_K,
        prefer_outcome: str = "fail",
    ) -> list[tuple[Any, float]]:
        """Rank the corpus against ``query``; best first, ties broken by note name."""
        terms = list(dict.fromkeys(tokenize(query)))  # dedupe, keep order
        if not terms or not self.docs or k <= 0:
            return []
        hits: list[tuple[Any, float]] = []
        for i, record in enumerate(self.records):
            score = self.score(terms, i)
            if score <= 0:
                continue
            if prefer_outcome and getattr(record, "outcome", "") == prefer_outcome:
                score *= OUTCOME_BOOST
            hits.append((record, round(score, 4)))
        hits.sort(key=lambda pair: (-pair[1], getattr(pair[0], "note_name", "")))
        return hits[:k]


def recall(
    query: str,
    records: Iterable[Any],
    *,
    k: int = DEFAULT_K,
    prefer_outcome: str = "fail",
) -> list[tuple[Any, float]]:
    """Rank ``records`` against ``query`` and return the top ``k`` (record, score)."""
    return BM25Index(records).search(query, k=k, prefer_outcome=prefer_outcome)


def one_line(text: str, limit: int = 160) -> str:
    """Collapse a note body to a single readable line."""
    flat = " ".join(text.split())
    if not flat:
        return "_(no lesson text recorded)_"
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _clip(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[: max(0, budget - 1)].rstrip() + "…"


def _render_hit(record: Any, score: float) -> str:
    summary = one_line(
        _field_text(record, "lesson_text") or _field_text(record, "what_happened")
    )
    outcome = getattr(record, "outcome", "unknown")
    return f"- [[{record.note_name}]] ({outcome}, score {score:.2f}): {summary}\n"


def render_block(
    hits: list[tuple[Any, float]], *, char_budget: int = DEFAULT_CHAR_BUDGET
) -> str:
    """Render recalled hits as a prompt block of at most ``char_budget`` chars.

    Returns "" when nothing was recalled, so callers can omit the section
    entirely rather than injecting an empty heading.
    """
    if not hits or char_budget <= 0:
        return ""
    block = (
        f"## {HEADING}\n"
        "Retrieved from knowledge/ because they rank highest against this task. "
        "Apply what they already establish; do not repeat a documented mistake.\n"
    )
    if len(block) > char_budget:
        return _clip(block, char_budget)
    for record, score in hits:
        entry = _render_hit(record, score)
        if len(block) + len(entry) <= char_budget:
            block += entry
        else:
            # llama_index node-parser discipline: an oversized leaf is clipped in
            # place, never silently dropped, so the budget is honoured without
            # pretending the note was never recalled.
            return _clip(block + entry, char_budget)
    return block
