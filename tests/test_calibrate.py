"""Outcome-calibrated routing: the fit, its refusal to guess, and the report."""
from hsai.calibrate import (
    DEFAULT_MIN_SAMPLES,
    Disagreement,
    disagreement,
    features_of,
    fit,
    labelled,
    render_article,
    tier_stats,
    write_article,
)
from hsai.ledger import LedgerRecord
from hsai.models import V1_THRESHOLDS, Thresholds


def _rec(
    iteration,
    *,
    tier,
    outcome,
    score=None,
    est_files=3,
    seconds=10.0,
    shadow_tier=None,
    size_label="",
):
    return LedgerRecord(
        iteration=iteration, block=1, ticket=iteration, kind="implement",
        tier=tier, model=tier, wall_clock_seconds=seconds, attempts=1,
        outcome=outcome, complexity_score=score, est_files=est_files,
        heavy_signals=0, light_signals=0, size_label=size_label,
        demoted=False, strategy="heuristic-v1", shadow_tier=shadow_tier,
    )


def _hand_built_ledger():
    """A distribution with a hand-checkable optimum.

    Twelve heavy-routed iterations (score 6) that merge half the time, and
    twelve standard-routed ones (score 3) that always merge. Every record
    touches 3 files, so the light tier - which requires <= 2 - is unreachable
    and only the heavy threshold is in play.

    Observed success rate: (6 + 12) / 24 = 75%, which becomes the floor.
    - heavy <= 3: everything routes heavy -> 50% success, below the floor.
    - heavy in 4..6: today's split -> 18 expected merges / 12 heavy = 1.50.
    - heavy >= 7: nothing routes heavy -> 24 expected merges / 1 = 24.00.
    So the optimum is the *cheapest admissible* heavy threshold above 6, and the
    tie-break toward v1 picks exactly heavy=7 (light stays at v1's -3).
    """
    records = []
    for i in range(12):
        records.append(_rec(100 + i, tier="heavy", outcome="merged" if i < 6 else "recovered", score=6))
    for i in range(12):
        records.append(_rec(200 + i, tier="standard", outcome="merged", score=3))
    return records


# --- insufficient data: an explicit refusal, never a fitted guess -------------

def test_fit_refuses_below_the_sample_floor():
    records = _hand_built_ledger()[:5]
    result = fit(records)

    assert result.ok is False
    assert result.thresholds is None          # the invariant: no guessed thresholds
    assert result.sample_size == 5
    assert result.min_samples == DEFAULT_MIN_SAMPLES
    assert "5 labelled" in result.reason and "20 required" in result.reason
    assert "Insufficient data" in result.render()
    assert "No recommendation" in result.recommendation()


def test_fit_refuses_when_records_carry_no_routing_features():
    # Pre-change ledger lines parse fine but are not training examples: they
    # never saw a complexity score, so nothing can be replayed through them.
    records = [
        LedgerRecord(
            iteration=i, block=1, ticket=1, kind="implement", tier="standard",
            model="sonnet", wall_clock_seconds=5.0, attempts=1, outcome="merged",
        )
        for i in range(50)
    ]
    result = fit(records)

    assert result.ok is False
    assert result.thresholds is None
    assert result.sample_size == 0
    # Per-tier observation still works - it needs only tier + outcome.
    assert result.stats[0].tier == "standard" and result.stats[0].count == 50


def test_min_samples_is_configurable():
    records = _hand_built_ledger()[:6]
    assert fit(records, min_samples=6).ok is True
    assert fit(records, min_samples=7).ok is False


# --- the fit itself -----------------------------------------------------------

def test_fit_finds_the_known_optimum_on_a_hand_built_distribution():
    result = fit(_hand_built_ledger())

    assert result.ok is True
    assert result.sample_size == 24
    assert result.thresholds == Thresholds(heavy=7, light=-3)
    assert result.objective == 24.0
    assert result.baseline_objective == 1.5
    assert result.heavy_iterations == 0
    assert result.baseline_heavy_iterations == 12
    assert result.success_floor == 0.75
    assert result.success_rate == 1.0
    # Exactly the twelve score-6 iterations move off the heavy tier.
    assert result.changed_records == 12
    assert result.changes_routing is True


def test_fit_holds_the_success_floor():
    # An explicit floor nothing can clear collapses the feasible set onto the
    # status quo rather than inventing thresholds that miss the bar.
    result = fit(_hand_built_ledger(), success_floor=1.01)

    assert result.ok is True
    assert result.thresholds == V1_THRESHOLDS
    assert result.changes_routing is False
    assert "Keep heuristic-v1" in result.recommendation()


