"""Heavy-model synthesis: turn reference-project study into substantial tickets.

This is the "planner" half of the two-phase engine. Instead of one small idea
copied from one project, a heavy model:

1. receives a context pack built from a rotating subset of the reference set -
   README, recent commit subjects, CI workflow *bodies*, recent closed-PR
   titles with labels, and CONTRIBUTING / issue templates, fetched via `gh`,
2. generates ~``ideas_target`` candidate improvements, each required to COMBINE
   practices from >= ``min_projects_combined`` different reference projects,
3. runs a reflection pass - critiques its own candidates for feasibility,
   originality, and fit with the goals in core.yaml,
4. prioritizes by impact x effort and emits the top ``file_top`` as fully
   structured tickets (schema in :mod:`hsai.tickets`), which are filed on
   GitHub for the cheaper implementation agents to pick up.

Two memories keep this from re-deriving the same ideas every cycle. The mining
pass appends what it saw to durable field notes under ``knowledge/reference/``
(:class:`hsai.knowledge.Observation`), each observation addressable by a stable
``practice_id``; :class:`AdoptionIndex` then reads those notes plus lesson
frontmatter and open tickets back, tells the planner which practices are already
adopted / failed / in flight, and backs a gate that REFUSES a candidate
re-proposing one - with the reason reported, never silently dropped.

The model call goes through :mod:`hsai.ai`, so it stays subscription-only.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from . import github
from .ai import run_agent
from .config import CoreConfig, ReferenceRepo
from .knowledge import KnowledgeBase, LessonRecord, Observation, reference_note_name, slugify
from .models import ModelChoice
from .proc import Runner, run
from .tickets import TicketSpec, practice_ids_in

_logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)

# Per-artifact ceilings. The miner reads bodies now, not just names, so every
# fetch is clipped at the source and the whole per-repo section is clipped
# again: a deeper study must not push the heavy prompt into the megabytes.
README_CHARS = 4000
COMMITS_CHARS = 2000
WORKFLOW_CHARS = 1200
CONTRIBUTING_CHARS = 1500
TEMPLATE_CHARS = 900
PRS_CHARS = 1500
MAX_WORKFLOW_BODIES = 3
MAX_ISSUE_TEMPLATES = 2
CLOSED_PRS = 20
PER_REPO_CHARS = 12000

# How much of an artifact is quoted into a field-note observation. Field notes
# are durable memory, not a mirror of the upstream repo: enough to recognise the
# practice, never enough to be a copy.
OBSERVATION_CHARS = 240
OBSERVATION_LINES = 6


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


@dataclass(frozen=True)
class Artifact:
    """One concrete thing the miner read out of a reference project.

    ``name`` is the citation - a path, or the query that produced the listing -
    so every observation derived from it says where it came from.
    """

    kind: str      # readme | commits | workflow | prs | contributing | issue-template
    name: str      # the citation, e.g. ".github/workflows/ci.yml"
    text: str
    label: str = ""  # discriminator within a kind; empty when the kind is a singleton

    def render(self, repo: str) -> str:
        """The digest form. Carries the practice_id so the planner can cite it -
        the id is how a proposal, a field note and a lesson refer to the same
        observed thing."""
        return f"{self.kind} `{self.name}` (practice_id: `{self.practice_id(repo)}`):\n{self.text}"

    def practice_id(self, repo: str) -> str:
        """Stable, addressable key: ``<field note stem>-<kind>[-<label>]``.

        Built from the label rather than the full path so the id stays readable
        and, more importantly, stable: moving a workflow's directory must not
        orphan every lesson that cites it.
        """
        parts = [reference_note_name(repo), self.kind, self.label]
        return slugify("-".join(p for p in parts if p))

    def observation(self, repo: str) -> Observation:
        lines = [ln.strip() for ln in self.text.splitlines() if ln.strip()][:OBSERVATION_LINES]
        return Observation(
            practice_id=self.practice_id(repo),
            artifact=self.name,
            detail=_clip(" / ".join(lines), OBSERVATION_CHARS),
        )


@dataclass
class ContextPack:
    """What the synthesizer knows about the reference projects this cycle."""

    repos: list[str]
    sections: dict[str, str]  # repo -> digest text
    notes: dict[str, list[str]] = field(default_factory=dict)  # repo -> appended practice_ids

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


def _raw(repo: str, path: str, *, runner: Runner) -> str:
    """Raw contents of one file in a reference repo ("" when absent)."""
    p = runner(
        [
            "gh", "api", f"repos/{repo}/contents/{path}",
            "-H", "Accept: application/vnd.github.raw",
        ]
    )
    return p.stdout if p.ok else ""


def _listing(repo: str, path: str, *, runner: Runner) -> list[str]:
    """File names in a directory of a reference repo ([] when absent)."""
    p = runner(["gh", "api", f"repos/{repo}/contents/{path}", "--jq", ".[].name"])
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()] if p.ok else []


def mine_repo(repo: str, *, runner: Runner = run, commits: int = 30) -> list[Artifact]:
    """Read one reference project down to the artifacts that carry practices.

    Deeper than a README skim on purpose: workflow *bodies* say how a project
    automates triage and release, closed-PR labels say how it classifies inbound
    work, and CONTRIBUTING / issue templates say what it refuses. Every fetch
    degrades to "absent" rather than raising, so one 404 never costs the pack.
    """
    artifacts: list[Artifact] = []

    readme = runner(
        ["gh", "api", f"repos/{repo}/readme", "-H", "Accept: application/vnd.github.raw"]
    )
    if readme.ok and readme.stdout.strip():
        artifacts.append(Artifact("readme", "README", _clip(readme.stdout, README_CHARS)))

    log = runner(
        [
            "gh", "api", f"repos/{repo}/commits?per_page={commits}",
            "--jq", ".[].commit.message | split(\"\\n\")[0]",
        ]
    )
    if log.ok and log.stdout.strip():
        artifacts.append(
            Artifact("commits", f"last {commits} commit subjects", _clip(log.stdout, COMMITS_CHARS))
        )

    for name in _listing(repo, ".github/workflows", runner=runner)[:MAX_WORKFLOW_BODIES]:
        body = _raw(repo, f".github/workflows/{name}", runner=runner)
        if body.strip():
            artifacts.append(
                Artifact(
                    "workflow", f".github/workflows/{name}",
                    _clip(body, WORKFLOW_CHARS), label=name,
                )
            )

    prs = runner(
        [
            "gh", "pr", "list", "--repo", repo, "--state", "closed",
            "--limit", str(CLOSED_PRS), "--json", "title,labels",
            "--jq", '.[] | "\\(.title) [\\([.labels[].name] | join(","))]"',
        ]
    )
    if prs.ok and prs.stdout.strip():
        artifacts.append(
            Artifact(
                "prs", f"last {CLOSED_PRS} closed PR titles + labels",
                _clip(prs.stdout, PRS_CHARS),
            )
        )

    contributing = _raw(repo, "CONTRIBUTING.md", runner=runner)
    if contributing.strip():
        artifacts.append(
            Artifact("contributing", "CONTRIBUTING.md", _clip(contributing, CONTRIBUTING_CHARS))
        )

    for name in _listing(repo, ".github/ISSUE_TEMPLATE", runner=runner)[:MAX_ISSUE_TEMPLATES]:
        body = _raw(repo, f".github/ISSUE_TEMPLATE/{name}", runner=runner)
        if body.strip():
            artifacts.append(
                Artifact(
                    "issue-template", f".github/ISSUE_TEMPLATE/{name}",
                    _clip(body, TEMPLATE_CHARS), label=name,
                )
            )

    return artifacts


def build_context_pack(
    repos: list[str],
    *,
    runner: Runner = run,
    commits: int = 30,
    kb: KnowledgeBase | None = None,
    catalog: tuple[ReferenceRepo, ...] = (),
    snapshot_date: str = "",
) -> ContextPack:
    """Study digest for each repo - and, with a ``kb``, durable field notes.

    The digest feeds this cycle's prompt and is then discarded; the field notes
    are the part that survives. Persisting them here (rather than in a separate
    pass) is deliberate: the miner is the only place that has both the artifact
    and its citation in hand.
    """
    meta = {r.repo: r for r in catalog}
    sections: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    for repo in repos:
        artifacts = mine_repo(repo, runner=runner, commits=commits)
        rendered = "\n\n".join(a.render(repo) for a in artifacts)
        sections[repo] = _clip(rendered, PER_REPO_CHARS) if artifacts else "(no data fetched)"
        if kb is None or not artifacts:
            continue
        ref = meta.get(repo)
        _, appended = kb.append_observations(
            repo,
            [a.observation(repo) for a in artifacts],
            stars=ref.stars if ref else 0,
            license=ref.license if ref else "",
            snapshot_date=snapshot_date,
        )
        notes[repo] = [obs.practice_id for obs in appended]
    return ContextPack(repos=repos, sections=sections, notes=notes)


MEMORY_HEADING = "What this loop has already tried"

# Hard cap on the memory section's rendered length: it exists to keep the
# planner from re-proposing done/queued/failed work, not to crowd out the
# reference-project study material that follows it.
DEFAULT_MEMORY_MAX_CHARS = 3000
DEFAULT_MEMORY_CLOSED_LIMIT = 15
DEFAULT_MEMORY_MAX_LESSONS = 25

# Above this Jaccard token-overlap ratio (over normalized titles) a candidate
# is treated as a re-proposal of prior work, not a new idea. Documented here
# because it is the one number that decides which ideas never get filed - every
# such decision is reported as a `Refusal`, never dropped in silence.
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


ADOPTION_HEADING = "Practices already adopted, already failed, or in flight"

# Statuses a practice_id can hold. "failed" is deliberately NOT a refusal
# reason: a practice we tried and lost may be worth retrying with a better
# design, and the planner is told so explicitly.
ADOPTED, FAILED, IN_FLIGHT = "adopted", "failed", "in flight"

DEFAULT_ADOPTION_MAX_CHARS = 2000


@dataclass(frozen=True)
class AdoptionIndex:
    """Which observed practices this loop has already acted on, and how it went.

    The reference field notes say what the top-10 do; this says what we did
    about it. Without it the synthesizer re-proposes practices it merged two
    blocks ago, because a repo slug in a lesson's References is not an
    addressable fact - a practice_id is.
    """

    adopted: tuple[tuple[str, str], ...] = ()     # (practice_id, evidence)
    failed: tuple[tuple[str, str], ...] = ()
    in_flight: tuple[tuple[str, str], ...] = ()
    known: tuple[tuple[str, str], ...] = ()       # (practice_id, repo) from field notes

    @classmethod
    def build(cls, kb: KnowledgeBase, memory: MemoryPack | None = None) -> AdoptionIndex:
        """Scan field notes, lesson frontmatter, and open tickets.

        Degrades to an empty index on a missing or unreadable vault: a thin
        index beats aborting synthesis.
        """
        memory = memory or MemoryPack()
        try:
            known = tuple(
                (pid, note.repo)
                for note in kb.read_field_notes()
                for pid in note.practice_ids
            )
        except OSError:
            known = ()
        try:
            records: list[LessonRecord] = kb.read_lessons()
        except OSError:
            records = []

        adopted: dict[str, str] = {}
        failed: dict[str, str] = {}
        for record in records:
            # A lesson is written pass only when the change reached green and
            # merged; recovered / blocked runs land as fail.
            target = adopted if record.outcome == "pass" else failed
            for pid in record.practices:
                target.setdefault(pid, f"[[{record.note_name}]]")

        in_flight: dict[str, str] = {}
        for issue in memory.open_tickets:
            for pid in practice_ids_in(issue.body):
                in_flight.setdefault(pid, f"#{issue.number}")

        # A practice that merged is adopted, whatever else references it.
        for pid in adopted:
            failed.pop(pid, None)
            in_flight.pop(pid, None)

        return cls(
            adopted=tuple(sorted(adopted.items())),
            failed=tuple(sorted(failed.items())),
            in_flight=tuple(sorted(in_flight.items())),
            known=tuple(sorted(dict(known).items())),
        )

    def status(self, practice_id: str) -> tuple[str, str]:
        """``(status, evidence)`` for one practice ("" when never acted on)."""
        for status, pairs in (
            (ADOPTED, self.adopted), (IN_FLIGHT, self.in_flight), (FAILED, self.failed),
        ):
            for pid, evidence in pairs:
                if pid == practice_id:
                    return status, evidence
        return "", ""

    def render(self, *, max_chars: int = DEFAULT_ADOPTION_MAX_CHARS) -> str:
        """The prompt section. Hard-capped like the memory section."""
        blocks = (
            ("Already ADOPTED (merged) - do not re-file these:", self.adopted),
            ("Currently IN FLIGHT (open ticket) - do not re-file these:", self.in_flight),
            ("Already FAILED (recovered/blocked) - only re-file with a "
             "materially different design:", self.failed),
        )
        lines: list[str] = []
        for heading, pairs in blocks:
            if not pairs:
                continue
            lines.append(heading)
            lines.extend(f"- `{pid}` ({evidence})" for pid, evidence in pairs)
            lines.append("")
        if self.known:
            lines.append(
                f"Practices on record in knowledge/reference/ but not yet acted on: "
                f"{len(self.known)} (cite their practice_id when you propose one)."
            )
        text = "\n".join(lines).strip()
        if not text:
            return "_(no practice has been recorded yet - every observation is fair game)_"
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."


def build_prompt(
    cfg: CoreConfig,
    pack: ContextPack,
    memory: MemoryPack | None = None,
    adoption: AdoptionIndex | None = None,
) -> str:
    goals = "\n".join(f"- {g.get('id')}: {g.get('title')} - {g.get('description', '')}"
                      for g in cfg.goals)
    ideas = int(cfg.synthesis.get("ideas_target", 10))
    top = int(cfg.synthesis.get("file_top", 3))
    combine = int(cfg.synthesis.get("min_projects_combined", 3))
    memory = memory or MemoryPack()
    max_chars = int(cfg.synthesis.get("memory_max_chars", DEFAULT_MEMORY_MAX_CHARS))
    memory_section = memory.render(max_chars=max_chars)
    adoption = adoption or AdoptionIndex()
    adoption_section = adoption.render(
        max_chars=int(cfg.synthesis.get("adoption_max_chars", DEFAULT_ADOPTION_MAX_CHARS))
    )
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

{ADOPTION_HEADING} - these are `practice_id`s from our own field notes in
knowledge/reference/. A candidate whose practice is adopted or in flight is
REFUSED before filing, so proposing one wastes the slot:
{adoption_section}

Study digest of reference projects for this cycle. Every artifact is tagged with
the `practice_id` under which it is recorded in our field notes - cite the ids of
the practices your proposal builds on:
{pack.render()}

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
  "practice_ids" (array of the `practice_id` slugs from the field notes above
    whose observed practice this ticket adopts - required, and none of them may
    be listed as adopted or in flight).

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
                    labels=("self-improve", "hsai", "priority:P2"),
                )
            )
        except (KeyError, TypeError):
            continue
    return specs


@dataclass(frozen=True)
class Refusal:
    """One candidate the dedupe gate declined to file, and why.

    Refusals are reported, never silently dropped: a wrongly-suppressed idea has
    to be visible to the architect in the block brief, or the gate becomes a
    place where good work disappears.
    """

    title: str
    reason: str
    matched: str = ""  # the prior title / practice the candidate collided with

    def line(self) -> str:
        return f"{self.title} - {self.reason}"

    def payload(self) -> dict[str, str]:
        """JSON-safe form for the cycle journal."""
        return {"title": self.title, "reason": self.reason, "matched": self.matched}


@dataclass
class SynthesisResult:
    ok: bool
    studied: list[str]
    filed: list[int]
    error: str = ""
    refused: list[Refusal] = field(default_factory=list)

    @property
    def rejected(self) -> int:
        return len(self.refused)

    @property
    def rejected_titles(self) -> list[str]:
        """What each refusal collided with - the candidate itself when nothing."""
        return [r.matched or r.title for r in self.refused]


def refuse_reason(
    spec: TicketSpec,
    memory: MemoryPack,
    adoption: AdoptionIndex,
    *,
    threshold: float = DUPLICATE_JACCARD_THRESHOLD,
) -> Refusal | None:
    """Should this spec be refused? Pure; ``None`` means "file it".

    Two independent gates, practice first because it is the precise one: a
    practice_id is an exact fact about what we already did, whereas title
    overlap is a heuristic about what we might have.
    """
    for pid in spec.practice_ids:
        status, evidence = adoption.status(pid)
        if status in (ADOPTED, IN_FLIGHT):
            return Refusal(
                title=spec.title,
                reason=f"practice `{pid}` is already {status} ({evidence})",
                matched=pid,
            )
    dup, matched = is_duplicate(spec, memory, threshold=threshold)
    if dup:
        return Refusal(
            title=spec.title,
            reason=f"title is a near-duplicate of prior work: {matched!r}",
            matched=matched,
        )
    return None


def _apply_gate(
    specs: list[TicketSpec],
    memory: MemoryPack,
    adoption: AdoptionIndex,
    *,
    threshold: float,
) -> tuple[list[TicketSpec], list[Refusal]]:
    """Split specs into those to file and those to refuse.

    Never back-fills: an honest thin block (fewer than `file_top` tickets
    filed) beats padding the backlog with a duplicate, and the caller surfaces
    why in `SynthesisResult` / the block review brief.
    """
    survivors: list[TicketSpec] = []
    refused: list[Refusal] = []
    for spec in specs:
        refusal = refuse_reason(spec, memory, adoption, threshold=threshold)
        if refusal is None:
            survivors.append(spec)
        else:
            _logger.info("synthesis: refusing %r - %s", spec.title, refusal.reason)
            refused.append(refusal)
    return survivors, refused


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    root: str = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Run one synthesis pass: mine, remember, refuse re-proposals, file the rest."""
    repos = pick_rotation(cfg, cycle_index)
    kb = KnowledgeBase.from_config(cfg, root)
    pack = build_context_pack(
        repos, runner=runner, kb=kb,
        catalog=cfg.reference_top10, snapshot_date=cfg.reference_snapshot,
    )
    memory = MemoryPack.gather(cfg, root=root, runner=runner)
    adoption = AdoptionIndex.build(kb, memory)
    tier = cfg.synthesis.get("tier", "heavy")
    model = cfg.tiers[tier].model if tier in cfg.tiers else cfg.tiers[cfg.default_tier].model
    choice = ModelChoice(
        tier=tier, model=model,
        rationale="synthesis is always heavy: cross-project combination + reflection",
        strategy="synthesis-v1",
    )
    ares = run_agent(
        build_prompt(cfg, pack, memory, adoption), choice, cfg,
        timeout=float(cfg.synthesis.get("timeout_seconds", 2400)),
        runner=ai_runner,
    )
    if not ares.ok:
        return SynthesisResult(ok=False, studied=repos, filed=[], error=ares.error[:500])

    specs = parse_ticket_specs(ares.output)
    threshold = float(cfg.synthesis.get("duplicate_threshold", DUPLICATE_JACCARD_THRESHOLD))
    survivors, refused = _apply_gate(specs, memory, adoption, threshold=threshold)

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
        error = f"all {len(specs)} candidate(s) refused as re-proposals of prior work"
    else:
        error = ""
    return SynthesisResult(
        ok=bool(filed), studied=repos, filed=filed, error=error, refused=refused,
    )
