"""Heavy-model synthesis: turn reference-project study into substantial tickets.

This is the "planner" half of the two-phase engine. Instead of one small idea
copied from one project, a heavy model:

1. receives a context pack built from a rotating subset of the reference set
   (README, recent commit subjects, CI workflow inventory - fetched via `gh`)
   *plus* a repo-memory pack of what this codebase has already tried,
2. generates ~``ideas_target`` candidate improvements, each required to COMBINE
   practices from >= ``min_projects_combined`` different reference projects,
3. runs a reflection pass - critiques its own candidates for feasibility,
   originality, and fit with the goals in core.yaml,
4. prioritizes by impact x effort and emits the top ``file_top`` as fully
   structured tickets (schema in :mod:`hsai.tickets`), which are filed on
   GitHub for the cheaper implementation agents to pick up.

Three defences keep a heavy call from being wasted:

- **Memory.** :class:`RepoMemory` renders shipped lessons, the open backlog and
  recently closed self-improve tickets into the prompt under an explicit
  "already tried" heading, so re-proposing finished work is a visible choice
  rather than an accident of statelessness.
- **Dedupe.** Whatever comes back is scored against every known ticket
  (:mod:`hsai.dedupe`) before anything is filed. Near-identical restatements are
  withheld and reported; borderline ones are filed with a ``possible-duplicate``
  label and a backlink. Existing tickets are never closed or edited.
- **Robust extraction.** Reasoning wrappers, prose preambles, nested fences and
  bare arrays all parse. When nothing parses, the raw output is persisted under
  ``.hsai/synthesis/`` and the failure mode is named, so a wasted heavy call is
  debuggable rather than a one-line shrug.

The model call goes through :mod:`hsai.ai`, so it stays subscription-only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dedupe, github
from .ai import run_agent
from .config import CoreConfig
from .dedupe import KnownTicket, Thresholds
from .github import Issue
from .knowledge import KnowledgeBase
from .models import ModelChoice
from .proc import Runner, run
from .tickets import TicketSpec

# Where an unparseable heavy-model reply is kept for the post-mortem. Local
# forensics like trajectories and journals, so it lives under gitignored .hsai/.
SYNTHESIS_DIR = ".hsai/synthesis"

# Reasoning models wrap their scratchpad in tags that are not part of the answer.
_REASONING_BLOCK = re.compile(
    r"<(think|thinking|reasoning|scratchpad)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE
)
_REASONING_CLOSE = re.compile(r"</(?:think|thinking|reasoning|scratchpad)\s*>", re.IGNORECASE)
_REASONING_OPEN = re.compile(r"<(?:think|thinking|reasoning|scratchpad)\b[^>]*>", re.IGNORECASE)

_FENCE = re.compile(r"^\s*```", re.MULTILINE)
# A ticket plan is always an array of objects; anchoring on `[{` skips markdown
# checkboxes and prose brackets that would otherwise flood the candidate list.
_ARRAY_START = re.compile(r"\[\s*\{")

_REQUIRED_KEYS = ("title", "problem", "proposal", "acceptance_criteria", "verification_plan")

_ELIDED = "\n[... elided to fit the prompt budget ...]"

MEMORY_HEADING = "What this repo has already tried"


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


# --- repo memory --------------------------------------------------------------
@dataclass(frozen=True)
class MemoryLesson:
    """One shipped lesson, compressed to what a planner needs to differentiate."""

    title: str
    headline: str
    references: tuple[str, ...] = ()

    def render(self) -> str:
        refs = f" [refs: {', '.join(self.references)}]" if self.references else ""
        return f"- {self.title}{f' - {self.headline}' if self.headline else ''}{refs}"


@dataclass
class Backlog:
    """One fetch of the repo's tickets, shared by repo memory and dedupe."""

    open_issues: list[Issue] = field(default_factory=list)
    closed_issues: list[Issue] = field(default_factory=list)


def load_backlog(cfg: CoreConfig, *, runner: Runner = run) -> Backlog:
    """Fetch open and recently-closed tickets once per synthesis pass."""
    limit = int(cfg.synthesis.get("memory_closed_tickets", 60))
    # Review briefs are governance artifacts, not proposals: they would only add
    # noise to the memory pack and can never be a candidate's duplicate.
    review_label = cfg.governance.get("review_label", "review")
    return Backlog(
        open_issues=[
            i for i in github.list_open_issues(cfg.repo_slug, runner=runner)
            if review_label not in i.labels
        ],
        closed_issues=[
            i for i in github.list_closed_issues(cfg.repo_slug, limit=limit, runner=runner)
            if review_label not in i.labels
        ],
    )


