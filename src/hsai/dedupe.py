"""Near-duplicate detection between a synthesized candidate and known tickets.

The planner is stateless: nothing stopped it from re-proposing work that already
shipped, or work an architect already rejected. This module is the memory's
enforcement half - a deterministic similarity score between a
:class:`~hsai.tickets.TicketSpec` and every ticket this repo has ever opened.

The score is intentionally boring: a Jaccard overlap of normalized title tokens
blended with the same overlap over acceptance criteria. No embeddings, no model
call - synthesis already spends heavy-tier quota, and a dedupe gate that needs
its own inference is a gate that can fail in ways nobody can reproduce. The same
inputs always yield the same verdict, which is what makes a skip auditable.

Three bands, and the top one is the only one that stops anything:

- ``>= skip`` - a restatement of known work. Not filed; reported in
  ``SynthesisResult.skipped`` with the issue it matched.
- ``>= flag`` - plausibly overlapping. Filed unchanged, but labeled
  ``possible-duplicate`` with a backlink so the architect can judge.
- below both - new ground. Filed as-is.

Nothing here ever closes or edits an existing ticket. The gate withholds a
*new* write; it never mutates the backlog, so a false positive costs one idea,
never an open ticket.

Synthesis: run-llama/llama_index's issue_classifier (classify and route an
incoming issue before a human spends attention on it), assafelovic/gpt-
researcher's source curation (deduplicate retrieved context *before* it drives
downstream work), and OpenBMB/ChatDev's persistent, independently-scoped agent
memory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import CoreConfig
from .github import Issue
from .tickets import TicketSpec

# Conventional-commit style prefixes carry no topical signal: every synthesized
# ticket starts with one, so leaving them in inflates every pairwise score.
_PREFIX_RE = re.compile(
    r"^(feat|fix|bugfix|refactor|skill|chore|docs|test|tests|ci|perf|build|heal)"
    r"(\([^)]*\))?\s*:\s*",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ACCEPTANCE_SECTION = re.compile(
    r"^#{2,3}\s*acceptance criteria\s*$(.*?)(?=^#{1,3}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_CHECKBOX_ITEM = re.compile(r"^\s*-\s*\[[ xX]?\]\s*(\S.*)$", re.MULTILINE)

# Function words plus the vocabulary every ticket in this repo shares ("hsai",
# "loop", "ticket"). Both kinds match everything, so neither discriminates.
_STOPWORDS = frozenset(
    """
    a an the and or but for nor so yet of to in on at by with from into over
    under via per as is are was were be been being it its this that these those
    which when while where how why what who whom than then there here not no
    all any some each every both either neither only just also more most less
    least very much many such same other another new old add adds added adding
    make makes made use uses used using get gets got have has had do does did
    can could should would will shall may might must about across after before
    between during without within against toward towards up down out off again
    hsai repo repos loop harness ticket tickets issue issues pr prs change
    changes support supports
    """.split()
)
# Below this a token is noise ("a", "up", "on") even when not a stopword.
_MIN_TOKEN_LEN = 3

# Default bands. Tuned so a paraphrase of a shipped ticket lands above `skip`
# while two tickets that merely share this repo's vocabulary stay below `flag`.
SKIP_THRESHOLD = 0.70
FLAG_THRESHOLD = 0.45

# How much of the score the title carries when both sides have criteria. Titles
# are the most deliberate signal a planner emits; criteria break near-ties.
_TITLE_WEIGHT = 0.65

DUPLICATE_LABEL = "possible-duplicate"

# Verdict actions, in escalating order of intervention.
FILE = "file"
FLAG = "flag"
SKIP = "skip"


@dataclass(frozen=True)
class Thresholds:
    """The two bands that separate file / flag / skip."""

    skip: float = SKIP_THRESHOLD
    flag: float = FLAG_THRESHOLD

    @classmethod
    def from_config(cls, cfg: CoreConfig) -> Thresholds:
        s = cfg.synthesis or {}
        return cls(
            skip=float(s.get("dedupe_skip_threshold", SKIP_THRESHOLD)),
            flag=float(s.get("dedupe_flag_threshold", FLAG_THRESHOLD)),
        )


@dataclass(frozen=True)
class KnownTicket:
    """A ticket the repo already knows about - open, closed, or filed moments ago."""

    number: int
    title: str
    criteria: tuple[str, ...] = ()
    state: str = "open"

    @classmethod
    def from_issue(cls, issue: Issue, *, state: str = "open") -> KnownTicket:
        return cls(
            number=issue.number,
            title=issue.title,
            criteria=acceptance_criteria(issue.body),
            state=state,
        )

    @classmethod
    def from_spec(cls, spec: TicketSpec, number: int, *, state: str = "open") -> KnownTicket:
        return cls(
            number=number, title=spec.title, criteria=spec.acceptance_criteria, state=state
        )

    def reference(self) -> str:
        return f"#{self.number} ({self.state})"


@dataclass(frozen=True)
class Verdict:
    """What to do with one candidate, and why."""

    action: str  # FILE | FLAG | SKIP
    score: float = 0.0
    matched: KnownTicket | None = None

    @property
    def is_skip(self) -> bool:
        return self.action == SKIP

    @property
    def is_flag(self) -> bool:
        return self.action == FLAG

    def explain(self) -> str:
        if self.matched is None:
            return "no comparable ticket in repo memory"
        return (
            f"{self.score:.2f} similar to #{self.matched.number} "
            f"({self.matched.state}) - {self.matched.title}"
        )


def acceptance_criteria(body: str) -> tuple[str, ...]:
    """Pull the checkbox lines out of a rendered ticket's acceptance section."""
    match = _ACCEPTANCE_SECTION.search(body or "")
    if not match:
        return ()
    return tuple(m.group(1).strip() for m in _CHECKBOX_ITEM.finditer(match.group(1)))


