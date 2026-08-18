"""Retrieval-grounded planning over the repo's own knowledge base.

Every ranking here is asserted exactly: scoring is pure, deterministic, and
wall-clock independent by design, so a fuzzy assertion would hide the very
regressions this module can suffer. Two of the tests run against the REAL
committed vault - the index is only worth anything if it works on what is
actually on disk.
"""
import json
from dataclasses import replace
from pathlib import Path

from hsai.config import load_config
from hsai.retrieval import (
    INDEX_FILENAME,
    PRIOR_ART_HEADING,
    DuplicateRisk,
    Note,
    NoteIndex,
    RetrievalConfig,
    duplicate_risk,
    for_queries,
    index_path,
    load_index,
    note_paths,
    read_notes,
    render_prior_art,
    resolve_citations,
    serialize,
    write_index,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cfg(**index_overrides):
    """The real core.yaml with the `knowledge.index` block overridden."""
    base = load_config()
    knowledge = dict(base.knowledge)
    knowledge["index"] = {**(knowledge.get("index") or {}), **index_overrides}
    return replace(base, knowledge=knowledge)


def _write_note(
    root: Path,
    rel_dir: str,
    name: str,
    *,
    title: str,
    body: str,
    outcome: str = "pass",
    kind: str = "implement",
    created: str = "2026-01-01",
    section: str = "Lesson learned",
) -> Path:
    directory = root / rel_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        "---\n"
        "tags:\n"
        "  - lesson\n"
        f"  - outcome/{outcome}\n"
        f"  - kind/{kind}\n"
        f"created: {created}\n"
        "---\n"
        f"\n# {title}\n\n## {section}\n{body}\n"
    )
    return path


def _lesson(root: Path, name: str, **kwargs) -> Path:
    return _write_note(root, "knowledge/lessons", name, **kwargs)


# --- index building -----------------------------------------------------------

def test_index_covers_every_lesson_whitepaper_and_adr_on_disk():
    """The real vault, not a fixture: nothing committed may be invisible."""
    cfg = load_config()
    indexed = {n.note_name for n in read_notes(REPO_ROOT, cfg)}

    expected = set()
    for rel in ("knowledge/lessons", "knowledge/whitepapers", "docs/adr"):
        for path in (REPO_ROOT / rel).glob("*.md"):
            if path.stem != "TEMPLATE" and not path.stem.endswith(" MOC"):
                expected.add(path.stem)

    assert expected, "the committed vault must not be empty"
    assert indexed == expected


def test_notes_carry_the_metadata_the_planner_reasons_about(tmp_path):
    _lesson(
        tmp_path, "2026-02-03-a-failed-idea",
        title="feat: adaptive budget throttling", body="It never converged.",
        outcome="fail", created="2026-02-03",
    )
    notes = read_notes(tmp_path, _cfg())

    assert len(notes) == 1
    note = notes[0]
    assert note.note_name == "2026-02-03-a-failed-idea"
    assert note.title == "feat: adaptive budget throttling"
    assert note.outcome == "fail"
    assert note.kind == "implement"
    assert note.source == "lesson"
    assert note.created == "2026-02-03"
    assert "outcome/fail" in note.tags
    assert "never converged" in note.body


def test_adr_dates_come_from_the_body_when_there_is_no_frontmatter(tmp_path):
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0007-a-decision.md").write_text(
        "# ADR-0007: Something\n\n- **Status**: accepted\n- **Date**: 2026-03-04\n\n"
        "## Decision\nWe decided.\n"
    )
    (adr / "TEMPLATE.md").write_text("# ADR-NNNN: Title\n")

    notes = read_notes(tmp_path, _cfg())
    assert [n.note_name for n in notes] == ["0007-a-decision"]  # TEMPLATE is skipped
    assert notes[0].created == "2026-03-04"
    assert notes[0].source == "adr"
    assert notes[0].outcome == "unknown"


def test_write_index_is_idempotent_byte_for_byte(tmp_path):
    _lesson(tmp_path, "2026-01-01-one", title="feat: one", body="First.")
    _lesson(tmp_path, "2026-01-02-two", title="feat: two", body="Second.", outcome="fail")
    cfg = _cfg()

    first = write_index(tmp_path, cfg)
    assert first == index_path(tmp_path, cfg)
    assert first.name == INDEX_FILENAME
    before = first.read_bytes()

    after = write_index(tmp_path, cfg).read_bytes()
    assert after == before


