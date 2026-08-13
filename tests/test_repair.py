"""Unit tests for the pure half of the verify-and-repair loop.

Nothing here spawns a subprocess or calls a model: `repair` is string handling
and configuration reading, and that is exactly what makes the repair prompt
auditable.
"""
from dataclasses import replace

from hsai import repair, review
from hsai.ci import CIResult
from hsai.config import load_config

RUFF_LOG = """$ ruff check .

src/hsai/widget.py:12:1: F401 [*] `os` imported but unused
Found 1 error.

$ pytest
collected 3 items / 3 passed
"""

PYTEST_LOG = """$ ruff check .
All checks passed!

$ pytest
============================= test session starts ==============================
collected 42 items

tests/test_widget.py F                                                   [100%]

=================================== FAILURES ===================================
_________________________________ test_widget __________________________________
    def test_widget():
>       assert widget() == 3
E       assert 2 == 3

tests/test_widget.py:9: AssertionError
=========================== short test summary info ============================
FAILED tests/test_widget.py::test_widget - assert 2 == 3
========================= 1 failed, 41 passed in 1.20s =========================
"""


def _red(**steps) -> CIResult:
    return CIResult(ok=all(steps.values()), steps=steps, log=PYTEST_LOG)


# --- configuration ----------------------------------------------------------

def test_repair_is_configured_and_enabled():
    """The repair budget is config, not code."""
    cfg = load_config()
    assert cfg.repair["enabled"] is True
    assert repair.is_enabled(cfg) is True
    assert repair.max_attempts(cfg) >= 1
    assert repair.max_log_chars(cfg) > 0


def test_missing_repair_block_falls_back_to_the_documented_defaults():
    cfg = replace(load_config(), repair={})
    assert repair.is_enabled(cfg) is True
    assert repair.max_attempts(cfg) == repair.DEFAULT_MAX_ATTEMPTS
    assert repair.max_log_chars(cfg) == repair.DEFAULT_MAX_LOG_CHARS


def test_skip_reason_covers_every_way_repair_is_turned_off():
    cfg = load_config()
    assert repair.skip_reason(cfg, demote_tier=False) == ""

    # A soft budget breach: the block is already burning quota.
    assert "soft budget breach" in repair.skip_reason(cfg, demote_tier=True)
    # Explicitly disabled, and a zero budget, both read as skips.
    off = replace(cfg, repair={"enabled": False})
    assert "disabled" in repair.skip_reason(off, demote_tier=False)
    zero = replace(cfg, repair={"max_attempts": 0})
    assert "max_attempts is 0" in repair.skip_reason(zero, demote_tier=False)
    # A negative budget is clamped, never turned into a negative loop bound.
    assert repair.max_attempts(replace(cfg, repair={"max_attempts": -3})) == 0


# --- log handling -----------------------------------------------------------

def test_failing_steps_names_only_the_red_steps_in_order():
    assert repair.failing_steps(_red(ruff=False, pytest=True)) == ["ruff"]
    assert repair.failing_steps(_red(ruff=False, pytest=False)) == ["ruff", "pytest"]
    assert repair.failing_steps(_red(ruff=True, pytest=True)) == []


def test_short_summary_lines_are_extracted_and_deduplicated():
    lines = repair.short_summary_lines(PYTEST_LOG)
    assert lines == ["FAILED tests/test_widget.py::test_widget - assert 2 == 3"]
    # The block terminator (the counters rule) is not part of the summary.
    assert not any("1 failed" in line for line in lines)
    # A log with no summary block yields nothing rather than raising.
    assert repair.short_summary_lines(RUFF_LOG) == []
    assert repair.short_summary_lines("") == []


def test_a_short_log_is_passed_through_untouched():
    assert repair.truncate_log(RUFF_LOG, 4000) == RUFF_LOG.strip()


