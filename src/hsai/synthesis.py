"""Heavy-model synthesis: turn reference-project study into substantial tickets.

This is the "planner" half of the two-phase engine. Instead of one small idea
copied from one project, a heavy model:

0. reads the *durable field notes* the loop has already accumulated about the
   reference set, plus an adoption index of which observed practices were
   merged, failed, or are still in flight - so heavy-tier quota buys new ideas
   instead of re-deriving old ones,
1. receives a context pack built from a rotating subset of the reference set
   (README, recent commit subjects, CI workflow bodies, closed-PR titles and
   labels, contribution/issue-template policy - fetched via `gh`), every pass of
   which is appended to that project's field note as dated, artifact-citing
   observations,
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
import logging
import re
from dataclasses import dataclass, field

from . import github
from .ai import run_agent
from .config import CoreConfig, ReferenceRepo
from .knowledge import FieldNote, KnowledgeBase, Observation, practice_id
from .models import ModelChoice
from .proc import Runner, run
from .tickets import TicketSpec, parse_practice_ids

_logger = logging.getLogger(__name__)

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


# Per-repo character budget for the study digest. The miner now fetches
# workflow *bodies*, closed-PR labels and contribution policy on top of the
# README, so without a ceiling three repos could swamp the heavy prompt.
DEFAULT_SECTION_MAX_CHARS = 6000
# How many workflow files are read in full. Bodies are where the actual
# automation lives (names alone say "issue_classifier.yml exists" and nothing
# about what it classifies), but they are also the most expensive part.
DEFAULT_WORKFLOW_BODIES = 3
DEFAULT_CLOSED_PRS = 20


def _raw(runner: Runner, repo: str, path: str) -> str:
    """Fetch one file's raw contents ("" when it does not exist)."""
    proc = runner(
        [
            "gh", "api", f"repos/{repo}/contents/{path}",
            "-H", "Accept: application/vnd.github.raw",
        ]
    )
    return proc.stdout if proc.ok else ""


@dataclass
class RepoDigest:
    """The raw material one mining pass pulled out of one project.

    Kept as structured fields rather than one blob because two consumers read
    it for different reasons: the prompt wants prose, the field note wants to
    cite each artifact separately.
    """

    repo: str
    readme: str = ""
    commits: str = ""
    workflow_names: tuple[str, ...] = ()
    workflow_bodies: tuple[tuple[str, str], ...] = ()  # (filename, body)
    closed_prs: str = ""
    contributing: str = ""
    issue_templates: tuple[str, ...] = ()

    def render(self, *, max_chars: int = DEFAULT_SECTION_MAX_CHARS) -> str:
        parts: list[str] = []
        if self.readme.strip():
            parts.append("README (truncated):\n" + self.readme[:2500])
        if self.commits.strip():
            parts.append("Recent commit subjects:\n" + self.commits[:1500])
        if self.workflow_names:
            parts.append("CI workflows:\n" + "\n".join(self.workflow_names)[:500])
        for name, body in self.workflow_bodies:
            parts.append(f"Workflow `.github/workflows/{name}` (truncated):\n{body[:1200]}")
        if self.closed_prs.strip():
            parts.append("Recently closed PRs (title [labels]):\n" + self.closed_prs[:1500])
        if self.contributing.strip():
            parts.append("CONTRIBUTING.md (truncated):\n" + self.contributing[:1200])
        if self.issue_templates:
            parts.append("Issue templates:\n" + "\n".join(self.issue_templates)[:400])
        text = "\n\n".join(parts)
        if not text:
            return "(no data fetched)"
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."

    def observations(self) -> list[Observation]:
        """Turn this pass into dated, artifact-citing field-note entries.

        Deliberately mechanical - no model call. An observation is only emitted
        when the artifact backing it was actually fetched, so the note can never
        claim evidence the miner did not see.
        """
        found: list[Observation] = []
        for name, body in self.workflow_bodies:
            if not body.strip():
                continue
            found.append(
                Observation(
                    practice=f"ci workflow {name}",
                    artifact=f"`.github/workflows/{name}`",
                    what=_first_meaningful_lines(body, limit=3),
                    why="CI automation this repo could mirror in its own gates.",
                    practice_id=practice_id(self.repo, f"ci-{name}"),
                )
            )
        if self.closed_prs.strip():
            found.append(
                Observation(
                    practice="closed-PR label taxonomy",
                    artifact=f"most recent {DEFAULT_CLOSED_PRS} closed pull requests",
                    what=_first_meaningful_lines(self.closed_prs, limit=5),
                    why="How inbound work is triaged and classified before review.",
                    practice_id=practice_id(self.repo, "closed-pr-labels"),
                )
            )
        if self.contributing.strip():
            found.append(
                Observation(
                    practice="contribution policy",
                    artifact="`CONTRIBUTING.md`",
                    what=_first_meaningful_lines(self.contributing, limit=4),
                    why="The contract contributors are held to before code is read.",
                    practice_id=practice_id(self.repo, "contributing"),
                )
            )
        if self.issue_templates:
            found.append(
                Observation(
                    practice="structured issue intake",
                    artifact="`.github/ISSUE_TEMPLATE/` - "
                    + ", ".join(self.issue_templates[:6]),
                    what="Inbound issues are forced into named templates rather than free text.",
                    why="Structured intake is what makes automated triage possible at all.",
                    practice_id=practice_id(self.repo, "issue-templates"),
                )
            )
        return found


