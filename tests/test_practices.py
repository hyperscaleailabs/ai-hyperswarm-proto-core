import pytest

from hsai.config import load_config
from hsai.practices import (
    ADOPTED_HEADING,
    TARGETING_HEADING,
    DuplicatePracticeError,
    PracticeRegistry,
    append,
    build_practice,
    citation_for,
    coverage_map,
    infer_dimension,
    is_duplicate,
    learn_from,
    least_covered,
    load,
    make_id,
    normalize_title,
    parse,
    render,
    render_adopted_section,
    render_coverage,
    render_targeting_section,
    repos_named_in,
)


def _cfg():
    return load_config()


def test_make_id_is_stable_and_deterministic():
    a = make_id("langchain-ai/langchain", "refresh model profiles")
    b = make_id("langchain-ai/langchain", "refresh model profiles")
    assert a == b
    assert a == "langchain-ai-langchain--refresh-model-profiles"


def test_normalize_title_collapses_whitespace_and_case():
    assert normalize_title("  Refresh   Model  Profiles ") == "refresh model profiles"
    assert normalize_title("refresh model profiles") == normalize_title(
        "  Refresh   Model  Profiles "
    )


def test_build_practice_defaults_date_and_derives_id():
    p = build_practice(
        title="strict source citation",
        source_repo="assafelovic/gpt-researcher",
        dimension="source_code",
        evidence="PR #47",
    )
    assert p.id == make_id(p.source_repo, p.title)
    assert p.adopted_date  # defaulted, never blank
    assert p.status == "adopted"
    assert p.ticket is None and p.lesson_note == ""


def test_render_and_parse_round_trip(tmp_path):
    p = build_practice(
        title="session durability", source_repo="OpenBMB/ChatDev",
        dimension="harness_design", evidence="PR #104", adopted_pr=104,
        adopted_date="2026-08-05", status="adopted", notes="landed cleanly",
        related=("2026-08-05-implement-feat-durable-cycle-journal",),
        ticket=99, lesson_note="2026-08-05-implement-feat-durable-cycle-journal",
    )
    path = tmp_path / f"{p.note_name()}.md"
    path.write_text(render(p))

    assert parse(path) == p


def test_registry_read_round_trips_a_written_note(tmp_path):
    """The acceptance-criteria round trip, through the registry rather than the file."""
    registry = PracticeRegistry(tmp_path)
    p = build_practice(
        title="explicit phase artifacts", source_repo="FoundationAgents/MetaGPT",
        dimension="harness_design", evidence="the SOP that each phase emits a document",
        adopted_date="2026-08-25", ticket=357, adopted_pr=358,
        lesson_note="2026-08-25-implement-feat-practices",
    )
    registry.write(p)

    assert registry.read() == [p]
    assert registry.get(p.id) == p
    assert registry.get("no-such-practice") is None


def test_note_frontmatter_carries_the_join_keys_and_links_its_moc(tmp_path):
    p = build_practice(
        title="telemetry with a stable id", source_repo="crewAIInc/crewAI",
        dimension="commit_history", evidence="feat(events): report project creation",
        ticket=41, adopted_pr=42, lesson_note="2026-08-25-lesson",
    )
    text = render(p)
    assert "source_repo: crewAIInc/crewAI" in text
    assert "dimension: commit_history" in text
    assert "status: adopted" in text
    assert "evidence: " in text
    assert "ticket: 41" in text
    assert "pr: 42" in text
    assert "[[Practices MOC]]" in text
    assert "[[2026-08-25-lesson]]" in text


def test_parse_still_reads_legacy_frontmatter_keys(tmp_path):
    """Notes written before the registry adopted core.yaml's vocabulary keep loading."""
    path = tmp_path / "legacy.md"
    path.write_text(
        "---\n"
        "practice_id: legacy\n"
        "source_project: OpenBMB/ChatDev\n"
        "source_artifact: harness_design\n"
        "status: adopted\n"
        "adopted_pr: 104\n"
        "adopted_date: '2026-08-05'\n"
        "---\n\n"
        "# session durability\n\n"
        "## Evidence\nPR #104\n\n"
        "## Notes\n_(none)_\n\n"
        "## Related\n- _(none linked yet)_\n"
    )
    back = parse(path)
    assert back.id == "legacy"
    assert back.source_repo == "OpenBMB/ChatDev"
    assert back.dimension == "harness_design"
    assert back.evidence == "PR #104"
    assert back.adopted_pr == 104