def test_truncation_respects_the_cap_and_keeps_the_short_summary():
    noise = "x" * 6000
    log = noise + PYTEST_LOG
    out = repair.truncate_log(log, 500)

    assert len(out) <= 500
    # The tail alone would have cut the line naming what broke; it survives.
    assert "FAILED tests/test_widget.py::test_widget - assert 2 == 3" in out
    assert repair.SUMMARY_HEADING in out
    assert repair.ELIDED.strip() in out
    # ...and the most recent output is still quoted verbatim.
    assert out.endswith(PYTEST_LOG.strip()[-100:])


def test_truncation_without_a_summary_block_is_a_plain_tail():
    log = "y" * 5000 + "ruff: F401 unused import"
    out = repair.truncate_log(log, 200)
    assert len(out) <= 200
    assert out.endswith("ruff: F401 unused import")
    assert repair.SUMMARY_HEADING not in out


def test_a_zero_cap_disables_truncation_rather_than_erasing_the_log():
    assert repair.truncate_log(PYTEST_LOG, 0) == PYTEST_LOG.strip()


# --- the prompt itself ------------------------------------------------------

def test_build_repair_prompt_carries_the_ticket_steps_and_log_tail():
    prompt = repair.build_repair_prompt(
        "feat: add widget", _red(ruff=True, pytest=False), 1, max_attempts=2
    )

    assert prompt.startswith(repair.PROMPT_MARKER)
    assert "(1 of 2)" in prompt
    assert "feat: add widget" in prompt
    # Only the RED step is named as failing (the prompt mentions both elsewhere).
    steps_line = next(
        line for line in prompt.splitlines() if line.startswith("Failing CI step(s):")
    )
    assert steps_line == "Failing CI step(s): `pytest`"
    assert "FAILED tests/test_widget.py::test_widget" in prompt
    assert "CI red (ruff=pass, pytest=FAIL)" in prompt

    # It is a FIX-only instruction, with the loop's invariants spelled out.
    assert "Do not implement new behaviour" in prompt
    assert "do not relax" in prompt and "delete a test" in prompt
    assert ".github/workflows/" in prompt
    assert "regression test" in prompt


def test_build_repair_prompt_bounds_the_log_it_quotes():
    huge = CIResult(ok=False, steps={"pytest": False}, log="z" * 50000)
    prompt = repair.build_repair_prompt(
        "fix: thing", huge, 1, max_attempts=1, max_log_chars=1000
    )
    # The log is the only fenced block in the prompt; it is capped, not dropped.
    quoted = prompt.split("```")[1]
    assert len(quoted) <= 1000 + 2          # + the fence's own two newlines
    assert "z" in quoted


def test_build_repair_prompt_survives_a_ci_result_with_no_output():
    prompt = repair.build_repair_prompt("chore: x", CIResult(ok=False), 1)
    assert "(no output captured)" in prompt
    assert "`(none reported)`" in prompt


def test_the_repair_prompt_is_distinguishable_from_a_reviewer_prompt():
    """The three kinds of model call must never be confused for one another."""
    prompt = repair.build_repair_prompt("feat: x", _red(ruff=False, pytest=True), 1)
    assert repair.PROMPT_MARKER != review.PROMPT_MARKER
    assert review.PROMPT_MARKER not in prompt


# --- recording what a pass achieved -----------------------------------------

def test_describe_transition_reports_each_previously_failing_step():
    before = _red(ruff=False, pytest=True)
    after = CIResult(ok=True, steps={"ruff": True, "pytest": True})
    assert repair.describe_transition(before, after) == "ruff FAIL -> pass"

    both = _red(ruff=False, pytest=False)
    half = CIResult(ok=False, steps={"ruff": True, "pytest": False})
    assert repair.describe_transition(both, half) == "ruff FAIL -> pass, pytest FAIL -> FAIL"

    green = CIResult(ok=True, steps={"ruff": True, "pytest": True})
    assert repair.describe_transition(green, green) == "nothing to repair"
