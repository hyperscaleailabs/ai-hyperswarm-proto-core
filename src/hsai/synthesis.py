"""Heavy-model synthesis: turn reference-project study into substantial tickets.

This is the "planner" half of the two-phase engine. Instead of one small idea
copied from one project, a heavy model:

1. receives a context pack built from a rotating subset of the reference set
   (README, recent commit subjects, CI workflow inventory - fetched via `gh`),
   plus the :mod:`hsai.practices` registry of what this loop has already
   adopted from that set, rendered as ground it must not re-propose, plus the
   prior art :mod:`hsai.retrieval` finds in this repo's OWN lessons,
   whitepapers and ADRs for the goals being planned against,
2. generates ~``ideas_target`` candidate improvements, each required to COMBINE
   practices from >= ``min_projects_combined`` different reference projects,
3. runs a reflection pass - critiques its own candidates for feasibility,
   originality, and fit with the goals in core.yaml; candidates that restate a
   lesson recorded as a failure are dropped here, and every keep/drop verdict
   is recorded (:func:`_screen_duplicate_risk`),
4. prioritizes by impact x effort and emits the top ``file_top`` as fully
   structured tickets (schema in :mod:`hsai.tickets`, each naming the
   practice(s) it adds or extends and citing the prior art it builds on),
   which are filed on GitHub for the cheaper implementation agents to pick up.

The model call goes through :mod:`hsai.ai`, so it stays subscription-only.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace

from . import github, retrieval
from .ai import run_agent
from .config import CoreConfig
from .knowledge import KnowledgeBase
from .models import ModelChoice
from .practices import ADOPTED_HEADING, Practice, render_adopted_section
from .proc import Runner, run
from .retrieval import PRIOR_ART_HEADING, NoteIndex, PriorArt
from .tickets import TicketSpec, check_well_formed

_logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


@dataclass
class ContextPack:
    """What the synthesizer knows this cycle: other projects, and our own notes.

    ``prior_art`` is the read path back into the knowledge base - the top notes
    this repo has already written about the goals it is planning against, each
    carrying the outcome it recorded, so the planner can tell new ground from
    ground it already walked.
    """

    repos: list[str]
    sections: dict[str, str]  # repo -> digest text
    prior_art: tuple[PriorArt, ...] = ()

    def render(self) -> str:
        parts = [f"### {repo}\n{text}" for repo, text in self.sections.items()]
        return "\n\n".join(parts)

    def render_prior_art(self) -> str:
        return retrieval.render_prior_art(self.prior_art)


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
    prior_art: tuple[PriorArt, ...] = (),
) -> ContextPack:
    """Fetch a compact study digest for each repo via the GitHub API.

    ``prior_art`` is retrieved separately (:func:`gather_prior_art`) and passed
    in: everything here talks to the network, everything there is pure.
    """
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
    return ContextPack(repos=repos, sections=sections, prior_art=prior_art)


def goal_queries(cfg: CoreConfig) -> list[str]:
    """One retrieval query per goal - what this cycle is planning against."""
    return [
        f"{g.get('title', '')} {g.get('description', '')}".strip()
        for g in cfg.goals
        if g.get("title") or g.get("description")
    ]


def gather_prior_art(cfg: CoreConfig, index: NoteIndex) -> tuple[PriorArt, ...]:
    """The repo's own notes most relevant to this cycle's goals, deduped."""
    return retrieval.for_queries(index, goal_queries(cfg))


MEMORY_HEADING = "What this loop has already tried"

# Hard cap on the memory section's rendered length: it exists to keep the
# planner from re-proposing done/queued/failed work, not to crowd out the
# reference-project study material that follows it.
DEFAULT_MEMORY_MAX_CHARS = 3000
DEFAULT_MEMORY_CLOSED_LIMIT = 15
DEFAULT_MEMORY_MAX_LESSONS = 25

# Above this Jaccard token-overlap ratio (over normalized titles) a candidate
# is treated as a re-proposal of prior work, not a new idea. Documented here
# because it is the one number that decides what gets silently dropped.
DUPLICATE_JACCARD_THRESHOLD = 0.6

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


def is_duplicate(
    spec: TicketSpec, memory: MemoryPack, *, threshold: float = DUPLICATE_JACCARD_THRESHOLD
) -> tuple[bool, str]:
    """Would filing `spec` duplicate something in `memory`?

    Pure and side-effect free. Two checks, either is enough to flag a match:

    1. exact title match (case-insensitive) - catches the CI-worker style
       stranded-run repeat that filed nine identical chore tickets;
    2. Jaccard token overlap over normalized titles (conventional-commit
       prefix and stopwords stripped) at or above `threshold` - catches the
       planner re-proposing the same idea with a different verb or a
       different type prefix (``feat:`` vs ``refactor:``).

    Returns ``(is_duplicate, matched_prior_title)`` - the matched title is
    what gets logged, so a rejection is always explainable.
    """
    candidate = spec.title.strip()
    candidate_tokens = _normalize_title(candidate)
    for prior in memory.all_titles():
        if candidate.lower() == prior.strip().lower():
            return True, prior

    best_title, best_score = "", 0.0
    for prior in memory.all_titles():
        prior_tokens = _normalize_title(prior)
        if not candidate_tokens or not prior_tokens:
            continue
        union = candidate_tokens | prior_tokens
        score = len(candidate_tokens & prior_tokens) / len(union) if union else 0.0
        if score > best_score:
            best_score, best_title = score, prior
    if best_score >= threshold:
        return True, best_title
    return False, ""


def build_prompt(
    cfg: CoreConfig,
    pack: ContextPack,
    memory: MemoryPack | None = None,
    practices: list[Practice] | None = None,
) -> str:
    goals = "\n".join(f"- {g.get('id')}: {g.get('title')} - {g.get('description', '')}"
                      for g in cfg.goals)
    ideas = int(cfg.synthesis.get("ideas_target", 10))
    top = int(cfg.synthesis.get("file_top", 3))
    combine = int(cfg.synthesis.get("min_projects_combined", 3))
    memory = memory or MemoryPack()
    max_chars = int(cfg.synthesis.get("memory_max_chars", DEFAULT_MEMORY_MAX_CHARS))
    memory_section = memory.render(max_chars=max_chars)
    practices_section = render_adopted_section(practices or [])
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

{ADOPTED_HEADING} - the registry of practices this loop has already pulled
from the reference set, each cited to its source project and evidence. Do NOT
propose a candidate that just re-adopts one of these; if a candidate genuinely
EXTENDS one, name its id in "practice_ids" instead of restating it as new:
{practices_section}

{PRIOR_ART_HEADING} - notes retrieved from OUR OWN knowledge base (lessons,
whitepapers, ADRs) for the goals above, each shown as an Obsidian wikilink with
the outcome it recorded. A note marked `fail` is an idea this repo already
tried and that did not work; a note marked `pass` is a shipped capability. Cite
these by name - they are the evidence trail behind every claim you make about
what this repo has already done:
{prior_art_section}

Study digest of reference projects for this cycle:
{pack.render()}

Work in three explicit phases and show them all in your output:

PHASE 1 - DIVERGE: generate {ideas} candidate improvements. Each MUST combine
practices from at least {combine} different reference projects (name them), be
implementable inside this repo, and advance a named goal. For EACH candidate,
CHECK IT AGAINST THE PRIOR ART above and say either which note(s) it builds on
(cite them as [[note-name]]) or that no prior note covers it.

PHASE 2 - REFLECT: critique every candidate honestly - feasibility in a small
codebase, real value vs novelty theater, risk to the loop's invariants
(ticket-linked PRs, green-gated merges, subscription-only models). State a
duplicate-risk verdict per candidate: the closest prior-art note, and keep or
drop. A candidate that restates a note whose outcome is `fail` must be DROPPED
here - re-running a recorded failure is the one thing this loop must not do.

PHASE 3 - CONVERGE: pick the best {top} and emit them as a fenced ```json code
block: a JSON array where each element has exactly these keys:
  "title" (string, prefixed feat:/refactor:/skill:),
  "problem" (string), "proposal" (string, concrete and multi-step),
  "acceptance_criteria" (array of 3-6 verifiable strings),
  "verification_plan" (array of 2-4 concrete check strings),
  "size" ("M" or "L" - substantial work, never "S"),
  "goal_ids" (array like ["G1","G4"]),
  "synthesis_rationale" (string naming the >= {combine} projects combined and how),
  "practice_ids" (array of strings - ids from the "{ADOPTED_HEADING}" section
    above that this ticket EXTENDS, or new slug-style ids it INTRODUCES for the
    practice(s) it adds to the registry; empty array if none apply),
  "prior_art" (array of strings - note names from the "{PRIOR_ART_HEADING}"
    section above that this ticket builds on, WITHOUT the [[ ]] brackets;
    empty array when no prior note covers it).

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
                    practice_ids=tuple(str(p) for p in item.get("practice_ids", [])),
                    prior_art=tuple(str(p) for p in item.get("prior_art", [])),
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
    rejected: int = 0                              # duplicate specs dropped before filing
    rejected_titles: list[str] = field(default_factory=list)  # matched prior title, one per drop
    # One line per surviving candidate: its closest prior-art note and the
    # keep/drop decision that followed - the reflection phase's audit trail.
    risk_flags: list[str] = field(default_factory=list)
    risk_dropped: int = 0                          # candidates dropped as restated failures


def _filter_duplicates(
    specs: list[TicketSpec], memory: MemoryPack, *, threshold: float
) -> tuple[list[TicketSpec], list[str]]:
    """Drop specs that duplicate something in `memory`.

    Never back-fills: an honest thin block (fewer than `file_top` tickets
    filed) beats padding the backlog with a duplicate, and the caller surfaces
    why in `SynthesisResult` / `BlockReport.notes`.
    """
    survivors: list[TicketSpec] = []
    rejected_titles: list[str] = []
    for spec in specs:
        dup, matched = is_duplicate(spec, memory, threshold=threshold)
        if dup:
            _logger.info("synthesis: dropping duplicate %r (matches %r)", spec.title, matched)
            rejected_titles.append(matched)
        else:
            survivors.append(spec)
    return survivors, rejected_titles


def _query_for(spec: TicketSpec) -> str:
    """What a candidate is "about", for retrieval - title, problem, proposal."""
    return f"{spec.title}\n{spec.problem}\n{spec.proposal}"


def ground_prior_art(specs: list[TicketSpec], index: NoteIndex) -> list[TicketSpec]:
    """Attach citation-grade prior art to every spec before it is filed.

    Retrieval - not the model - is the authority here: a cited note either
    exists in the index or is dropped, so every ``[[wikilink]]`` in a filed
    ticket resolves in the Obsidian graph.
    """
    return [
        replace(spec, prior_art=retrieval.resolve_citations(index, spec.prior_art, _query_for(spec)))
        for spec in specs
    ]


def _screen_duplicate_risk(
    specs: list[TicketSpec], index: NoteIndex
) -> tuple[list[TicketSpec], list[str]]:
    """Drop candidates that restate a recorded failure, and record every verdict.

    Both halves matter for the audit trail: a dropped candidate names the failed
    note it restates, and a kept one names the closest note it did NOT restate.
    """
    survivors: list[TicketSpec] = []
    flags: list[str] = []
    for spec in specs:
        risk = retrieval.duplicate_risk(index, _query_for(spec))
        flags.append(risk.render(spec.title))
        if risk.flagged:
            _logger.info(
                "synthesis: dropping %r - restates failed note %s (coverage %.2f)",
                spec.title, risk.note_name, risk.coverage,
            )
        else:
            survivors.append(spec)
    return survivors, flags


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    root: str = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Run one synthesis pass, drop duplicates of prior work, and file the rest.

    Grounded in the repo's own knowledge base at both ends: prior art goes INTO
    the prompt, and the citations of what each filed ticket builds on come back
    OUT of it (deterministically, from the same index).
    """
    repos = pick_rotation(cfg, cycle_index)
    index = retrieval.load_index(root, cfg)
    pack = build_context_pack(repos, runner=runner, prior_art=gather_prior_art(cfg, index))
    memory = MemoryPack.gather(cfg, root=root, runner=runner)
    try:
        practices = KnowledgeBase.from_config(cfg, root).read_practices()
    except OSError:
        practices = []
    tier = cfg.synthesis.get("tier", "heavy")
    model = cfg.tiers[tier].model if tier in cfg.tiers else cfg.tiers[cfg.default_tier].model
    choice = ModelChoice(
        tier=tier, model=model,
        rationale="synthesis is always heavy: cross-project combination + reflection",
        strategy="synthesis-v1",
    )
    ares = run_agent(
        build_prompt(cfg, pack, memory, practices), choice, cfg,
        timeout=float(cfg.synthesis.get("timeout_seconds", 2400)),
        runner=ai_runner,
    )
    if not ares.ok:
        return SynthesisResult(ok=False, studied=repos, filed=[], error=ares.error[:500])

    # `.text` unwraps the envelope; the raw `.output` under `--output-format
    # stream-json` is a JSONL event log whose fenced ticket blocks are buried
    # (and JSON-escaped) inside a `result` event, where the parser cannot see them.
    specs = parse_ticket_specs(ares.text)
    threshold = float(cfg.synthesis.get("duplicate_threshold", DUPLICATE_JACCARD_THRESHOLD))
    survivors, rejected_titles = _filter_duplicates(specs, memory, threshold=threshold)
    grounded = ground_prior_art(survivors, index)
    survivors, risk_flags = _screen_duplicate_risk(grounded, index)
    risk_dropped = len(grounded) - len(survivors)

    filed: list[int] = []
    for spec in survivors:
        body = spec.render()
        # Belt and braces: a synthesis-filed ticket without its citations would
        # break the provenance chain the prompt just spent a phase building.
        wf = check_well_formed(spec.title, body, require_prior_art=True)
        if not wf.ok:
            _logger.warning("synthesis: not filing %r - %s", spec.title, "; ".join(wf.reasons))
            continue
        num = github.create_issue(
            cfg.repo_slug, spec.title, body, spec.all_labels(), runner=runner
        )
        if num:
            filed.append(num)

    if not specs:
        error = "no parseable ticket specs in output"
    elif not survivors:
        error = (
            f"all {len(specs)} candidate(s) rejected as duplicates of prior work "
            f"({len(rejected_titles)} by title, {risk_dropped} restating a recorded failure)"
        )
    else:
        error = ""
    return SynthesisResult(
        ok=bool(filed), studied=repos, filed=filed, error=error,
        rejected=len(rejected_titles), rejected_titles=rejected_titles,
        risk_flags=risk_flags, risk_dropped=risk_dropped,
    )
