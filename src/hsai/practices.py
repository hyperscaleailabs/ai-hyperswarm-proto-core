"""The adopted-practice registry: durable, cited memory of what this loop has
already learned from the reference set (G1's traceability claim, made real).

Until this module existed, "every improvement traces back to a field
observation" (G1) lived only in free-text PR prose and module docstrings -
nothing indexed it, and nothing stopped the planner from re-deriving ground
already covered (the recurring "chore: refresh reference-set snapshot and
extract one practice" ticket is the visible symptom). A :class:`Practice` is
one committed, Obsidian-ready note per adopted practice: which reference
project it came from, what kind of artifact taught it (one of core.yaml's
``reference_set.learn_from`` values), and the evidence (a PR or commit) that
proves it landed.

The registry is plain files under ``knowledge/practices/`` - :func:`load`,
:func:`append`, and :func:`render` are the whole read/write surface, plus
:func:`is_duplicate` so the same (source project, title) pair is never
recorded twice. :mod:`hsai.knowledge` composes these into a "Practices MOC",
:mod:`hsai.synthesis` renders them into the planner's prompt as ground it must
not re-propose, and :mod:`hsai.tickets` lets a synthesized ticket name which
practice it adds or extends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PRACTICES_DIR_DEFAULT = "knowledge/practices"

# Where a Practice's "notes" learned something. Not enforced against
# core.yaml's reference_set.learn_from list here - callers that have a
# CoreConfig in hand (the CLI) are better placed to warn on a mismatch - but
# named so the intended vocabulary is discoverable from the code.
STATUSES = ("adopted", "partial", "rejected")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
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
class Practice:
    """One practice this loop has adopted (or considered and rejected).

    ``source_artifact`` should be one of core.yaml's
    ``reference_set.learn_from`` values (source_code, commit_history, ci_cd,
    issue_history, harness_design, readme) - what KIND of field observation
    taught this, not just which project.
    """

    id: str
    title: str
    source_project: str
    source_artifact: str
    evidence: str  # URL or commit/PR reference
    adopted_pr: int | None = None
    adopted_date: str = ""
    status: str = "adopted"  # adopted | partial | rejected
    notes: str = ""
    related: tuple[str, ...] = ()  # wikilinked note names (lessons, whitepapers)

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
) -> Practice:
    """Convenience constructor: derives the id, defaults the date to today."""
    return Practice(
        id=make_id(source_project, title),
        title=title,
        source_project=source_project,
        source_artifact=source_artifact,
        evidence=evidence,
        adopted_pr=adopted_pr,
        adopted_date=adopted_date or _today(),
        status=status,
        notes=notes,
        related=related,
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
    fm_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    related = "\n".join(f"- [[{r}]]" for r in practice.related) or "- _(none linked yet)_"
    pr = f"#{practice.adopted_pr}" if practice.adopted_pr else "_(none)_"
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
| adopted PR | {pr} |
| adopted date | {practice.adopted_date or "_(none)_"} |

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


def append(root: str | Path, practice: Practice, *, cfg: Any = None) -> Path:
    """Write `practice` as a new note - refuses a (source_project, title) duplicate."""
    existing = load(root, cfg)
    dup = is_duplicate(existing, practice.source_project, practice.title)
    if dup is not None:
        raise DuplicatePracticeError(
            f"a practice for source_project={practice.source_project!r} "
            f"title={practice.title!r} is already recorded as [[{dup.note_name()}]]"
        )
    path = practices_dir(root, cfg) / f"{practice.note_name()}.md"
    path.write_text(render(practice))
    return path


ADOPTED_HEADING = "Already adopted - do NOT re-propose"


def render_adopted_section(practices: list[Practice]) -> str:
    """The block injected into the synthesis prompt: what NOT to re-propose.

    Rejected practices are shown too (rejecting an idea is still a field
    observation worth remembering), but each carries its status so the
    planner can tell "already shipped" from "already tried and dropped".
    """
    if not practices:
        return "_(no practices recorded yet - this is an early cycle)_"
    lines = [
        f"- **{p.title}** (from `{p.source_project}`, {p.source_artifact}, "
        f"status: {p.status}) [id: `{p.id}`] - {p.evidence or 'no evidence recorded'}"
        for p in practices
    ]
    return "\n".join(lines)
