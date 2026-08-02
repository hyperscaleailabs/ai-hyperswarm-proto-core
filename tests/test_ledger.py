import json

from hsai.config import load_config
from hsai.ledger import (
    HARD,
    OK,
    SOFT,
    BlockAggregate,
    LedgerRecord,
    aggregate_block,
    append_record,
    demote_tier,
    evaluate_budget,
    ledger_path,
    parse_tokens,
    read_records,
)
from hsai.models import Task, select


def _rec(block=1, tier="standard", seconds=10.0, attempts=1, outcome="merged", **kw):
    return LedgerRecord(
        iteration=block * 100 + 1, block=block, ticket=7, kind="implement",
        tier=tier, model=tier, wall_clock_seconds=seconds, attempts=attempts,
        outcome=outcome, **kw,
    )


# --- ledger append + read (append-only, parseable) --------------------------

def test_append_is_append_only_and_parseable(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_record(path, _rec(seconds=12.5))
    append_record(path, _rec(tier="heavy", seconds=30.0))

    # Every line is a standalone, parseable JSON object (append-only JSONL).
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)

    # A third append never rewrites the first two lines.
    first_two = path.read_text()
    append_record(path, _rec(tier="light", seconds=2.0))
    assert path.read_text().startswith(first_two)

    records = read_records(path)
    assert [r.tier for r in records] == ["standard", "heavy", "light"]
    assert records[0].wall_clock_seconds == 12.5


def test_read_records_missing_file_is_empty(tmp_path):
    assert read_records(tmp_path / "nope.jsonl") == []


def test_ledger_path_from_config(tmp_path):
    cfg = load_config()
    path = ledger_path(cfg, tmp_path)
    assert path == tmp_path / "knowledge" / "ledger" / "iterations.jsonl"


# --- token parsing (subscription-safe, best-effort) -------------------------

def test_parse_tokens_from_json_usage():
    out = json.dumps({"result": "ok", "usage": {"input_tokens": 120, "output_tokens": 45}})
    assert parse_tokens(out) == (120, 45)


def test_parse_tokens_none_when_absent_or_plain_text():
    assert parse_tokens("ok\n") is None
    assert parse_tokens("") is None
    assert parse_tokens(json.dumps({"result": "ok"})) is None
    assert parse_tokens("{not json") is None


# --- block aggregation ------------------------------------------------------

def test_aggregate_block_folds_only_matching_block():
    records = [
        _rec(block=1, tier="heavy", seconds=100.0, attempts=1,
             input_tokens=10, output_tokens=5),
        _rec(block=1, tier="standard", seconds=50.0, attempts=2),
        _rec(block=1, tier="heavy", seconds=25.0, attempts=1),
        _rec(block=2, tier="heavy", seconds=999.0),  # different block, ignored
    ]
    agg = aggregate_block(records, block=1)
    assert agg.iterations == 3
    assert agg.heavy_iterations == 2
    assert agg.total_seconds == 175.0
    assert agg.total_attempts == 4
    assert agg.tier_counts == {"heavy": 2, "standard": 1}
    assert agg.input_tokens == 10 and agg.output_tokens == 5
    assert "heavy-tier=2" in agg.summary()


def test_aggregate_block_folds_review_cost_and_verdicts():
    records = [
        _rec(block=1, review_verdict="PASS", review_tier="light", review_seconds=8.0),
        _rec(block=1, review_verdict="FAIL", review_tier="light", review_seconds=6.5),
        _rec(block=1, review_verdict="PASS", review_tier="light", review_seconds=5.5),
        _rec(block=1),  # an iteration with no review at all
        _rec(block=2, review_verdict="PASS", review_seconds=99.0),  # other block
    ]
    agg = aggregate_block(records, block=1)
    assert agg.review_seconds == 20.0
    assert agg.review_verdicts == {"PASS": 2, "FAIL": 1}
    assert "review[FAIL=1, PASS=2] 20s" in agg.summary()


def test_records_without_review_fields_still_parse(tmp_path):
    """Ledger lines written before the review gate existed remain readable."""
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({
        "iteration": 1, "block": 0, "ticket": 3, "kind": "implement",
        "tier": "standard", "model": "sonnet", "wall_clock_seconds": 4.0,
        "attempts": 1, "outcome": "merged", "created": "2026-07-01T00:00:00+00:00",
    }) + "\n")
    records = read_records(path)
    assert records[0].review_verdict is None
    assert aggregate_block(records, block=0).review_verdicts == {}


# --- budget gate transitions ------------------------------------------------

BUDGET = {"max_heavy_iterations_per_block": 3, "max_seconds_per_block": 100, "soft_ratio": 0.8}


def test_budget_ok_below_soft_threshold():
    agg = BlockAggregate(block=1, heavy_iterations=1, total_seconds=50.0)
    decision = evaluate_budget(agg, BUDGET)
    assert decision.status == OK
    assert not decision.demote and not decision.halt


def test_budget_soft_breach_biases_cheaper():
    # heavy=2 hits 0.8*3=2.4? no -> use seconds: 80 >= 0.8*100 triggers soft.
    agg = BlockAggregate(block=1, heavy_iterations=1, total_seconds=80.0)
    decision = evaluate_budget(agg, BUDGET)
    assert decision.status == SOFT
    assert decision.demote and not decision.halt
    assert "wall-clock" in decision.reason


def test_budget_hard_breach_halts():
    agg = BlockAggregate(block=1, heavy_iterations=3, total_seconds=40.0)
    decision = evaluate_budget(agg, BUDGET)
    assert decision.status == HARD
    assert decision.halt and not decision.demote
    assert "heavy-tier" in decision.reason


def test_budget_seconds_hard_breach_halts():
    agg = BlockAggregate(block=1, heavy_iterations=0, total_seconds=100.0)
    decision = evaluate_budget(agg, BUDGET)
    assert decision.status == HARD


def test_empty_budget_never_gates():
    agg = BlockAggregate(block=1, heavy_iterations=99, total_seconds=1e9)
    assert evaluate_budget(agg, {}).status == OK


# --- tier demotion drives selection cheaper ---------------------------------

def test_demote_tier_steps_down_and_floors():
    assert demote_tier("heavy") == "standard"
    assert demote_tier("standard") == "light"
    assert demote_tier("light") == "light"
    assert demote_tier("unknown") == "unknown"


def test_select_demote_biases_toward_cheaper_tier():
    cfg = load_config()
    heavy_task = Task(kind="implement", title="feat: big", body="", labels=("size:L",))
    normal = select(heavy_task, cfg)
    demoted = select(heavy_task, cfg, demote=True)
    assert normal.tier == "heavy"
    assert demoted.tier == "standard"
    assert "soft budget breach" in demoted.rationale