@dataclass
class RepoMemory:
    """A compact index of what this repo has already learned, filed, and shipped."""

    lessons: list[MemoryLesson] = field(default_factory=list)
    open_tickets: list[str] = field(default_factory=list)
    closed_tickets: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.lessons or self.open_tickets or self.closed_tickets)

    def render(self, *, budget: int = 6000) -> str:
        """Render the memory pack, trimmed to ``budget`` characters.

        Sections shrink from the tail (oldest entries first) and say how many
        entries were elided, so a truncated pack never silently reads as "this
        is everything the repo has done".
        """
        if self.empty:
            return (
                "_No prior lessons or tickets on record - this repo has no history "
                "to differentiate from yet._"
            )
        sections = [
            ("Lessons already learned", [lesson.render() for lesson in self.lessons]),
            ("Open backlog (already filed - do NOT re-propose)", list(self.open_tickets)),
            (
                "Recently closed tickets (already shipped, or rejected)",
                list(self.closed_tickets),
            ),
        ]
        return _fit_sections(sections, budget)


def _fit_sections(sections: list[tuple[str, list[str]]], budget: int) -> str:
    """Render titled line-lists, dropping tail entries until they fit ``budget``."""
    dropped = dict.fromkeys((name for name, _ in sections), 0)

    def render() -> str:
        parts = []
        for name, lines in sections:
            body = "\n".join(lines) if lines else "- _(none)_"
            if dropped[name]:
                body += f"\n- _(+{dropped[name]} older entr(ies) elided for prompt budget)_"
            parts.append(f"#### {name}\n{body}")
        return "\n\n".join(parts)

    text = render()
    while len(text) > budget:
        name, lines = max(sections, key=lambda s: sum(len(line) for line in s[1]))
        if not lines:
            break
        lines.pop()
        dropped[name] += 1
        text = render()
    return text


def build_repo_memory(
    cfg: CoreConfig, *, backlog: Backlog, repo_root: str | Path = "."
) -> RepoMemory:
    """Assemble repo memory from the knowledge base and the ticket backlog."""
    kb = KnowledgeBase.from_config(cfg, repo_root)
    lessons = [
        MemoryLesson(title=r.title, headline=r.headline(), references=r.references)
        for r in reversed(kb.read_lessons())  # newest first: oldest gets elided first
    ]
    closed_labels = {"self-improve", "skill"}
    return RepoMemory(
        lessons=lessons,
        open_tickets=[f"#{i.number} {i.title}" for i in backlog.open_issues],
        closed_tickets=[
            f"#{i.number} {i.title}"
            for i in backlog.closed_issues
            if closed_labels.intersection(i.labels)
        ],
    )


def known_tickets(backlog: Backlog) -> list[KnownTicket]:
    """Every ticket a candidate is scored against - open first, then closed."""
    return [
        *(KnownTicket.from_issue(i, state="open") for i in backlog.open_issues),
        *(KnownTicket.from_issue(i, state="closed") for i in backlog.closed_issues),
    ]


# --- prompt -------------------------------------------------------------------
def _shrink(text: str, over: int) -> str:
    """Cut ``over`` characters off ``text``, or drop it entirely if that is all."""
    keep = len(text) - over - len(_ELIDED)
    return text[:keep] + _ELIDED if keep > 0 else ""


