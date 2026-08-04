from hsai.config import load_config
from hsai.provenance import (
    UNATTRIBUTED,
    Provenance,
    check,
    parse_practice,
    parse_rationale,
    prompt_contract,
)

# The three repos the loop used to stamp on every artifact regardless of work.
FABRICATED = ("langchain-ai/langchain", "FoundationAgents/MetaGPT", "crewAIInc/crewAI")

TICKET_WITH_RATIONALE = """## Problem
The trace is fabricated.

## Acceptance criteria
- [ ] cite what was actually studied

## Synthesis rationale
SWE-agent supplies the end-to-end issue-to-PR provenance chain, and
openai/swarm contributes the ergonomics of a handoff that carries its context.

## Meta
- goals: G1, G2
"""

TICKET_WITHOUT_RATIONALE = """## Problem
Something is broken.

## Proposal
Fix it.
"""


def _known():
    return load_config().known_repos()


def test_rationale_resolves_exactly_the_repos_it_names():
    cited = parse_rationale(TICKET_WITH_RATIONALE, _known())
    # ordered by where each project is first cited, and nothing else
    assert cited == ("SWE-agent/SWE-agent", "openai/swarm")
    assert not any(repo in cited for repo in FABRICATED)


def test_ticket_without_a_rationale_is_unattributed_not_invented():
    """The honest answer to 'where did this come from?' is sometimes nothing."""
    assert parse_rationale(TICKET_WITHOUT_RATIONALE, _known()) == ()
    assert parse_rationale("", _known()) == ()
    assert UNATTRIBUTED == "_(unattributed)_"


def test_generic_bare_name_is_not_a_citation():
    """`openai/swarm` must be named in full: 'swarm' is a word we use constantly."""
    body = "## Synthesis rationale\nWe made the swarm harness faster.\n"
    assert parse_rationale(body, _known()) == ()


def test_practice_block_round_trips():
    prov = Provenance(
        repos=("SWE-agent/SWE-agent",),
        practice="link-integrity-in-ci",
        claim="Gate documentation link integrity in CI so the vault cannot rot.",
        artifact_kind="ci_cd",
    )
    assert parse_practice(f"## Practice adopted\n{prov.render()}\n") == prov


def test_practice_block_accepts_several_repos():
    prov = parse_practice(
        "## Practice adopted\n"
        "- repos: `openai/swarm`, `SWE-agent/SWE-agent`\n"
        "- artifact: source_code\n"
        "- practice: Handoff Context\n"
        "- claim: carry the problem identity into the artifact\n"
    )
    assert prov is not None
    assert prov.repos == ("openai/swarm", "SWE-agent/SWE-agent")
    assert prov.practice == "handoff-context"        # normalized to a slug
    assert prov.artifact_kind == "source_code"


VALID_BLOCK = (
    "Done.\n\n## Practice adopted\n"
    "- repos: `SWE-agent/SWE-agent`\n"
    "- artifact: ci_cd\n"
    "- practice: link-integrity-in-ci\n"
    "- claim: gate link integrity in CI\n"
)


def test_check_accepts_a_pinned_citation():
    result = check(
        ticket_title="feat: kb integrity gate", text=VALID_BLOCK, known_repos=_known()
    )
    assert result.ok is True and result.exempt is False
    assert result.provenance is not None
    assert result.provenance.repos == ("SWE-agent/SWE-agent",)
    assert "verified" in result.note()


def test_check_rejects_a_repo_outside_the_pinned_reference_set():
    text = VALID_BLOCK.replace("SWE-agent/SWE-agent", "acme/not-a-reference-project")
    result = check(ticket_title="feat: widget", text=text, known_repos=_known())
    assert result.ok is False
    assert "acme/not-a-reference-project" in result.reason
    assert "not in the pinned reference set" in result.reason


def test_check_rejects_a_missing_block_and_a_bogus_artifact_kind():
    missing = check(
        ticket_title="feat: widget", text="I implemented it.", known_repos=_known()
    )
    assert missing.ok is False and "Practice adopted" in missing.reason

    bogus = check(
        ticket_title="feat: widget",
        text=VALID_BLOCK.replace("ci_cd", "vibes"),
        known_repos=_known(),
    )
    assert bogus.ok is False and "artifact kind" in bogus.reason


def test_check_exempts_docs_chore_and_heal_tickets():
    """Routine upkeep is never blocked on citing a reference project."""
    for title in ("docs: fix a typo", "chore: refresh the snapshot", "ci: main is red - auto-heal"):
        result = check(ticket_title=title, text="no citation here", known_repos=_known())
        assert result.ok is True, title
        assert result.exempt is True
        assert "exempt" in result.note()


def test_prompt_contract_names_the_block_and_the_allowed_projects():
    contract = prompt_contract(_known())
    assert "## Practice adopted" in contract
    assert "ci_cd" in contract and "issue_history" in contract
    assert "SWE-agent/SWE-agent" in contract
    assert "Never invent a citation" in contract