def test_render_shows_none_placeholders_for_unset_pr():
    p = build_practice(
        title="reconciliation discipline", source_repo="assafelovic/gpt-researcher",
        dimension="harness_design", evidence="PR #104",
    )
    assert p.adopted_pr is None
    text = render(p)
    assert "| adopted PR | _(none)_ |" in text


# --- duplicate check -----------------------------------------------------

def test_is_duplicate_matches_normalized_title_and_repo():
    existing = [
        build_practice(
            title="Cost Accounting", source_repo="assafelovic/gpt-researcher",
            dimension="source_code", evidence="PR #47",
        )
    ]
    dup = is_duplicate(existing, "assafelovic/gpt-researcher", "  cost   accounting ")
    assert dup is not None
    assert dup.title == "Cost Accounting"


def test_is_duplicate_distinct_repo_is_not_a_duplicate():
    existing = [
        build_practice(
            title="cost accounting", source_repo="assafelovic/gpt-researcher",
            dimension="source_code", evidence="PR #47",
        )
    ]
    assert is_duplicate(existing, "OpenBMB/ChatDev", "cost accounting") is None


def test_append_refuses_a_duplicate_source_repo_and_title(tmp_path):
    practice = build_practice(
        title="hard numeric CI gate", source_repo="run-llama/llama_index",
        dimension="ci_cd", evidence="PR #47",
    )
    append(tmp_path, practice)
    with pytest.raises(DuplicatePracticeError):
        append(tmp_path, practice)

    # only one note was ever written
    assert len(load(tmp_path)) == 1


def test_registry_write_overwrites_where_append_refuses(tmp_path):
    """Promotion is a rewrite, so `write` must not enforce the duplicate rule."""
    registry = PracticeRegistry(tmp_path)
    p = build_practice(
        title="hard numeric CI gate", source_repo="run-llama/llama_index",
        dimension="ci_cd", evidence="PR #47", status="observed", ticket=5,
    )
    registry.write(p)
    registry.write(p)
    assert len(registry.read()) == 1


def test_append_writes_a_loadable_note(tmp_path):
    practice = build_practice(
        title="observability at one choke point", source_repo="langchain-ai/langchain",
        dimension="source_code", evidence="PR #94",
    )
    path = append(tmp_path, practice)
    assert path.exists()
    loaded = load(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].title == "observability at one choke point"


# --- observed -> adopted, the provenance join ------------------------------

def test_adopt_flips_only_this_tickets_observed_practices(tmp_path):
    registry = PracticeRegistry(tmp_path)
    mine = build_practice(
        title="explicit phase artifacts", source_repo="FoundationAgents/MetaGPT",
        dimension="harness_design", evidence="SOP artifacts", status="observed", ticket=7,
    )
    other_ticket = build_practice(
        title="docs as a maintained artifact", source_repo="run-llama/llama_index",
        dimension="ci_cd", evidence="sync-docs.yml", status="observed", ticket=8,
    )
    already = build_practice(
        title="cost accounting", source_repo="assafelovic/gpt-researcher",
        dimension="source_code", evidence="PR #47", status="adopted",
        ticket=7, adopted_pr=47,
    )
    for p in (mine, other_ticket, already):
        registry.write(p)

    adopted = registry.adopt(7, pr=123, lesson_note="2026-08-25-lesson")

    assert [p.id for p in adopted] == [mine.id]
    back = registry.get(mine.id)
    assert back.status == "adopted"
    assert back.adopted_pr == 123
    assert back.lesson_note == "2026-08-25-lesson"
    assert "2026-08-25-lesson" in back.related

    # A different ticket's practice is untouched...
    assert registry.get(other_ticket.id).status == "observed"
    # ...and a settled one keeps the PR that first proved it.
    assert registry.get(already.id).adopted_pr == 47


