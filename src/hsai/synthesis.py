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

The model call goes through :mod:`hsai.ai`, so it stays subscription-only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import github
from .ai import run_agent
from .config import CoreConfig
from .knowledge import KnowledgeBase
from .models import ModelChoice
from .proc import Runner, run
from .tickets import TicketSpec

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


@dataclass
class ContextPack:
    """What the synthesizer knows about the reference projects this cycle."""

    repos: list[str]
    sections: dict[str, str]  # repo -> digest text

    def render(self) -> str:
        parts = [f"### {repo}\n{text}" for repo, text in self.sections.items()]
        return "\n\n".join(parts)


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
    repos: list[str], *, runner: Runner = run, commits: int = 30
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
    return ContextPack(repos=repos, sections=sections)


TRIED_HEADING = "What this loop has already tried"

# Threshold for is_duplicate(): the fraction of normalized title tokens two
# candidates must share (Jaccard over the union) before the newer one is
# treated as a re-proposal rather than a genuinely new idea. Tuned so a
# prefix-only rename (feat: -> refactor:) always matches (union == overlap =>
# 1.0) while two candidates that merely share a couple of common words do not.
DUPLICATE_JACCARD_THRESHOLD = 0.6


@dataclass(frozen=True)
class TicketRef:
    number: int
    title: str
    blocked: bool = False


@dataclass
class MemoryPack:
    """What this loop has already tried, queued, or finished with.

    Three sources, each capped and newest-first: open tickets (still queued or
    in flight), recently closed tickets (finished, one way or another), and
    knowledge-base lesson outcomes (pass/fail on ideas actually attempted).

    Kept separate from :class:`ContextPack` - that studies OTHER projects;
    this is our OWN history - so it can never be silently displaced by the
    rotating reference digest, and so :func:`is_duplicate` has one place to
    read prior titles from.
    """

    open_tickets: tuple[TicketRef, ...] = ()
    closed_tickets: tuple[TicketRef, ...] = ()
    lesson_lines: tuple[str, ...] = ()  # "**outcome** - title", failures first

    def prior_titles(self) -> tuple[str, ...]:
        """Every title a new candidate could duplicate, order preserved, deduped."""
        lesson_titles = (
            line.split(" - ", 1)[1] for line in self.lesson_lines if " - " in line
        )
        titles = (
            [t.title for t in self.open_tickets]
            + [t.title for t in self.closed_tickets]
            + list(lesson_titles)
        )
        return tuple(dict.fromkeys(titles))

    def render(self, max_chars: int = 3000) -> str:
        """Titles-only digest for the prompt, hard-capped so it can never
        crowd out the reference-project study material."""
        lines: list[str] = []
        if self.lesson_lines:
            lines.append("Lessons already recorded (outcome - title):")
            lines.extend(f"- {line}" for line in self.lesson_lines)
        if self.open_tickets:
            lines.append("")
            lines.append("Tickets already filed and still open:")
            lines.extend(
                f"- #{t.number} {t.title}" + (" (blocked)" if t.blocked else "")
                for t in self.open_tickets
            )
        if self.closed_tickets:
            lines.append("")
            lines.append("Recently closed tickets:")
            lines.extend(f"- #{t.number} {t.title}" for t in self.closed_tickets)
        text = "\n".join(lines).strip()
        if not text:
            return "_(nothing recorded yet - this is an early cycle)_"
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text


def build_memory_pack(
    cfg: CoreConfig,
    *,
    root: str = ".",
    runner: Runner = run,
    max_lessons: int = 25,
) -> MemoryPack:
    """Gather what this loop has already tried: open tickets, recently closed
    tickets, and knowledge-base lesson outcomes.

    Without it the planner keeps re-proposing ideas that were tried and failed
    or are already queued, because its only context is a freshly fetched
    digest of OTHER projects. Every source degrades independently to empty
    (unreachable `gh`, unreadable/empty knowledge base) rather than aborting
    synthesis - a planner with partial memory is still far better than one
    that never runs.
    """
    open_issues = github.list_open_issues(cfg.repo_slug, runner=runner)
    open_refs = tuple(
        TicketRef(i.number, i.title, blocked=i.is_blocked)
        for i in sorted(open_issues, key=lambda i: i.number, reverse=True)
    )

    closed_cap = int(cfg.synthesis.get("memory_closed_tickets", 15))
    closed_issues = github.list_closed_issues(cfg.repo_slug, limit=closed_cap, runner=runner)
    closed_refs = tuple(TicketRef(i.number, i.title) for i in closed_issues)

    max_lessons = int(cfg.synthesis.get("memory_max_lessons", max_lessons))
    try:
        records = KnowledgeBase.from_config(cfg, root).read_lessons()
    except OSError:
        records = []
    lesson_lines: tuple[str, ...] = ()
    if records:
        # Failures first, then by note name: a total, reproducible order.
        window = sorted(
            records[-max_lessons:], key=lambda r: (r.outcome != "fail", r.note_name)
        )
        lesson_lines = tuple(f"**{r.outcome}** - {r.title}" for r in window)

    return MemoryPack(open_tickets=open_refs, closed_tickets=closed_refs, lesson_lines=lesson_lines)


_CONVENTIONAL_PREFIX_RE = re.compile(
    r"^(feat|fix|refactor|chore|docs|test|perf|build|ci|skill)(\([^)]*\))?:\s*",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TITLE_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "with", "by",
    "is", "are", "be", "as", "at", "it", "its", "this", "that", "via",
    "add", "adds", "adding", "new", "into", "from", "so", "not", "no",
}


