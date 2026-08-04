from hsai.knowledge import KnowledgeBase, Lesson
from hsai.practices import (
    adopted_section,
    build_registry,
    coverage_table,
    render_moc,
    render_note,
)
from hsai.provenance import Provenance

KNOWN = ("openai/swarm", "SWE-agent/SWE-agent", "microsoft/JARVIS")

VERIFIED_LESSON = """---
tags:
  - lesson
---

# implement: feat: knowledge-base integrity gate

## Lesson learned
Link rot is a broken audit trail.

## Practice adopted
- repos: `SWE-agent/SWE-agent`
- artifact: ci_cd
- practice: link-integrity-in-ci
- claim: gate documentation link integrity in CI

## References (reference-set evidence)
- `SWE-agent/SWE-agent`
"""

# A lesson from before the gate: its References block was filled with whatever
# `reference_top10[:3]` happened to be, so the citation proves nothing.
LEGACY_LESSON = """---
tags:
  - lesson
---

# implement: feat: poll remote CI as a pre-merge gate

## Lesson learned
Remote CI is the source of truth for whether a change may merge.

## References (reference-set evidence)
- `openai/swarm`
- `microsoft/JARVIS`
"""


def _registry():
    return build_registry(
        [("2026-08-01-kb-gate", VERIFIED_LESSON), ("2026-07-26-remote-ci", LEGACY_LESSON)],
        KNOWN,
    )


def test_registry_keeps_verified_and_legacy_provenance_apart():
    registry = _registry()
    verified = [n for n in registry if n.verified]
    legacy = [n for n in registry if not n.verified]

    assert [n.repo for n in verified] == ["SWE-agent/SWE-agent"]
    assert verified[0].practice == "link-integrity-in-ci"
    assert verified[0].artifact_kind == "ci_cd"
    assert verified[0].lessons == ("2026-08-01-kb-gate",)

    # the pre-gate citations are recorded for coverage, never presented as evidence
    assert sorted(n.repo for n in legacy) == ["microsoft/JARVIS", "openai/swarm"]
    assert all(n.practice == "poll-remote-ci-as-a-pre-merge-gate" for n in legacy)
    assert all("legacy" in n.grade() for n in legacy)


def test_registry_ignores_citations_outside_the_pinned_set():
    text = VERIFIED_LESSON.replace("SWE-agent/SWE-agent", "acme/invented")
    assert build_registry([("n", text)], KNOWN) == []


def test_coverage_table_has_a_row_per_reference_repo_including_the_unmined():
    table = coverage_table(_registry(), KNOWN)
    assert "| reference repo | practices | verified | lessons |" in table
    assert "| `SWE-agent/SWE-agent` | 1 | 1 | 1 |" in table
    assert "| `openai/swarm` | 1 | 0 | 1 |" in table          # legacy only
    assert "| `microsoft/JARVIS` | 1 | 0 | 1 |" in table
    # a pinned repo nothing has come from still gets a row - the zero is the signal
    assert "| `crewAIInc/crewAI` | 0 | 0 | 0 |" in coverage_table(
        _registry(), (*KNOWN, "crewAIInc/crewAI")
    )


def test_adopted_section_names_what_landed_and_what_is_unmined():
    section = adopted_section(_registry(), (*KNOWN, "crewAIInc/crewAI"))
    assert "adopted: link-integrity-in-ci" in section
    assert "unverified legacy citations: poll-remote-ci-as-a-pre-merge-gate" in section
    assert "`crewAIInc/crewAI`: nothing adopted yet - unmined ground." in section


def test_rendered_note_and_moc_are_obsidian_linked():
    registry = _registry()
    note = next(n for n in registry if n.verified)
    text = render_note(note, created="2026-08-04")
    assert "# link-integrity-in-ci" in text
    assert "[[Practices MOC]]" in text and "[[Knowledge Base MOC]]" in text
    assert "[[2026-08-01-kb-gate]]" in text
    assert "`SWE-agent/SWE-agent`" in text and "ci_cd" in text

    moc = render_moc(registry, KNOWN, created="2026-08-04")
    assert "# Practices MOC" in moc
    assert "Up: [[Knowledge Base MOC]]" in moc
    assert "Total: **3** practice(s), **1** with verified provenance." in moc
    assert "## Coverage by reference project" in moc
    assert "[[link-integrity-in-ci]]" in moc


# --- the registry on disk ----------------------------------------------------


def _lesson(**kwargs) -> Lesson:
    base = dict(
        title="implement: feat: kb integrity gate",
        outcome="pass",
        kind="implement",
        context="ctx",
        what_happened="did the thing",
        lesson="Link rot is a broken audit trail.",
    )
    base.update(kwargs)
    return Lesson(**base)


def test_practices_command_writes_one_note_per_practice_and_links_the_mocs(tmp_path):
    kb = KnowledgeBase(tmp_path, reference_repos=("SWE-agent/SWE-agent", "openai/swarm"))
    kb.write_lesson(
        _lesson(
            provenance=Provenance(
                repos=("SWE-agent/SWE-agent",),
                practice="link-integrity-in-ci",
                claim="gate documentation link integrity in CI",
                artifact_kind="ci_cd",
            )
        )
    )

    written = kb.write_practices()
    assert [p.relative_to(tmp_path).as_posix() for p in written] == [
        "knowledge/practices/swe-agent-swe-agent/link-integrity-in-ci.md"
    ]

    mocs = {p.name: p for p in kb.reindex_mocs()}
    assert "Practices MOC.md" in mocs
    practices_moc = mocs["Practices MOC.md"].read_text()
    assert "[[link-integrity-in-ci]]" in practices_moc
    assert "| `openai/swarm` | 0 | 0 | 0 |" in practices_moc      # coverage table

    root = mocs["Knowledge Base MOC.md"].read_text()
    assert "[[Practices MOC]] - 1 adopted practice(s)" in root


def test_registry_round_trips_a_written_lesson(tmp_path):
    """A lesson's committed provenance block is what the registry reads back."""
    kb = KnowledgeBase(tmp_path, reference_repos=("openai/swarm",))
    kb.write_lesson(
        _lesson(
            provenance=Provenance(
                repos=("openai/swarm",),
                practice="phase-context-in-errors",
                claim="carry the failing phase into the error string",
                artifact_kind="source_code",
            )
        )
    )
    registry = kb.registry()
    assert len(registry) == 1
    assert registry[0].repo == "openai/swarm"
    assert registry[0].practice == "phase-context-in-errors"
    assert registry[0].artifact_kind == "source_code"
    assert registry[0].verified is True
