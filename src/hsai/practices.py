"""The adopted-practice registry: what has been mined from which project.

A flat pile of lessons cannot answer "what have we already taken from
SWE-agent?" - so the synthesizer kept re-proposing ground that had already been
covered. Following run-llama/llama_index's discipline that a corpus is only
useful once it is indexed and queryable, this module turns the lessons into a
registry keyed by (reference repo, practice) and renders it as Obsidian notes
plus a per-repo coverage table.

Two provenance grades are kept apart on purpose:

* **verified** - the lesson carries a ``## Practice adopted`` block naming the
  source repo, the artifact kind studied, and the claim;
* **legacy** - the lesson only has the old ``## References`` block, which the
  loop used to fill with ``reference_top10[:3]`` regardless of what was learned.
  Those citations are recorded so coverage is complete, but never presented as
  evidence.

Pure data + rendering: :class:`hsai.knowledge.KnowledgeBase` owns the files.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import provenance
from .provenance import slugify

_REFERENCES_RE = re.compile(
    r"^#{2,3}[ \t]*references[^\n]*$\n(.*?)(?=^#{2,3}[ \t]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_LESSON_RE = re.compile(
    r"^#{2,3}[ \t]*lesson learned[ \t]*$\n(.*?)(?=^#{2,3}[ \t]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_KIND_PREFIX_RE = re.compile(
    r"^(heal|implement|improve)\s*:\s*(feat|fix|chore|docs|skill|refactor|perf|test)?\s*:?\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PracticeNote:
    """One practice, adopted from one reference repo, across >= 1 lesson."""

    repo: str
    practice: str
    claim: str = ""
    artifact_kind: str = ""
    lessons: tuple[str, ...] = ()
    verified: bool = True

    def repo_slug(self) -> str:
        return slugify(self.repo)

    def note_name(self) -> str:
        return self.practice

    def grade(self) -> str:
        return "verified" if self.verified else "legacy (unverified citation)"


def _practice_slug(title: str) -> str:
    """Derive a practice slug from a lesson title, minus its loop-kind prefix."""
    return slugify(_KIND_PREFIX_RE.sub("", title.strip()))[:80] or "unnamed-practice"


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def legacy_citations(note_text: str, known_repos: Iterable[str]) -> tuple[str, ...]:
    """Repos named in a lesson's old ``## References`` block."""
    return provenance.match_repos(
        provenance.last_section(_REFERENCES_RE, note_text), known_repos
    )


def build_registry(
    lessons: Iterable[tuple[str, str]], known_repos: Sequence[str]
) -> list[PracticeNote]:
    """Index ``(note_name, note_text)`` pairs into the practice registry.

    A verified adoption always wins over a legacy citation for the same
    (repo, practice) key, and every lesson that contributed is linked.
    """
    known = tuple(known_repos)
    merged: dict[tuple[str, str], PracticeNote] = {}

    def _add(note: PracticeNote) -> None:
        key = (note.repo, note.practice)
        prior = merged.get(key)
        if prior is None:
            merged[key] = note
            return
        lessons_seen = tuple(dict.fromkeys((*prior.lessons, *note.lessons)))
        winner, loser = (note, prior) if note.verified and not prior.verified else (prior, note)
        merged[key] = PracticeNote(
            repo=winner.repo,
            practice=winner.practice,
            claim=winner.claim or loser.claim,
            artifact_kind=winner.artifact_kind or loser.artifact_kind,
            lessons=lessons_seen,
            verified=prior.verified or note.verified,
        )

    for note_name, text in lessons:
        title_match = _TITLE_RE.search(text)
        title = title_match.group(1).strip() if title_match else note_name
        prov = provenance.parse_practice(text)
        if prov is not None and not prov.is_empty():
            for repo in prov.repos:
                if repo not in known:
                    continue
                _add(
                    PracticeNote(
                        repo=repo,
                        practice=prov.practice or _practice_slug(title),
                        claim=prov.claim,
                        artifact_kind=prov.artifact_kind,
                        lessons=(note_name,),
                        verified=True,
                    )
                )
            continue
        claim = _first_line(provenance.last_section(_LESSON_RE, text))
        for repo in legacy_citations(text, known):
            _add(
                PracticeNote(
                    repo=repo,
                    practice=_practice_slug(title),
                    claim=claim,
                    lessons=(note_name,),
                    verified=False,
                )
            )

    return sorted(merged.values(), key=lambda n: (n.repo, n.practice))