def _first_meaningful_lines(text: str, *, limit: int = 3, width: int = 160) -> str:
    """A short, single-line digest of a fetched artifact, safe to embed in a note."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return "; ".join(lines[:limit])[:width] or "_(empty)_"


def mine_repo(repo: str, *, runner: Runner = run, commits: int = 30) -> RepoDigest:
    """Fetch one project's study material. Every call degrades to "" on failure."""
    digest = RepoDigest(repo=repo)
    readme = runner(
        ["gh", "api", f"repos/{repo}/readme", "-H", "Accept: application/vnd.github.raw"]
    )
    if readme.ok:
        digest.readme = readme.stdout
    log = runner(
        [
            "gh", "api", f"repos/{repo}/commits?per_page={commits}",
            "--jq", ".[].commit.message | split(\"\\n\")[0]",
        ]
    )
    if log.ok:
        digest.commits = log.stdout
    workflows = runner(
        ["gh", "api", f"repos/{repo}/contents/.github/workflows", "--jq", ".[].name"]
    )
    if workflows.ok and workflows.stdout.strip():
        names = tuple(n.strip() for n in workflows.stdout.splitlines() if n.strip())
        digest.workflow_names = names
        bodies: list[tuple[str, str]] = []
        for name in names[:DEFAULT_WORKFLOW_BODIES]:
            body = _raw(runner, repo, f".github/workflows/{name}")
            if body.strip():
                bodies.append((name, body))
        digest.workflow_bodies = tuple(bodies)
    prs = runner(
        [
            "gh", "api", f"repos/{repo}/pulls?state=closed&per_page={DEFAULT_CLOSED_PRS}",
            # Titles WITH their labels: the label is the triage decision, and a
            # bare title cannot show how inbound work gets classified.
            "--jq", '.[] | "\\(.title) [\\([.labels[].name] | join(","))]"',
        ]
    )
    if prs.ok:
        digest.closed_prs = prs.stdout
    digest.contributing = _raw(runner, repo, "CONTRIBUTING.md")
    templates = runner(
        ["gh", "api", f"repos/{repo}/contents/.github/ISSUE_TEMPLATE", "--jq", ".[].name"]
    )
    if templates.ok and templates.stdout.strip():
        digest.issue_templates = tuple(
            n.strip() for n in templates.stdout.splitlines() if n.strip()
        )
    return digest


