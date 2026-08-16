import dataclasses

import pytest

from hsai.tickets import (
    PRIOR_ART_HEADING,
    TicketSpec,
    check_spec,
    check_well_formed,
    prior_art_citations,
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


# --- prior art: every synthesized ticket cites our own record ------------------


def _spec(**overrides) -> TicketSpec:
    base = TicketSpec(
        title="feat: adaptive retry budget",
        problem="Retries are fixed.",
        proposal="Budget adapts to failure class.",
        acceptance_criteria=("budget adapts", "tests cover classes"),
        verification_plan=("pytest green",),
        prior_art="[[2026-08-04-worker-trajectory-capture]] shows attempts are never counted.",
    )
    return dataclasses.replace(base, **overrides)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("see [[2026-01-01-a-lesson]]", ["[[2026-01-01-a-lesson]]"]),
        ("closed as #142", ["#142"]),
        ("the ledger shows 1425s per merged PR", ["ledger shows 1425"]),
        ("[[a-note]] and #7", ["[[a-note]]", "#7"]),
        ("we have learned this lesson before", []),
        ("the ledger is instructive", []),   # a claim with no figure cites nothing
        ("", []),
    ],
)
def test_prior_art_citations_recognises_only_resolvable_refs(text, expected):
    assert prior_art_citations(text) == expected


def test_a_spec_citing_internal_evidence_is_accepted():
    assert check_spec(_spec()).ok
    assert check_spec(_spec(prior_art="closed ticket #142 tried this")).ok
    assert check_spec(_spec(prior_art="ledger block 41339: 1425s/merged PR")).ok


def test_a_spec_without_prior_art_is_rejected():
    """The rejection path: no internal citation, no ticket."""
    empty = check_spec(_spec(prior_art=""))
    assert not empty.ok
    assert any("empty 'prior_art'" in r for r in empty.reasons)

    vague = check_spec(_spec(prior_art="this builds on what the loop already learned"))
    assert not vague.ok
    assert any("cites no internal artifact" in r for r in vague.reasons)


def test_check_spec_also_guards_the_rest_of_the_schema():
    thin = check_spec(_spec(acceptance_criteria=("only one",), verification_plan=()))
    assert not thin.ok
    assert any("acceptance criteria" in r for r in thin.reasons)
    assert any("verification plan" in r for r in thin.reasons)


def test_prior_art_is_rendered_into_the_issue_body():
    body = _spec().render()
    assert f"## {PRIOR_ART_HEADING}" in body
    assert "[[2026-08-04-worker-trajectory-capture]]" in body
    # and a spec without it renders exactly as it did before the field existed
    assert PRIOR_ART_HEADING not in _spec(prior_art="").render()
