"""Episodic memory: retrieving prior lessons back into the next worker's prompt.

The knowledge base used to be write-only from the loop's point of view - every
iteration appended a lesson, and no iteration ever read one. Twenty-odd lessons,
several of them hard-won *failure* lessons, therefore had zero influence on the
next worker's behaviour, and each failure mode had to be re-fixed as a Python
guard. Guards are O(n) code for O(n) mistakes; remembering is O(1).

This module is the retrieval half. It is PURE - no filesystem, no network, no
model - in the tradition of :func:`hsai.orchestrator.decide_path` and
:func:`hsai.models.select`: the caller supplies the corpus
(``KnowledgeBase.read_lessons()``) and the task, and gets back the handful of
notes worth spending prompt budget on.

Ranking is deterministic and explainable (inspect it with ``hsai recall``):

- token overlap between the ticket (title weighted above body) and the lesson
  (its title plus its ``## Lesson learned`` text),
- a boost for ``outcome/fail`` - the mistakes are what must not repeat,
- a boost for a matching ``kind/``,
- newer notes win ties,
- zero overlap is never recalled: an irrelevant failure is noise, not memory,
- a hard character budget, enforced against the *rendered* block, so injected
  memory can never dominate the prompt.

Synthesis: FoundationAgents/MetaGPT (a shared memory every role reads before it
acts, rather than starting cold), langchain-ai/langchain (retrieval-augmented
context assembly as the way to ground a model in project-specific prior art),
run-llama/llama_index (top-k retrieval from a corpus under a budget) and
SWE-agent/SWE-agent (prior trajectories fed back into the agent's context to
raise first-attempt success).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .knowledge import LessonRecord, tokenize
from .models import Task

# How much memory one prompt may carry. Deliberately small: recall exists to
# nudge a worker off a known-bad path, not to re-read the vault at it.
DEFAULT_K = 3
DEFAULT_CHAR_BUDGET = 2000
SNIPPET_CHARS = 280

# The retry block is allowed to be larger - it is the single most informative
# context a second attempt can have - but still bounded.
PRIOR_ATTEMPT_CHARS = 1200
MAX_PRIOR_DIGESTS = 3

TITLE_WEIGHT = 2.0  # a term shared with the ticket TITLE is worth more
BODY_WEIGHT = 1.0
FAIL_BOOST = 3.0  # outcome/fail: the whole point of remembering
KIND_BOOST = 1.5  # same kind/ (heal|implement|improve) as the task at hand

RECALL_HEADING = "## Prior lessons from this repo - do not repeat these failures"
RECALL_PREAMBLE = (
    "Retrieved from this repo's knowledge base by relevance to the ticket below; "
    "failures rank first. Read them before you plan, and cite a note by its "
    "[[wikilink]] if it shaped your approach."
)
PRIOR_HEADING = "## Previous attempt on this ticket"


@dataclass(frozen=True)
class Scored:
    """One ranked candidate, with the arithmetic that ranked it."""

    record: LessonRecord
    score: float
    reason: str


def _one_line(text: str, limit: int) -> str:
    """Flatten ``text`` to a single clipped line."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 3)].rstrip() + "..."