def by_repo(registry: Sequence[PracticeNote]) -> dict[str, list[PracticeNote]]:
    grouped: dict[str, list[PracticeNote]] = {}
    for note in registry:
        grouped.setdefault(note.repo, []).append(note)
    return grouped


def coverage_table(registry: Sequence[PracticeNote], known_repos: Sequence[str]) -> str:
    """Per-reference-repo coverage: how much of each project has been mined."""
    grouped = by_repo(registry)
    rows = ["| reference repo | practices | verified | lessons |", "| --- | --- | --- | --- |"]
    # Every pinned repo gets a row, including the ones nothing has come from -
    # the zeros are the point: they are the unmined ground.
    for repo in [*known_repos, *(r for r in grouped if r not in known_repos)]:
        notes = grouped.get(repo, [])
        verified = sum(1 for n in notes if n.verified)
        lessons = len({lesson for n in notes for lesson in n.lessons})
        rows.append(f"| `{repo}` | {len(notes)} | {verified} | {lessons} |")
    return "\n".join(rows)


def adopted_section(registry: Sequence[PracticeNote], known_repos: Sequence[str]) -> str:
    """The 'already adopted' briefing handed to the synthesis planner.

    Candidates must go beyond what has already landed - this is what closes the
    duplicate-idea hole that once filed nine identical chore tickets.
    """
    grouped = by_repo(registry)
    lines: list[str] = []
    for repo in [*known_repos, *(r for r in grouped if r not in known_repos)]:
        notes = grouped.get(repo, [])
        if not notes:
            lines.append(f"- `{repo}`: nothing adopted yet - unmined ground.")
            continue
        verified = [n.practice for n in notes if n.verified]
        legacy = [n.practice for n in notes if not n.verified]
        parts = []
        if verified:
            parts.append("adopted: " + ", ".join(sorted(verified)))
        if legacy:
            parts.append("unverified legacy citations: " + ", ".join(sorted(legacy)))
        lines.append(f"- `{repo}`: " + "; ".join(parts) + ".")
    return "\n".join(lines)


def render_note(note: PracticeNote, *, created: str) -> str:
    """One Obsidian note per adopted practice."""
    lessons = "\n".join(f"- [[{n}]]" for n in note.lessons) or "- _(none linked)_"
    tags = [
        "  - practice",
        f"  - repo/{note.repo_slug()}",
        f"  - artifact/{note.artifact_kind or 'unspecified'}",
        f"  - provenance/{'verified' if note.verified else 'legacy'}",
    ]
    return f"""---
tags:
{chr(10).join(tags)}
created: {created}
---

# {note.practice}

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `{note.repo}` |
| source artifact | {note.artifact_kind or "_(unspecified)_"} |
| provenance | {note.grade()} |

## Claim
{note.claim or "_(no claim recorded)_"}

## Lessons that adopted it
{lessons}
"""


def render_moc(
    registry: Sequence[PracticeNote], known_repos: Sequence[str], *, created: str
) -> str:
    """The Practices MOC: coverage table plus the per-repo index."""
    grouped = by_repo(registry)
    sections = []
    for repo in sorted(grouped):
        links = "\n".join(
            f"- [[{n.note_name()}]] - {n.grade()}" for n in grouped[repo]
        )
        sections.append(f"### `{repo}`\n{links}")
    body = "\n\n".join(sections) or "_No practice has been registered yet._"
    verified = sum(1 for n in registry if n.verified)
    return f"""---
tags:
  - moc
  - practices
updated: {created}
---

# Practices MOC

Up: [[Knowledge Base MOC]]

What this harness has actually taken from the reference set, and from where.
Total: **{len(registry)}** practice(s), **{verified}** with verified provenance.

## Coverage by reference project
{coverage_table(registry, known_repos)}

Rows with zero practices are unmined ground - the synthesizer is told about
them so it stops re-proposing what has already landed.

## Practices by project
{body}
"""
