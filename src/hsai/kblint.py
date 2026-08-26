"""``hsai kb-lint``: an integrity gate for the Obsidian knowledge base.

The knowledge base is the loop's actual product under G3, and until now it was
the only major artifact with no automated gate: ``ci.run_local`` checks
``ruff`` + ``pytest`` only, and nothing verified that the ``[[wikilinks]]``
wiring lessons, whitepapers, articles and MOCs together actually resolves. A
renamed or never-created note yields a dangling link invisible until a human
opens the vault in Obsidian, and a note nobody indexes (persona articles, until
this module existed) silently drops out of the graph.

Four checks, a stable code table:

- **KB001** dangling wikilink - the target resolves against no note in
  lessons, whitepapers, articles, MOCs, practices, or ``knowledge/hsai.md``.
- **KB002** missing or malformed frontmatter tags for the note's kind.
- **KB003** orphan note - reachable from no MOC (a MOC-kind note never counts
  as an orphan of itself; it is the index, not indexed content).
- **KB004** a lesson missing a ticket reference, or carrying an empty or
  placeholder ``## Lesson learned`` body.

KB001/KB003 are ``error`` severity (a broken graph edge or a note that fell out
of the index entirely); KB002/KB004 are ``warn`` (a schema nit or a thin
lesson, not a broken graph). ``--strict`` promotes warnings to errors for the
exit-code decision - see :func:`has_errors`.

Pure and read-only: :func:`lint` never writes to disk. It is used by
``hsai kb-lint`` (see :mod:`hsai.cli`), by ``ci.run_local`` as a pre-flight
step, and by ``cycle.run_cycle`` to surface findings in the block review issue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import practices as practices_mod
from .config import CoreConfig
from .knowledge import parse_note

# --- the code table -------------------------------------------------------

KB_DANGLING_LINK = "KB001"
KB_BAD_FRONTMATTER = "KB002"
KB_ORPHAN = "KB003"
KB_LESSON_THIN = "KB004"

SEVERITY: dict[str, str] = {
    KB_DANGLING_LINK: "error",
    KB_BAD_FRONTMATTER: "warn",
    KB_ORPHAN: "error",
    KB_LESSON_THIN: "warn",
}

# Note kinds, derived from which directory a note lives in - robust even when
# the frontmatter itself is what's broken (KB002 checks it, so kind can't
# depend on it).
KIND_LESSON = "lesson"
KIND_WHITEPAPER = "whitepaper"
KIND_ARTICLE = "article"
KIND_MOC = "moc"
KIND_PRACTICE = "practice"
KIND_CONCEPT = "concept"

# Required frontmatter tags per kind. A plain string must appear verbatim; a
# string ending in "/" means "at least one tag with this prefix" (e.g. a
# lesson's `outcome/pass` vs `outcome/fail`).
_REQUIRED_TAGS: dict[str, tuple[str, ...]] = {
    KIND_LESSON: ("lesson", "outcome/", "kind/"),
    KIND_WHITEPAPER: ("whitepaper",),
    KIND_ARTICLE: ("article", "persona/"),
    KIND_MOC: ("moc",),
    KIND_PRACTICE: ("practice", "status/", "source/"),
    KIND_CONCEPT: ("concept",),
}

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_TICKET_ROW_RE = re.compile(r"\|\s*ticket\s*\|\s*#\d+", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"^_\(.*\)_$")
_PLACEHOLDER_WORDS = frozenset({"tbd", "todo", "n/a", "none", "..."})


@dataclass(frozen=True)
class Finding:
    """One integrity defect: where, what code, what it means, how bad."""

    path: str  # repo-relative, e.g. "knowledge/lessons/2026-01-01-x.md"
    code: str
    message: str
    severity: str  # "error" | "warn"

    def render(self) -> str:
        return f"{self.path}:{self.code}: {self.message}"


@dataclass(frozen=True)
class _Note:
    stem: str
    rel: str
    kind: str
    tags: tuple[str, ...]
    body: str
    lesson_text: str


def _missing_tags(tags: tuple[str, ...], kind: str) -> list[str]:
    missing: list[str] = []
    for required in _REQUIRED_TAGS.get(kind, ()):
        if required.endswith("/"):
            if not any(t.startswith(required) for t in tags):
                missing.append(f"{required}*")
        elif required not in tags:
            missing.append(required)
    return missing


def _wikilink_targets(body: str) -> list[str]:
    return [m.group(1).split("#", 1)[0].strip() for m in _WIKILINK_RE.finditer(body)]


def _is_placeholder_lesson(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if _PLACEHOLDER_RE.match(stripped):
        return True
    return stripped.strip("_() ").lower() in _PLACEHOLDER_WORDS


def _dirs(root: Path, cfg: CoreConfig | None) -> dict[str, Path]:
    """The five directories this linter scans, resolved the same way
    :class:`hsai.knowledge.KnowledgeBase` does - but read-only: unlike the
    knowledge base, a lint pass must never create a directory as a side effect.
    """
    k = (cfg.knowledge if cfg else None) or {}
    return {
        KIND_LESSON: root / k.get("lessons_dir", "knowledge/lessons"),
        KIND_WHITEPAPER: root / k.get("whitepapers_dir", "knowledge/whitepapers"),
        KIND_ARTICLE: root / k.get("articles_dir", "knowledge/articles"),
        KIND_MOC: root / k.get("mocs_dir", "knowledge/MOCs"),
        KIND_PRACTICE: root / k.get("practices_dir", practices_mod.PRACTICES_DIR_DEFAULT),
    }


def _load_notes(root: Path, cfg: CoreConfig | None) -> list[_Note]:
    notes: list[_Note] = []
    for kind, directory in _dirs(root, cfg).items():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            record = parse_note(path)
            notes.append(_Note(
                stem=path.stem, rel=str(path.relative_to(root)), kind=kind,
                tags=record.tags, body=record.body, lesson_text=record.lesson_text,
            ))
    # The one single-file "concept" note: not a directory, so it is not one of
    # the five scanned above, but it IS a valid KB001 resolution target and a
    # KB002/KB003 subject in its own right.
    hsai_note = root / "knowledge" / "hsai.md"
    if hsai_note.is_file():
        record = parse_note(hsai_note)
        notes.append(_Note(
            stem=hsai_note.stem, rel=str(hsai_note.relative_to(root)), kind=KIND_CONCEPT,
            tags=record.tags, body=record.body, lesson_text=record.lesson_text,
        ))
    notes.sort(key=lambda n: n.rel)
    return notes


def lint(root: str | Path, cfg: CoreConfig | None = None) -> list[Finding]:
    """Lint the vault under ``root``. Pure and read-only.

    ``cfg`` supplies the ``knowledge.*_dir`` overrides the same way every other
    reader in this codebase does (:mod:`hsai.recall`, :mod:`hsai.retrieval`);
    ``None`` means "the repo's defaults".
    """
    root = Path(root)
    notes = _load_notes(root, cfg)
    known = {n.stem for n in notes}

    # A note is reachable when some MOC-kind note links to it directly. MOCs
    # themselves are the index, not indexed content, so they are exempt.
    linked_from_moc: set[str] = set()
    for n in notes:
        if n.kind == KIND_MOC:
            linked_from_moc.update(_wikilink_targets(n.body))

    findings: list[Finding] = []
    for n in notes:
        for target in _wikilink_targets(n.body):
            if target not in known:
                findings.append(Finding(
                    n.rel, KB_DANGLING_LINK, f"dangling wikilink to [[{target}]]",
                    SEVERITY[KB_DANGLING_LINK],
                ))

        missing = _missing_tags(n.tags, n.kind)
        if missing:
            findings.append(Finding(
                n.rel, KB_BAD_FRONTMATTER,
                f"missing required frontmatter tag(s) for kind '{n.kind}': "
                f"{', '.join(missing)}",
                SEVERITY[KB_BAD_FRONTMATTER],
            ))

        if n.kind != KIND_MOC and n.stem not in linked_from_moc:
            findings.append(Finding(
                n.rel, KB_ORPHAN, "orphan note: not linked from any MOC",
                SEVERITY[KB_ORPHAN],
            ))

        if n.kind == KIND_LESSON:
            reasons = []
            if not _TICKET_ROW_RE.search(n.body):
                reasons.append("missing a ticket reference")
            if _is_placeholder_lesson(n.lesson_text):
                reasons.append("placeholder or empty 'Lesson learned' body")
            if reasons:
                findings.append(Finding(
                    n.rel, KB_LESSON_THIN, "; ".join(reasons), SEVERITY[KB_LESSON_THIN],
                ))

    findings.sort(key=lambda f: (f.path, f.code))
    return findings


def effective_severity(finding: Finding, *, strict: bool = False) -> str:
    """``finding.severity``, with every warning promoted to error under ``--strict``."""
    return "error" if strict else finding.severity


def has_errors(findings: list[Finding], *, strict: bool = False) -> bool:
    return any(effective_severity(f, strict=strict) == "error" for f in findings)


def sorted_by_severity(findings: list[Finding], *, strict: bool = False) -> list[Finding]:
    """Errors first (post-``--strict`` promotion), then path, then code - the
    order both the CLI and the CI step print in."""
    return sorted(
        findings,
        key=lambda f: (effective_severity(f, strict=strict) != "error", f.path, f.code),
    )
