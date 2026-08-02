from pathlib import Path

from hsai import practices
from hsai.config import load_config
from hsai.practices import (
    PracticeCard,
    PracticeProposal,
    Registry,
    cite,
    evaluate_pr_evidence,
    load_cards,
    reindex,
    requires_citation,
    resolve_citation,
    validate_card,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cfg():
    return load_config()


def _card(**overrides) -> PracticeCard:
    base = dict(
        id="PR-0042",
        title="Some observed practice",
        source_repo="openai/swarm",
        artifact_kind="code",
        artifact_ref="swarm/core.py",
        observed_on="2026-07-26",
    )
    base.update(overrides)
    return PracticeCard(**base)


# --- the real vault -----------------------------------------------------------
def test_seeded_vault_cards_are_valid_and_pinned():
    cfg = _cfg()
    cards = load_cards(REPO_ROOT, cfg)
    assert len(cards) >= 6, "the registry must seed at least six observed practices"
    problems = [p for c in cards for p in validate_card(c, cfg)]
    assert problems == []
    assert {c.source_repo for c in cards} <= cfg.pinned_repos()
    assert all(c.artifact_ref for c in cards)
    # ids are unique - a duplicate would make citations ambiguous
    assert len({c.id for c in cards}) == len(cards)


# --- schema validation --------------------------------------------------------
def test_validate_rejects_repo_outside_the_reference_set():
    problems = validate_card(_card(source_repo="acme/not-a-reference"), _cfg())
    assert len(problems) == 1
    assert "not in the pinned reference set" in problems[0]


def test_validate_accepts_a_watchlist_repo():
    assert validate_card(_card(source_repo="camel-ai/camel"), _cfg()) == []


def test_validate_rejects_missing_frontmatter_fields():
    problems = validate_card(_card(source_repo="", artifact_ref="", observed_on=""), _cfg())
    joined = "\n".join(problems)
    assert "missing frontmatter field 'source_repo'" in joined
    assert "missing frontmatter field 'artifact_ref'" in joined
    assert "missing frontmatter field 'observed_on'" in joined


def test_validate_rejects_malformed_fields():
    assert any("id must look like" in p for p in validate_card(_card(id="0042"), _cfg()))
    assert any(
        "artifact_kind must be one of" in p
        for p in validate_card(_card(artifact_kind="vibes"), _cfg())
    )
    assert any(
        "observed_on must be ISO" in p
        for p in validate_card(_card(observed_on="last tuesday"), _cfg())
    )
    assert any(
        "must be a hex SHA" in p
        for p in validate_card(_card(artifact_kind="commit", artifact_ref="main"), _cfg())
    )
    assert any(
        "must be a workflow file" in p
        for p in validate_card(_card(artifact_kind="ci", artifact_ref="ci"), _cfg())
    )


def test_parse_card_round_trips_through_render():
    card = _card()
    parsed = practices.parse_card(practices.render_card(card, what="w", why="y"))
    assert parsed.id == card.id
    assert parsed.title == card.title
    assert parsed.source_repo == card.source_repo
    assert parsed.artifact_kind == card.artifact_kind
    assert parsed.artifact_ref == card.artifact_ref
    assert parsed.observed_on == card.observed_on


# --- citation parsing ---------------------------------------------------------
TICKET_WITH_PRACTICES = """## Problem
Provenance is asserted, not proven.

## Practices
- PR-0003
- PR-0008

## Synthesis rationale
Combines langchain-ai/langchain and crewAIInc/crewAI.

## Meta
- size: L
"""


def test_cite_reads_only_the_practices_section():
    assert cite(TICKET_WITH_PRACTICES) == ("PR-0003", "PR-0008")
    assert cite("no practices here, though PR-0001 is mentioned") == ()


def test_resolve_citation_prefers_registered_practice_ids():
    cfg = _cfg()
    cards = load_cards(REPO_ROOT, cfg)
    citation = resolve_citation(TICKET_WITH_PRACTICES, cards, cfg)

    assert citation.source == "practices"
    assert citation.ids == ("PR-0003", "PR-0008")
    assert citation.repos == ("microsoft/JARVIS", "openai/swarm")
    # the rationale's repos are NOT what a cited ticket claims as evidence
    assert "langchain-ai/langchain" not in citation.repos
    assert all(n.startswith("PR-") for n in citation.notes)


def test_resolve_citation_falls_back_to_the_synthesis_rationale():
    cfg = _cfg()
    body = (
        "## Problem\np\n\n## Synthesis rationale\n"
        "Combines assafelovic/gpt-researcher with SWE-agent/SWE-agent and "
        "some/unpinned-repo.\n"
    )
    citation = resolve_citation(body, [], cfg)

    assert citation.source == "rationale"
    assert citation.repos == ("assafelovic/gpt-researcher", "SWE-agent/SWE-agent")
    assert citation.ids == ()
    assert citation.ok


def test_resolve_citation_is_empty_when_nothing_is_cited():
    citation = resolve_citation("## Problem\njust do the thing\n", [], _cfg())
    assert not citation.ok
    assert citation.source == "none"
    assert citation.repos == ()


def test_unregistered_ids_do_not_resolve():
    citation = resolve_citation("## Practices\n- PR-9999\n", load_cards(REPO_ROOT, _cfg()), _cfg())
    assert not citation.ok


def test_requires_citation_covers_code_tickets_only():
    assert requires_citation("feat: evidence registry")
    assert requires_citation("skill: better routing")
    assert requires_citation("refactor: split the orchestrator")
    assert not requires_citation("docs: explain the loop")
    assert not requires_citation("chore: refresh the snapshot")
    assert not requires_citation("ci: main is red - auto-heal")


# --- the registry -------------------------------------------------------------
def test_registry_files_a_card_for_an_unregistered_practice(tmp_path):
    cfg = _cfg()
    registry = Registry(tmp_path, cfg)
    assert registry.cards == []

    card = registry.register(
        PracticeProposal(
            title="Gatekeeper blocks the merge",
            source_repo="microsoft/semantic-kernel",
            artifact_kind="ci",
            artifact_ref=".github/workflows/merge-gatekeeper.yml",
            what="what it does",
            why="why it applies",
        )
    )

    assert card is not None and card.id == "PR-0001"
    written = practices.practices_dir(tmp_path, cfg) / f"{card.note_name}.md"
    assert written.exists()
    assert "why it applies" in written.read_text()
    assert validate_card(practices.parse_card(written.read_text()), cfg) == []

    # the same practice proposed again reuses the card instead of duplicating it
    again = registry.register(
        PracticeProposal(
            source_repo="microsoft/semantic-kernel",
            artifact_kind="ci",
            artifact_ref=".github/workflows/merge-gatekeeper.yml",
        )
    )
    assert again is not None and again.id == "PR-0001"
    assert len(list(practices.practices_dir(tmp_path, cfg).glob("*.md"))) == 1


def test_registry_refuses_a_practice_from_an_unpinned_repo(tmp_path):
    cfg = _cfg()
    registry = Registry(tmp_path, cfg)
    proposal = PracticeProposal(
        title="Invented", source_repo="acme/not-a-reference",
        artifact_kind="code", artifact_ref="src/x.py",
    )
    assert registry.register(proposal) is None
    assert registry.resolve_all([proposal]) == ()
    assert not practices.practices_dir(tmp_path, cfg).exists()


def test_registry_next_id_continues_the_real_vault():
    registry = Registry(REPO_ROOT, _cfg())
    assert registry.next_id() > registry.cards[-1].id
    assert registry.cards_for_repos(("microsoft/JARVIS",)) == ("PR-0003",)
    assert "PR-0003" in registry.catalog()


# --- indexing -----------------------------------------------------------------
def test_reindex_writes_the_moc_and_lesson_backlinks(tmp_path):
    cfg = _cfg()
    registry = Registry(tmp_path, cfg)
    card = registry.register(
        PracticeProposal(
            title="Cost tracking", source_repo="assafelovic/gpt-researcher",
            artifact_kind="code", artifact_ref="gpt_researcher/utils/costs.py",
        )
    )
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "2026-08-02-implement-ledger.md").write_text(
        f"# implement: ledger\n\n## Practices cited\n- [[{card.note_name}]]\n"
    )

    written = reindex(tmp_path)

    moc = next(p for p in written if p.name == "Practices MOC.md")
    moc_text = moc.read_text()
    assert card.id in moc_text
    assert f"[[{card.note_name}" in moc_text
    assert "assafelovic/gpt-researcher" in moc_text

    card_text = (practices.practices_dir(tmp_path, cfg) / f"{card.note_name}.md").read_text()
    assert "## Cited by" in card_text
    assert "[[2026-08-02-implement-ledger]]" in card_text

    # re-indexing is idempotent: backlinks are replaced, not appended
    reindex(tmp_path)
    assert card_text == (practices.practices_dir(tmp_path, cfg) / f"{card.note_name}.md").read_text()


