from hsai.tickets import TicketSpec, check_well_formed, parse_practice_ids


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


def test_practice_ids_render_under_the_rationale_and_parse_back():
    """The evidence trail: named practices, not bare repo slugs."""
    spec = TicketSpec(
        title="feat: dedupe gate",
        problem="p",
        proposal="pp",
        acceptance_criteria=("a", "b"),
        verification_plan=("v",),
        synthesis_rationale="Combines llama_index triage + crewAI snapshots.",
        practice_ids=("run-llama-llama-index--triage", "crewaiinc-crewai--docs-freeze"),
    )
    body = spec.render()

    assert "## Synthesis rationale" in body
    assert body.index("Combines llama_index") < body.index("- practice_ids:")
    assert "`run-llama-llama-index--triage`" in body
    assert parse_practice_ids(body) == (
        "run-llama-llama-index--triage", "crewaiinc-crewai--docs-freeze",
    )
    # the citation line must not be mistaken for an acceptance-criteria checkbox
    assert check_well_formed(spec.title, body).ok


def test_no_practice_ids_renders_no_citation_line():
    spec = TicketSpec(
        title="feat: thing", problem="p", proposal="pp",
        acceptance_criteria=("a", "b"), verification_plan=("v",),
    )
    assert "practice_ids" not in spec.render()
    assert parse_practice_ids(spec.render()) == ()


def test_parse_practice_ids_tolerates_a_hand_edited_list():
    """An architect refining a ticket must not silently lose its citations."""
    body = "## Synthesis rationale\nx\n- practice_ids: openai-swarm--handoff, metagpt--news\n"
    assert parse_practice_ids(body) == ("openai-swarm--handoff", "metagpt--news")


def test_parse_practice_ids_dedupes_and_ignores_prose():
    body = (
        "The spec must carry a `practice_ids` array.\n"
        "- practice_ids: `a--b`, `a--b`, `c--d`\n"
    )
    assert parse_practice_ids(body) == ("a--b", "c--d")
