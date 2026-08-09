from hsai.config import load_config
from hsai.practices import (
    ADOPTED,
    QUEUED,
    Coverage,
    Practice,
    PracticeRef,
    PracticeRegistry,
    extract_practice_refs,
    parse_practices,
    practice_id,
    render_coverage_table,
    render_practices_section,
)
from hsai.tickets import TicketSpec

RATIONALE = (
    "Combines crewAIInc/crewAI's practice of committing a durable provenance "
    "artifact alongside each change; run-llama/llama_index's machine-readable "
    "attribution conventions in scoped commit titles; and openai/swarm's "
    "insistence on a tiny, inspectable control loop."
)


def _spec(**kw) -> TicketSpec:
    base = dict(
        title="feat: provenance registry",
        problem="p",
        proposal="pp",
        acceptance_criteria=("a", "b"),
        verification_plan=("v",),
    )
    base.update(kw)
    return TicketSpec(**base)


# --- notes round-trip to disk ------------------------------------------------


def test_practice_round_trips_to_disk(tmp_path):
    registry = PracticeRegistry(tmp_path)
    practice = Practice(
        id=practice_id("crewAIInc/crewAI", "docs-freeze snapshot commits"),
        source_repo="crewAIInc/crewAI",
        artifact="commit `[docs-freeze] docs: snapshot and changelog for v0.86`",
        summary="Commit a durable provenance artifact alongside each change.",
        adopted_by_ticket=42,
    )
    path = registry.write(practice)

    text = path.read_text()
    assert text.startswith("---\n")                      # Obsidian frontmatter
    assert "source_repo: crewAIInc/crewAI" in text
    assert "status: queued" in text
    assert "[docs-freeze]" in text                       # the artifact reference
    assert "[[Practices MOC]]" in text                   # up-links
    assert "[[ticket-42]]" in text                       # wikilink to its ticket

    back = registry.read(practice.id)
    assert back is not None
    assert back.source_repo == "crewAIInc/crewAI"
    assert back.artifact == practice.artifact
    assert back.summary == practice.summary
    assert back.status == QUEUED
    assert back.adopted_by_ticket == 42
    assert back.adopted_by_pr is None
    assert registry.read_all() == [back]


def test_read_of_a_missing_practice_is_none(tmp_path):
    assert PracticeRegistry(tmp_path).read("nope") is None


def test_queue_never_demotes_an_adopted_practice(tmp_path):
    registry = PracticeRegistry(tmp_path)
    ref = PracticeRef("openai/swarm", "keep the control loop inspectable")
    registry.mark_adopted((ref,), ticket=7, pr=8, lesson_note="2026-08-09-loop")

    registry.queue(Practice(
        id=ref.practice_id(), source_repo=ref.source_repo,
        artifact="re-cited", summary=ref.practice, adopted_by_ticket=99,
    ))

    stored = registry.read(ref.practice_id())
    assert stored.status == ADOPTED          # a later citation cannot un-ship it
    assert stored.adopted_by_pr == 8
    assert stored.adopted_by_ticket == 7


def test_mark_adopted_files_a_note_even_when_none_was_queued(tmp_path):
    """A merged PR is the receipt; the registry records it either way."""
    registry = PracticeRegistry(tmp_path)
    refs = (PracticeRef("SWE-agent/SWE-agent", "turn an issue into a PR end to end"),)

    written = registry.mark_adopted(refs, ticket=11, pr=12, lesson_note="2026-08-09-x")

    assert len(written) == 1
    stored = registry.read(refs[0].practice_id())
    assert stored.status == ADOPTED
    assert stored.adopted_by_pr == 12
    assert stored.lesson_note == "2026-08-09-x"
    assert "[[2026-08-09-x]]" in written[0].read_text()   # wikilink to its lesson


def test_coverage_counts_and_filters(tmp_path):
    registry = PracticeRegistry(tmp_path)
    registry.queue(Practice(
        id="a", source_repo="crewAIInc/crewAI", artifact="x", summary="queued one"
    ))
    registry.mark_adopted(
        (PracticeRef("crewAIInc/crewAI", "shipped one"),), ticket=1, pr=2
    )
    registry.queue(Practice(
        id="c", source_repo="openai/swarm", artifact="z", summary="other project"
    ))

    everything = registry.coverage()
    assert everything == [
        Coverage("crewAIInc/crewAI", queued=1, adopted=1, rejected=0),
        Coverage("openai/swarm", queued=1, adopted=0, rejected=0),
    ]
    assert everything[0].total == 2

    only_crew = registry.coverage(repo="crewAIInc/crewAI")
    assert [c.source_repo for c in only_crew] == ["crewAIInc/crewAI"]
    assert registry.coverage(status=ADOPTED) == [
        Coverage("crewAIInc/crewAI", queued=0, adopted=1, rejected=0)
    ]


def test_render_coverage_table_handles_an_empty_registry():
    assert "no practices recorded yet" in render_coverage_table([])


# --- provenance on the ticket -------------------------------------------------


def test_ticket_render_declares_practices_and_round_trips():
    refs = (
        PracticeRef("crewAIInc/crewAI", "durable provenance artifact per change"),
        PracticeRef("openai/swarm", "tiny inspectable control loop"),
    )
    body = _spec(practices=refs).render()

    assert "## Practices adopted" in body
    assert "- crewAIInc/crewAI -> durable provenance artifact per change" in body
    assert parse_practices(body) == refs


def test_ticket_without_practices_declares_none():
    body = _spec().render()
    assert "## Practices adopted" in body
    assert "_(none declared)_" in body
    assert parse_practices(body) == ()


def test_parse_practices_fails_closed():
    assert parse_practices("") == ()
    assert parse_practices("## Problem\nno provenance here\n") == ()
    # the section ends at the next heading - later bullets are not provenance
    body = "## Practices adopted\n- a/b -> real\n\n## Meta\n- c/d -> not a practice\n"
    assert parse_practices(body) == (PracticeRef("a/b", "real"),)


def test_render_practices_section_is_parseable_when_empty():
    assert parse_practices(render_practices_section(())) == ()


# --- mining the synthesis rationale -------------------------------------------


def test_extract_practice_refs_only_matches_pinned_projects():
    cfg = load_config()
    refs = extract_practice_refs(RATIONALE, cfg.known_reference_slugs())
    repos = [r.source_repo for r in refs]

    assert "crewAIInc/crewAI" in repos
    assert "run-llama/llama_index" in repos
    assert "openai/swarm" in repos
    assert all(r in cfg.known_reference_slugs() for r in repos)
    # the practice text quotes the rationale rather than inventing a claim
    crew = next(r for r in refs if r.source_repo == "crewAIInc/crewAI")
    assert "durable provenance artifact" in crew.practice


def test_extract_practice_refs_ignores_unpinned_projects():
    refs = extract_practice_refs(
        "Adopts evil/invented's brilliant idea.", load_config().known_reference_slugs()
    )
    assert refs == ()


def test_extract_practice_refs_matches_the_watchlist():
    cfg = load_config()
    assert "camel-ai/camel" in cfg.known_reference_slugs()
    refs = extract_practice_refs(
        "Borrows camel-ai/camel's role-playing message protocol.",
        cfg.known_reference_slugs(),
    )
    assert [r.source_repo for r in refs] == ["camel-ai/camel"]
