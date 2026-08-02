"""The practice registry: evidence-cited provenance for goal G1.

G1 says every improvement must trace back to something *observed* in the field.
Asserting that in prose is cheap; this module makes it falsifiable.

A **practice card** is one Obsidian note under ``knowledge/practices/``
describing a single practice observed in a reference project, pinned to a
concrete artifact (a file path, a workflow filename, a commit SHA, an issue
number). Cards carry an id (``PR-0001``), and tickets cite those ids in a
``## Practices`` section. The orchestrator resolves a ticket's citations into
the repos that actually informed the work and stamps *those* onto the PR body
and the lesson - instead of the fabricated "top 3 pinned repos" it used to
print on every PR regardless of content.

Adopted from assafelovic/gpt-researcher, whose research reports are only
trustworthy because each claim carries a validated source; and from
microsoft/semantic-kernel, whose PR metadata is only real because a required
check enforces it (see :func:`evaluate_pr_evidence`, wired to ``hsai
evidence-check``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from .config import CoreConfig
from .knowledge import slugify
from .proc import Runner, run

PRACTICES_DIR = "knowledge/practices"
MOC_NAME = "Practices MOC"

ARTIFACT_KINDS = ("code", "ci", "commit", "issue", "readme")

CARD_ID_RE = re.compile(r"^PR-\d{4}$")
CARD_ID_IN_TEXT = re.compile(r"\bPR-\d{4}\b")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_FIELD_RE = re.compile(r"^([a-z_]+):[ \t]*(\S.*?)\s*$", re.MULTILINE)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_ISSUE_REF_RE = re.compile(r"^#?\d+$")
# owner/name as it appears in prose; the trailing char may not be punctuation, so
# "combines openai/swarm." yields the slug and not the sentence's full stop.
_REPO_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]*[A-Za-z0-9_-]")
_BACKLINKS_HEADING = "## Cited by"

# Ticket titles whose work MUST be anchored to observed evidence. Docs, chores
# and heal tickets are exempt: they are housekeeping, not adopted practice.
CITED_TITLE_PREFIXES = ("feat:", "skill:", "refactor:")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _section(body: str, heading: str) -> str:
    """Return the text under ``## <heading>``, stopping at the next heading or
    horizontal rule (empty when the section is absent)."""
    pattern = re.compile(
        rf"^#{{2,3}}\s*{re.escape(heading)}\s*$(.*?)(?=^#{{1,3}}\s|^-{{3,}}\s*$|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(body)
    return m.group(1).strip() if m else ""


@dataclass(frozen=True)
class PracticeCard:
    """One observed practice, pinned to the artifact it was observed in."""

    id: str
    title: str
    source_repo: str
    artifact_kind: str
    artifact_ref: str
    observed_on: str
    body: str = ""
    note_name: str = ""

    def default_note_name(self) -> str:
        return f"{self.id}-{slugify(self.title)}"

    def wikilink(self) -> str:
        return f"[[{self.note_name or self.default_note_name()}]]"

    def artifact_url(self) -> str:
        """A human-checkable URL for the cited artifact."""
        if self.artifact_kind == "commit":
            return f"https://github.com/{self.source_repo}/commit/{self.artifact_ref}"
        if self.artifact_kind == "issue":
            return f"https://github.com/{self.source_repo}/issues/{self.artifact_ref.lstrip('#')}"
        return f"https://github.com/{self.source_repo}/blob/HEAD/{self.artifact_ref}"

    def api_path(self) -> str:
        """The ``gh api`` path that proves the artifact exists."""
        if self.artifact_kind == "commit":
            return f"repos/{self.source_repo}/commits/{self.artifact_ref}"
        if self.artifact_kind == "issue":
            return f"repos/{self.source_repo}/issues/{self.artifact_ref.lstrip('#')}"
        return f"repos/{self.source_repo}/contents/{self.artifact_ref}"


# --- parsing / loading --------------------------------------------------------
def parse_card(text: str, *, note_name: str = "") -> PracticeCard:
    """Parse a card note. Missing fields come back empty for the validator."""
    fm_match = _FRONTMATTER_RE.match(text)
    fields = dict(_FIELD_RE.findall(fm_match.group(1))) if fm_match else {}
    title_match = _TITLE_RE.search(text)
    return PracticeCard(
        id=fields.get("id", ""),
        title=title_match.group(1).strip() if title_match else "",
        source_repo=fields.get("source_repo", ""),
        artifact_kind=fields.get("artifact_kind", ""),
        artifact_ref=fields.get("artifact_ref", ""),
        observed_on=fields.get("observed_on", ""),
        body=text,
        note_name=note_name,
    )


def practices_dir(root: str | Path, cfg: CoreConfig | None = None) -> Path:
    rel = (cfg.knowledge or {}).get("practices_dir", PRACTICES_DIR) if cfg else PRACTICES_DIR
    return Path(root) / rel


def load_cards_in(directory: Path) -> list[PracticeCard]:
    """Load every practice card in ``directory``, ordered by id."""
    if not directory.is_dir():
        return []
    cards = [parse_card(p.read_text(), note_name=p.stem) for p in directory.glob("*.md")]
    return sorted(cards, key=lambda c: (c.id, c.note_name))


def load_cards(root: str | Path, cfg: CoreConfig | None = None) -> list[PracticeCard]:
    """Load every practice card under the vault, ordered by id."""
    return load_cards_in(practices_dir(root, cfg))


def validate_card(card: PracticeCard, cfg: CoreConfig) -> list[str]:
    """Schema + evidence checks. Empty list means the card is valid."""
    problems: list[str] = []
    where = card.note_name or card.id or "<unnamed card>"

    if not CARD_ID_RE.match(card.id):
        problems.append(f"{where}: id must look like PR-0001 (got {card.id!r})")
    if not card.title:
        problems.append(f"{where}: missing '# <title>' heading")
    if not card.source_repo:
        problems.append(f"{where}: missing frontmatter field 'source_repo'")
    elif card.source_repo not in cfg.pinned_repos():
        problems.append(
            f"{where}: source_repo {card.source_repo!r} is not in the pinned "
            "reference set or watchlist of .ai-swarm/core.yaml"
        )
    if card.artifact_kind not in ARTIFACT_KINDS:
        problems.append(
            f"{where}: artifact_kind must be one of {'|'.join(ARTIFACT_KINDS)} "
            f"(got {card.artifact_kind!r})"
        )
    if not card.artifact_ref:
        problems.append(f"{where}: missing frontmatter field 'artifact_ref'")
    else:
        problems.extend(_artifact_ref_problems(card, where))
    if not card.observed_on:
        problems.append(f"{where}: missing frontmatter field 'observed_on'")
    else:
        try:
            date.fromisoformat(card.observed_on)
        except ValueError:
            problems.append(f"{where}: observed_on must be ISO (YYYY-MM-DD), got {card.observed_on!r}")
    return problems


def _artifact_ref_problems(card: PracticeCard, where: str) -> list[str]:
    ref = card.artifact_ref
    if card.artifact_kind == "commit" and not _SHA_RE.match(ref):
        return [f"{where}: artifact_ref for a commit must be a hex SHA (got {ref!r})"]
    if card.artifact_kind == "issue" and not _ISSUE_REF_RE.match(ref):
        return [f"{where}: artifact_ref for an issue must be a number (got {ref!r})"]
    if card.artifact_kind == "ci" and not ref.endswith((".yml", ".yaml")):
        return [f"{where}: artifact_ref for a CI artifact must be a workflow file (got {ref!r})"]
    if card.artifact_kind in ("code", "readme") and (" " in ref or ref.startswith("/")):
        return [f"{where}: artifact_ref must be a repo-relative path (got {ref!r})"]
    return []


def resolve_artifact(card: PracticeCard, *, runner: Runner = run) -> bool:
    """Ask GitHub whether the cited artifact actually exists (needs network)."""
    return runner(["gh", "api", card.api_path(), "--silent"]).ok


# --- citations ----------------------------------------------------------------
@dataclass(frozen=True)
class Citation:
    """What a ticket body actually claims as its evidence."""

    ids: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()  # card note names, for [[wikilinks]] in the lesson
    source: str = "none"  # practices | rationale | none

    @property
    def ok(self) -> bool:
        return bool(self.repos)


def cite(ticket_body: str) -> tuple[str, ...]:
    """Card ids listed in a ticket's ``## Practices`` section, in order."""
    section = _section(ticket_body, "Practices")
    seen: dict[str, None] = {}
    for cid in CARD_ID_IN_TEXT.findall(section):
        seen.setdefault(cid, None)
    return tuple(seen)