def tokenize(text: str) -> frozenset[str]:
    """Normalize free text into the comparable token set the score works on."""
    stripped = _PREFIX_RE.sub("", (text or "").strip())
    return frozenset(
        t
        for t in _TOKEN_RE.findall(stripped.lower())
        if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    )


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Overlap of two token sets in [0, 1] (0 when either side is empty)."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def similarity(spec: TicketSpec, known: KnownTicket) -> float:
    """Deterministic similarity in [0, 1] between a candidate and a known ticket.

    Titles carry most of the weight; acceptance criteria break near-ties. When
    either side has no criteria to compare - a chore ticket, or an issue whose
    body predates the structured schema - the title decides alone rather than
    being diluted by a guaranteed-zero second term.
    """
    title_score = jaccard(tokenize(spec.title), tokenize(known.title))
    candidate_criteria = tokenize(" ".join(spec.acceptance_criteria))
    known_criteria = tokenize(" ".join(known.criteria))
    if not candidate_criteria or not known_criteria:
        return title_score
    criteria_score = jaccard(candidate_criteria, known_criteria)
    return _TITLE_WEIGHT * title_score + (1.0 - _TITLE_WEIGHT) * criteria_score


def best_match(spec: TicketSpec, known: list[KnownTicket]) -> tuple[float, KnownTicket | None]:
    """The highest-scoring known ticket (ties broken by lowest issue number)."""
    best: tuple[float, KnownTicket | None] = (0.0, None)
    for candidate in known:
        score = similarity(spec, candidate)
        if score > best[0] or (
            best[1] is not None and score == best[0] and candidate.number < best[1].number
        ):
            best = (score, candidate)
    return best


def classify(
    spec: TicketSpec, known: list[KnownTicket], *, thresholds: Thresholds | None = None
) -> Verdict:
    """Decide whether to file, flag, or skip one candidate."""
    th = thresholds or Thresholds()
    score, matched = best_match(spec, known)
    if matched is None:
        return Verdict(action=FILE)
    if score >= th.skip:
        return Verdict(action=SKIP, score=score, matched=matched)
    if score >= th.flag:
        return Verdict(action=FLAG, score=score, matched=matched)
    return Verdict(action=FILE, score=score, matched=matched)


def annotate_body(body: str, verdict: Verdict) -> str:
    """Prepend the possible-duplicate backlink that makes a flag actionable.

    The note goes first so the architect sees it before the problem statement,
    and it says out loud that nothing was auto-resolved: the existing ticket is
    untouched, and this one is filed for a human to merge or reject.
    """
    if verdict.matched is None:
        return body
    return (
        f"> **Possible duplicate of #{verdict.matched.number}** "
        f"(\"{verdict.matched.title}\", {verdict.matched.state}) - "
        f"similarity {verdict.score:.2f}.\n"
        f"> Filed anyway and nothing was closed or edited; the architect decides "
        f"whether this supersedes, extends, or duplicates that ticket.\n\n"
        f"{body}"
    )