def build_context_pack(
    repos: list[str],
    *,
    runner: Runner = run,
    commits: int = 30,
    kb: KnowledgeBase | None = None,
    facts: dict[str, ReferenceRepo] | None = None,
    snapshot_date: str = "",
    max_section_chars: int = DEFAULT_SECTION_MAX_CHARS,
) -> ContextPack:
    """Fetch a study digest for each repo, and persist what was mined.

    When ``kb`` is given, each pass appends its observations to that project's
    append-only field note under ``knowledge/reference/``. That is the whole
    point: the digest used to be discarded the moment the cycle ended, so every
    rotation paid heavy-tier quota to re-derive the same shallow observations.
    """
    sections: dict[str, str] = {}
    for repo in repos:
        digest = mine_repo(repo, runner=runner, commits=commits)
        sections[repo] = digest.render(max_chars=max_section_chars)
        if kb is not None:
            ref = (facts or {}).get(repo)
            kb.append_field_note(
                FieldNote(
                    repo=repo,
                    stars=ref.stars if ref else 0,
                    license=ref.license if ref else "",
                    snapshot_date=snapshot_date,
                    observations=tuple(digest.observations()),
                )
            )
    return ContextPack(repos=repos, sections=sections)


def repo_facts(cfg: CoreConfig) -> dict[str, ReferenceRepo]:
    """Pinned stars/license per reference repo, keyed by slug."""
    return {r.repo: r for r in cfg.reference_top10}


def reference_snapshot_date(cfg: CoreConfig) -> str:
    """When the pinned reference set was last refreshed (stamped into field notes)."""
    return str(cfg.raw.get("reference_set", {}).get("snapshot_date", "") or "")


MEMORY_HEADING = "What this loop has already tried"

# Hard cap on the memory section's rendered length: it exists to keep the
# planner from re-proposing done/queued/failed work, not to crowd out the
# reference-project study material that follows it.
DEFAULT_MEMORY_MAX_CHARS = 3000
DEFAULT_MEMORY_CLOSED_LIMIT = 15
DEFAULT_MEMORY_MAX_LESSONS = 25

# Above this Jaccard token-overlap ratio (over normalized titles) a candidate
# is treated as a re-proposal of prior work, not a new idea. Documented here
# because it is the one number that decides what the gate refuses; nothing is
# dropped silently - every refusal carries a reason into the review brief.
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

    def source_of(self, title: str) -> str:
        """Where a matched title came from, so a refusal names its evidence."""
        needle = title.strip().lower()
        for issue in self.open_tickets:
            if issue.title.strip().lower() == needle:
                return f"open ticket #{issue.number}"
        for closed in self.closed_titles:
            if closed.strip().lower() == needle:
                return "a recently closed ticket"
        for outcome, lesson_title in self.lessons:
            if lesson_title.strip().lower() == needle:
                return f"a recorded lesson (outcome {outcome})"
        return "prior work"

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


ADOPTION_HEADING = "Practices already acted on"

DEFAULT_ADOPTION_MAX_CHARS = 2000

ADOPTED = "adopted"
FAILED = "failed"
IN_FLIGHT = "in-flight"