def test_index_file_uses_the_node_shape_and_round_trips(tmp_path):
    _lesson(
        tmp_path, "2026-01-02-two", title="feat: two", body="Second.",
        outcome="fail", created="2026-01-02",
    )
    path = write_index(tmp_path, _cfg())
    payload = json.loads(path.read_text())

    assert payload["schema_version"] == 1
    node = payload["nodes"][0]
    assert node["id_"] == "2026-01-02-two"
    assert "Second." in node["text"]
    assert node["metadata"]["outcome"] == "fail"
    assert node["metadata"]["created"] == "2026-01-02"

    restored = Note.from_node(node)
    assert restored == read_notes(tmp_path, _cfg())[0]


def test_serializing_the_real_vault_twice_is_identical():
    """The committed index must never produce diff noise on an unchanged vault."""
    cfg = load_config()
    assert serialize(read_notes(REPO_ROOT, cfg)) == serialize(read_notes(REPO_ROOT, cfg))


def test_load_index_prefers_the_file_but_rebuilds_when_it_is_stale(tmp_path):
    cfg = _cfg()
    _lesson(tmp_path, "2026-01-01-one", title="feat: one", body="First.")
    write_index(tmp_path, cfg)
    assert [n.note_name for n in load_index(tmp_path, cfg).notes] == ["2026-01-01-one"]

    # A lesson written after the last reindex must not be invisible to the planner.
    _lesson(tmp_path, "2026-01-05-two", title="feat: two", body="Second.")
    assert [n.note_name for n in load_index(tmp_path, cfg).notes] == [
        "2026-01-01-one", "2026-01-05-two",
    ]