def parse_repo_slugs(text: str, allowed: set[str]) -> tuple[str, ...]:
    """Pinned reference repos named literally in ``text`` (order preserved)."""
    seen: dict[str, None] = {}
    for slug in _REPO_SLUG_RE.findall(text):
        if slug in allowed:
            seen.setdefault(slug, None)
    return tuple(seen)


def resolve_citation(
    ticket_body: str, cards: list[PracticeCard], cfg: CoreConfig
) -> Citation:
    """Turn a ticket body into the evidence its PR and lesson may claim.

    Preferred path: registered practice ids. Fallback: reference repos named in
    the ticket's ``## Synthesis rationale`` - the section the synthesizer
    already writes but which nothing used to carry forward.
    """
    by_id = {c.id: c for c in cards}
    ids = tuple(cid for cid in cite(ticket_body) if cid in by_id)
    if ids:
        repos: dict[str, None] = {}
        for cid in ids:
            repos.setdefault(by_id[cid].source_repo, None)
        notes = tuple(by_id[cid].note_name or by_id[cid].default_note_name() for cid in ids)
        return Citation(ids=ids, repos=tuple(repos), notes=notes, source="practices")

    rationale = _section(ticket_body, "Synthesis rationale")
    repos_from_prose = parse_repo_slugs(rationale, cfg.pinned_repos())
    if repos_from_prose:
        return Citation(repos=repos_from_prose, source="rationale")
    return Citation()


