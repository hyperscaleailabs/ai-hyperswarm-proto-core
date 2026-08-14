from hsai.tickets import (
    TicketSpec,
    check_well_formed,
    cited_projects,
    synthesis_rationale,
)


def test_spec_renders_all_required_sections():
    spec = TicketSpec(
        title="feat: adaptive retry budget",
        problem="Retries are fixed.",
        proposal="Budget adapts to failure class.",
        acceptance_criteria=("budget adapts", "tests cover classes", "docs updated"),
        verification_plan=("pytest green", "manual failure injection"),
        size="M",
        goal_ids=("G4",),
        synthesis_rationale="Combines SWE-agent retries + JARVIS routing + crewAI config.",
    )
    body = spec.render()
    assert "## Acceptance criteria" in body
    assert body.count("- [ ]") == 5
    assert "## Verification plan" in body
    assert "## Synthesis rationale" in body
    assert "size: M" in body
    assert "size:M" in spec.all_labels()
    wf = check_well_formed(spec.title, body)
    assert wf.ok, wf.reasons


def test_vague_feature_ticket_is_malformed():
    wf = check_well_formed("feat: make it better", "please improve things")
    assert not wf.ok
    assert any("Acceptance criteria" in r for r in wf.reasons)


def test_checkbox_minimum_enforced():
    body = "## Acceptance criteria\n- [ ] only one\n\n## Verification plan\nprose only"
    wf = check_well_formed("feat: thing", body)
    assert not wf.ok
    assert any("checkbox" in r for r in wf.reasons)


def test_two_checkboxes_and_both_sections_pass():
    body = "## Acceptance criteria\n- [ ] a\n- [ ] b\n\n## Verification plan\n- [ ] pytest"
    assert check_well_formed("feat: thing", body).ok


def test_docs_and_chores_are_exempt():
    assert check_well_formed("docs: fix typo", "tiny").ok
    assert check_well_formed("chore: bump dep", "routine").ok
    assert check_well_formed("ci: main is red - auto-heal", "incident").ok


# --- provenance ---------------------------------------------------------------

RATIONALE_BODY = """## Problem
Something is wrong.

## Synthesis rationale
Combines openai/swarm (its `Result` object) and microsoft/JARVIS, plus a nod to
assafelovic/gpt-researcher. Paths like `src/hsai/orchestrator.py` are identifiers.

## Meta
- goals: G1
- size: L
"""


def test_synthesis_rationale_stops_at_the_next_heading():
    section = synthesis_rationale(RATIONALE_BODY)
    assert section.startswith("Combines openai/swarm")
    assert section.endswith("are identifiers.")
    assert "goals:" not in section          # the next section is not swallowed


def test_synthesis_rationale_is_empty_when_the_ticket_has_none():
    assert synthesis_rationale("## Problem\nno rationale here\n") == ""
    assert synthesis_rationale("") == ""


def test_cited_projects_reads_prose_citations_in_first_mention_order():
    assert cited_projects(RATIONALE_BODY) == (
        "openai/swarm", "microsoft/JARVIS", "assafelovic/gpt-researcher",
    )
    # A path inside a code span is an identifier, never a citation.
    assert not any("src/hsai" in c for c in cited_projects(RATIONALE_BODY))


def test_cited_projects_ignores_prose_slashes_when_given_the_known_set():
    body = "## Synthesis rationale\nWeighs input/output and and/or against openai/swarm.\n"
    # Unfiltered, the parse is shape-based and catches incidental slashes...
    assert cited_projects(body) == ("input/output", "and/or", "openai/swarm")
    # ...which is exactly why the reference set is passed as an allow-list.
    assert cited_projects(body, ["openai/swarm", "microsoft/JARVIS"]) == ("openai/swarm",)


def test_cited_projects_is_empty_for_a_ticket_that_cites_nothing():
    spec = TicketSpec(
        title="feat: no citations",
        problem="p", proposal="pp",
        acceptance_criteria=("a", "b"), verification_plan=("v",),
    )
    assert cited_projects(spec.render()) == ()
