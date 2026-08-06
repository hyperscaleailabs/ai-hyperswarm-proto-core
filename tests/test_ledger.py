import json

from hsai.ai import run_agent
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
from hsai.models import ModelChoice, Task, select
from hsai.proc import Proc


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


def test_parse_tokens_from_a_realistic_claude_json_payload():
    """The envelope `claude -p --output-format json` actually emits."""
    payload = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 41230,
        "duration_api_ms": 39980,
        "num_turns": 14,
        "result": "Implemented the ticket and added tests.",
        "session_id": "0f2a9c31-6d4e-4a11-9d0f-7c2b5e8a1d33",
        "total_cost_usd": 0.0,
        "usage": {
            "input_tokens": 1512,
            "cache_creation_input_tokens": 8241,
            "cache_read_input_tokens": 130422,
            "output_tokens": 3987,
        },
    })
    assert parse_tokens(payload) == (1512, 3987)


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


# --- token ceiling (max_tokens_per_block) ------------------------------------

TOKEN_BUDGET = {"max_tokens_per_block": 1000, "soft_ratio": 0.8}


def test_budget_tokens_ok_below_soft_threshold():
    agg = BlockAggregate(block=1, input_tokens=400, output_tokens=100)  # 500 < 0.8*1000
    decision = evaluate_budget(agg, TOKEN_BUDGET)
    assert decision.status == OK


def test_budget_tokens_soft_breach_biases_cheaper():
    agg = BlockAggregate(block=1, input_tokens=700, output_tokens=100)  # 800 >= 0.8*1000
    decision = evaluate_budget(agg, TOKEN_BUDGET)
    assert decision.status == SOFT
    assert decision.demote and not decision.halt
    assert "tokens" in decision.reason


def test_budget_tokens_hard_breach_halts():
    agg = BlockAggregate(block=1, input_tokens=900, output_tokens=200)  # 1100 >= 1000
    decision = evaluate_budget(agg, TOKEN_BUDGET)
    assert decision.status == HARD
    assert decision.halt and not decision.demote
    assert "tokens" in decision.reason


def test_budget_tokens_unset_ceiling_disables_dimension():
    agg = BlockAggregate(block=1, input_tokens=10**9, output_tokens=10**9)
    decision = evaluate_budget(agg, {"max_heavy_iterations_per_block": 99})
    assert decision.status == OK


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


# --- the previously-dead path: real stdout -> tokens -> LedgerRecord --------

REAL_CLAUDE_STDOUT = json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 41230,
    "num_turns": 14,
    "result": "Implemented the ticket and added tests.",
    "session_id": "0f2a9c31-6d4e-4a11-9d0f-7c2b5e8a1d33",
    "total_cost_usd": 0.0,
    "usage": {
        "input_tokens": 1512,
        "cache_creation_input_tokens": 8241,
        "cache_read_input_tokens": 130422,
        "output_tokens": 3987,
    },
})


def test_parse_tokens_accepts_an_already_parsed_payload():
    """Callers holding `AIResult.payload` pass it straight in - no re-parsing."""
    assert parse_tokens(json.loads(REAL_CLAUDE_STDOUT)) == (1512, 3987)
    assert parse_tokens({"result": "ok"}) is None
    assert parse_tokens(None) is None


def test_run_agent_output_populates_the_ledger_record(tmp_path):
    """End to end: `claude -p` stdout -> run_agent -> parse_tokens -> ledger.

    Before `--output-format json` was actually passed, `parse_tokens` could
    never fire and these two columns were dead. This is the regression test for
    that wiring.
    """
    cfg = load_config()

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        return Proc(cmd, 0, REAL_CLAUDE_STDOUT, "")

    ares = run_agent(
        "implement the ticket",
        ModelChoice(tier="standard", model="sonnet", rationale="t"),
        cfg, runner=runner,
    )

    tokens = parse_tokens(ares.payload)
    assert tokens == (1512, 3987)          # non-None: the path fires

    path = tmp_path / "ledger.jsonl"
    append_record(path, _rec(input_tokens=tokens[0], output_tokens=tokens[1]))

    stored = read_records(path)[0]
    assert stored.input_tokens == 1512 and stored.output_tokens == 3987
    # And it survives the JSONL round-trip as real numbers, not strings.
    assert json.loads(path.read_text())["input_tokens"] == 1512


# --- tokens per merged PR (the block's efficiency signal) -------------------

def test_tokens_per_merged_pr():
    records = [
        _rec(block=1, outcome="merged", input_tokens=1000, output_tokens=200),
        _rec(block=1, outcome="merged", input_tokens=600, output_tokens=200),
        _rec(block=1, outcome="recovered", input_tokens=400, output_tokens=0),
    ]
    agg = aggregate_block(records, block=1)
    assert agg.merged_iterations == 2
    assert agg.total_tokens == 2400
    assert agg.tokens_per_merged_pr() == 1200.0
    assert "1200 tokens/merged PR" in agg.summary()


def test_tokens_per_merged_pr_is_undefined_without_merges_or_tokens():
    nothing_merged = aggregate_block(
        [_rec(block=1, outcome="recovered", input_tokens=500, output_tokens=100)], block=1
    )
    assert nothing_merged.tokens_per_merged_pr() is None
    assert "tokens/merged PR" not in nothing_merged.summary()

    no_tokens = aggregate_block([_rec(block=1, outcome="merged")], block=1)
    assert no_tokens.tokens_per_merged_pr() is None
