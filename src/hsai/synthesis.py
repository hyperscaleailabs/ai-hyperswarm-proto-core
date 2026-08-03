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
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from . import github
from .ai import run_agent
from .config import CoreConfig
from .models import ModelChoice
from .practices import Practice, PracticeRegistry
from .proc import Runner, run
from .tickets import TicketSpec

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
_TITLE_PREFIX_RE = re.compile(
    r"^(feat|fix|chore|refactor|skill|docs|perf|test|improve)\s*:\s*", re.IGNORECASE
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

DEFAULT_DEDUPE_THRESHOLD = 0.85


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


def _render_practices_section(registry: PracticeRegistry | None) -> str:
    """The 'do not re-propose' section: practices already adopted or rejected."""
    records = registry.non_proposed() if registry is not None else []
    if not records:
        return "_(none recorded yet - the registry is empty or everything is still proposed)_"
    return "\n".join(
        f"- **{r.title}** - status: {r.status} (source: {r.source_repo or 'unknown'})"
        for r in records
    )


def build_prompt(
    cfg: CoreConfig, pack: ContextPack, registry: PracticeRegistry | None = None
) -> str:
    goals = "\n".join(f"- {g.get('id')}: {g.get('title')} - {g.get('description', '')}"
                      for g in cfg.goals)
    ideas = int(cfg.synthesis.get("ideas_target", 10))
    top = int(cfg.synthesis.get("file_top", 3))
    combine = int(cfg.synthesis.get("min_projects_combined", 3))
    practices = _render_practices_section(registry)
    return f"""You are the SYNTHESIS planner for ai-hyperswarm-proto-core, an
autonomous self-improving AI-swarm harness. Your job is NOT to copy one idea
from one project, but to COMBINE practices across projects into substantial,
creative improvements for THIS codebase (a Python CLI orchestrator: worktrees,
gh tickets, claude -p workers, CI gates, Obsidian knowledge base).

Project goals:
{goals}

Study digest of reference projects for this cycle:
{pack.render()}

Practices already adopted or rejected - do not re-propose:
{practices}

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
class SkippedSpec:
    """A TicketSpec the dedupe gate refused to file."""

    title: str
    matched_title: str
    matched_issue: int = 0  # 0 when the match was a practice with no linked ticket

    def describe(self) -> str:
        target = f"issue #{self.matched_issue}" if self.matched_issue else "an adopted/rejected practice"
        return f"{self.title!r} (matches {target}: {self.matched_title!r})"


@dataclass
class SynthesisResult:
    ok: bool
    studied: list[str]
    filed: list[int]
    error: str = ""
    skipped: list[SkippedSpec] = field(default_factory=list)


def normalize_title(title: str) -> str:
    """Strip the kind prefix and punctuation so titles compare on substance,
    not phrasing - the same normalization discipline crewAI's pr-title.yml
    applies before matching a PR title against a convention."""
    lowered = _TITLE_PREFIX_RE.sub("", title.strip().lower())
    return _NON_ALNUM_RE.sub(" ", lowered).strip()


def _find_duplicate(
    title: str,
    candidates: list[tuple[str, int]],
    threshold: float,
) -> tuple[str, int] | None:
    """Best match for ``title`` among (title, issue_number) candidates, or None."""
    norm = normalize_title(title)
    best: tuple[str, int] | None = None
    best_ratio = 0.0
    for cand_title, cand_num in candidates:
        ratio = SequenceMatcher(None, norm, normalize_title(cand_title)).ratio()
        if ratio >= threshold and ratio > best_ratio:
            best, best_ratio = (cand_title, cand_num), ratio
    return best


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    repo_dir: str = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Run one synthesis pass and file the resulting tickets.

    Before filing, every candidate is checked against a normalized-title
    dedupe: existing open AND closed GitHub issues, plus any non-``proposed``
    practice in the registry (already adopted or rejected). A match above
    ``synthesis.dedupe_threshold`` (default 0.85) is skipped, not filed, and
    recorded in the result rather than silently dropped. Each ticket actually
    filed gets a ``proposed`` practice note linked to its ticket number.
    """
    repos = pick_rotation(cfg, cycle_index)
    pack = build_context_pack(repos, runner=runner)
    registry = PracticeRegistry(repo_dir)
    tier = cfg.synthesis.get("tier", "heavy")
    model = cfg.tiers[tier].model if tier in cfg.tiers else cfg.tiers[cfg.default_tier].model
    choice = ModelChoice(
        tier=tier, model=model,
        rationale="synthesis is always heavy: cross-project combination + reflection",
        strategy="synthesis-v1",
    )
    ares = run_agent(
        build_prompt(cfg, pack, registry), choice, cfg,
        timeout=float(cfg.synthesis.get("timeout_seconds", 2400)),
        runner=ai_runner,
    )
    if not ares.ok:
        return SynthesisResult(ok=False, studied=repos, filed=[], error=ares.error[:500])

    specs = parse_ticket_specs(ares.output)

    existing_issues = github.list_issues(cfg.repo_slug, state="all", limit=200, runner=runner)
    candidates: list[tuple[str, int]] = [(i.title, i.number) for i in existing_issues]
    candidates += [(r.title, r.ticket or 0) for r in registry.non_proposed()]
    threshold = float(cfg.synthesis.get("dedupe_threshold", DEFAULT_DEDUPE_THRESHOLD))

    filed: list[int] = []
    skipped: list[SkippedSpec] = []
    for spec in specs:
        match = _find_duplicate(spec.title, candidates, threshold)
        if match is not None:
            skipped.append(
                SkippedSpec(title=spec.title, matched_title=match[0], matched_issue=match[1])
            )
            continue
        num = github.create_issue(
            cfg.repo_slug, spec.title, spec.render(), spec.all_labels(), runner=runner
        )
        if num:
            filed.append(num)
            registry.write(
                Practice(
                    title=spec.title,
                    source_repo=", ".join(repos),
                    summary=spec.synthesis_rationale or spec.problem,
                    status="proposed",
                    ticket=num,
                )
            )
    return SynthesisResult(
        ok=bool(filed), studied=repos, filed=filed, skipped=skipped,
        error="" if specs else "no parseable ticket specs in output",
    )