def test_load_index_survives_a_corrupt_index_file(tmp_path):
    cfg = _cfg()
    _lesson(tmp_path, "2026-01-01-one", title="feat: one", body="First.")
    path = index_path(tmp_path, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all")

    assert [n.note_name for n in load_index(tmp_path, cfg).notes] == ["2026-01-01-one"]


def test_an_empty_vault_yields_an_empty_index(tmp_path):
    index = load_index(tmp_path, _cfg())
    assert len(index) == 0
    assert index.search("anything") == []
    assert note_paths(tmp_path, _cfg()) == []


# --- scoring ------------------------------------------------------------------

def test_query_naming_a_subject_returns_that_note_first(tmp_path):
    _lesson(tmp_path, "2026-01-01-budget", title="feat: adaptive budget throttling",
            body="Quota ceilings per block.")
    _lesson(tmp_path, "2026-01-02-review", title="feat: adversarial review gate",
            body="A second model grades the diff.")
    _lesson(tmp_path, "2026-01-03-traj", title="feat: trajectory store",
            body="Agent runs are persisted as json.")

    hits = load_index(tmp_path, _cfg()).search("adaptive budget throttling per block")
    assert hits[0].note_name == "2026-01-01-budget"
    assert hits[0].outcome == "pass"


def test_real_lessons_rank_in_the_top_three_for_their_own_subject():
    """Top-k relevance on the committed lessons, not a synthetic corpus."""
    index = load_index(REPO_ROOT, load_config())
    assert len(index) > 10

    cases = {
        "worker trajectory capture and token cost telemetry": "worker-trajectory",
        "failure taxonomy postmortem pareto backlog trigger": "failure-taxonomy",
        "adopted practice registry with provenance": "adopted-practice-registry",
    }
    for query, fragment in cases.items():
        top3 = [h.note_name for h in index.search(query, 3)]
        assert any(fragment in name for name in top3), (query, top3)


def test_ranking_is_deterministic_across_index_rebuilds(tmp_path):
    # Identical content and date on purpose: only the tie-break can order these.
    for i in range(5):
        _lesson(tmp_path, f"2026-01-01-note-{i}", title="feat: retry policy",
                body="Retries and backoff for flaky CI checks.")
    cfg = _cfg()
    query = "retry policy backoff for flaky checks"

    first = load_index(tmp_path, cfg).search(query, 5)
    second = load_index(tmp_path, cfg).search(query, 5)
    assert [(h.note_name, h.score, h.coverage) for h in first] == [
        (h.note_name, h.score, h.coverage) for h in second
    ]
    # Equal scores fall back to the note name, so the order is total.
    assert [h.note_name for h in first] == sorted(h.note_name for h in first)


def test_title_and_tag_hits_outweigh_body_hits(tmp_path):
    _lesson(tmp_path, "2026-01-01-titled", title="feat: quota ledger",
            body="Unrelated prose about worktrees.")
    _lesson(tmp_path, "2026-01-01-buried", title="feat: something else",
            body="A quota ledger is mentioned once, deep in the body.")

    hits = load_index(tmp_path, _cfg()).search("quota ledger")
    assert hits[0].note_name == "2026-01-01-titled"


def test_recency_decay_is_measured_against_the_newest_note_not_the_clock(tmp_path):
    _lesson(tmp_path, "2026-01-01-old", title="feat: retry policy",
            body="Same words in both notes.", created="2025-01-01")
    _lesson(tmp_path, "2026-01-01-new", title="feat: retry policy",
            body="Same words in both notes.", created="2026-01-01")

    hits = load_index(tmp_path, _cfg(half_life_days=180)).search("retry policy", 2)
    assert [h.note_name for h in hits] == ["2026-01-01-new", "2026-01-01-old"]
    assert hits[0].score > hits[1].score

    # Decay off => the two notes score identically, so the tie-break decides.
    even = load_index(tmp_path, _cfg(half_life_days=0)).search("retry policy", 2)
    assert even[0].score == even[1].score


def test_search_respects_k_and_a_non_positive_k(tmp_path):
    for i in range(4):
        _lesson(tmp_path, f"2026-01-0{i + 1}-n{i}", title="feat: retry policy", body="Retries.")
    index = load_index(tmp_path, _cfg())
    assert len(index.search("retry policy", 2)) == 2
    assert index.search("retry policy", 0) == []
    assert index.search("   ") == []


# --- prior art rendering ------------------------------------------------------

def test_citation_is_an_obsidian_wikilink_carrying_the_outcome(tmp_path):
    _lesson(tmp_path, "2026-01-02-failed", title="feat: adaptive budget",
            body="Did not converge.", outcome="fail")
    hit = load_index(tmp_path, _cfg()).search("adaptive budget")[0]

    assert hit.citation() == "[[2026-01-02-failed]] (fail) - feat: adaptive budget"
    assert render_prior_art([hit]) == f"- {hit.citation()}"
    assert "no prior art found" in render_prior_art([]).lower()


def test_for_queries_merges_and_dedupes_across_goals(tmp_path):
    _lesson(tmp_path, "2026-01-01-a", title="feat: quota ledger", body="Cost telemetry.")
    _lesson(tmp_path, "2026-01-02-b", title="feat: knowledge base", body="Lessons and MOCs.")
    index = load_index(tmp_path, _cfg())

    merged = for_queries(index, ["quota ledger cost", "quota ledger telemetry", "knowledge base"])
    assert [p.note_name for p in merged] == ["2026-01-01-a", "2026-01-02-b"]
    assert merged[0].score >= merged[1].score


def test_resolve_citations_keeps_real_notes_and_drops_invented_ones(tmp_path):
    _lesson(tmp_path, "2026-01-01-real", title="feat: quota ledger", body="Cost telemetry.")
    index = load_index(tmp_path, _cfg())

    cites = resolve_citations(
        index, ["[[2026-01-01-real]]", "2099-12-31-invented"], "quota ledger cost"
    )
    assert cites == ("[[2026-01-01-real]] (pass) - feat: quota ledger",)


def test_resolve_citations_falls_back_to_retrieval_when_the_model_cited_nothing(tmp_path):
    _lesson(tmp_path, "2026-01-01-real", title="feat: quota ledger", body="Cost telemetry.")
    index = load_index(tmp_path, _cfg())

    assert resolve_citations(index, [], "quota ledger cost telemetry") == (
        "[[2026-01-01-real]] (pass) - feat: quota ledger",
    )
    # Nothing on the subject at all: the caller renders the explicit statement.
    assert resolve_citations(index, [], "kubernetes sidecar autoscaling") == ()


def test_prior_art_heading_is_a_stable_contract():
    assert PRIOR_ART_HEADING == "Prior art from this repo"


# --- duplicate risk -----------------------------------------------------------

FAILED_IDEA = (
    "Give every worker its own persistent vector memory of past runs so it can "
    "look up similar situations before acting, backed by an embedding store "
    "refreshed on every iteration."
)


def _seed_failed_lesson(root: Path) -> None:
    _write_note(
        root, "knowledge/lessons", "2026-01-04-vector-memory",
        title="feat: per-worker persistent vector memory of past runs",
        body=FAILED_IDEA + " It was abandoned: the embedding store never stayed fresh.",
        outcome="fail", created="2026-01-04",
    )


def test_duplicate_risk_flags_a_candidate_restating_a_failed_lesson(tmp_path):
    _seed_failed_lesson(tmp_path)
    _lesson(tmp_path, "2026-01-05-other", title="feat: quota ledger", body="Cost telemetry.")
    index = load_index(tmp_path, _cfg())

    risk = duplicate_risk(index, "feat: worker vector memory of past runs\n" + FAILED_IDEA)
    assert risk.flagged is True
    assert risk.note_name == "2026-01-04-vector-memory"
    assert risk.outcome == "fail"
    assert risk.coverage >= _cfg().knowledge["index"].get("duplicate_threshold", 0.55)
    assert risk.decision == "drop"
    assert "[[2026-01-04-vector-memory]]" in risk.render("feat: worker vector memory")
    assert "drop" in risk.render("feat: worker vector memory")


def test_duplicate_risk_keeps_a_genuinely_new_candidate(tmp_path):
    _seed_failed_lesson(tmp_path)
    index = load_index(tmp_path, _cfg())

    risk = duplicate_risk(
        index,
        "feat: publish a signed provenance attestation for every merged pull request "
        "so downstream consumers can verify which model produced which diff",
    )
    assert risk.flagged is False
    assert risk.decision == "keep"
    assert "keep" in risk.render("feat: signed provenance attestation")


def test_duplicate_risk_never_flags_a_passing_note(tmp_path):
    """Restating a SHIPPED capability is caught by title duplication, not here -
    this scorer exists for the expensive knowledge: recorded failures."""
    _write_note(
        tmp_path, "knowledge/lessons", "2026-01-04-vector-memory",
        title="feat: per-worker persistent vector memory of past runs",
        body=FAILED_IDEA, outcome="pass", created="2026-01-04",
    )
    index = load_index(tmp_path, _cfg())

    risk = duplicate_risk(index, "feat: worker vector memory of past runs\n" + FAILED_IDEA)
    assert risk.flagged is False
    assert risk.note_name == "2026-01-04-vector-memory"
    assert risk.outcome == "pass"


def test_duplicate_risk_ignores_a_candidate_too_short_to_judge(tmp_path):
    _seed_failed_lesson(tmp_path)
    index = load_index(tmp_path, _cfg())

    risk = duplicate_risk(index, "feat: vector memory")
    assert risk.flagged is False  # 3 terms is noise, not evidence
    assert duplicate_risk(index, "feat: vector memory", k=0) == DuplicateRisk()


def test_duplicate_risk_threshold_is_configurable(tmp_path):
    _seed_failed_lesson(tmp_path)
    index = load_index(tmp_path, _cfg())
    candidate = "feat: worker vector memory of past runs\n" + FAILED_IDEA

    # Coverage is bounded by 1.0, so a threshold above it disables the flag.
    assert duplicate_risk(index, candidate, threshold=1.01).flagged is False
    assert duplicate_risk(index, candidate, threshold=0.1).flagged is True


def test_duplicate_risk_on_an_empty_index_is_never_flagged(tmp_path):
    assert duplicate_risk(load_index(tmp_path, _cfg()), FAILED_IDEA) == DuplicateRisk()
    assert DuplicateRisk().render("feat: anything").endswith("(no prior art matched)")


# --- configuration ------------------------------------------------------------

def test_retrieval_config_reads_core_yaml_and_documents_its_defaults():
    cfg = RetrievalConfig.from_core(load_config())
    assert cfg.index_dir == "knowledge/index"
    assert cfg.k >= 1
    assert cfg.title_boost > cfg.tag_boost > 1.0
    assert 0 < cfg.duplicate_threshold < 1

    # No `knowledge.index` block at all: the documented defaults apply.
    assert RetrievalConfig.from_core(None) == RetrievalConfig()


def test_note_index_defaults_to_the_documented_config(tmp_path):
    index = NoteIndex([Note(note_name="n", title="t", body="body")])
    assert index.cfg == RetrievalConfig()
    assert len(index) == 1
