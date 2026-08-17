import pytest

from hsai.practices import (
    ADOPTED_HEADING,
    DuplicatePracticeError,
    append,
    build_practice,
    is_duplicate,
    load,
    make_id,
    normalize_title,
    parse,
    render,
    render_adopted_section,
)


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
        source_project="assafelovic/gpt-researcher",
        source_artifact="source_code",
        evidence="PR #47",
    )
    assert p.id == make_id(p.source_project, p.title)
    assert p.adopted_date  # defaulted, never blank
    assert p.status == "adopted"


def test_render_and_parse_round_trip(tmp_path):
    p = build_practice(
        title="session durability", source_project="OpenBMB/ChatDev",
        source_artifact="harness_design", evidence="PR #104", adopted_pr=104,
        adopted_date="2026-08-05", status="adopted", notes="landed cleanly",
        related=("2026-08-05-implement-feat-durable-cycle-journal",),
    )
    path = tmp_path / f"{p.note_name()}.md"
    path.write_text(render(p))

    back = parse(path)
    assert back.id == p.id
    assert back.title == p.title
    assert back.source_project == p.source_project
    assert back.source_artifact == p.source_artifact
    assert back.evidence == p.evidence
    assert back.adopted_pr == p.adopted_pr
    assert back.adopted_date == p.adopted_date
    assert back.status == p.status
    assert back.notes == p.notes
    assert back.related == p.related


def test_render_shows_none_placeholders_for_unset_pr():
    p = build_practice(
        title="reconciliation discipline", source_project="assafelovic/gpt-researcher",
        source_artifact="harness_design", evidence="PR #104",
    )
    assert p.adopted_pr is None
    text = render(p)
    assert "| adopted PR | _(none)_ |" in text


# --- duplicate check -----------------------------------------------------

def test_is_duplicate_matches_normalized_title_and_project():
    existing = [
        build_practice(
            title="Cost Accounting", source_project="assafelovic/gpt-researcher",
            source_artifact="source_code", evidence="PR #47",
        )
    ]
    dup = is_duplicate(existing, "assafelovic/gpt-researcher", "  cost   accounting ")
    assert dup is not None
    assert dup.title == "Cost Accounting"


def test_is_duplicate_distinct_project_is_not_a_duplicate():
    existing = [
        build_practice(
            title="cost accounting", source_project="assafelovic/gpt-researcher",
            source_artifact="source_code", evidence="PR #47",
        )
    ]
    assert is_duplicate(existing, "OpenBMB/ChatDev", "cost accounting") is None


def test_append_refuses_a_duplicate_source_project_and_title(tmp_path):
    practice = build_practice(
        title="hard numeric CI gate", source_project="run-llama/llama_index",
        source_artifact="ci_cd", evidence="PR #47",
    )
    append(tmp_path, practice)
    with pytest.raises(DuplicatePracticeError):
        append(tmp_path, practice)

    # only one note was ever written
    assert len(load(tmp_path)) == 1


def test_append_writes_a_loadable_note(tmp_path):
    practice = build_practice(
        title="observability at one choke point", source_project="langchain-ai/langchain",
        source_artifact="source_code", evidence="PR #94",
    )
    path = append(tmp_path, practice)
    assert path.exists()
    loaded = load(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].title == "observability at one choke point"


# --- prompt rendering ------------------------------------------------------

def test_render_adopted_section_lists_every_practice_with_status():
    practices = [
        build_practice(
            title="session durability", source_project="OpenBMB/ChatDev",
            source_artifact="harness_design", evidence="PR #104",
        ),
        build_practice(
            title="a rejected idea", source_project="microsoft/JARVIS",
            source_artifact="harness_design", evidence="considered, not adopted",
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


def test_adopted_heading_is_an_explicit_do_not_reproduce_instruction():
    assert "do not" in ADOPTED_HEADING.lower() or "not re-propose" in ADOPTED_HEADING.lower()
