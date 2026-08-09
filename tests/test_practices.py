from hsai.config import load_config
from hsai.knowledge import KnowledgeBase
from hsai.practices import (
    ADOPTED,
    QUEUED,
    Practice,
    PracticeRef,
    PracticeRegistry,
    extract_practices,
    parse_practices_section,
    practice_id,
    render_practices_section,
    validate_references,
)

RATIONALE = (
    "Combines crewAIInc/crewAI's practice of committing a durable provenance artifact "
    "alongside each change (the recurring `[docs-freeze]` commits), "
    "run-llama/llama_index's machine-readable attribution conventions such as "
    "`fix(anthropic):` scoped titles. FoundationAgents/MetaGPT insists on explicit "
    "named artifacts per role rather than implicit outputs."
)


def _ref(repo="crewAIInc/crewAI", practice="snapshot docs alongside each change"):
    return PracticeRef(repo, practice, artifact=".github/workflows/docs.yml")


# --- declaration: render / parse round trip ---------------------------------

def test_practices_section_round_trips():
    refs = (_ref(), PracticeRef("openai/swarm", "keeps the orchestrator ergonomic"))
    body = f"## Problem\np\n\n{render_practices_section(refs)}\n## Meta\n- size: L\n"
    assert parse_practices_section(body) == refs


def test_parse_ignores_bodies_without_a_practices_section():
    assert parse_practices_section("") == ()
    assert parse_practices_section("## Problem\nlangchain-ai/langchain is great\n") == ()
    # An empty declaration is a declaration of nothing, not of everything.
    assert parse_practices_section(render_practices_section(())) == ()


def test_parse_dedupes_repeated_declarations():
    body = render_practices_section((_ref(), _ref()))
    assert len(parse_practices_section(body)) == 1


# --- validation: only pinned reference projects may be cited -----------------

def test_validate_references_drops_invented_slugs():
    cfg = load_config()
    refs = validate_references(
        cfg, ("crewAIInc/crewAI", "evil-corp/not-a-reference", "camel-ai/camel")
    )
    assert refs == ("crewAIInc/crewAI", "camel-ai/camel")  # watchlist counts, invention does not


def test_validate_references_dedupes_and_keeps_order():
    cfg = load_config()
    assert validate_references(cfg, ("openai/swarm", "openai/swarm")) == ("openai/swarm",)
    assert validate_references(cfg, ()) == ()


# --- mining a synthesis rationale -------------------------------------------

def test_extract_practices_credits_only_pinned_projects():
    cfg = load_config()
    refs = extract_practices(RATIONALE, cfg.reference_repos())
    by_repo = {r.source_repo: r for r in refs}

    assert set(by_repo) == {
        "crewAIInc/crewAI", "run-llama/llama_index", "FoundationAgents/MetaGPT",
    }
    # the practice text is the model's own sentence, and the artifact is its citation
    assert "durable provenance artifact" in by_repo["crewAIInc/crewAI"].practice
    assert by_repo["crewAIInc/crewAI"].artifact == "[docs-freeze]"
    assert by_repo["FoundationAgents/MetaGPT"].artifact == ""


def test_extract_practices_on_a_rationale_naming_nothing():
    cfg = load_config()
    assert extract_practices("combines x + y + z", cfg.reference_repos()) == ()
    assert extract_practices("", cfg.reference_repos()) == ()


# --- the durable registry ----------------------------------------------------

def test_practice_round_trips_to_disk(tmp_path):
    registry = PracticeRegistry(tmp_path)
    practice = Practice.from_ref(_ref(), adopted_by_ticket=42)
    path = registry.write(practice)

    text = path.read_text()
    assert text.startswith("---\n")                       # Obsidian frontmatter
    assert "source_repo: crewAIInc/crewAI" in text
    assert "artifact: .github/workflows/docs.yml" in text
    assert "status: queued" in text
    assert "[[Practices MOC]]" in text                    # up-link
    assert "[[ticket-42]]" in text                        # wikilink to its ticket

    back = registry.read(practice.id)
    assert back == practice


def test_record_queued_is_idempotent_and_never_demotes(tmp_path):
    registry = PracticeRegistry(tmp_path)
    ref = _ref()

    registry.record_queued([ref], ticket=7)
    assert [p.status for p in registry.read_all()] == [QUEUED]

    registry.mark_adopted([ref], pr=13, ticket=7, lesson_note="2026-08-09-do-thing")
    registry.record_queued([ref], ticket=7)               # a later cycle re-proposes it

    stored = registry.read_all()
    assert len(stored) == 1                               # one note per practice, not two
    assert stored[0].status == ADOPTED                    # the verdict survives
    assert stored[0].adopted_by_pr == 13


def test_mark_adopted_stamps_pr_and_lesson_and_creates_missing_notes(tmp_path):
    registry = PracticeRegistry(tmp_path)
    ref = _ref()

    adopted = registry.mark_adopted(
        [ref], pr=13, ticket=7, lesson_note="2026-08-09-do-thing"
    )

    assert [p.id for p in adopted] == [practice_id(ref.source_repo, ref.practice)]
    stored = registry.read(ref.id)
    assert stored.status == ADOPTED
    assert stored.adopted_by_pr == 13 and stored.adopted_by_ticket == 7
    assert stored.lesson_note == "2026-08-09-do-thing"
    assert "[[2026-08-09-do-thing]]" in registry.path_for(ref.id).read_text()
    assert [p.id for p in registry.adopted()] == [stored.id]


def test_coverage_reports_every_reference_project(tmp_path):
    registry = PracticeRegistry(tmp_path)
    registry.record_queued([_ref()], ticket=1)
    registry.mark_adopted(
        [PracticeRef("openai/swarm", "keeps orchestration ergonomic")], pr=2
    )

    coverage = {c.repo: c for c in registry.coverage(load_config().reference_repos())}

    assert coverage["crewAIInc/crewAI"].queued == 1
    assert coverage["openai/swarm"].adopted == 1
    assert coverage["microsoft/JARVIS"].total == 0        # unmined projects stay visible


# --- the MOC ----------------------------------------------------------------

def test_practices_moc_groups_by_source_repo_and_is_idempotent(tmp_path):
    registry = PracticeRegistry(tmp_path)
    registry.record_queued([_ref()], ticket=1)
    registry.mark_adopted(
        [PracticeRef("openai/swarm", "keeps orchestration ergonomic")], pr=2
    )
    kb = KnowledgeBase(tmp_path)

    kb.reindex_mocs()
    moc = kb.mocs_dir / "Practices MOC.md"
    first = moc.read_text()

    assert "### `crewAIInc/crewAI` - 1 practice(s) (queued=1)" in first
    assert "### `openai/swarm` - 1 practice(s) (adopted=1)" in first
    for p in registry.read_all():
        assert f"[[{p.id}]]" in first                     # every note is linked
    assert "[[Practices MOC]]" in (kb.mocs_dir / "Knowledge Base MOC.md").read_text()

    kb.reindex_mocs()
    assert moc.read_text() == first                       # regenerating changes nothing


def test_practices_moc_on_an_empty_registry(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.reindex_mocs()
    assert "_No practices extracted yet._" in (kb.mocs_dir / "Practices MOC.md").read_text()
