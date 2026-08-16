"""Heavy-model synthesis: turn reference-project study into substantial tickets.

This is the "planner" half of the two-phase engine. Instead of one small idea
copied from one project, a heavy model:

1. receives a context pack built from a rotating subset of the reference set
   (README, recent commit subjects, CI workflow inventory - fetched via `gh`),
2. generates ~``ideas_target`` candidate improvements, each required to COMBINE
   practices from >= ``min_projects_combined`` different reference projects,
3. runs a reflection pass - critiques its own candidates for feasibility,
   originality, and fit with the goals in core.yaml,
4. prioritizes by impact x effort and emits the top ``file_top`` as fully
   structured tickets (schema in :mod:`hsai.tickets`), which are filed on
   GitHub for the cheaper implementation agents to pick up.

Before any of that, the planner is grounded in *our own* record: a
:class:`~hsai.recall.PriorArt` digest retrieved from the vault, the quota ledger
and the closed/blocked backlog is injected alongside the reference digest, and
every ticket the model files must cite at least one of those artifacts. What
comes back is then screened - an exact re-proposal is refused, a near-duplicate
is demoted below genuinely new ideas - so heavy-tier quota is never spent
filing work we have already shipped or already abandoned.

The model call goes through :mod:`hsai.ai`, so it stays subscription-only.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from . import github, recall, tickets
from .ai import run_agent
from .config import CoreConfig
from .knowledge import KnowledgeBase
from .models import ModelChoice
from .proc import Runner, run
from .recall import PriorArt
from .tickets import TicketSpec

_logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


@dataclass
class ContextPack:
    """What the synthesizer knows this cycle: other projects, and ourselves.

    ``sections`` is the outward-looking half (what the reference projects do);
    ``prior_art`` is the inward-looking half (what we already did about it).
    Both are rendered into the prompt, external evidence after internal, because
    an idea is only worth generating if it is new *to us*.
    """

    repos: list[str]
    sections: dict[str, str]  # repo -> digest text
    prior_art: PriorArt = field(default_factory=PriorArt)

    def render(self) -> str:
        parts = [f"### {repo}\n{text}" for repo, text in self.sections.items()]
        return "\n\n".join(parts)

    def render_prior_art(self) -> str:
        """The prior-art block, with the heading kept even when nothing matched."""
        if self.prior_art.section:
            return self.prior_art.section
        return (
            f"{recall.PRIOR_ART_HEADING}: _(nothing retrieved - the vault, the "
            "ledger and the backlog are all empty)_"
        )


def pick_rotation(cfg: CoreConfig, cycle_index: int) -> list[str]:
    """Rotate deterministically through the top-10 so every cycle studies a
    different subset and the whole set is covered over consecutive cycles."""
    repos = [r.repo for r in cfg.reference_top10]
    if not repos:
        return []
    k = int(cfg.synthesis.get("refs_per_cycle", 3))
    start = (cycle_index * k) % len(repos)
    return [repos[(start + i) % len(repos)] for i in range(min(k, len(repos)))]


def build_context_pack(
    repos: list[str],
    *,
    runner: Runner = run,
    commits: int = 30,
    prior_art: PriorArt | None = None,
) -> ContextPack:
    """Fetch a compact study digest for each repo via the GitHub API."""
    sections: dict[str, str] = {}
    for repo in repos:
        parts: list[str] = []
        readme = runner(
            ["gh", "api", f"repos/{repo}/readme", "-H", "Accept: application/vnd.github.raw"]
        )
        if readme.ok:
            parts.append("README (truncated):\n" + readme.stdout[:4000])
        log = runner(
            [
                "gh", "api", f"repos/{repo}/commits?per_page={commits}",
                "--jq", ".[].commit.message | split(\"\\n\")[0]",
            ]
        )
        if log.ok and log.stdout.strip():
            parts.append("Recent commit subjects:\n" + log.stdout[:2000])
        workflows = runner(
            [
                "gh", "api", f"repos/{repo}/contents/.github/workflows",
                "--jq", ".[].name",
            ]
        )
        if workflows.ok and workflows.stdout.strip():
            parts.append("CI workflows:\n" + workflows.stdout[:500])
        sections[repo] = "\n\n".join(parts) or "(no data fetched)"
    return ContextPack(
        repos=repos, sections=sections, prior_art=prior_art or PriorArt()
    )


MEMORY_HEADING = "What this loop has already tried"

# Hard cap on the memory section's rendered length: it exists to keep the
# planner from re-proposing done/queued/failed work, not to crowd out the
# reference-project study material that follows it.
DEFAULT_MEMORY_MAX_CHARS = 3000
DEFAULT_MEMORY_CLOSED_LIMIT = 15
DEFAULT_MEMORY_MAX_LESSONS = 25

# Above this Jaccard token-overlap ratio (over normalized titles) a candidate
# is treated as a re-proposal of prior work, not a new idea. Documented here
# because it is the one number that decides what gets demoted below new ideas.
DUPLICATE_JACCARD_THRESHOLD = 0.6

# Hard ceiling on the whole rendered prompt. The study digest is the part that
# gives - it is bounded per repo but scales with `refs_per_cycle`, while the
# memory and prior-art sections are already individually capped and are the two
# the planner most needs in full.
DEFAULT_MAX_PROMPT_CHARS = 32000
_DIGEST_TRUNCATED = "\n... (study digest truncated to fit the prompt budget)"

# Fraction of a prior title's distinctive tokens that must appear in a
# re-proposal's `prior_art` for it to count as citing that prior failure.
REPROPOSAL_CITATION_COVERAGE = 0.75

_CONVENTIONAL_PREFIX_RE = re.compile(
    r"^\s*(feat|fix|refactor|chore|docs|test|ci|skill|perf|style|build)"
    r"(\([^)]*\))?\s*:\s*",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]+")
# Small and generic on purpose: this only strips connective noise, never the
# domain words ("memory", "duplicate", "budget", ...) that actually
# distinguish one proposal from another.
_DUP_STOPWORDS = frozenset(
    """
    a an and are as at be by can for from had has have how in into is it its
    of on or so than that the their then there this those to via was were
    what when where which who will with without new add adds adding
    """.split()
)


def _normalize_title(title: str) -> frozenset[str]:
    """Tokens a title is "about", ignoring its conventional-commit prefix.

    ``feat: adaptive budget throttling`` and ``refactor: adaptive budget
    throttling`` normalize to the same token set - the prefix says how the
    work will land, not what it is.
    """
    stripped = _CONVENTIONAL_PREFIX_RE.sub("", title.strip())
    return frozenset(
        w for w in _WORD_RE.findall(stripped.lower())
        if len(w) > 2 and w not in _DUP_STOPWORDS
    )


@dataclass
class MemoryPack:
    """What this loop already knows about its own state, for the planner.

    Built from three sources so the heavy model stops re-proposing work that
    is already open, already shipped, or already tried and recorded as a
    failure: open tickets, recently closed tickets, and knowledge-base
    lessons. Purely data + rendering - :func:`MemoryPack.gather` is the only
    place that touches `gh` or the filesystem.
    """

    open_tickets: tuple[github.Issue, ...] = ()
    closed_titles: tuple[str, ...] = ()          # newest first
    lessons: tuple[tuple[str, str], ...] = ()     # (outcome, title), newest first

    @classmethod
    def gather(
        cls,
        cfg: CoreConfig,
        *,
        root: str = ".",
        runner: Runner = run,
        closed_limit: int | None = None,
        max_lessons: int | None = None,
    ) -> MemoryPack:
        """Collect the pack. Each source degrades to empty on its own failure
        (missing `gh`, empty/unwritable knowledge base) rather than aborting
        synthesis - a thin memory section beats no synthesis at all."""
        closed_limit = (
            int(cfg.synthesis.get("memory_closed_limit", DEFAULT_MEMORY_CLOSED_LIMIT))
            if closed_limit is None else closed_limit
        )
        max_lessons = (
            int(cfg.synthesis.get("memory_max_lessons", DEFAULT_MEMORY_MAX_LESSONS))
            if max_lessons is None else max_lessons
        )

        try:
            open_tickets = tuple(github.list_open_issues(cfg.repo_slug, runner=runner))
        except (OSError, ValueError):
            open_tickets = ()

        try:
            closed = github.list_closed_issues(cfg.repo_slug, limit=closed_limit, runner=runner)
            closed_titles = tuple(i.title for i in closed)
        except (OSError, ValueError):
            closed_titles = ()

        try:
            records = KnowledgeBase.from_config(cfg, root).read_lessons()
        except OSError:
            records = []
        # read_lessons() is oldest-first; take the newest window, then flip it.
        lessons = tuple((r.outcome, r.title) for r in reversed(records[-max_lessons:]))

        return cls(open_tickets=open_tickets, closed_titles=closed_titles, lessons=lessons)

    def all_titles(self) -> list[str]:
        """Every title this pack knows about - what :func:`is_duplicate` checks against."""
        return (
            [i.title for i in self.open_tickets]
            + list(self.closed_titles)
            + [title for _, title in self.lessons]
        )

    def open_titles(self) -> list[str]:
        """Titles of work that is queued or in flight right now."""
        return [i.title for i in self.open_tickets]

    def failed_titles(self) -> list[str]:
        """Titles of work that was tried and recorded as a failure.

        These are the only titles a candidate may legitimately re-propose, and
        only by citing the failure and saying what changed - see
        :func:`reproposal_justification`.
        """
        return [title for outcome, title in self.lessons if outcome == "fail"]

    def render(self, *, max_chars: int = DEFAULT_MEMORY_MAX_CHARS) -> str:
        """Titles only, newest first, hard-capped so it can never crowd out
        the reference-project study material that follows it in the prompt."""
        lines: list[str] = []
        if self.open_tickets:
            lines.append("Open tickets:")
            for i in self.open_tickets:
                flag = " BLOCKED" if i.is_blocked else ""
                labels = ", ".join(i.labels) or "-"
                lines.append(f"- #{i.number} {i.title} [{labels}]{flag}")
        if self.closed_titles:
            lines.append("")
            lines.append("Recently closed tickets:")
            lines.extend(f"- {t}" for t in self.closed_titles)
        if self.lessons:
            lines.append("")
            lines.append("Lesson outcomes recorded (newest first):")
            lines.extend(f"- **{outcome}** - {title}" for outcome, title in self.lessons)

        text = "\n".join(lines)
        if not text:
            return "_(nothing recorded yet - this is an early cycle)_"
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."


EXACT = "exact"
NEAR = "near"


@dataclass(frozen=True)
class DuplicateMatch:
    """How close a candidate came to something we already know about.

    ``kind`` is ``""`` (novel), ``"near"`` (demote it below new ideas) or
    ``"exact"`` (refuse it). The distinction is the whole point: a reworded
    variant of open work is worth less than a new idea but may still be worth
    filing when the block has nothing better, whereas re-filing an idea
    verbatim is pure waste.
    """

    kind: str = ""
    title: str = ""     # the prior title that matched
    score: float = 0.0  # Jaccard overlap of normalized titles

    def __bool__(self) -> bool:
        return bool(self.kind)


def _title_overlap(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def classify_duplicate(
    spec: TicketSpec, memory: MemoryPack, *, threshold: float = DUPLICATE_JACCARD_THRESHOLD
) -> DuplicateMatch:
    """Grade `spec` against everything in `memory`. Pure and side-effect free.

    ``exact`` means the *idea* is identical, not merely the string: either the
    raw titles match case-insensitively, or their normalized token sets are
    equal - which is what makes ``feat: X`` and ``refactor: X`` the same
    proposal wearing a different prefix.

    ``near`` is a Jaccard token overlap at or above `threshold` - the planner
    re-proposing the same idea with a different verb or a few extra words.

    The matched title travels with the verdict so a refusal is always
    explainable in one line.
    """
    candidate = spec.title.strip()
    candidate_tokens = _normalize_title(candidate)

    best = DuplicateMatch()
    for prior in memory.all_titles():
        prior_clean = prior.strip()
        prior_tokens = _normalize_title(prior_clean)
        score = _title_overlap(candidate_tokens, prior_tokens)
        exact = candidate.lower() == prior_clean.lower() or (
            bool(candidate_tokens) and candidate_tokens == prior_tokens
        )
        if exact:
            return DuplicateMatch(kind=EXACT, title=prior_clean, score=1.0)
        if score >= threshold and score > best.score:
            best = DuplicateMatch(kind=NEAR, title=prior_clean, score=score)
    return best


def is_duplicate(
    spec: TicketSpec, memory: MemoryPack, *, threshold: float = DUPLICATE_JACCARD_THRESHOLD
) -> tuple[bool, str]:
    """Would filing `spec` overlap something in `memory`?

    The boolean view of :func:`classify_duplicate`, kept for callers that only
    need "have we seen this before" and not what to do about it. Returns
    ``(is_duplicate, matched_prior_title)``.
    """
    match = classify_duplicate(spec, memory, threshold=threshold)
    return bool(match), match.title


def _cites(text: str, title: str) -> bool:
    """Does `text` name the work `title` refers to?

    Coverage, not Jaccard: `text` is a paragraph and `title` a few words, so
    symmetric overlap would score near zero however faithfully the paragraph
    quotes the title. What matters is that the title's distinctive tokens are
    all (or nearly all) present - whether written out or carried inside a
    ``[[2026-01-02-some-lesson]]`` wikilink, which tokenizes the same way.
    """
    wanted = _normalize_title(title)
    if not wanted:
        return False
    present = frozenset(w for w in _WORD_RE.findall(text.lower()) if len(w) > 2)
    return len(wanted & present) / len(wanted) >= REPROPOSAL_CITATION_COVERAGE


def reproposal_justification(
    spec: TicketSpec, memory: MemoryPack, *, threshold: float = DUPLICATE_JACCARD_THRESHOLD
) -> str:
    """The failed prior work `spec` legitimately re-proposes, or ``""``.

    A previously-failed idea is the one kind of duplicate worth filing again -
    but only on evidence. Both halves are required, and both live in
    ``prior_art`` so a reviewer finds them in one place:

    1. the ticket cites the failing lesson it is retrying, and
    2. it states *what changed* since that failure.

    Without (1) the planner is guessing; without (2) it is repeating.
    """
    if not tickets.WHAT_CHANGED.search(spec.prior_art):
        return ""
    candidate_tokens = _normalize_title(spec.title)
    for failed in memory.failed_titles():
        overlap = _title_overlap(candidate_tokens, _normalize_title(failed))
        if overlap >= threshold and _cites(spec.prior_art, failed):
            return failed
    return ""


def build_prompt(cfg: CoreConfig, pack: ContextPack, memory: MemoryPack | None = None) -> str:
    """Render the planner's instruction, hard-capped at ``max_prompt_chars``.

    When the cap binds, the *study digest* is what gives: it is the bulkiest
    section and the only one whose value degrades gracefully with length, while
    the memory and prior-art sections are already individually capped and are
    precisely what stops the planner re-proposing dead work.
    """
    cap = int(cfg.synthesis.get("max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS))
    digest = pack.render()
    prompt = _render_prompt(cfg, pack, memory, digest)
    if cap > 0 and len(prompt) > cap:
        digest = _clip_digest(digest, len(digest) - (len(prompt) - cap))
        prompt = _render_prompt(cfg, pack, memory, digest)
    return prompt


def _clip_digest(digest: str, limit: int) -> str:
    """Clip to exactly ``limit`` characters, marker included, never longer."""
    if len(digest) <= limit:
        return digest
    if limit <= len(_DIGEST_TRUNCATED):
        return _DIGEST_TRUNCATED[: max(0, limit)]
    return digest[: limit - len(_DIGEST_TRUNCATED)] + _DIGEST_TRUNCATED


def _render_prompt(
    cfg: CoreConfig, pack: ContextPack, memory: MemoryPack | None, digest: str
) -> str:
    goals = "\n".join(f"- {g.get('id')}: {g.get('title')} - {g.get('description', '')}"
                      for g in cfg.goals)
    ideas = int(cfg.synthesis.get("ideas_target", 10))
    top = int(cfg.synthesis.get("file_top", 3))
    combine = int(cfg.synthesis.get("min_projects_combined", 3))
    memory = memory or MemoryPack()
    max_chars = int(cfg.synthesis.get("memory_max_chars", DEFAULT_MEMORY_MAX_CHARS))
    memory_section = memory.render(max_chars=max_chars)
    prior_art_section = pack.render_prior_art()
    return f"""You are the SYNTHESIS planner for ai-hyperswarm-proto-core, an
