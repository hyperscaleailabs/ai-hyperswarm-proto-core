import json

import pytest

from hsai.github import TicketOutcome
from hsai.practices import (
    ADOPTED_HEADING,
    REJECTED_RULE,
    STATUSES,
    DuplicatePracticeError,
    Observation,
    Practice,
    append,
    build_practice,
    core_yaml_hash,
    current_provenance,
    get,
    is_duplicate,
    link_ticket,
    load,
    make_id,
    next_status,
    normalize_id,
    normalize_title,
    parse,
    record_observations,
    render,
    render_adopted_section,
    sync,
    sync_statuses,
)
from hsai.proc import Proc


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


def test_rejected_rule_forbids_resurrection_without_new_evidence():
    assert "rejected" in REJECTED_RULE.lower()
    assert "new evidence" in REJECTED_RULE.lower()


def test_render_adopted_section_names_the_ticket_that_proposed_a_practice():
    practice = build_practice(
        title="pr size gate", source_project="crewAIInc/crewAI",
        source_artifact="ci_cd", evidence=".github/workflows/pr-size.yml",
        status="proposed", ticket=412,
    )
    assert "ticket #412" in render_adopted_section([practice])


# --- the adoption lifecycle ------------------------------------------------

def test_statuses_are_the_documented_lifecycle():
    assert STATUSES == ("observed", "proposed", "adopted", "rejected")


def test_normalize_id_preserves_the_project_title_separator():
    """`_slugify` over a whole id would collapse `--` and rename every note."""
    derived = make_id("run-llama/llama_index", "a hard numeric CI gate")
    assert "--" in derived
    assert normalize_id(derived) == derived
    assert normalize_id("  CrewAI Inc--PR Size Gate ") == "crewai-inc--pr-size-gate"


def test_full_round_trip_carries_the_lifecycle_fields(tmp_path):
    p = build_practice(
        title="issue classifier workflow", source_project="run-llama/llama_index",
        source_artifact="ci_cd", evidence=".github/workflows/issue_classifier.yml",
        status="proposed", ticket=299, lesson_note="2026-08-20-implement-feat-classifier",
        first_seen_cycle=41363, provenance="hsai@abc1234 core.yaml@0123456789ab",
        adopted_date="2026-08-20", notes="mirrors our needs-refinement gate",
        related=("2026-08-20-implement-feat-classifier",),
    )
    path = tmp_path / f"{p.note_name()}.md"
    path.write_text(render(p))

    back = parse(path)
    assert back == p


def test_render_shows_placeholders_for_unset_lifecycle_fields():
    text = render(
        build_practice(
            title="t", source_project="o/r", source_artifact="ci_cd", evidence="e",
        )
    )
    assert "| ticket | _(none)_ |" in text
    assert "| first seen (cycle) | _(none)_ |" in text
    assert "| provenance | `unstamped` |" in text


def test_a_pre_lifecycle_note_still_parses(tmp_path):
    """Notes written before these fields existed must keep loading."""
    path = tmp_path / "openbmb-chatdev--session-durability.md"
    path.write_text(
        "---\ntags:\n- practice\nstatus: adopted\n"
        "practice_id: openbmb-chatdev--session-durability\n"
        "source_project: OpenBMB/ChatDev\nsource_artifact: harness_design\n---\n\n"
        "# session durability\n\n## Evidence\nPR #104\n"
    )
    back = parse(path)
    assert back.status == "adopted"
    assert back.ticket is None
    assert back.first_seen_cycle is None
    assert back.provenance == ""


# --- provenance stamping ---------------------------------------------------

def _core_yaml(root, body: str = "identity:\n  owner: hyperscaleailabs\n"):
    (root / ".ai-swarm").mkdir(parents=True, exist_ok=True)
    (root / ".ai-swarm" / "core.yaml").write_text(body)


def test_core_yaml_hash_tracks_the_config_contents(tmp_path):
    assert core_yaml_hash(tmp_path) == "unknown"
    _core_yaml(tmp_path)
    first = core_yaml_hash(tmp_path)
    assert len(first) == 12 and first != "unknown"
    _core_yaml(tmp_path, "identity:\n  owner: someone-else\n")
    assert core_yaml_hash(tmp_path) != first