def requires_citation(ticket_title: str) -> bool:
    """Does this ticket have to name the evidence it is adopting?"""
    return ticket_title.strip().lower().startswith(CITED_TITLE_PREFIXES)


def render_section(ids: tuple[str, ...]) -> str:
    """The ``## Practices`` block a TicketSpec renders."""
    listed = "\n".join(f"- {cid}" for cid in ids) or "- _(none cited)_"
    return f"## Practices\n{listed}\n"


# --- the registry -------------------------------------------------------------
@dataclass(frozen=True)
class PracticeProposal:
    """A practice the synthesizer claims to have drawn on.

    Either it is already registered (``id`` set) or it is new, in which case
    the registry files a card for it so the citation resolves next time.
    """

    id: str = ""
    title: str = ""
    source_repo: str = ""
    artifact_kind: str = "readme"
    artifact_ref: str = "README.md"
    what: str = ""
    why: str = ""


class Registry:
    """The cards on disk, plus the ability to file new ones."""

    def __init__(self, root: str | Path, cfg: CoreConfig) -> None:
        self.root = Path(root)
        self.cfg = cfg
        self.dir = practices_dir(root, cfg)
        self.cards = load_cards(root, cfg)

    def by_id(self, card_id: str) -> PracticeCard | None:
        return next((c for c in self.cards if c.id == card_id), None)

    def match(self, proposal: PracticeProposal) -> PracticeCard | None:
        """An existing card for the same repo+artifact, if any."""
        if proposal.id:
            found = self.by_id(proposal.id)
            if found:
                return found
        return next(
            (
                c for c in self.cards
                if c.source_repo == proposal.source_repo
                and c.artifact_ref == proposal.artifact_ref
            ),
            None,
        )

    def next_id(self) -> str:
        used = [int(c.id.split("-")[1]) for c in self.cards if CARD_ID_RE.match(c.id)]
        return f"PR-{(max(used) + 1) if used else 1:04d}"

    def register(self, proposal: PracticeProposal) -> PracticeCard | None:
        """Resolve a proposal to a card, filing a new one when unregistered.

        Returns ``None`` when the proposal cites a repo outside the pinned
        reference set - unverifiable evidence is dropped, never invented.
        """
        existing = self.match(proposal)
        if existing:
            return existing
        card = PracticeCard(
            id=self.next_id(),
            title=proposal.title or f"Practice observed in {proposal.source_repo}",
            source_repo=proposal.source_repo,
            artifact_kind=proposal.artifact_kind,
            artifact_ref=proposal.artifact_ref,
            observed_on=_today(),
        )
        card = replace(card, note_name=card.default_note_name())
        if validate_card(card, self.cfg):
            return None
        self.write(card, what=proposal.what, why=proposal.why)
        self.cards = sorted([*self.cards, card], key=lambda c: (c.id, c.note_name))
        return card

    def resolve_all(self, proposals: list[PracticeProposal]) -> tuple[str, ...]:
        ids: dict[str, None] = {}
        for proposal in proposals:
            card = self.register(proposal)
            if card:
                ids.setdefault(card.id, None)
        return tuple(ids)

    def cards_for_repos(self, repos: tuple[str, ...]) -> tuple[str, ...]:
        """Ids of registered cards observed in any of ``repos``."""
        ids: dict[str, None] = {}
        for repo in repos:
            for card in self.cards:
                if card.source_repo == repo:
                    ids.setdefault(card.id, None)
                    break
        return tuple(ids)

    def catalog(self) -> str:
        """A compact listing the synthesis prompt can cite from."""
        if not self.cards:
            return "_(no practices registered yet)_"
        return "\n".join(
            f"- {c.id}: {c.title} - observed in {c.source_repo} ({c.artifact_kind}: {c.artifact_ref})"
            for c in self.cards
        )

    def write(self, card: PracticeCard, *, what: str = "", why: str = "") -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{card.note_name or card.default_note_name()}.md"
        path.write_text(render_card(card, what=what, why=why))
        return path


