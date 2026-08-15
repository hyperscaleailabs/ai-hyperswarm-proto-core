from pathlib import Path

import pytest

from hsai import practices
from hsai.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _practice(id_, repo, *, status=practices.OBSERVED, first_seen="2020-01-01", **kw):
    return practices.Practice(
        id=id_,
        repo=repo,
        artifact=kw.pop("artifact", "src/pkg/mod.py"),
        category=kw.pop("category", "testing"),
        observation=kw.pop("observation", "observed something concrete"),
        goal_ids=kw.pop("goal_ids", ("G1",)),
        status=status,
        first_seen=first_seen,
        **kw,
    )


# --- load / append / mark ----------------------------------------------------

def test_load_missing_store_is_empty(tmp_path):
    assert practices.load(tmp_path / "nope.jsonl") == []


def test_append_and_load_roundtrip(tmp_path):
    path = tmp_path / "practices.jsonl"
    p = _practice("p-a", "repoA/x", artifact="path/a.py", goal_ids=("G1", "G4"))
    practices.append(path, p)

    loaded = practices.load(path)
    assert len(loaded) == 1
    assert loaded[0].id == "p-a"
    assert loaded[0].repo == "repoA/x"
    assert loaded[0].goal_ids == ("G1", "G4")


def test_mark_appends_a_new_line_and_load_folds_to_the_latest(tmp_path):
    path = tmp_path / "practices.jsonl"
    practices.append(path, _practice("p-a", "repoA/x"))
    practices.append(path, _practice("p-b", "repoB/y"))

    practices.mark(path, "p-a", practices.IN_FLIGHT, ticket=42)

    # append-only: the file itself now has 3 lines, not 2 (the update never
    # rewrites the original).
    assert len(path.read_text().splitlines()) == 3

    loaded = {p.id: p for p in practices.load(path)}
    assert len(loaded) == 2                      # folded to one record per id
    assert loaded["p-a"].status == practices.IN_FLIGHT
    assert loaded["p-a"].ticket == 42
    assert loaded["p-b"].status == practices.OBSERVED  # untouched


def test_mark_unknown_id_raises(tmp_path):
    path = tmp_path / "practices.jsonl"
    practices.append(path, _practice("p-a", "repoA/x"))
    with pytest.raises(KeyError):
        practices.mark(path, "does-not-exist", practices.ADOPTED)


# --- next_unadopted -----------------------------------------------------------

def test_next_unadopted_never_returns_adopted_rejected_or_in_flight():
    cfg = load_config()
    store = [
        _practice("p-adopted", "repoA/x", status=practices.ADOPTED),
        _practice("p-rejected", "repoB/y", status=practices.REJECTED),
        _practice("p-in-flight", "repoC/z", status=practices.IN_FLIGHT),
        _practice("p-observed", "repoD/w", status=practices.OBSERVED),
    ]
    picked = practices.next_unadopted(cfg, store)
    assert picked is not None
    assert picked.id == "p-observed"


def test_next_unadopted_returns_none_when_nothing_is_observed():
    cfg = load_config()
    store = [
        _practice("p-adopted", "repoA/x", status=practices.ADOPTED),
        _practice("p-in-flight", "repoC/z", status=practices.IN_FLIGHT),
    ]
    assert practices.next_unadopted(cfg, store) is None


def test_next_unadopted_prefers_better_goal_fit():
    cfg = load_config()
    store = [
        _practice("low-fit", "repoA/x", goal_ids=("G3",), first_seen="2020-01-01"),
        _practice("high-fit", "repoB/y", goal_ids=("G1", "G2"), first_seen="2020-01-02"),
    ]
    picked = practices.next_unadopted(cfg, store)
    assert picked.id == "high-fit"


def test_next_unadopted_prefers_under_mined_repos_on_a_goal_fit_tie():
    cfg = load_config()
    store = [
        # repoA already has an adopted practice; repoB has none yet.
        _practice("repoA-adopted", "repoA/x", status=practices.ADOPTED, first_seen="2019-01-01"),
        _practice("repoA-candidate", "repoA/x", first_seen="2020-01-01"),
        _practice("repoB-candidate", "repoB/y", first_seen="2020-01-02"),
    ]
    picked = practices.next_unadopted(cfg, store)
    assert picked.id == "repoB-candidate"  # under-mined repo wins despite later evidence


def test_next_unadopted_falls_back_to_oldest_evidence_then_id():
    cfg = load_config()
    store = [
        _practice("z-newer", "repoA/x", first_seen="2020-06-01"),
        _practice("a-older", "repoB/y", first_seen="2020-01-01"),
    ]
    picked = practices.next_unadopted(cfg, store)
    assert picked.id == "a-older"


# --- coverage_report ----------------------------------------------------------

def test_coverage_report_counts_per_repo_and_lists_every_reference_repo():
    cfg = load_config()
    store = [
        _practice("p1", "run-llama/llama_index", status=practices.ADOPTED),
        _practice("p2", "run-llama/llama_index", status=practices.OBSERVED),
        _practice("p3", "openai/swarm", status=practices.IN_FLIGHT),
    ]
    rows = practices.coverage_report(cfg, store)
    by_repo = {r["repo"]: r for r in rows}

    # every reference-set repo appears, even ones with zero mined practices
    assert len(rows) == len(cfg.reference_top10)
    assert by_repo["langchain-ai/langchain"]["observed"] == 0
    assert by_repo["langchain-ai/langchain"]["adopted"] == 0

    assert by_repo["run-llama/llama_index"]["observed"] == 2
    assert by_repo["run-llama/llama_index"]["adopted"] == 1
    assert by_repo["openai/swarm"]["observed"] == 1
    assert by_repo["openai/swarm"]["adopted"] == 0
    assert by_repo["openai/swarm"]["in_flight"] == 1


def test_coverage_report_is_ordered_by_reference_set_rank():
    cfg = load_config()
    rows = practices.coverage_report(cfg, [])
    ranks = [r["rank"] for r in rows]
    assert ranks == sorted(ranks)


# --- the seeded, real provenance ledger (acceptance criterion #1) ------------

def test_seeded_practices_store_has_real_backfilled_records():
    path = practices.practices_path(load_config(), REPO_ROOT)
    records = practices.load(path)
    assert len(records) >= 8

    adopted = [p for p in records if p.status == practices.ADOPTED]
    assert len(adopted) >= 8
    for p in adopted:
        assert p.repo and p.artifact and p.observation
        assert p.lesson, f"{p.id} claims adopted but cites no lesson"
        # every adopted practice's lesson file actually exists on disk
        assert (REPO_ROOT / "knowledge" / "lessons" / f"{p.lesson}.md").exists()

    # the four practices the ticket explicitly names as back-fill evidence
    repos = {p.repo for p in adopted}
    assert "FoundationAgents/MetaGPT" in repos          # phase-artifacts adoption
    assert "run-llama/llama_index" in repos             # repro-guard adoption
    assert "openai/swarm" in repos                      # error-context adoption
    assert {"assafelovic/gpt-researcher", "OpenBMB/ChatDev"} & repos  # cost-ledger


def test_seeded_store_leaves_real_unadopted_candidates_for_next_unadopted():
    cfg = load_config()
    store = practices.load(practices.practices_path(cfg, REPO_ROOT))
    picked = practices.next_unadopted(cfg, store)
    assert picked is not None
    assert picked.status == practices.OBSERVED