def test_current_provenance_pins_both_halves(tmp_path):
    _core_yaml(tmp_path)

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        assert cmd[:2] == ["git", "rev-parse"]
        return Proc(cmd, 0, "abc1234\n", "")

    prov = current_provenance(tmp_path, runner=runner)
    assert prov.hsai_sha == "abc1234"
    assert prov.stamp().startswith("hsai@abc1234 core.yaml@")
    assert "unknown" not in prov.stamp()


def test_current_provenance_degrades_when_git_is_unavailable(tmp_path):
    def broken(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        return Proc(cmd, 127, "", "git: command not found")

    assert current_provenance(tmp_path, runner=broken).stamp() == (
        "hsai@unknown core.yaml@unknown"
    )


# --- observations: the planner's PHASE 1 catalog ---------------------------

def test_record_observations_writes_observed_notes(tmp_path):
    written = record_observations(
        tmp_path,
        [
            Observation(
                title="PR size gate", source_project="crewAIInc/crewAI",
                source_artifact="ci_cd", evidence=".github/workflows/pr-size.yml",
                practice_id="crewaiinc-crewai--pr-size-gate",
            ),
        ],
        cycle_index=41363,
        provenance="hsai@abc1234 core.yaml@0123456789ab",
    )
    assert [p.id for p in written] == ["crewaiinc-crewai--pr-size-gate"]

    on_disk = load(tmp_path)
    assert len(on_disk) == 1
    assert on_disk[0].status == "observed"
    assert on_disk[0].first_seen_cycle == 41363
    assert on_disk[0].provenance == "hsai@abc1234 core.yaml@0123456789ab"
    assert on_disk[0].ticket is None


def test_record_observations_never_overwrites_an_earned_status(tmp_path):
    append(
        tmp_path,
        build_practice(
            title="PR size gate", source_project="crewAIInc/crewAI",
            source_artifact="ci_cd", evidence="PR #12", status="adopted", adopted_pr=12,
        ),
    )
    written = record_observations(
        tmp_path,
        [Observation(title="pr   size GATE", source_project="crewaiinc/crewai")],
    )
    assert written == []
    assert [p.status for p in load(tmp_path)] == ["adopted"]


def test_record_observations_skips_incomplete_entries(tmp_path):
    written = record_observations(
        tmp_path,
        [
            Observation(title="", source_project="crewAIInc/crewAI"),
            Observation(title="a practice", source_project="  "),
        ],
    )
    assert written == []
    assert load(tmp_path) == []


def test_link_ticket_flips_observed_to_proposed(tmp_path):
    record_observations(
        tmp_path,
        [Observation(title="PR size gate", source_project="crewAIInc/crewAI")],
    )
    practice_id = make_id("crewAIInc/crewAI", "PR size gate")

    updated = link_ticket(tmp_path, practice_id, 412)
    assert updated is not None
    assert updated.status == "proposed"
    assert updated.ticket == 412
    assert get(tmp_path, practice_id).ticket == 412


def test_link_ticket_leaves_a_settled_practice_alone(tmp_path):
    practice = build_practice(
        title="cost accounting", source_project="assafelovic/gpt-researcher",
        source_artifact="source_code", evidence="PR #47", status="adopted", adopted_pr=47,
    )
    append(tmp_path, practice)

    assert link_ticket(tmp_path, practice.id, 999) is None
    assert get(tmp_path, practice.id).status == "adopted"
    assert get(tmp_path, practice.id).ticket is None


def test_link_ticket_keeps_the_first_claim_on_an_already_proposed_practice(tmp_path):
    """First ticket wins - the link the audit trail hangs from must not move."""
    record_observations(
        tmp_path,
        [Observation(title="PR size gate", source_project="crewAIInc/crewAI")],
    )
    practice_id = make_id("crewAIInc/crewAI", "PR size gate")
    link_ticket(tmp_path, practice_id, 412)

    assert link_ticket(tmp_path, practice_id, 500) is None
    assert get(tmp_path, practice_id).ticket == 412


def test_link_ticket_ignores_an_unknown_id(tmp_path):
    assert link_ticket(tmp_path, "nobody-ever--saw-this", 412) is None


# --- status sync from real ticket state ------------------------------------

TICKET_STATES = {
    10: {
        "number": 10, "state": "CLOSED", "labels": [],
        "closedByPullRequestsReferences": [{"number": 99, "state": "MERGED"}],
    },
    11: {
        "number": 11, "state": "CLOSED", "labels": [],
        "closedByPullRequestsReferences": [{"number": 98, "state": "CLOSED"}],
    },
    12: {
        "number": 12, "state": "OPEN", "labels": [{"name": "blocked"}],
        "closedByPullRequestsReferences": [],
    },
    13: {
        "number": 13, "state": "OPEN", "labels": [{"name": "hsai"}],
        "closedByPullRequestsReferences": [],
    },
}


def _ticket_runner(states=None):
    """A fake `gh issue view` answering with each ticket's real state."""
    states = TICKET_STATES if states is None else states
    calls: list[list[str]] = []

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "issue", "view"]:
            return Proc(cmd, 0, json.dumps(states.get(int(cmd[3]), {})), "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _proposed(practice_id: str, ticket: int) -> Practice:
    return build_practice(
        title=practice_id, source_project="o/r", source_artifact="ci_cd",
        evidence="e", status="proposed", ticket=ticket, practice_id=practice_id,
    )


def test_next_status_is_pure_and_explains_itself():
    practice = _proposed("p", 10)
    status, reason = next_status(practice, TicketOutcome(10, "CLOSED", (), 99))
    assert status == "adopted"
    assert "#99" in reason

    assert next_status(practice, None)[0] == "proposed"  # unknown state changes nothing
    assert next_status(build_practice(
        title="t", source_project="o/r", source_artifact="ci_cd", evidence="e",
    ), None) == ("adopted", "no ticket linked")


def test_sync_statuses_derives_every_transition_from_ticket_state():
    records = [
        _proposed("merged", 10),
        _proposed("closed-unmerged", 11),
        _proposed("blocked", 12),
        _proposed("still-open", 13),
    ]
    updated, changes = sync_statuses(records, repo="o/r", runner=_ticket_runner())

    by_id = {p.id: p for p in updated}
    assert by_id["merged"].status == "adopted"
    assert by_id["merged"].adopted_pr == 99
    assert by_id["closed-unmerged"].status == "rejected"
    assert by_id["blocked"].status == "rejected"
    assert by_id["still-open"].status == "proposed"   # unchanged

    moved = {c.practice_id: c for c in changes}
    assert set(moved) == {"merged", "closed-unmerged", "blocked"}
    assert "merged in PR #99" in moved["merged"].reason
    assert "blocked" in moved["blocked"].reason
    assert "closed without a merged PR" in moved["closed-unmerged"].reason
    assert moved["merged"].render().startswith("merged: proposed -> adopted")


def test_sync_statuses_never_queries_an_unlinked_practice():
    records = [build_practice(
        title="hand-recorded", source_project="o/r", source_artifact="ci_cd", evidence="e",
    )]
    runner = _ticket_runner()
    updated, changes = sync_statuses(records, repo="o/r", runner=runner)
    assert updated == records
    assert changes == []
    assert runner.calls == []


def test_sync_statuses_holds_position_when_github_is_unreadable():
    """An unparseable `gh` reply must not silently reject a live practice."""

    def broken(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        return Proc(cmd, 127, "", "gh: command not found")

    records = [_proposed("merged", 10)]
    updated, changes = sync_statuses(records, repo="o/r", runner=broken)
    assert updated[0].status == "proposed"
    assert changes == []


def test_sync_persists_only_the_notes_that_moved(tmp_path):
    for practice in (_proposed("merged", 10), _proposed("still-open", 13)):
        append(tmp_path, practice)

    changes = sync(tmp_path, repo="o/r", runner=_ticket_runner())

    assert [c.practice_id for c in changes] == ["merged"]
    on_disk = {p.id: p for p in load(tmp_path)}
    assert on_disk["merged"].status == "adopted"
    assert on_disk["merged"].adopted_pr == 99
    assert on_disk["still-open"].status == "proposed"


def test_sync_dry_run_reports_without_writing(tmp_path):
    append(tmp_path, _proposed("merged", 10))

    changes = sync(tmp_path, repo="o/r", runner=_ticket_runner(), dry_run=True)

    assert [c.after for c in changes] == ["adopted"]
    assert load(tmp_path)[0].status == "proposed"  # nothing written