def render_card(card: PracticeCard, *, what: str = "", why: str = "") -> str:
    return f"""---
tags:
  - practice
  - source/{slugify(card.source_repo)}
id: {card.id}
source_repo: {card.source_repo}
artifact_kind: {card.artifact_kind}
artifact_ref: {card.artifact_ref}
observed_on: {card.observed_on}
---

# {card.title}

> Part of [[{MOC_NAME}]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `{card.source_repo}` |
| artifact | [{card.artifact_kind}: `{card.artifact_ref}`]({card.artifact_url()}) |
| observed | {card.observed_on} |

## What it does
{what or "_(to be written up on first adoption)_"}

## Why it applies to hsai
{why or "_(to be written up on first adoption)_"}
"""


# --- indexing -----------------------------------------------------------------
def reindex(
    root: str | Path,
    *,
    lessons_dir: str = "knowledge/lessons",
    mocs_dir: str = "knowledge/MOCs",
    practices_dir: str = PRACTICES_DIR,
) -> list[Path]:
    """Rebuild the Practices MOC and refresh every card's lesson backlinks."""
    cards_dir = Path(root) / practices_dir
    cards = load_cards_in(cards_dir)
    lessons = _lesson_citations(Path(root) / lessons_dir)
    written = [_write_moc(Path(root) / mocs_dir, cards, lessons)]
    for card in cards:
        written.append(_write_backlinks(cards_dir, card, lessons.get(card.id, ())))
    return written


