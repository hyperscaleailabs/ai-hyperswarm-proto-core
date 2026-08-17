from hsai.tickets import TicketSpec, check_well_formed


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


def test_spec_renders_practice_ids_in_the_meta_section():
    spec = TicketSpec(
        title="feat: adaptive retry budget",
        problem="Retries are fixed.",
        proposal="Budget adapts to failure class.",
        acceptance_criteria=("budget adapts", "tests cover classes"),
        verification_plan=("pytest green",),
        practice_ids=("swe-agent-swe-agent--persist-a-traj-per-run",),
    )
    body = spec.render()
    assert "- practice_ids: swe-agent-swe-agent--persist-a-traj-per-run" in body


def test_spec_practice_ids_defaults_to_empty_and_still_renders():
    """Existing specs that omit practice_ids must not break."""
    spec = TicketSpec(
        title="feat: thing",
        problem="p",
        proposal="pp",
        acceptance_criteria=("a", "b"),
        verification_plan=("v",),
    )
    assert spec.practice_ids == ()
    assert "- practice_ids: -" in spec.render()


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