def build_prompt(
    cfg: CoreConfig, pack: ContextPack, memory: RepoMemory | None = None
) -> str:
    """Render the synthesis prompt, bounded by ``synthesis.prompt_char_budget``.

    The instructions are the one part that must survive intact - a prompt that
    loses its output schema wastes the whole heavy call - so trimming eats the
    reference digest first, then the memory pack, and never the schema.
    """
    budget = int(cfg.synthesis.get("prompt_char_budget", 48000))
    memory_budget = min(int(cfg.synthesis.get("memory_char_budget", 6000)), max(budget // 3, 0))
    memory_text = (memory or RepoMemory()).render(budget=memory_budget)
    digest = pack.render()

    prompt = _render_prompt(cfg, digest, memory_text)
    for slot in ("digest", "memory"):
        if len(prompt) <= budget:
            break
        over = len(prompt) - budget
        if slot == "digest":
            digest = _shrink(digest, over)
        else:
            memory_text = _shrink(memory_text, over)
        prompt = _render_prompt(cfg, digest, memory_text)
    return prompt


def _render_prompt(cfg: CoreConfig, digest: str, memory_text: str) -> str:
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
{digest}

## {MEMORY_HEADING}
This is the repo's own memory. Treat every entry as ground already covered: do
NOT re-propose it. A candidate that touches one of these must either attack a
DIFFERENT problem, or explicitly SUPERSEDE the prior work and say in its problem
statement what the earlier attempt left unsolved.

{memory_text}

Work in three explicit phases and show them all in your output:

PHASE 1 - DIVERGE: generate {ideas} candidate improvements. Each MUST combine
practices from at least {combine} different reference projects (name them), be
implementable inside this repo, and advance a named goal.

PHASE 2 - REFLECT: critique every candidate honestly - feasibility in a small
codebase, real value vs novelty theater, risk to the loop's invariants
(ticket-linked PRs, green-gated merges, subscription-only models) - and drop
anything the memory above shows is already done.

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


# --- extraction ---------------------------------------------------------------
def strip_reasoning(output: str) -> str:
    """Remove reasoning-model scratchpad wrappers from ``output``.

    Closed ``<think>...</think>`` pairs go first. A *dangling* closer means the
    model opened its scratchpad before the transport started capturing, so
    everything up to the last closer is scratchpad and the answer follows it.
    """
    text = _REASONING_BLOCK.sub("", output or "")
    closers = list(_REASONING_CLOSE.finditer(text))
    if closers:
        text = text[closers[-1].end():]
    return _REASONING_OPEN.sub("", text).strip()


def _scan_array(text: str, start: int) -> str | None:
    """Return the balanced ``[...]`` beginning at ``start``, or None if unclosed.

    String-aware, so brackets and fence markers inside JSON string values cannot
    end the scan early - which is what lets a nested code fence survive.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def json_array_candidates(text: str) -> list[str]:
    """Every balanced array-of-objects in ``text``, in order of appearance.

    Deliberately not fence-driven: scanning the text itself finds the plan
    whether it is fenced, nested inside another fence, or emitted bare.
    """
    return [
        found
        for m in _ARRAY_START.finditer(text)
        if (found := _scan_array(text, m.start())) is not None
    ]


def _spec_from_item(item: Any) -> TicketSpec | None:
    if not isinstance(item, dict) or not all(k in item for k in _REQUIRED_KEYS):
        return None
    try:
        return TicketSpec(
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
    except TypeError:
        return None


@dataclass
class Extraction:
    """The outcome of reading a plan out of one heavy-model reply."""

    specs: list[TicketSpec] = field(default_factory=list)
    error: str = ""
    candidates: int = 0  # balanced JSON arrays seen
    dropped: int = 0  # array elements rejected for missing keys

    @property
    def ok(self) -> bool:
        return bool(self.specs)


def extract_ticket_specs(output: str) -> Extraction:
    """Recover the ticket plan from a model reply, naming the failure if absent.

    Candidates are tried last-to-first because the planner is instructed to emit
    the plan last: an earlier array is a draft, an illustration, or the schema
    quoted back at us.
    """
    text = strip_reasoning(output)
    candidates = json_array_candidates(text)
    if not candidates:
        fences = len(_FENCE.findall(text))
        return Extraction(
            error=(
                f"no JSON array of ticket objects found in model output "
                f"({len(text)} chars, {fences} fenced block(s))"
            ),
        )
    dropped = 0
    unparsed = 0
    for candidate in reversed(candidates):
        try:
            raw = json.loads(candidate)
        except json.JSONDecodeError:
            unparsed += 1
            continue
        if not isinstance(raw, list):
            continue
        specs = [spec for item in raw if (spec := _spec_from_item(item)) is not None]
        if specs:
            return Extraction(
                specs=specs, candidates=len(candidates), dropped=len(raw) - len(specs)
            )
        dropped += len(raw)
    if unparsed == len(candidates):
        return Extraction(
            candidates=len(candidates),
            error=(
                f"found {len(candidates)} JSON array candidate(s) but none was valid JSON "
                f"(malformed plan block)"
            ),
        )
    return Extraction(
        candidates=len(candidates),
        dropped=dropped,
        error=(
            f"parsed a JSON array of {dropped} element(s) but none carried the required "
            f"ticket keys ({', '.join(_REQUIRED_KEYS)})"
        ),
    )


def parse_ticket_specs(output: str) -> list[TicketSpec]:
    """Extract the ticket plan from a model reply (see :func:`extract_ticket_specs`)."""
    return extract_ticket_specs(output).specs


def persist_raw_output(repo_root: str | Path, cycle_index: int, output: str) -> str:
    """Keep an unparseable reply on disk so the wasted heavy call is debuggable."""
    path = Path(repo_root) / SYNTHESIS_DIR / f"{cycle_index}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output or "", encoding="utf-8")
    return str(path)


# --- filing -------------------------------------------------------------------
@dataclass(frozen=True)
class SkippedCandidate:
    """A candidate withheld because repo memory already covers it."""

    title: str
    score: float
    matched_issue: int
    matched_title: str
    matched_state: str = "open"

    def describe(self) -> str:
        return (
            f"{self.title} - {self.score:.2f} match with #{self.matched_issue} "
            f"({self.matched_state}) {self.matched_title}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "score": self.score,
            "matched_issue": self.matched_issue, "matched_title": self.matched_title,
            "matched_state": self.matched_state,
        }


@dataclass
class SynthesisResult:
    ok: bool
    studied: list[str]
    filed: list[int]
    error: str = ""
    skipped: list[SkippedCandidate] = field(default_factory=list)
    flagged: list[int] = field(default_factory=list)  # filed as possible-duplicate
    candidates: int = 0  # ticket specs parsed out of the reply
    prompt: str = ""  # what was (or would have been) sent
    raw_path: str = ""  # where an unparseable reply was persisted

    @property
    def filed_count(self) -> int:
        return len(self.filed)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _file_specs(
    cfg: CoreConfig,
    specs: list[TicketSpec],
    known: list[KnownTicket],
    *,
    runner: Runner,
) -> tuple[list[int], list[int], list[SkippedCandidate]]:
    """File the candidates repo memory does not already cover.

    Each filed spec joins ``known`` immediately, so two near-identical
    candidates inside one batch cannot both reach the backlog.
    """
    thresholds = Thresholds.from_config(cfg)
    filed: list[int] = []
    flagged: list[int] = []
    skipped: list[SkippedCandidate] = []
    for spec in specs:
        verdict = dedupe.classify(spec, known, thresholds=thresholds)
        if verdict.is_skip and verdict.matched is not None:
            skipped.append(
                SkippedCandidate(
                    title=spec.title, score=verdict.score,
                    matched_issue=verdict.matched.number,
                    matched_title=verdict.matched.title,
                    matched_state=verdict.matched.state,
                )
            )
            continue
        body = dedupe.annotate_body(spec.render(), verdict) if verdict.is_flag else spec.render()
        labels = spec.all_labels() + ([dedupe.DUPLICATE_LABEL] if verdict.is_flag else [])
        num = github.create_issue(cfg.repo_slug, spec.title, body, labels, runner=runner)
        known.append(KnownTicket.from_spec(spec, num))
        if num:
            filed.append(num)
            if verdict.is_flag:
                flagged.append(num)
    return filed, flagged, skipped


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    repo_root: str | Path = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
    dry_run: bool = False,
) -> SynthesisResult:
    """Run one synthesis pass and file the resulting tickets.

    ``dry_run`` renders the prompt (reference digest + repo memory) and stops
    there: no agent call, no quota spent, and no GitHub write of any kind.
    """
    repos = pick_rotation(cfg, cycle_index)
    pack = build_context_pack(repos, runner=runner)
    backlog = load_backlog(cfg, runner=runner)
    memory = build_repo_memory(cfg, backlog=backlog, repo_root=repo_root)
    prompt = build_prompt(cfg, pack, memory)
    if dry_run:
        return SynthesisResult(
            ok=True, studied=repos, filed=[], prompt=prompt,
            error="dry-run: prompt rendered; no agent call and no tickets filed",
        )

    tier = cfg.synthesis.get("tier", "heavy")
    model = cfg.tiers[tier].model if tier in cfg.tiers else cfg.tiers[cfg.default_tier].model
    choice = ModelChoice(
        tier=tier, model=model,
        rationale="synthesis is always heavy: cross-project combination + reflection",
        strategy="synthesis-v1",
    )
    ares = run_agent(
        prompt, choice, cfg,
        timeout=float(cfg.synthesis.get("timeout_seconds", 2400)),
        runner=ai_runner,
    )
    if not ares.ok:
        return SynthesisResult(
            ok=False, studied=repos, filed=[], error=ares.error[:500], prompt=prompt
        )

    extraction = extract_ticket_specs(ares.text)
    if not extraction.specs:
        raw_path = persist_raw_output(repo_root, cycle_index, ares.text)
        return SynthesisResult(
            ok=False, studied=repos, filed=[], prompt=prompt, raw_path=raw_path,
            error=f"{extraction.error}; raw output saved to {raw_path}",
        )

    filed, flagged, skipped = _file_specs(
        cfg, extraction.specs, known_tickets(backlog), runner=runner
    )
    notes = []
    if not filed and skipped:
        notes.append(
            f"all {len(skipped)} candidate(s) matched existing tickets - nothing new to file"
        )
    elif skipped:
        notes.append(f"{len(skipped)} candidate(s) skipped as near-duplicates")
    if flagged:
        notes.append(f"{len(flagged)} filed as possible-duplicate for the architect")
    return SynthesisResult(
        ok=bool(filed or skipped),
        studied=repos,
        filed=filed,
        flagged=flagged,
        skipped=skipped,
        candidates=len(extraction.specs),
        prompt=prompt,
        error="; ".join(notes),
    )