# --- the CI gate --------------------------------------------------------------
def _pr_body(evidence: str) -> str:
    return f"""Closes #1

## Model used
- **model**: `sonnet`

## Reference-set evidence
{evidence}

---
_Filed automatically by the `hsai` loop._
"""


def test_evidence_check_passes_a_code_pr_citing_pinned_repos():
    result = evaluate_pr_evidence(
        "implement: feat: evidence registry", _pr_body("`microsoft/JARVIS`"), _cfg()
    )
    assert result.ok
    assert "microsoft/JARVIS" in result.reason


def test_evidence_check_fails_a_code_pr_with_an_empty_evidence_section():
    result = evaluate_pr_evidence(
        "implement: feat: evidence registry", _pr_body("_(none)_"), _cfg()
    )
    assert not result.ok
    assert "names no reference repo" in result.reason


def test_evidence_check_fails_a_code_pr_missing_the_section_entirely():
    result = evaluate_pr_evidence(
        "refactor: split the orchestrator", "Closes #1\n\n## Model used\n- `sonnet`\n", _cfg()
    )
    assert not result.ok
    assert "no '## Reference-set evidence' section" in result.reason


def test_evidence_check_fails_a_code_pr_citing_an_unpinned_repo():
    result = evaluate_pr_evidence(
        "implement: skill: routing", _pr_body("`acme/invented-repo`"), _cfg()
    )
    assert not result.ok
    assert "unpinned repo(s)" in result.reason


def test_evidence_check_exempts_non_code_prs():
    for title in ("heal: ci: main is red - auto-heal", "improve: chore: refresh snapshot"):
        result = evaluate_pr_evidence(title, _pr_body("_(none)_"), _cfg())
        assert result.ok, title
        assert "exempt" in result.reason


def test_resolve_artifact_asks_github_for_the_cited_path():
    calls: list[list[str]] = []

    class _Proc:
        ok = True

    def fake_runner(cmd, **kwargs):
        calls.append(list(cmd))
        return _Proc()

    assert practices.resolve_artifact(_card(), runner=fake_runner)
    assert calls == [["gh", "api", "repos/openai/swarm/contents/swarm/core.py", "--silent"]]
