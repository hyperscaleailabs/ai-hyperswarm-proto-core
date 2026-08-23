"""The reference-practice catalog: durable, cited memory of what this loop has
already learned from the reference set (G1's traceability claim, made real).

Until this module existed, "every improvement traces back to a field
observation" (G1) lived only in free-text PR prose and module docstrings -
nothing indexed it, and nothing stopped the planner from re-deriving ground
already covered (the recurring "chore: refresh reference-set snapshot and
extract one practice" ticket is the visible symptom). A :class:`Practice` is
one committed, Obsidian-ready note per practice: which reference project it
came from, what kind of artifact taught it (one of core.yaml's
``reference_set.learn_from`` values), the evidence behind it, and where it
currently sits in the adoption lifecycle.

That lifecycle is the cumulative part, and it is derived from real state
rather than prose (:data:`STATUSES`):

``observed``
    the planner saw it in a reference project this cycle and wrote it down;
``proposed``
    a ticket citing it has been filed (the note carries the ticket number);
``adopted``
    that ticket's pull request merged;
``rejected``
    that ticket was closed without a merged PR, or hit the ``blocked`` label.

:func:`sync_statuses` recomputes those transitions from GitHub through an
injectable runner (``hsai practices sync``), so a status in the catalog is
always something that happened, never something a model asserted.

The registry is plain files under ``knowledge/practices/`` - :func:`load`,
:func:`append`, :func:`write` and :func:`render` are the whole read/write
surface, plus :func:`is_duplicate` so the same (source project, title) pair is
never recorded twice. Every note carries a :class:`Provenance` stamp (the hsai
commit that wrote it + a hash of core.yaml), so a claim can be replayed against
the exact harness that made it. :mod:`hsai.knowledge` composes these into a
"Practices MOC", :mod:`hsai.synthesis` records observations and renders the
catalog into the planner's prompt as ground it must not re-propose, and
:mod:`hsai.tickets` lets a synthesized ticket name which practice it adds or
extends.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import github
from .config import CORE_PATH
from .proc import Runner, run

PRACTICES_DIR_DEFAULT = "knowledge/practices"

# The adoption lifecycle, in order. Every transition after `observed` is
# derived from GitHub state by `sync_statuses`, never from model prose.
STATUSES = ("observed", "proposed", "adopted", "rejected")

# Where a practice was learned from. Not enforced against core.yaml's
# reference_set.learn_from list here - callers that have a CoreConfig in hand
# (the CLI) are better placed to warn on a mismatch - but named so the intended
# vocabulary is discoverable from the code.
ARTIFACT_KINDS = (
    "source_code", "commit_history", "ci_cd", "issue_history", "harness_design", "readme",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Ids keep the `project--title` separator, so they are normalized with a rule
# that spares hyphens: running `_slugify` over a whole id would collapse the
# `--` and silently rename every note.
_ID_RE = re.compile(r"[^a-z0-9-]+")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def normalize_title(title: str) -> str:
    """Whitespace-collapsed, case-folded title - what the duplicate check compares."""
    return re.sub(r"\s+", " ", title.strip().lower())


def normalize_id(practice_id: str) -> str:
    """Filename-safe form of an id the planner supplied, separator preserved.

    ``normalize_id(make_id(p, t)) == make_id(p, t)`` for every input, so an id
    that came out of this module round-trips through the planner's JSON and
    back to the same note.
    """
    cleaned = _ID_RE.sub("-", practice_id.strip().lower()).strip("-")
    return re.sub(r"-{3,}", "--", cleaned) or "untitled"


def make_id(source_project: str, title: str) -> str:
    """Stable identifier for a practice - also its note filename (deterministic:
    the same (source project, title) pair always yields the same id, so a
    resumed backfill or a re-run of `hsai practices add` never renames a note).
    """
    return f"{_slugify(source_project)}--{_slugify(title)}"


def _split_sections(text: str) -> dict[str, str]:
    parts = _SECTION_RE.split(text)
    sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lower()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections[heading] = body.strip()
    return sections


@dataclass(frozen=True)
class Provenance:
    """Which harness produced a record: hsai's commit, and core.yaml's content.

    Two halves because they answer different questions. The SHA says which code
    mined and rendered the note; the config hash says which reference set,
    goals and synthesis budget were in force at the time. A claim is only
    replayable when both are pinned.
    """

    hsai_sha: str = ""
    core_hash: str = ""

    def stamp(self) -> str:
        return f"hsai@{self.hsai_sha or 'unknown'} core.yaml@{self.core_hash or 'unknown'}"


def core_yaml_hash(root: str | Path) -> str:
    """Short content hash of ``.ai-swarm/core.yaml`` ("unknown" when absent)."""
    path = Path(root) / CORE_PATH
    try:
        data = path.read_bytes()
    except OSError:
        return "unknown"
    return hashlib.sha256(data).hexdigest()[:12]


def current_provenance(root: str | Path = ".", *, runner: Runner = run) -> Provenance:
    """Stamp for records written right now. Degrades to "unknown" halves rather
    than raising: a missing git or an unreadable core.yaml must never be the
    reason a practice goes unrecorded."""
    p = runner(["git", "rev-parse", "--short", "HEAD"], cwd=str(root))
    sha = p.stdout.strip() if p.ok else ""
    return Provenance(hsai_sha=sha, core_hash=core_yaml_hash(root))


@dataclass(frozen=True)
class Practice:
    """One practice this loop has observed, proposed, adopted, or rejected.

    ``source_artifact`` should be one of :data:`ARTIFACT_KINDS` - what KIND of
    field observation taught this, not just which project. ``ticket`` is the
    link that makes the status derivable: :func:`sync_statuses` reads that
    ticket's real state back off GitHub instead of trusting prose.
    """

    id: str
    title: str
    source_project: str
    source_artifact: str
    evidence: str  # URL or commit/PR reference
    adopted_pr: int | None = None
    adopted_date: str = ""
    status: str = "adopted"  # see STATUSES
    notes: str = ""
    related: tuple[str, ...] = ()  # wikilinked note names (lessons, whitepapers)
    ticket: int | None = None      # the ticket that proposes/adopts this practice
    lesson_note: str = ""          # the lesson note recording how it went
    first_seen_cycle: int | None = None  # cycle index that first observed it
    provenance: str = ""           # Provenance.stamp() of the harness that wrote this

    def note_name(self) -> str:
        return self.id


class DuplicatePracticeError(ValueError):
    """Raised when a (source_project, title) pair is already registered."""


def build_practice(
    *,
    title: str,
    source_project: str,
    source_artifact: str,
    evidence: str,
    status: str = "adopted",
    adopted_pr: int | None = None,
    adopted_date: str = "",
    notes: str = "",
    related: tuple[str, ...] = (),
    ticket: int | None = None,
    lesson_note: str = "",
    first_seen_cycle: int | None = None,
    provenance: str = "",
    practice_id: str = "",
) -> Practice:
    """Convenience constructor: derives the id, defaults the date to today.

    ``practice_id`` overrides the derived id - used when the planner introduces
    its own slug in ``practice_ids`` and the filed ticket must cite that exact
    string for the link to resolve.
    """
    return Practice(
        id=normalize_id(practice_id) if practice_id else make_id(source_project, title),
        title=title,
        source_project=source_project,
        source_artifact=source_artifact,
        evidence=evidence,
        adopted_pr=adopted_pr,
        adopted_date=adopted_date or _today(),
        status=status,
        notes=notes,
        related=related,
        ticket=ticket,
        lesson_note=lesson_note,
        first_seen_cycle=first_seen_cycle,
        provenance=provenance,
    )


def practices_dir(root: str | Path, cfg: Any = None) -> Path:
    """The registry directory, created if absent."""
    if cfg is not None:
        rel = (cfg.knowledge or {}).get("practices_dir", PRACTICES_DIR_DEFAULT)
    else:
        rel = PRACTICES_DIR_DEFAULT
    path = Path(root) / rel
    path.mkdir(parents=True, exist_ok=True)
    return path


def practice_notes(root: str | Path, cfg: Any = None) -> list[str]:
    return sorted(p.stem for p in practices_dir(root, cfg).glob("*.md"))


def render(practice: Practice) -> str:
    """Obsidian-ready markdown: YAML frontmatter + wikilinks to its MOC."""
    fm: dict[str, Any] = {
        "tags": [
            "practice",
            f"status/{practice.status}",
            f"source/{_slugify(practice.source_project)}",
        ],
        "created": practice.adopted_date or _today(),
        "practice_id": practice.id,
        "source_project": practice.source_project,
        "source_artifact": practice.source_artifact,
        "status": practice.status,
    }
    if practice.adopted_pr:
        fm["adopted_pr"] = practice.adopted_pr
    if practice.adopted_date:
        fm["adopted_date"] = practice.adopted_date
    # Only emitted when set, so a note written before these fields existed
    # re-renders byte-for-byte as it did before.
    if practice.ticket:
        fm["ticket"] = practice.ticket
    if practice.lesson_note:
        fm["lesson_note"] = practice.lesson_note
    if practice.first_seen_cycle is not None:
        fm["first_seen_cycle"] = practice.first_seen_cycle
    if practice.provenance:
        fm["provenance"] = practice.provenance
    fm_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    related = "\n".join(f"- [[{r}]]" for r in practice.related) or "- _(none linked yet)_"
    pr = f"#{practice.adopted_pr}" if practice.adopted_pr else "_(none)_"
    ticket = f"#{practice.ticket}" if practice.ticket else "_(none)_"
    lesson = f"[[{practice.lesson_note}]]" if practice.lesson_note else "_(none)_"
    first_seen = (
        str(practice.first_seen_cycle) if practice.first_seen_cycle is not None else "_(none)_"
    )
    return f"""---
{fm_text}
---

