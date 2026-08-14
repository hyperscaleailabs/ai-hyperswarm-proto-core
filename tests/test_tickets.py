from hsai.tickets import TicketSpec, check_well_formed, practice_ids_in


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


def test_spec_renders_practice_ids_and_they_read_back():
    """The filed body is the only channel from the planner to the worker's lesson."""
    spec = TicketSpec(
        title="feat: automate inbound triage",
        problem="p",
        proposal="pp",
        acceptance_criteria=("a", "b"),
        verification_plan=("v",),
        synthesis_rationale="Combines llama_index triage + crewAI snapshots + MetaGPT SOPs.",
        practice_ids=("run-llama-llama-index-workflow-issue-classifier-yml",
                      "crewaiinc-crewai-commits"),
    )
    body = spec.render()

    assert "## Synthesis rationale" in body
    assert "`run-llama-llama-index-workflow-issue-classifier-yml`" in body
    assert practice_ids_in(body) == (
        "run-llama-llama-index-workflow-issue-classifier-yml", "crewaiinc-crewai-commits",
    )


def test_practice_ids_are_optional_everywhere():
    """Hand-written tickets (and every ticket filed before this existed) still work."""
    assert practice_ids_in("## Problem\nnothing structured here") == ()
    spec = TicketSpec(
        title="feat: x", problem="p", proposal="pp",
        acceptance_criteria=("a", "b"), verification_plan=("v",),
        synthesis_rationale="just a rationale",
    )
    assert "Practice IDs" not in spec.render()
    assert practice_ids_in(spec.render()) == ()


def test_spec_renders_practice_ids_without_a_rationale():
    spec = TicketSpec(
        title="feat: x", problem="p", proposal="pp",
        acceptance_criteria=("a", "b"), verification_plan=("v",),
        practice_ids=("crewaiinc-crewai-commits",),
    )
    assert practice_ids_in(spec.render()) == ("crewaiinc-crewai-commits",)


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
