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
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from . import github
from .ai import run_agent
from .config import CoreConfig
from .knowledge import KnowledgeBase, Practice, slugify
from .models import ModelChoice
from .proc import Runner, run
from .tickets import TicketSpec

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
# "gpt-researcher (per-claim source attribution ...)" - how the planner already
# writes its rationale, and the fallback when it omits the `practices` array.
_RATIONALE_CLAUSE = re.compile(r"([A-Za-z0-9][\w./-]*)\s*\(([^()]{10,})\)")
# Two flavours of missing evidence, kept distinct so a note never overstates
# where it came from.
_NO_ARTIFACT = "(not recorded by the planner)"
_RATIONALE_ARTIFACT = "(not recorded: derived from the synthesis rationale)"


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


TRIED_HEADING = "Already tried in this repo"


def build_tried_digest(
    cfg: CoreConfig,
    *,
    root: str = ".",
    runner: Runner = run,
    max_lessons: int = 25,
) -> str:
    """What this repo has already attempted: lessons (with outcomes) + open tickets.

    Without it the planner keeps re-proposing ideas that were tried and failed,
    because its only context is a freshly fetched digest of OTHER projects.
    Failures are listed first - they are the ones worth not repeating.
    """
    lines: list[str] = []
    try:
        records = KnowledgeBase.from_config(cfg, root).read_lessons()
    except OSError:
        records = []
    if records:
        # Failures first, then by note name: a total, reproducible order.
        window = sorted(
            records[-max_lessons:], key=lambda r: (r.outcome != "fail", r.note_name)
        )
        lines.append("Lessons already recorded (outcome - title):")
        lines.extend(f"- **{r.outcome}** - {r.title}" for r in window)

    filed = [
        i.title for i in github.list_open_issues(cfg.repo_slug, runner=runner)
        if "self-improve" in i.labels
    ]
    if filed:
        lines.append("")
        lines.append("Synthesis tickets already filed and still open:")
        lines.extend(f"- {t}" for t in sorted(set(filed)))

    return "\n".join(lines) or "_(nothing recorded yet - this is an early cycle)_"


def build_prompt(cfg: CoreConfig, pack: ContextPack, tried: str = "") -> str:
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

{TRIED_HEADING} - this is our OWN history, not another project's. Do NOT
re-propose an idea whose lesson is listed here, and never duplicate the title of
a ticket that is still open; build on them instead:
{tried or "_(nothing recorded yet - this is an early cycle)_"}

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
  "practices" (array of >= {combine} objects, one per project you actually drew
    on, each with "source_repo" (owner/name from the reference set), "artifact"
    (the concrete thing you looked at: a file path, a workflow filename, a PR or
    commit URL), "observation" (what that project really does), and "adaptation"
    (what we should do here instead)).

Every practice becomes a registry note cited by the ticket, so an artifact you
did not actually see in the digest above must be left as an empty string rather
than guessed.