def _clip(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` characters, keeping its line structure."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[: max(0, limit - 3)].rstrip() + "..."


def score(record: LessonRecord, task: Task) -> Scored:
    """Score one lesson against one task. Zero means "do not recall"."""
    title_terms = tokenize(task.title)
    body_terms = tokenize(task.body) - title_terms
    lesson_terms = tokenize(f"{record.title}\n{record.lesson_text}")

    title_hits = title_terms & lesson_terms
    body_hits = body_terms & lesson_terms
    overlap = TITLE_WEIGHT * len(title_hits) + BODY_WEIGHT * len(body_hits)
    if not overlap:
        # An irrelevant lesson is noise however instructive it was elsewhere.
        return Scored(record=record, score=0.0, reason="no shared terms")

    total = overlap
    reasons = [f"overlap={overlap:g} ({len(title_hits)} title, {len(body_hits)} body)"]
    if record.outcome == "fail":
        total += FAIL_BOOST
        reasons.append(f"outcome/fail +{FAIL_BOOST:g}")
    if record.kind and record.kind == task.kind:
        total += KIND_BOOST
        reasons.append(f"kind/{record.kind} +{KIND_BOOST:g}")
    return Scored(record=record, score=total, reason="; ".join(reasons))


def rank(lessons: Sequence[LessonRecord], task: Task) -> list[Scored]:
    """Every lesson that shares vocabulary with ``task``, best match first.

    ``lessons`` is expected oldest-first (as ``read_lessons`` returns it), which
    is what makes the recency tiebreak work: equal scores resolve to the newer
    note.
    """
    scored = [
        (position, score(record, task)) for position, record in enumerate(lessons)
    ]
    relevant = [(position, s) for position, s in scored if s.score > 0]
    relevant.sort(key=lambda item: (-item[1].score, -item[0]))
    return [s for _, s in relevant]


def recall(
    lessons: Sequence[LessonRecord],
    task: Task,
    *,
    k: int = DEFAULT_K,
    char_budget: int = DEFAULT_CHAR_BUDGET,
    exclude: Sequence[str] = (),
) -> list[LessonRecord]:
    """The at-most-``k`` lessons worth injecting for ``task``, best first.

    The budget is enforced against :func:`render_recall` of the selection, so
    ``len(render_recall(recall(...))) <= char_budget`` always holds - a lesson
    that would not fit is skipped rather than truncated, and a shorter one
    further down the ranking may take its place.
    """
    if k <= 0 or char_budget <= 0:
        return []
    skip = set(exclude)
    chosen: list[LessonRecord] = []
    for candidate in rank(lessons, task):
        if len(chosen) >= k:
            break
        if candidate.record.note_name in skip:
            continue
        proposed = [*chosen, candidate.record]
        if len(render_recall(proposed)) > char_budget:
            continue
        chosen = proposed
    return chosen


def render_recall(records: Sequence[LessonRecord]) -> str:
    """Render recalled lessons as a compact, wikilinked prompt section.

    Empty in, empty out: with no corpus (or no relevant note) the worker prompt
    is exactly what it was before this module existed.
    """
    if not records:
        return ""
    lines = [RECALL_HEADING, "", RECALL_PREAMBLE, ""]
    for r in records:
        summary = _one_line(r.lesson_text, SNIPPET_CHARS) or "_(no lesson text recorded)_"
        lines.append(f"- [[{r.note_name}]] ({r.outcome}/{r.kind}) **{r.title}**: {summary}")
    return "\n".join(lines) + "\n"


def prior_attempt(
    lessons: Sequence[LessonRecord], ticket: int | None
) -> LessonRecord | None:
    """The most recent lesson recorded against ``ticket``, if one survived.

    A failed attempt's PR is closed and its branch deleted, so its lesson often
    never reaches ``main``; the trajectory store is the other (local) half of
    the retry record - see :func:`hsai.trajectory.recent_for_ticket`.
    """
    if not ticket:
        return None
    matching = [r for r in lessons if r.ticket == ticket]
    return matching[-1] if matching else None


def render_prior_attempt(
    *,
    attempts: int,
    lesson: LessonRecord | None = None,
    digests: Sequence[str] = (),
    limit: int = PRIOR_ATTEMPT_CHARS,
) -> str:
    """Render what the previous attempt(s) on this same ticket did.

    ``attempts`` is how many attempts have ALREADY been made. Without it a retry
    re-runs an identical prompt and is little more than a re-roll; with it the
    worker at least knows it is attempt N and what N-1 recorded.
    """
    if attempts < 1:
        return ""
    lines = [
        f"{PRIOR_HEADING} (this is attempt {attempts + 1})",
        "",
        f"This ticket has already been attempted {attempts} time(s) and did not land. "
        "Do NOT re-run the same approach: read the record below, work out why it "
        "failed, and change something material.",
    ]
    if lesson is not None:
        if lesson.what_happened:
            lines += [
                "",
                "### What the prior attempt did",
                _clip(lesson.what_happened, limit),
            ]
        if lesson.lesson_text:
            lines += [
                "",
                "### Lesson the prior attempt recorded",
                _clip(lesson.lesson_text, limit // 2),
            ]
        lines += ["", f"Source note: [[{lesson.note_name}]]"]
    kept = [d for d in digests if d][:MAX_PRIOR_DIGESTS]
    if kept:
        lines += ["", "### Prior attempt trajectory digest"]
        lines += [f"- {_one_line(d, limit // 2)}" for d in kept]
    if lesson is None and not kept:
        lines += [
            "",
            "No lesson or trajectory survived from the prior attempt "
            "(its PR was closed and its branch deleted), so treat this as a "
            "fresh design problem rather than a retry of the same plan.",
        ]
    return "\n".join(lines) + "\n"