def test_fit_is_deterministic():
    records = _hand_built_ledger()
    assert fit(records).thresholds == fit(list(reversed(records))).thresholds


def test_fit_recommendation_names_the_thresholds_and_the_human_step():
    text = fit(_hand_built_ledger()).recommendation()
    assert "heavy>=7, light<=-3" in text
    assert "models.calibration" in text
    assert "heuristic-v2" in text  # nothing moves until a human flips it


# --- per-tier observation -----------------------------------------------------

def test_tier_stats_reports_success_rate_and_mean_wall_clock():
    records = [
        _rec(1, tier="heavy", outcome="merged", score=6, seconds=100.0),
        _rec(2, tier="heavy", outcome="recovered", score=6, seconds=200.0),
        _rec(3, tier="standard", outcome="merged", score=3, seconds=50.0),
    ]
    stats = {s.tier: s for s in tier_stats(records)}

    assert stats["heavy"].count == 2 and stats["heavy"].merged == 1
    assert stats["heavy"].success_rate == 0.5
    assert stats["heavy"].mean_seconds == 150.0
    assert stats["standard"].success_rate == 1.0
    # Cheapest tier first, so the table reads in cost order.
    assert [s.tier for s in tier_stats(records)] == ["standard", "heavy"]


def test_features_of_round_trips_the_recorded_routing_features():
    rec = _rec(1, tier="heavy", outcome="merged", score=6, est_files=9, size_label="L")
    feats = features_of(rec)

    assert feats is not None
    assert feats.complexity_score == 6 and feats.est_files == 9
    assert feats.size_label == "L" and feats.kind == "implement"
    assert len(labelled([rec, LedgerRecord(
        iteration=2, block=1, ticket=1, kind="implement", tier="light",
        model="haiku", wall_clock_seconds=1.0, attempts=1, outcome="merged",
    )])) == 1


# --- shadow-vs-active disagreement --------------------------------------------

def test_disagreement_rate_counts_only_records_carrying_a_shadow_tier():
    records = [
        _rec(1, tier="standard", outcome="merged", score=3, shadow_tier="heavy"),
        _rec(2, tier="standard", outcome="merged", score=3, shadow_tier="standard"),
        _rec(3, tier="heavy", outcome="merged", score=6, shadow_tier="heavy"),
        _rec(4, tier="heavy", outcome="merged", score=6),  # no shadow recorded
    ]
    dis = disagreement(records)

    assert dis.observed == 3 and dis.disagreed == 1
    assert dis.rate == 1 / 3
    assert "iteration 1: `standard` -> `heavy`" in dis.render()


def test_disagreement_on_an_empty_ledger_is_zero_not_a_crash():
    dis = disagreement([])
    assert dis.observed == 0 and dis.rate == 0.0
    assert "no shadow-tier records yet" in dis.render()


# --- the committed report -------------------------------------------------------

def test_article_carries_sample_size_thresholds_disagreement_and_recommendation():
    result = fit(_hand_built_ledger())
    dis = disagreement([
        _rec(1, tier="standard", outcome="merged", score=3, shadow_tier="heavy")
    ])
    text = render_article(result, dis, date="2026-08-20")

    assert "# Model-routing calibration - 2026-08-20" in text
    assert "sample size): **24**" in text
    assert "heavy>=7, light<=-3" in text
    assert "Shadow disagreement: 1/1 (100%)" in text
    assert "## Recommendation" in text and "models.calibration" in text


def test_article_states_insufficient_data_without_emitting_thresholds():
    result = fit(_hand_built_ledger()[:3])
    text = render_article(result, Disagreement(observed=0, disagreed=0), date="2026-08-20")

    assert "insufficient data" in text
    assert "sample size): **3**" in text
    assert "heavy>=" not in text  # no thresholds anywhere in a refusal


def test_write_article_writes_exactly_one_dated_file(tmp_path):
    result = fit(_hand_built_ledger())
    path = write_article(tmp_path, result, disagreement([]), date="2026-08-20")

    assert path == tmp_path / "knowledge" / "articles" / "2026-08-20-model-routing-calibration.md"
    assert path.is_file()
    assert "24" in path.read_text()
    assert [p.name for p in (tmp_path / "knowledge" / "articles").iterdir()] == [path.name]
