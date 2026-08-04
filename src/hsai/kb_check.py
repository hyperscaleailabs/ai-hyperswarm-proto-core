"""Integrity gate for the Obsidian vault under ``knowledge/``.

SWE-agent runs ``check-links-pr.yaml`` / ``check-links-periodic.yaml`` so its
documentation cannot rot silently. The same practice applied here: the vault is
a growing web of ``[[wikilinks]]``, and a link that points at nothing is a
broken audit trail, not a cosmetic defect.

Three findings, deliberately graded:

* **dangling wikilink** - an error. A note claims evidence that does not exist,
  and no amount of reindexing can repair it; only an author can.
* **orphan lesson note** - a warning. A lesson lands with its own PR while the
  MOCs are rebuilt once per block by ``hsai reindex``, so a freshly merged
  lesson is legitimately unindexed for a while.
* **MOC drift** - a warning, for the same reason.

``--strict`` promotes the warnings to errors; that is the mode to run after
``hsai reindex``, when the index is supposed to be current. CI runs the default
mode so an ordinary worker PR is never failed for a race it cannot win.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# `[[target]]`, `[[target|alias]]`, `[[target#heading]]`.
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
_COUNT_RE = re.compile(r"Total: \*\*(\d+)\*\*")
_MAP_COUNT_RE = re.compile(r"^- \[\[(?P<moc>[^\]|]+)\]\] - (?P<count>\d+) ", re.MULTILINE)

# Templates hold placeholder links (`[[lesson-note-name]]`) on purpose: they are
# scaffolding for notes yet to be written, not claims about existing evidence.
SKIP_DIRS = ("templates",)


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    detail: str

    def render(self) -> str:
        return f"{self.path}: {self.kind} - {self.detail}"


@dataclass
class Report:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    notes_scanned: int = 0
    links_scanned: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [
            f"kb-check: scanned {self.notes_scanned} note(s), "
            f"{self.links_scanned} wikilink(s)"
        ]
        for f in self.errors:
            lines.append(f"  ERROR   {f.render()}")
        for f in self.warnings:
            lines.append(f"  warning {f.render()}")
        lines.append(
            f"kb-check: {'PASS' if self.ok else 'FAIL'} "
            f"({len(self.errors)} error(s), {len(self.warnings)} warning(s))"
        )
        return "\n".join(lines)


def link_targets(text: str) -> list[str]:
    """Every wikilink target in ``text``, with aliases and headings stripped."""
    targets = []
    for raw in WIKILINK_RE.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


def _notes(vault: Path) -> list[Path]:
    return sorted(
        p for p in vault.rglob("*.md")
        if not any(part in SKIP_DIRS for part in p.relative_to(vault).parts)
    )


def check_vault(root: str | Path, *, knowledge_dir: str = "knowledge") -> Report:
    """Check link integrity, lesson orphans, and MOC drift over the vault."""
    root = Path(root)
    vault = root / knowledge_dir
    report = Report()
    if not vault.is_dir():
        report.errors.append(
            Finding("missing vault", knowledge_dir, "no knowledge directory to check")
        )
        return report

    notes = _notes(vault)
    report.notes_scanned = len(notes)
    resolvable = {p.stem for p in notes}
    texts = {p: p.read_text() for p in notes}

    for path, text in texts.items():
        rel = str(path.relative_to(root))
        for target in link_targets(text):
            report.links_scanned += 1
            if target not in resolvable:
                report.errors.append(
                    Finding("dangling wikilink", rel, f"[[{target}]] resolves to no note")
                )

    _check_index(report, root, vault, texts)
    return report


def _check_index(
    report: Report, root: Path, vault: Path, texts: dict[Path, str]
) -> None:
    """Orphan lessons and MOC drift - the index falling behind the content."""
    mocs = vault / "MOCs"
    lessons_moc = mocs / "Lessons MOC.md"
    lesson_notes = sorted(p.stem for p in (vault / "lessons").glob("*.md"))
    if lessons_moc.is_file():
        indexed = set(link_targets(texts.get(lessons_moc, lessons_moc.read_text())))
        for note in lesson_notes:
            if note not in indexed:
                report.warnings.append(
                    Finding(
                        "orphan lesson note",
                        str((vault / "lessons" / f"{note}.md").relative_to(root)),
                        "not linked from Lessons MOC; run `hsai reindex`",
                    )
                )

    counted = {
        "Lessons MOC": len(lesson_notes),
        "Whitepapers MOC": len(list((vault / "whitepapers").glob("*.md"))),
        "Practices MOC": len(list((vault / "practices").glob("*/*.md"))),
    }
    for name, actual in counted.items():
        path = mocs / f"{name}.md"
        if not path.is_file():
            continue
        claimed = _COUNT_RE.search(texts.get(path, path.read_text()))
        if claimed and int(claimed.group(1)) != actual:
            report.warnings.append(
                Finding(
                    "MOC drift",
                    str(path.relative_to(root)),
                    f"claims {claimed.group(1)} note(s), found {actual}",
                )
            )

    root_moc = mocs / "Knowledge Base MOC.md"
    if root_moc.is_file():
        for match in _MAP_COUNT_RE.finditer(texts.get(root_moc, root_moc.read_text())):
            name, claimed_count = match.group("moc"), int(match.group("count"))
            if name in counted and counted[name] != claimed_count:
                report.warnings.append(
                    Finding(
                        "MOC drift",
                        str(root_moc.relative_to(root)),
                        f"claims {claimed_count} for {name}, found {counted[name]}",
                    )
                )