def _lesson_citations(lessons_dir: Path) -> dict[str, tuple[str, ...]]:
    """Map card id -> the lesson notes that cite it."""
    citations: dict[str, list[str]] = {}
    if not lessons_dir.is_dir():
        return {}
    for path in sorted(lessons_dir.glob("*.md")):
        for cid in set(CARD_ID_IN_TEXT.findall(path.read_text())):
            citations.setdefault(cid, []).append(path.stem)
    return {cid: tuple(sorted(notes)) for cid, notes in citations.items()}


def _write_moc(
    mocs_dir: Path, cards: list[PracticeCard], lessons: dict[str, tuple[str, ...]]
) -> Path:
    mocs_dir.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(
        f"| [[{c.note_name or c.default_note_name()}\\|{c.id}]] | {c.title} | "
        f"`{c.source_repo}` | {c.artifact_kind}: `{c.artifact_ref}` | "
        f"{len(lessons.get(c.id, ()))} |"
        for c in cards
    ) or "| _(none)_ | | | | |"
    content = f"""---
tags:
  - moc
  - practices
---

# {MOC_NAME}

Up: [[Knowledge Base MOC]]

Every practice this repo has adopted from the pinned reference set, pinned to
the artifact it was observed in. Tickets cite these ids in a `## Practices`
section; the loop stamps the cited repos onto the PR and the lesson. Total:
**{len(cards)}**.

| id | practice | source | artifact | cited by |
| --- | --- | --- | --- | --- |
{rows}

## How this is maintained
- `hsai practices --validate` checks the schema and that every source repo is pinned in `.ai-swarm/core.yaml`.
- `hsai practices --index` (also run by `hsai reindex`) rebuilds this table and each card's backlinks.
"""
    path = mocs_dir / f"{MOC_NAME}.md"
    path.write_text(content)
    return path


def _write_backlinks(cards_dir: Path, card: PracticeCard, notes: tuple[str, ...]) -> Path:
    path = cards_dir / f"{card.note_name or card.default_note_name()}.md"
    text = path.read_text()
    head = text.split(_BACKLINKS_HEADING, 1)[0].rstrip()
    links = "\n".join(f"- [[{n}]]" for n in notes) or "- _(not yet cited by a lesson)_"
    path.write_text(f"{head}\n\n{_BACKLINKS_HEADING}\n{links}\n")
    return path


# --- CI gate ------------------------------------------------------------------
@dataclass(frozen=True)
class EvidenceResult:
    ok: bool
    reason: str


PR_EVIDENCE_HEADING = "Reference-set evidence"


def evaluate_pr_evidence(pr_title: str, pr_body: str, cfg: CoreConfig) -> EvidenceResult:
    """Required-check counterpart of the loop's citation resolution.

    A code PR (``feat:``/``skill:``/``refactor:``, however the loop prefixed the
    title) must carry a non-empty ``## Reference-set evidence`` section naming
    only repos pinned in ``.ai-swarm/core.yaml``.
    """
    title = pr_title.strip()
    _, _, tail = title.partition(": ")
    if not (requires_citation(title) or requires_citation(tail)):
        return EvidenceResult(ok=True, reason="exempt: not a feat/skill/refactor PR")

    section = _section(pr_body, PR_EVIDENCE_HEADING)
    if not section:
        return EvidenceResult(
            ok=False, reason=f"PR body has no '## {PR_EVIDENCE_HEADING}' section"
        )
    named = _REPO_SLUG_RE.findall(section)
    if not named:
        return EvidenceResult(
            ok=False,
            reason=f"'## {PR_EVIDENCE_HEADING}' names no reference repo (found: {section!r})",
        )
    pinned = cfg.pinned_repos()
    unpinned = [slug for slug in named if slug not in pinned]
    if unpinned:
        return EvidenceResult(
            ok=False,
            reason=(
                f"'## {PR_EVIDENCE_HEADING}' cites unpinned repo(s) {unpinned} - "
                "evidence must come from the reference set in .ai-swarm/core.yaml"
            ),
        )
    return EvidenceResult(ok=True, reason=f"evidence cites {', '.join(named)}")