def test_for_ticket_joins_practices_back_to_their_ticket(tmp_path):
    registry = PracticeRegistry(tmp_path)
    registry.write(build_practice(
        title="a", source_repo="openai/swarm", dimension="source_code",
        evidence="e", status="observed", ticket=7,
    ))
    registry.write(build_practice(
        title="b", source_repo="microsoft/JARVIS", dimension="source_code",
        evidence="e", status="observed", ticket=9,
    ))
    assert [p.source_repo for p in registry.for_ticket(7)] == ["openai/swarm"]
    assert registry.for_ticket(404) == []


# --- citing the pinned set -------------------------------------------------

def test_repos_named_in_finds_full_slugs_and_bare_names():
    cfg = _cfg()
    text = (
        "Combines FoundationAgents/MetaGPT's SOP discipline with the sync-docs.yml "
        "workflow from llama_index and the telemetry events in crewAI."
    )
    named = repos_named_in(text, cfg)
    assert set(named) == {
        "FoundationAgents/MetaGPT", "run-llama/llama_index", "crewAIInc/crewAI"
    }
    # Rank order, not mention order - deterministic regardless of the prose.
    assert list(named) == [
        "FoundationAgents/MetaGPT", "crewAIInc/crewAI", "run-llama/llama_index"
    ]


def test_repos_named_in_ignores_unpinned_projects():
    cfg = _cfg()
    assert repos_named_in("Borrows from AutoGen, BabyAGI and Camel.", cfg) == ()


def test_generic_bare_name_is_not_a_citation():
    """This repo calls itself an AI-swarm harness; 'swarm' alone proves nothing."""
    cfg = _cfg()
    assert repos_named_in("A swarm of agents cooperating.", cfg) == ()
    assert repos_named_in("Adopted from openai/swarm.", cfg) == ("openai/swarm",)


def test_citation_for_returns_the_sentence_naming_the_repo():
    text = (
        "MetaGPT contributes explicit phase artifacts. "
        "run-llama/llama_index contributes its sync-docs.yml discipline."
    )
    assert citation_for(text, "run-llama/llama_index").startswith("run-llama/llama_index")
    assert "sync-docs.yml" in citation_for(text, "run-llama/llama_index")
    assert citation_for(text, "openai/swarm") == ""


@pytest.mark.parametrize(
    "text, expected",
    [
        ("its sync-docs.yml workflow keeps docs in step", "ci_cd"),
        ("the commit subject feat(events): report project creation", "commit_history"),
        ("a long issue thread about retries", "issue_history"),
        ("the README quickstart section", "readme"),
        ("its orchestration harness design", "harness_design"),
        ("the way the executor class is written", "source_code"),
        ("", "source_code"),
    ],
)
def test_infer_dimension_is_deterministic_and_cue_based(text, expected):
    assert infer_dimension(text) == expected


# --- the coverage map ------------------------------------------------------

def test_coverage_map_spans_every_pinned_repo_and_dimension():
    cfg = _cfg()
    practices = [
        build_practice(
            title="a hard numeric CI gate", source_repo="run-llama/llama_index",
            dimension="ci_cd", evidence="e",
        ),
        build_practice(
            title="docs as a maintained artifact", source_repo="run-llama/llama_index",
            dimension="ci_cd", evidence="e",
        ),
        build_practice(
            title="explicit phase artifacts", source_repo="FoundationAgents/MetaGPT",
            dimension="harness_design", evidence="e",
        ),
    ]
    matrix = coverage_map(cfg, practices)

    assert set(matrix) == {r.repo for r in cfg.reference_top10}
    assert len(matrix) == 10
    for row in matrix.values():
        assert set(row) == set(learn_from(cfg))
        assert len(row) == 6

    assert matrix["run-llama/llama_index"]["ci_cd"] == 2
    assert matrix["run-llama/llama_index"]["readme"] == 0
    assert matrix["FoundationAgents/MetaGPT"]["harness_design"] == 1
    assert sum(matrix["microsoft/JARVIS"].values()) == 0