def _normalize_title(title: str) -> set[str]:
    """Strip a conventional-commit prefix and stopwords, tokenize the rest."""
    stripped = _CONVENTIONAL_PREFIX_RE.sub("", title.strip())
    tokens = _TOKEN_RE.findall(stripped.lower())
    return {t for t in tokens if t not in _TITLE_STOPWORDS and len(t) > 2}


def is_duplicate(
    spec: TicketSpec,
    memory: MemoryPack,
    *,
    threshold: float = DUPLICATE_JACCARD_THRESHOLD,
) -> str | None:
    """Is this candidate substantially the same as something already tried?

    Pure and side-effect-free: an exact-title check (case-insensitive), then
    normalized-token Jaccard overlap against every prior title in `memory`
    (open tickets, closed tickets, lesson titles). Returns the matched prior
    title so the caller can log/report it, or ``None`` when the candidate is
    genuinely new.
    """
    candidate_lower = spec.title.strip().lower()
    candidate_norm = _normalize_title(spec.title)
    for prior_title in memory.prior_titles():
        if candidate_lower == prior_title.strip().lower():
            return prior_title
        prior_norm = _normalize_title(prior_title)
        if not candidate_norm or not prior_norm:
            continue
        overlap = len(candidate_norm & prior_norm) / len(candidate_norm | prior_norm)
        if overlap >= threshold:
            return prior_title
    return None


def build_prompt(cfg: CoreConfig, pack: ContextPack, memory: MemoryPack | None = None) -> str:
    goals = "\n".join(f"- {g.get('id')}: {g.get('title')} - {g.get('description', '')}"
                      for g in cfg.goals)
    ideas = int(cfg.synthesis.get("ideas_target", 10))
    top = int(cfg.synthesis.get("file_top", 3))
    combine = int(cfg.synthesis.get("min_projects_combined", 3))
    max_chars = int(cfg.synthesis.get("memory_max_chars", 3000))
    mem_text = (memory or MemoryPack()).render(max_chars)
    return f"""You are the SYNTHESIS planner for ai-hyperswarm-proto-core, an
autonomous self-improving AI-swarm harness. Your job is NOT to copy one idea
from one project, but to COMBINE practices across projects into substantial,
creative improvements for THIS codebase (a Python CLI orchestrator: worktrees,
gh tickets, claude -p workers, CI gates, Obsidian knowledge base).

Project goals:
{goals}

{TRIED_HEADING} - this is our OWN history, not another project's. Do NOT
re-propose an idea whose lesson is listed here, and never duplicate the title of
a ticket that is still open or was recently closed; build on them instead:
{mem_text}

Study digest of reference projects for this cycle:
{pack.render()}

Work in three explicit phases and show them all in your output:

PHASE 1 - DIVERGE: generate {ideas} candidate improvements. Each MUST combine
practices from at least {combine} different reference projects (name them), be
implementable inside this repo, and advance a named goal.

PHASE 2 - REFLECT: critique every candidate honestly - feasibility in a small
codebase, real value vs novelty theater, risk to the loop's invariants
(ticket-linked PRs, green-gated merges, subscription-only models). A candidate
that substantially overlaps anything in "{TRIED_HEADING}" above MUST be
dropped here and its slot refilled with a genuinely new one - do not carry a
re-proposal into PHASE 3.

PHASE 3 - CONVERGE: pick the best {top} and emit them as a fenced ```json code
block: a JSON array where each element has exactly these keys:
  "title" (string, prefixed feat:/refactor:/skill:),
  "problem" (string), "proposal" (string, concrete and multi-step),
  "acceptance_criteria" (array of 3-6 verifiable strings),
  "verification_plan" (array of 2-4 concrete check strings),
  "size" ("M" or "L" - substantial work, never "S"),
  "goal_ids" (array like ["G1","G4"]),
  "synthesis_rationale" (string naming the >= {combine} projects combined and how).

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
    duplicates_rejected: int = 0
    rejected_titles: tuple[str, ...] = ()


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    root: str = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Run one synthesis pass, drop duplicates of prior work, and file the rest.

    A candidate that duplicates something already open, recently closed, or
    recorded as a lesson is dropped rather than filed - see :func:`is_duplicate`.
    Filtering never back-fills: if fewer than ``file_top`` candidates survive,
    only the survivors are filed. An honest thin block beats a padded one.
    """
    repos = pick_rotation(cfg, cycle_index)
    pack = build_context_pack(repos, runner=runner)
    memory = build_memory_pack(cfg, root=root, runner=runner)
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
    threshold = float(cfg.synthesis.get("duplicate_threshold", DUPLICATE_JACCARD_THRESHOLD))
    file_top = int(cfg.synthesis.get("file_top", 3))

    survivors: list[TicketSpec] = []
    rejected_titles: list[str] = []
    for spec in specs:
        matched = is_duplicate(spec, memory, threshold=threshold)
        if matched:
            rejected_titles.append(matched)
        else:
            survivors.append(spec)

    filed: list[int] = []
    for spec in survivors:
        num = github.create_issue(
            cfg.repo_slug, spec.title, spec.render(), spec.all_labels(), runner=runner
        )
        if num:
            filed.append(num)

    if not specs:
        error = "no parseable ticket specs in output"
    elif not survivors:
        error = f"all {len(specs)} candidate(s) were duplicates of prior work"
    elif len(survivors) < file_top:
        error = (
            f"only {len(survivors)}/{file_top} candidate(s) were novel - "
            f"{len(rejected_titles)} rejected as duplicates; filing survivors only, no backfill"
        )
    else:
        error = ""

    return SynthesisResult(
        ok=bool(filed), studied=repos, filed=filed, error=error,
        duplicates_rejected=len(rejected_titles), rejected_titles=tuple(rejected_titles),
    )