The JSON block must be the LAST fenced block in your reply."""


def _practice_id(source_repo: str, title: str) -> str:
    """A stable, readable id: which project, for which ticket."""
    project = slugify(source_repo.split("/")[-1])
    subject = slugify(title.split(":", 1)[-1])[:48].rstrip("-")
    return f"{project}-{subject}".strip("-") or "unnamed-practice"


def _match_repo(name: str, known_repos: Iterable[str]) -> str:
    """Resolve a bare project name from prose to its pinned ``owner/name`` slug."""
    lowered = name.lower()
    for repo in known_repos:
        if lowered in (repo.lower(), repo.split("/")[-1].lower()):
            return repo
    return ""


def practices_from_rationale(
    rationale: str, title: str, known_repos: Iterable[str]
) -> list[Practice]:
    """Fallback provenance: the projects the rationale itself names.

    Weaker evidence than an explicit ``practices`` entry - there is no artifact
    to point at - so the note says so out loud instead of inventing a file path.
    """
    known = list(known_repos)
    practices: list[Practice] = []
    seen: set[str] = set()
    for name, observation in _RATIONALE_CLAUSE.findall(rationale or ""):
        repo = _match_repo(name, known)
        if not repo or repo in seen:
            continue
        seen.add(repo)
        practices.append(
            Practice(
                id=_practice_id(repo, title),
                source_repo=repo,
                artifact=_RATIONALE_ARTIFACT,
                observation=" ".join(observation.split()),
                adaptation=f"Adapted here by the ticket \"{title}\".",
            )
        )
    return practices


def _parse_practices(item: dict, title: str, known_repos: Iterable[str]) -> list[Practice]:
    """Practice records for one ticket: explicit entries first, rationale second."""
    practices: list[Practice] = []
    seen: set[str] = set()
    for entry in item.get("practices") or []:
        if not isinstance(entry, dict):
            continue
        source_repo = str(entry.get("source_repo", "")).strip()
        observation = str(entry.get("observation", "")).strip()
        if not source_repo or not observation:
            continue  # a practice without a source or an observation is not evidence
        pid = slugify(str(entry.get("id", "")) or _practice_id(source_repo, title))
        if pid in seen:
            continue
        seen.add(pid)
        practices.append(
            Practice(
                id=pid,
                source_repo=source_repo,
                artifact=str(entry.get("artifact", "")).strip() or _NO_ARTIFACT,
                observation=observation,
                adaptation=str(entry.get("adaptation", "")).strip()
                or f"Adapted here by the ticket \"{title}\".",
            )
        )
    if practices:
        return practices
    return practices_from_rationale(
        str(item.get("synthesis_rationale", "")), title, known_repos
    )


def parse_ticket_specs(
    output: str, *, known_repos: Iterable[str] = ()
) -> list[TicketSpec]:
    """Extract the final JSON block and convert it into TicketSpecs.

    ``known_repos`` (the pinned reference set) bounds the rationale fallback: a
    project we never pinned is not accepted as a source.
    """
    blocks = _JSON_BLOCK.findall(output)
    if not blocks:
        return []
    try:
        raw = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return []
    known = list(known_repos)
    specs: list[TicketSpec] = []
    for item in raw:
        try:
            title = str(item["title"])[:150]
            specs.append(
                TicketSpec(
                    title=title,
                    problem=str(item["problem"]),
                    proposal=str(item["proposal"]),
                    acceptance_criteria=tuple(str(c) for c in item["acceptance_criteria"]),
                    verification_plan=tuple(str(v) for v in item["verification_plan"]),
                    size=str(item.get("size", "M")),
                    goal_ids=tuple(str(g) for g in item.get("goal_ids", [])),
                    synthesis_rationale=str(item.get("synthesis_rationale", "")),
                    practices=tuple(_parse_practices(item, title, known)),
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
    practices: list[str] = field(default_factory=list)  # registry ids written


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    root: str = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Run one synthesis pass and file the resulting tickets."""
    repos = pick_rotation(cfg, cycle_index)
    pack = build_context_pack(repos, runner=runner)
    tried = build_tried_digest(cfg, root=root, runner=runner)
    tier = cfg.synthesis.get("tier", "heavy")
    model = cfg.tiers[tier].model if tier in cfg.tiers else cfg.tiers[cfg.default_tier].model
    choice = ModelChoice(
        tier=tier, model=model,
        rationale="synthesis is always heavy: cross-project combination + reflection",
        strategy="synthesis-v1",
    )
    ares = run_agent(
        build_prompt(cfg, pack, tried), choice, cfg,
        timeout=float(cfg.synthesis.get("timeout_seconds", 2400)),
        runner=ai_runner,
    )
    if not ares.ok:
        return SynthesisResult(ok=False, studied=repos, filed=[], error=ares.error[:500])

    specs = parse_ticket_specs(
        ares.output, known_repos=[r.repo for r in cfg.reference_top10]
    )
    kb = KnowledgeBase.from_config(cfg, root)
    filed: list[int] = []
    written: list[str] = []
    for spec in specs:
        # The registry note has to exist before the ticket that cites it: the
        # orchestrator's evidence guard resolves those citations later, and a
        # dangling one is exactly what it is there to catch.
        for practice in spec.practices:
            kb.write_practice(practice)
            written.append(practice.note_name())
        num = github.create_issue(
            cfg.repo_slug, spec.title, spec.render(), spec.all_labels(), runner=runner
        )
        if num:
            filed.append(num)
            # Close the loop the other way too: the practice records which
            # ticket adopted it, so the registry is navigable from either end.
            for practice in spec.practices:
                kb.write_practice(replace(practice, adopted_by=(f"#{num}",)))
    return SynthesisResult(ok=bool(filed), studied=repos, filed=filed,
                           error="" if specs else "no parseable ticket specs in output",
                           practices=written)