@dataclass
class AdoptionIndex:
    """Which observed practices this loop already acted on, and how it went.

    Field notes say what we *saw*; this says what we *did about it*. Without
    it the synthesizer can re-file a practice the loop merged three cycles ago,
    because a reworded title sails past a title-similarity check.

    Precedence is deliberate: a practice that shipped stays ``adopted`` even if
    a later ticket mentions it, and a failure is never masked by an open
    ticket - the planner should see "we tried this and it did not work" rather
    than "someone is on it".
    """

    adopted: dict[str, str] = field(default_factory=dict)     # practice_id -> evidence
    failed: dict[str, str] = field(default_factory=dict)
    in_flight: dict[str, str] = field(default_factory=dict)
    observed: tuple[str, ...] = ()  # every practice_id the field notes know about

    @classmethod
    def build(
        cls,
        *,
        kb: KnowledgeBase | None = None,
        open_issues: tuple[github.Issue, ...] = (),
    ) -> AdoptionIndex:
        """Scan field notes + lesson frontmatter + the open backlog.

        Every source degrades to empty on its own failure, exactly like
        :meth:`MemoryPack.gather`: a thin index beats aborting synthesis.
        """
        index = cls()
        if kb is not None:
            try:
                index.observed = tuple(
                    dict.fromkeys(
                        pid for note in kb.read_field_notes() for pid in note.practice_ids
                    )
                )
            except OSError:
                index.observed = ()
            try:
                records = kb.read_lessons()
            except OSError:
                records = []
            # read_lessons() is oldest-first, so a later success genuinely
            # supersedes an earlier failed attempt at the same practice.
            for record in records:
                # merged work is recorded as outcome/pass; a recovered or
                # review-blocked attempt lands as outcome/fail.
                evidence = f"[[{record.note_name}]] ({record.outcome})"
                for pid in record.practices:
                    if record.outcome == "pass":
                        index.failed.pop(pid, None)
                        index.adopted[pid] = evidence
                    elif pid not in index.adopted:
                        index.failed.setdefault(pid, evidence)
        for issue in open_issues:
            for pid in parse_practice_ids(issue.body):
                if pid in index.adopted or pid in index.failed:
                    continue
                flag = " BLOCKED" if issue.is_blocked else ""
                index.in_flight.setdefault(pid, f"#{issue.number}{flag}")
        return index

    def status(self, pid: str) -> str:
        """``adopted`` / ``failed`` / ``in-flight`` / "" for an unseen practice."""
        if pid in self.adopted:
            return ADOPTED
        if pid in self.failed:
            return FAILED
        if pid in self.in_flight:
            return IN_FLIGHT
        return ""

    def evidence(self, pid: str) -> str:
        return self.adopted.get(pid) or self.failed.get(pid) or self.in_flight.get(pid) or ""

    def render(self, *, max_chars: int = DEFAULT_ADOPTION_MAX_CHARS) -> str:
        def block(label: str, entries: dict[str, str]) -> list[str]:
            if not entries:
                return [f"{label}: _(none)_"]
            return [label + ":"] + [f"- `{pid}` -> {why}" for pid, why in sorted(entries.items())]

        lines: list[str] = []
        lines += block("Already adopted (merged - do NOT re-file)", self.adopted)
        lines.append("")
        lines += block(
            "Already failed (tried and recorded as a failure - only re-propose with a "
            "materially different approach)",
            self.failed,
        )
        lines.append("")
        lines += block("Currently in flight (open ticket - do NOT re-file)", self.in_flight)
        if self.observed:
            lines.append("")
            lines.append(
                f"Observed but not yet acted on ({len(self.observed)}): "
                + ", ".join(f"`{p}`" for p in self.observed[:40])
            )
        text = "\n".join(lines)
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

{ADOPTION_HEADING} - keyed by `practice_id`, the addressable key each observed
practice carries in `knowledge/reference/<owner>-<repo>.md`. A candidate whose
practice_id appears as adopted or in flight below WILL BE REFUSED before filing;
do not spend a slot on one:
{adoption_section}

Study digest of reference projects for this cycle:
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
  "practice_ids" (array of >= 1 `practice_id` slugs from the field notes /
    adoption index above, or - for a practice you observed in THIS cycle's study
    digest that has no id yet - a new slug of the form
    `<owner>-<repo>--<short-practice-name>`, lowercase and hyphenated).

Every practice_id you emit must trace to a concrete artifact you can name (a
workflow file, a commit convention, a doc). This is the evidence trail: a
merged PR is expected to cite these ids back, so a bare repo slug is not enough.

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
                    practice_ids=tuple(
                        str(p).strip() for p in item.get("practice_ids", []) if str(p).strip()
                    ),
                    labels=("self-improve", "hsai", "priority:P2"),
                )
            )
        except (KeyError, TypeError):
            continue
    return specs