# {practice.title}

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `{practice.source_project}` |
| source artifact | {practice.source_artifact} |
| status | **{practice.status}** |
| ticket | {ticket} |
| adopted PR | {pr} |
| adopted date | {practice.adopted_date or "_(none)_"} |
| first seen (cycle) | {first_seen} |
| lesson | {lesson} |
| provenance | `{practice.provenance or "unstamped"}` |

## Evidence
{practice.evidence or "_(none recorded)_"}

## Notes
{practice.notes or "_(none)_"}

## Related
{related}
"""


def parse(path: str | Path) -> Practice:
    """Parse a practice note back off disk - the read-side counterpart of :func:`render`."""
    path = Path(path)
    text = path.read_text()
    fm_match = _FRONTMATTER_RE.match(text)
    fm: dict[str, Any] = (yaml.safe_load(fm_match.group(1)) or {}) if fm_match else {}
    body = text[fm_match.end():] if fm_match else text
    title_match = _TITLE_RE.search(text)
    title = title_match.group(1).strip() if title_match else path.stem
    sections = _split_sections(body)
    related = tuple(_WIKILINK_RE.findall(sections.get("related", "")))
    adopted_pr = fm.get("adopted_pr")
    ticket = fm.get("ticket")
    first_seen = fm.get("first_seen_cycle")
    return Practice(
        id=str(fm.get("practice_id") or path.stem),
        title=title,
        source_project=str(fm.get("source_project", "")),
        source_artifact=str(fm.get("source_artifact", "")),
        evidence=sections.get("evidence", ""),
        adopted_pr=int(adopted_pr) if adopted_pr else None,
        adopted_date=str(fm.get("adopted_date", "")),
        status=str(fm.get("status", "adopted")),
        notes=sections.get("notes", ""),
        related=related,
        ticket=int(ticket) if ticket else None,
        lesson_note=str(fm.get("lesson_note", "")),
        first_seen_cycle=int(first_seen) if first_seen is not None else None,
        provenance=str(fm.get("provenance", "")),
    )


def load(root: str | Path, cfg: Any = None) -> list[Practice]:
    """Every practice on disk, sorted by id (deterministic - no dependence on
    filesystem iteration order, so `hsai reindex` never produces a spurious diff)."""
    d = practices_dir(root, cfg)
    return [parse(d / f"{name}.md") for name in practice_notes(root, cfg)]


def is_duplicate(practices: list[Practice], source_project: str, title: str) -> Practice | None:
    """Would `(source_project, title)` duplicate an existing entry?

    Keyed on normalized title (whitespace-collapsed, case-folded) and
    case-insensitive source project, so "LangChain" vs "langchain-ai/langchain"
    with an identically-worded title cannot both be filed. Returns the
    conflicting record, or ``None``.
    """
    norm_title = normalize_title(title)
    norm_project = source_project.strip().lower()
    for p in practices:
        if p.source_project.strip().lower() == norm_project and normalize_title(p.title) == norm_title:
            return p
    return None


def write(root: str | Path, practice: Practice, *, cfg: Any = None) -> Path:
    """Render `practice` to its note, overwriting any earlier revision.

    The unguarded write: :func:`append` is the entry point for NEW records (it
    refuses duplicates); this one is for updating a record that already exists,
    which is what a status transition is.
    """
    path = practices_dir(root, cfg) / f"{practice.note_name()}.md"
    path.write_text(render(practice))
    return path


def append(root: str | Path, practice: Practice, *, cfg: Any = None) -> Path:
    """Write `practice` as a new note - refuses a (source_project, title) duplicate."""
    existing = load(root, cfg)
    dup = is_duplicate(existing, practice.source_project, practice.title)
    if dup is not None:
        raise DuplicatePracticeError(
            f"a practice for source_project={practice.source_project!r} "
            f"title={practice.title!r} is already recorded as [[{dup.note_name()}]]"
        )
    return write(root, practice, cfg=cfg)


def get(root: str | Path, practice_id: str, *, cfg: Any = None) -> Practice | None:
    """One practice by id, or ``None`` when the catalog has no such note."""
    path = practices_dir(root, cfg) / f"{normalize_id(practice_id)}.md"
    return parse(path) if path.is_file() else None


# --- observation: what the planner saw this cycle ----------------------------

@dataclass(frozen=True)
class Observation:
    """A practice the planner named in PHASE 1, before any ticket exists.

    This is the raw mining output - one distinct practice spotted in one
    reference project's artifacts. Recording it is what makes G1 learning
    cumulative: next cycle's prompt shows it back to the planner whether or not
    a ticket was ever filed for it.
    """

    title: str
    source_project: str
    source_artifact: str = ""
    evidence: str = ""
    practice_id: str = ""  # the slug the planner used in `practice_ids`, if any

    def resolved_id(self) -> str:
        return normalize_id(self.practice_id) if self.practice_id else make_id(
            self.source_project, self.title
        )


def record_observations(
    root: str | Path,
    observations: list[Observation],
    *,
    cycle_index: int = 0,
    provenance: str = "",
    cfg: Any = None,
) -> list[Practice]:
    """File an ``observed`` note for every genuinely new observation.

    Duplicates - by (source_project, title) or by id - are skipped silently:
    re-seeing a practice in a later cycle is the normal case, and it must not
    overwrite the status the earlier note has since earned.
    """
    existing = load(root, cfg)
    known_ids = {p.id for p in existing}
    written: list[Practice] = []
    for obs in observations:
        if not obs.title.strip() or not obs.source_project.strip():
            continue
        if obs.resolved_id() in known_ids:
            continue
        if is_duplicate(existing, obs.source_project, obs.title) is not None:
            continue
        practice = build_practice(
            title=obs.title.strip(),
            source_project=obs.source_project.strip(),
            source_artifact=obs.source_artifact.strip(),
            evidence=obs.evidence.strip(),
            status="observed",
            first_seen_cycle=cycle_index,
            provenance=provenance,
            practice_id=obs.practice_id,
        )
        write(root, practice, cfg=cfg)
        existing.append(practice)
        known_ids.add(practice.id)
        written.append(practice)
    return written


def link_ticket(
    root: str | Path, practice_id: str, ticket: int, *, cfg: Any = None
) -> Practice | None:
    """Point a practice at the ticket that proposes it, flipping it to ``proposed``.

    Only an ``observed`` record moves. First claim wins: a practice already
    ``proposed`` is pointing at a live ticket whose outcome will decide its
    status, and one already ``adopted`` or ``rejected`` has that outcome on
    record - a later ticket citing either must not quietly repoint the link the
    audit trail hangs from.
    """
    practice = get(root, practice_id, cfg=cfg)
    if practice is None or practice.status != "observed":
        return None
    updated = replace(practice, ticket=ticket, status="proposed")
    write(root, updated, cfg=cfg)
    return updated


# --- status sync: transitions derived from real GitHub state -----------------

@dataclass(frozen=True)
class StatusChange:
    """One recomputed transition, with the state that justified it."""

    practice_id: str
    before: str
    after: str
    reason: str

    def render(self) -> str:
        return f"{self.practice_id}: {self.before} -> {self.after} ({self.reason})"


def next_status(practice: Practice, outcome: github.TicketOutcome | None) -> tuple[str, str]:
    """The status `practice` should have given its ticket's real state.

    Pure. Returns ``(status, reason)``; the reason is what gets logged, so
    every transition in the catalog is explainable without re-querying GitHub.
    """
    if not practice.ticket:
        return practice.status, "no ticket linked"
    if outcome is None:
        return practice.status, f"ticket #{practice.ticket} state unavailable"
    if outcome.merged:
        return "adopted", f"ticket #{practice.ticket} merged in PR #{outcome.merged_pr}"
    if outcome.is_blocked:
        return "rejected", f"ticket #{practice.ticket} is labelled `blocked`"
    if not outcome.is_open:
        return "rejected", f"ticket #{practice.ticket} closed without a merged PR"
    return "proposed", f"ticket #{practice.ticket} is still open"


def sync_statuses(
    records: list[Practice],
    *,
    repo: str,
    runner: Runner = run,
    fetch: Any = None,
) -> tuple[list[Practice], list[StatusChange]]:
    """Recompute every linked practice's status from GitHub.

    ``runner`` is the injection point (tests hand it a fake `gh`); ``fetch``
    exists for the same reason one level up, so a caller can stub the whole
    query rather than its transport. Returns the full catalog - updated where a
    transition fired, untouched everywhere else - plus one
    :class:`StatusChange` per actual move.
    """
    query = fetch or github.ticket_outcome
    updated: list[Practice] = []
    changes: list[StatusChange] = []
    for practice in records:
        if not practice.ticket:
            updated.append(practice)
            continue
        outcome = query(repo, practice.ticket, runner=runner)
        status, reason = next_status(practice, outcome)
        if status == practice.status:
            updated.append(practice)
            continue
        moved = replace(practice, status=status)
        if status == "adopted" and outcome is not None:
            moved = replace(
                moved,
                adopted_pr=outcome.merged_pr or practice.adopted_pr,
                adopted_date=practice.adopted_date or _today(),
            )
        updated.append(moved)
        changes.append(StatusChange(practice.id, practice.status, status, reason))
    return updated, changes


def sync(
    root: str | Path,
    *,
    repo: str,
    cfg: Any = None,
    runner: Runner = run,
    dry_run: bool = False,
) -> list[StatusChange]:
    """Load the catalog, recompute statuses, and persist the notes that moved."""
    records = load(root, cfg)
    updated, changes = sync_statuses(records, repo=repo, runner=runner)
    if not dry_run:
        moved = {c.practice_id for c in changes}
        for practice in updated:
            if practice.id in moved:
                write(root, practice, cfg=cfg)
    return changes


# --- prompt rendering --------------------------------------------------------

ADOPTED_HEADING = "Already adopted - do NOT re-propose"

# Spelled out next to the heading so the two halves of the catalog carry
# different instructions: shipped work is closed ground, a rejected idea is
# reopenable but only against evidence the last attempt did not have.
REJECTED_RULE = (
    "Entries marked `rejected` were tried and dropped - do NOT resurrect one "
    "without NEW evidence that the reason it failed no longer holds, and say "
    "what that evidence is."
)


def render_adopted_section(practices: list[Practice]) -> str:
    """The block injected into the synthesis prompt: what NOT to re-propose.

    The whole catalog is shown, not just the adopted half - an `observed`
    practice nobody has ticketed yet is a standing invitation, and a `rejected`
    one is a field observation worth remembering. Each line carries its status
    and ticket so the planner can tell "already shipped" from "already tried
    and dropped" from "spotted, still open ground".
    """
    if not practices:
        return "_(no practices recorded yet - this is an early cycle)_"
    lines = []
    for p in practices:
        ticket = f", ticket #{p.ticket}" if p.ticket else ""
        lines.append(
            f"- **{p.title}** (from `{p.source_project}`, {p.source_artifact}, "
            f"status: {p.status}{ticket}) [id: `{p.id}`] - "
            f"{p.evidence or 'no evidence recorded'}"
        )
    return "\n".join(lines)