autonomous self-improving AI-swarm harness. Your job is NOT to copy one idea
from one project, but to COMBINE practices across projects into substantial,
creative improvements for THIS codebase (a Python CLI orchestrator: worktrees,
gh tickets, claude -p workers, CI gates, Obsidian knowledge base).

Project goals:
{goals}

{MEMORY_HEADING} - this is our OWN history, not another project's. A candidate
that substantially overlaps anything listed below must be DROPPED in PHASE 2
(reflect) and its slot refilled with a genuinely new idea. Never duplicate the
title of a ticket that is still open or already closed; build on them instead:
{memory_section}

{prior_art_section}

Study digest of reference projects for this cycle:
{digest}

Work in three explicit phases and show them all in your output:

PHASE 1 - DIVERGE: generate {ideas} candidate improvements. Each MUST combine
practices from at least {combine} different reference projects (name them), be
implementable inside this repo, and advance a named goal.

PHASE 2 - REFLECT: critique every candidate honestly - feasibility in a small
codebase, real value vs novelty theater, risk to the loop's invariants
(ticket-linked PRs, green-gated merges, subscription-only models).

PHASE 3 - CONVERGE: pick the best {top} and emit them as a fenced ```json code
block: a JSON array where each element has exactly these keys:
  "title" (string, prefixed feat:/refactor:/skill:),
  "problem" (string), "proposal" (string, concrete and multi-step),
  "acceptance_criteria" (array of 3-6 verifiable strings),
  "verification_plan" (array of 2-4 concrete check strings),
  "size" ("M" or "L" - substantial work, never "S"),
  "goal_ids" (array like ["G1","G4"]),
  "synthesis_rationale" (string naming the >= {combine} projects combined and how),
  "prior_art" (string citing >= 1 artifact from the prior-art section above,
    by its ref VERBATIM - a [[note-name]], a #ticket number, or a ledger figure
    such as "ledger block 41339: 1425s per merged PR" - and saying in one
    sentence what that evidence implies for this ticket).

A ticket with no "prior_art" citation is REFUSED before it is filed, as is one
whose title duplicates prior work. If you deliberately retry an idea recorded as
a FAILURE, cite that failing lesson in "prior_art" and add an explicit
"what changed: ..." clause - that is the only accepted form of a re-proposal.

The JSON block must be the LAST fenced block in your reply."""


def parse_ticket_specs(output: str) -> list[TicketSpec]:
    """Extract the final JSON block and convert it into TicketSpecs."""
    blocks = _JSON_BLOCK.findall(output)
    if not blocks:
        return []
    try:
        raw = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return []
    specs: list[TicketSpec] = []
    for item in raw:
        try:
            specs.append(
                TicketSpec(
                    title=str(item["title"])[:150],
                    problem=str(item["problem"]),
                    proposal=str(item["proposal"]),
                    acceptance_criteria=tuple(str(c) for c in item["acceptance_criteria"]),
                    verification_plan=tuple(str(v) for v in item["verification_plan"]),
                    size=str(item.get("size", "M")),
                    goal_ids=tuple(str(g) for g in item.get("goal_ids", [])),
                    synthesis_rationale=str(item.get("synthesis_rationale", "")),
                    # Absent rather than required at parse time: a missing
                    # citation is a *screening* refusal with a logged reason,
                    # not a silently unparseable candidate.
                    prior_art=str(item.get("prior_art", "")),
                    labels=("self-improve", "hsai", "priority:P2"),
                )
            )
        except (KeyError, TypeError):
            continue
    return specs


@dataclass
class SynthesisResult:
    ok: bool
    studied: list[str]
    filed: list[int]
    error: str = ""
    rejected: int = 0                              # specs refused before filing
    rejected_titles: list[str] = field(default_factory=list)  # matched prior title, one per drop
    refusals: list[str] = field(default_factory=list)         # "<title>: <reason>", one per drop
    demoted_titles: list[str] = field(default_factory=list)   # near-duplicates ranked last
    prior_art_cited: int = 0                       # filed tickets citing internal evidence


@dataclass(frozen=True)
class Screened:
    """The outcome of grading one batch of candidates against our own record."""

    accepted: list[TicketSpec] = field(default_factory=list)   # ranked, capped
    refusals: list[tuple[str, str, str]] = field(default_factory=list)  # title, reason, matched
    demoted: list[str] = field(default_factory=list)           # titles ranked below new ideas

    @property
    def refused_titles(self) -> list[str]:
        """The prior title each refusal matched (empty for schema refusals)."""
        return [matched for _, _, matched in self.refusals]

    @property
    def refusal_lines(self) -> list[str]:
        return [f"{title}: {reason}" for title, reason, _ in self.refusals]


def screen_candidates(
    specs: list[TicketSpec],
    memory: MemoryPack,
    *,
    threshold: float = DUPLICATE_JACCARD_THRESHOLD,
    file_top: int = 0,
) -> Screened:
    """Grade the model's candidates against our own record before anything is filed.

    Three verdicts, in the order they are checked:

    * **refused - malformed.** :func:`hsai.tickets.check_spec` rejects a spec
      that cites no internal artifact. Novelty we cannot trace is not evidence.
    * **refused - exact duplicate.** The idea is already open, closed, or
      recorded as a lesson. The one exception is a previously *failed* idea
      that is not currently open and whose ticket cites that failure and says
      what changed (:func:`reproposal_justification`) - the loop is allowed to
      retry a failure it has understood.
    * **demoted - near duplicate.** A reworded variant of prior work is kept
      but ranked below every genuinely new idea, so it is filed only when the
      block has nothing better to offer.

    Refusals are never back-filled: an honest thin block beats padding the
    backlog. ``file_top`` (0 = unlimited) caps what is left, which is what makes
    demotion bite.
    """
    accepted: list[tuple[int, TicketSpec]] = []   # (rank, spec); rank 0 = novel, 1 = demoted
    refusals: list[tuple[str, str, str]] = []
    demoted: list[str] = []
    open_titles = {t.strip().lower() for t in memory.open_titles()}

    for spec in specs:
        schema = tickets.check_spec(spec)
        if not schema.ok:
            reason = "; ".join(schema.reasons)
            _logger.info("synthesis: refusing malformed %r (%s)", spec.title, reason)
            refusals.append((spec.title, reason, ""))
            continue

        match = classify_duplicate(spec, memory, threshold=threshold)
        if match.kind == EXACT:
            retried = (
                ""
                if match.title.strip().lower() in open_titles
                else reproposal_justification(spec, memory, threshold=threshold)
            )
            if not retried:
                reason = f"exact duplicate of prior work: {match.title!r}"
                _logger.info("synthesis: refusing %r (%s)", spec.title, reason)
                refusals.append((spec.title, reason, match.title))
                continue
            _logger.info(
                "synthesis: accepting re-proposal %r - cites failed %r and states what changed",
                spec.title, retried,
            )
            accepted.append((0, spec))
            continue

        if match.kind == NEAR:
            _logger.info(
                "synthesis: demoting %r (%.2f overlap with %r)",
                spec.title, match.score, match.title,
            )
            demoted.append(spec.title)
            accepted.append((1, spec))
            continue

        accepted.append((0, spec))

    # `sorted` is stable, so within a rank the planner's own prioritization holds.
    ranked = [spec for _, spec in sorted(accepted, key=lambda pair: pair[0])]
    if file_top > 0:
        ranked = ranked[:file_top]
    return Screened(accepted=ranked, refusals=refusals, demoted=demoted)


def prior_art_query(cfg: CoreConfig, repos: list[str]) -> str:
    """What to retrieve our own record against, for one cycle.

    The planner's job this cycle *is* the goals plus the projects it is about
    to study, so that text is the query. Deterministic and model-free: the same
    cycle index always retrieves the same artifacts.
    """
    goals = " ".join(
        f"{g.get('title', '')} {g.get('description', '')}" for g in cfg.goals
    )
    return f"{goals}\n{' '.join(repos)}"


def gather_context(
    cfg: CoreConfig, *, cycle_index: int = 0, root: str = ".", runner: Runner = run
) -> tuple[ContextPack, MemoryPack]:
    """Everything the planner is shown: reference digests, prior art, memory."""
    repos = pick_rotation(cfg, cycle_index)
    prior = recall.build_prior_art(
        prior_art_query(cfg, repos),
        int(cfg.synthesis.get("prior_art_max_chars", recall.DEFAULT_PRIOR_ART_CHARS)),
        root=root,
        cfg=cfg,
        k=int(cfg.synthesis.get("prior_art_k", recall.DEFAULT_PRIOR_ART_K)),
        runner=runner,
    )
    pack = build_context_pack(repos, runner=runner, prior_art=prior)
    memory = MemoryPack.gather(cfg, root=root, runner=runner)
    return pack, memory


def preview(
    cfg: CoreConfig, *, cycle_index: int = 0, root: str = ".", runner: Runner = run
) -> str:
    """Render exactly the prompt `synthesize` would send - and send nothing.

    Backs ``hsai synthesize --dry-run``: it reads the vault, the ledger and the
    backlog, but spends no quota and files no ticket, so the retrieved prior art
    and the prompt budget can be inspected before a heavy run.
    """
    pack, memory = gather_context(cfg, cycle_index=cycle_index, root=root, runner=runner)
    return build_prompt(cfg, pack, memory)


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    root: str = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Run one synthesis pass, screen the candidates, and file the survivors."""
    pack, memory = gather_context(cfg, cycle_index=cycle_index, root=root, runner=runner)
    repos = pack.repos
    tier = cfg.synthesis.get("tier", "heavy")
    model = cfg.tiers[tier].model if tier in cfg.tiers else cfg.tiers[cfg.default_tier].model
    choice = ModelChoice(
        tier=tier, model=model,
        rationale="synthesis is always heavy: cross-project combination + reflection",
        strategy="synthesis-v1",
    )
    ares = run_agent(
        build_prompt(cfg, pack, memory), choice, cfg,
        timeout=float(cfg.synthesis.get("timeout_seconds", 2400)),
        runner=ai_runner,
    )
    if not ares.ok:
        return SynthesisResult(ok=False, studied=repos, filed=[], error=ares.error[:500])

    specs = parse_ticket_specs(ares.output)
    screened = screen_candidates(
        specs, memory,
        threshold=float(cfg.synthesis.get("duplicate_threshold", DUPLICATE_JACCARD_THRESHOLD)),
        file_top=int(cfg.synthesis.get("file_top", 0)),
    )

    filed: list[int] = []
    cited = 0
    for spec in screened.accepted:
        num = github.create_issue(
            cfg.repo_slug, spec.title, spec.render(), spec.all_labels(), runner=runner
        )
        if num:
            filed.append(num)
            if tickets.prior_art_citations(spec.prior_art):
                cited += 1

    if not specs:
        error = "no parseable ticket specs in output"
    elif not screened.accepted:
        error = f"all {len(specs)} candidate(s) refused: {'; '.join(screened.refusal_lines)}"
    else:
        error = ""
    return SynthesisResult(
        ok=bool(filed), studied=repos, filed=filed, error=error,
        rejected=len(screened.refusals), rejected_titles=screened.refused_titles,
        refusals=screened.refusal_lines, demoted_titles=screened.demoted,
        prior_art_cited=cited,
    )
