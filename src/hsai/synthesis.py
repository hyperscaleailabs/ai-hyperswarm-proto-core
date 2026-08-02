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
from dataclasses import dataclass, replace

from . import github, practices
from .ai import run_agent
from .config import CoreConfig
from .models import ModelChoice
from .practices import PracticeProposal, Registry
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


def build_prompt(cfg: CoreConfig, pack: ContextPack, catalog: str = "") -> str:
    goals = "\n".join(f"- {g.get('id')}: {g.get('title')} - {g.get('description', '')}"
                      for g in cfg.goals)
    ideas = int(cfg.synthesis.get("ideas_target", 10))
    top = int(cfg.synthesis.get("file_top", 3))
    combine = int(cfg.synthesis.get("min_projects_combined", 3))
    return f"""You are the SYNTHESIS planner for ai-hyperswarm-proto-core, an
autonomous self-improving AI-swarm harness. Your job is NOT to copy one idea
from one project, but to COMBINE practices across projects into substantial,
creative improvements for THIS codebase (a Python CLI orchestrator: worktrees,
gh tickets, claude -p workers, CI gates, Obsidian knowledge base).

Project goals:
{goals}

Study digest of reference projects for this cycle:
{pack.render()}

Practice cards already registered in knowledge/practices/ (cite these ids
whenever the practice you are drawing on is one of them):
{catalog or "_(none registered yet)_"}

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
  "practices" (array of >= {combine} objects, ONE PER PRACTICE YOU DREW ON:
     {{"id": "PR-0003"}} when it is already registered above, otherwise
     {{"title": short practice name, "source_repo": "owner/name" from the pinned
      reference set, "artifact_kind": one of code|ci|commit|issue|readme,
      "artifact_ref": the exact path / workflow file / commit SHA / issue number
      you observed it in, "what": what the project does, "why": why it applies
      here}} - never invent an artifact you did not see in the digest above).

Every ticket MUST carry citable evidence: unregistered practices are filed as
new cards, and a ticket whose evidence cannot be resolved is refused by the
loop rather than implemented.

The JSON block must be the LAST fenced block in your reply."""


def _proposals(item: dict) -> list[PracticeProposal]:
    """Read the practices the model claims it drew on, ignoring malformed ones."""
    out: list[PracticeProposal] = []
    for raw in item.get("practices", []) or []:
        if not isinstance(raw, dict):
            continue
        fields = {
            k: str(v) for k, v in raw.items()
            if k in PracticeProposal.__dataclass_fields__ and v is not None
        }
        if fields:
            out.append(PracticeProposal(**fields))
    return out


def parse_ticket_specs(output: str, *, registry: Registry | None = None) -> list[TicketSpec]:
    """Extract the final JSON block and convert it into TicketSpecs.

    With a ``registry``, each ticket's claimed practices are resolved to card
    ids - filing a card for any practice not yet registered - so the filed
    ticket carries a citation the loop can later resolve. Without one, only
    already-known ids are carried through.
    """
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
            proposals = _proposals(item)
            practice_ids = (
                registry.resolve_all(proposals)
                if registry is not None
                else tuple(p.id for p in proposals if p.id)
            )
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
                    practice_ids=practice_ids,
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


def _ensure_citation(spec: TicketSpec, registry: Registry, cfg: CoreConfig) -> TicketSpec:
    """Back-fill a ticket's citation from its own synthesis rationale.

    The rationale always names the projects combined; when the model forgot the
    structured ``practices`` block, any registered card for one of those repos
    is a real, checkable citation - and better than filing a ticket the loop
    will refuse for lack of evidence.
    """
    if spec.practice_ids:
        return spec
    repos = practices.parse_repo_slugs(spec.synthesis_rationale, cfg.pinned_repos())
    return replace(spec, practice_ids=registry.cards_for_repos(repos))


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    repo_dir: str = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Run one synthesis pass and file the resulting tickets."""
    repos = pick_rotation(cfg, cycle_index)
    pack = build_context_pack(repos, runner=runner)
    registry = Registry(repo_dir, cfg)
    tier = cfg.synthesis.get("tier", "heavy")
    model = cfg.tiers[tier].model if tier in cfg.tiers else cfg.tiers[cfg.default_tier].model
    choice = ModelChoice(
        tier=tier, model=model,
        rationale="synthesis is always heavy: cross-project combination + reflection",
        strategy="synthesis-v1",
    )
    ares = run_agent(
        build_prompt(cfg, pack, registry.catalog()), choice, cfg,
        timeout=float(cfg.synthesis.get("timeout_seconds", 2400)),
        runner=ai_runner,
    )
    if not ares.ok:
        return SynthesisResult(ok=False, studied=repos, filed=[], error=ares.error[:500])

    specs = [
        _ensure_citation(s, registry, cfg)
        for s in parse_ticket_specs(ares.output, registry=registry)
    ]
    filed: list[int] = []
    for spec in specs:
        num = github.create_issue(
            cfg.repo_slug, spec.title, spec.render(), spec.all_labels(), runner=runner
        )
        if num:
            filed.append(num)
    return SynthesisResult(ok=bool(filed), studied=repos, filed=filed,
                           error="" if specs else "no parseable ticket specs in output")