@dataclass(frozen=True)
class Refusal:
    """One candidate the dedupe gate would not let through, and why.

    Refusals are *reported*, never silently dropped: a wrongly-suppressed idea
    has to be visible to the architect in the block review brief, or the gate
    becomes an unauditable filter on the loop's own imagination.
    """

    title: str  # the refused candidate
    reason: str  # one line, human-readable
    matched: str = ""  # the prior title or practice_id it collided with

    def line(self) -> str:
        return f"{self.title} - {self.reason}"

    def as_dict(self) -> dict[str, str]:
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
        """What each refusal collided with - the prior title or practice_id."""
        return [r.matched or r.title for r in self.refused]

    def refusal_lines(self) -> list[str]:
        return [r.line() for r in self.refused]


def screen_specs(
    specs: list[TicketSpec],
    memory: MemoryPack,
    adoption: AdoptionIndex | None = None,
    *,
    threshold: float = DUPLICATE_JACCARD_THRESHOLD,
) -> tuple[list[TicketSpec], list[Refusal]]:
    """The dedupe gate: which candidates may be filed, and why the rest may not.

    Two independent grounds for refusal, checked in this order:

    1. **practice_id** - the candidate re-proposes a practice already merged, or
       already covered by an open ticket. This is the check a title-similarity
       test cannot make: the same practice reworded is a different string but
       the same work.
    2. **title similarity** - see :func:`is_duplicate`.

    A practice recorded as *failed* is deliberately NOT refused: "we tried it
    and it did not work" is an argument for a different approach, not for
    never trying again.

    Never back-fills: an honest thin block (fewer than `file_top` tickets filed)
    beats padding the backlog with a duplicate.
    """
    adoption = adoption or AdoptionIndex()
    survivors: list[TicketSpec] = []
    refused: list[Refusal] = []
    for spec in specs:
        blocked_pid = ""
        blocked_status = ""
        for pid in spec.practice_ids:
            status = adoption.status(pid)
            if status in (ADOPTED, IN_FLIGHT):
                blocked_pid, blocked_status = pid, status
                break
        if blocked_pid:
            reason = (
                f"practice `{blocked_pid}` is already {blocked_status} "
                f"({adoption.evidence(blocked_pid) or 'no evidence recorded'})"
            )
            _logger.info("synthesis: refusing %r - %s", spec.title, reason)
            refused.append(Refusal(title=spec.title, reason=reason, matched=blocked_pid))
            continue
        dup, matched = is_duplicate(spec, memory, threshold=threshold)
        if dup:
            source = memory.source_of(matched)
            reason = f"title duplicates {source}: {matched!r}"
            _logger.info("synthesis: refusing %r - %s", spec.title, reason)
            refused.append(Refusal(title=spec.title, reason=reason, matched=matched))
            continue
        survivors.append(spec)
    return survivors, refused


def synthesize(
    cfg: CoreConfig,
    *,
    cycle_index: int = 0,
    root: str = ".",
    runner: Runner = run,
    ai_runner: Runner = run,
) -> SynthesisResult:
    """Run one synthesis pass, refuse re-proposals of prior work, file the rest."""
    repos = pick_rotation(cfg, cycle_index)
    try:
        kb: KnowledgeBase | None = KnowledgeBase.from_config(cfg, root)
    except OSError:
        kb = None
    pack = build_context_pack(
        repos, runner=runner, kb=kb,
        facts=repo_facts(cfg), snapshot_date=reference_snapshot_date(cfg),
    )
    memory = MemoryPack.gather(cfg, root=root, runner=runner)
    adoption = AdoptionIndex.build(kb=kb, open_issues=memory.open_tickets)
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
    survivors, refused = screen_specs(specs, memory, adoption, threshold=threshold)

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