def test_coverage_map_ignores_practices_from_unpinned_repos():
    cfg = _cfg()
    practices = [build_practice(
        title="x", source_repo="some/other-repo", dimension="ci_cd", evidence="e",
    )]
    matrix = coverage_map(cfg, practices)
    assert "some/other-repo" not in matrix
    assert sum(sum(row.values()) for row in matrix.values()) == 0


def test_least_covered_is_deterministic_for_a_fixed_registry():
    cfg = _cfg()
    practices = [
        build_practice(
            title=f"p{i}", source_repo="langchain-ai/langchain",
            dimension="source_code", evidence="e",
        )
        for i in range(3)
    ] + [
        build_practice(
            title="q", source_repo="FoundationAgents/MetaGPT",
            dimension="harness_design", evidence="e",
        )
    ]

    first = least_covered(cfg, practices, 4)
    assert first == least_covered(cfg, practices, 4)
    # Reordering the registry cannot reorder the answer: ties break on rank.
    assert first == least_covered(cfg, list(reversed(practices)), 4)

    # The two mined repos are the LAST the loop should study next.
    assert "langchain-ai/langchain" not in first
    assert "FoundationAgents/MetaGPT" not in first
    # Rank order among the eight untouched repos: crewAI (3) before llama_index (4).
    assert first[:2] == ["crewAIInc/crewAI", "run-llama/llama_index"]


def test_least_covered_on_an_empty_registry_is_plain_rank_order():
    cfg = _cfg()
    assert least_covered(cfg, [], 3) == [r.repo for r in cfg.reference_top10[:3]]


def test_least_covered_accepts_a_registry_object(tmp_path):
    cfg = _cfg()
    registry = PracticeRegistry(tmp_path)
    registry.write(build_practice(
        title="x", source_repo="langchain-ai/langchain", dimension="source_code",
        evidence="e",
    ))
    assert "langchain-ai/langchain" not in least_covered(cfg, registry, 5)


def test_render_coverage_prints_a_row_per_repo_and_column_per_dimension():
    cfg = _cfg()
    practices = [build_practice(
        title="x", source_repo="OpenBMB/ChatDev", dimension="ci_cd", evidence="e",
    )]
    text = render_coverage(cfg, practices)
    for ref in cfg.reference_top10:
        assert ref.repo in text
    for dim in learn_from(cfg):
        assert dim in text
    assert "10 pinned repo(s) x 6 dimension(s); 1 practice(s)" in text


# --- prompt rendering ------------------------------------------------------

def test_render_adopted_section_lists_every_practice_with_status():
    practices = [
        build_practice(
            title="session durability", source_repo="OpenBMB/ChatDev",
            dimension="harness_design", evidence="PR #104",
        ),
        build_practice(
            title="a rejected idea", source_repo="microsoft/JARVIS",
            dimension="harness_design", evidence="considered, not adopted",
            status="rejected",
        ),
    ]
    text = render_adopted_section(practices)
    assert "session durability" in text and "OpenBMB/ChatDev" in text
    assert "status: adopted" in text
    assert "status: rejected" in text
    assert "a rejected idea" in text


def test_render_adopted_section_degrades_when_empty():
    text = render_adopted_section([])
    assert "no practices recorded" in text.lower()


def test_render_targeting_section_names_the_unexplored_dimensions():
    cfg = _cfg()
    practices = [build_practice(
        title="x", source_repo="langchain-ai/langchain", dimension="ci_cd", evidence="e",
    )]
    text = render_targeting_section(cfg, practices, k=3)
    assert "langchain-ai/langchain" not in text
    assert "issue_history" in text  # never studied anywhere
    assert text.count("\n") == 2  # exactly k lines


def test_adopted_heading_is_an_explicit_do_not_reproduce_instruction():
    assert "do not" in ADOPTED_HEADING.lower() or "not re-propose" in ADOPTED_HEADING.lower()


def test_targeting_heading_points_at_the_thin_cells():
    assert "least-covered" in TARGETING_HEADING.lower()
