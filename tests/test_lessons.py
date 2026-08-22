"""Evidence-driven lesson synthesis: the fail-safe fallback contract, the
evidence bundle, and the cheap-tier-only, budget-aware model call."""
import json

from hsai import ledger, lessons
from hsai.config import load_config
from hsai.knowledge import FALLBACK_FAIL, FALLBACK_PASS, FALLBACK_TAG
from hsai.proc import Proc

TICKET_TITLE = "feat: add widget"
TICKET_BODY = "Build the widget end to end."


def _envelope(text: str, *, tokens: tuple[int, int] = (200, 40)) -> str:
    return json.dumps(
        {"type": "result", "result": text, "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}}
    )


SUCCESS_JSON = """```json
{"what_attempted": "Implement the widget per the ticket.",
 "what_happened": "The agent added the widget module and a passing test; CI stayed green.",
 "lesson": "Ship the smallest widget that satisfies every acceptance checkbox before generalizing.",
 "references": ["openai/swarm"]}
```"""

MISSING_FIELD_JSON = """```json
{"what_attempted": "Implement the widget.", "what_happened": "It worked.", "lesson": ""}
```"""


class _LessonRunner:
    """Answers `claude -p` for the synthesis call; records every command."""

    def __init__(self, *, output: str = _envelope(SUCCESS_JSON), code: int = 0, stderr: str = "") -> None:
        self.output = output
        self.code = code
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:1] == ["claude"]:
            return Proc(cmd, self.code, self.output, self.stderr)
        raise AssertionError(f"unexpected command {cmd!r}")

    @property
    def claude_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:1] == ["claude"]]


def _evidence(**overrides) -> lessons.EvidenceBundle:
    kwargs = dict(
        kind="implement", ticket_title=TICKET_TITLE, ticket_body=TICKET_BODY, outcome="pass",
        changed_files=["src/hsai/widget.py"], ci_ok=True, ci_log="",
        attempts=1, tier="light", shadow_tier="light",
    )
    kwargs.update(overrides)
    return lessons.build_evidence(**kwargs)


def _synthesize(cfg, root, runner, **overrides):
    return lessons.synthesize_lesson(
        _evidence(**overrides), cfg, repo_root=str(root), block=0, ai_runner=runner,
    )


# --- build_evidence / EvidenceBundle.render ----------------------------------

def test_build_evidence_diffstat_and_log_tail_only_when_ci_is_red():
    green = lessons.build_evidence(
        kind="implement", ticket_title="t", ticket_body="b", outcome="pass",
        changed_files=["a.py", "b.py"], ci_ok=True, ci_log="should not appear",
    )
    assert green.diffstat == "2 file(s) changed"
    assert green.ci_log_tail == ""

    red = lessons.build_evidence(
        kind="heal", ticket_title="t", ticket_body="b", outcome="fail",
        changed_files=[], ci_ok=False, ci_log="ruff: E501 line too long",
    )
    assert red.diffstat == "no files changed"
    assert "E501" in red.ci_log_tail


def test_build_evidence_truncates_a_long_failure_log():
    long_log = "x" * 5000
    red = lessons.build_evidence(
        kind="heal", ticket_title="t", ticket_body="b", outcome="fail",
        ci_ok=False, ci_log=long_log, max_log_chars=100,
    )
    assert len(red.ci_log_tail) == 100


def test_evidence_render_includes_guards_review_and_shadow_tier():
    evidence = lessons.build_evidence(
        kind="implement", ticket_title=TICKET_TITLE, ticket_body=TICKET_BODY, outcome="pass",
        changed_files=["src/hsai/widget.py"], ci_ok=True, ci_log="",
        guards=[lessons.GuardOutcome("independent-review", fired=False, detail="approve by `haiku`")],
        review_verdict="- verdict: **APPROVED**",
        attempts=2, tier="standard", shadow_tier="heavy",
    )
    rendered = evidence.render()
    assert "attempts: 2" in rendered
    assert "tier used: standard (would have been heavy without a budget demotion)" in rendered
    assert "independent-review: clear - approve by `haiku`" in rendered
    assert "src/hsai/widget.py" in rendered
    assert "**APPROVED**" in rendered


def test_build_prompt_carries_the_marker_evidence_and_ticket():
    prompt = lessons.build_prompt(_evidence())
    assert lessons.PROMPT_MARKER in prompt
    assert TICKET_TITLE in prompt
    assert TICKET_BODY in prompt
    assert "src/hsai/widget.py" in prompt
    assert "```json" in prompt


# --- synthesize_lesson: success and every fail-safe path ---------------------

def test_synthesize_lesson_success_parses_all_three_fields_and_a_citation(tmp_path):
    cfg = load_config()
    runner = _LessonRunner()

    result = _synthesize(cfg, tmp_path, runner)

    assert result.fallback is False
    assert result.what_attempted and result.what_happened and result.lesson_text
    assert result.references == ("openai/swarm",)
    assert "openai/swarm" in result.render_lesson()
    # cheap tier only - never heavy (see acceptance criteria)
    assert result.tier in ("light", "standard")
    assert result.model


def test_synthesize_lesson_never_routes_to_the_heavy_tier(tmp_path):
    cfg = load_config()
    runner = _LessonRunner()
    result = _synthesize(cfg, tmp_path, runner)
    assert result.tier != "heavy"
    # and light is preferred whenever it is configured
    assert result.tier == "light"


def test_synthesize_lesson_falls_back_on_agent_error(tmp_path):
    cfg = load_config()
    runner = _LessonRunner(output="", code=1, stderr="boom")

    result = _synthesize(cfg, tmp_path, runner, outcome="fail")

    assert result.fallback is True
    assert result.lesson_text == FALLBACK_FAIL
    assert "synthesis call failed" in result.fallback_reason
    assert FALLBACK_TAG in result.tags


def test_synthesize_lesson_falls_back_on_timeout(tmp_path):
    cfg = load_config()
    # hsai.proc.run stamps a real timeout exactly like this (see proc.py).
    runner = _LessonRunner(output="", code=124, stderr="timeout after 180.0s")

    result = _synthesize(cfg, tmp_path, runner)

    assert result.fallback is True
    assert result.lesson_text == FALLBACK_PASS
    assert "timeout after 180.0s" in result.fallback_reason


def test_synthesize_lesson_falls_back_on_empty_output(tmp_path):
    cfg = load_config()
    runner = _LessonRunner(output="")

    result = _synthesize(cfg, tmp_path, runner)

    assert result.fallback is True
    assert result.lesson_text == FALLBACK_PASS
    assert "no parseable JSON" in result.fallback_reason


def test_synthesize_lesson_falls_back_on_a_missing_required_field(tmp_path):
    cfg = load_config()
    runner = _LessonRunner(output=_envelope(MISSING_FIELD_JSON))

    result = _synthesize(cfg, tmp_path, runner)

    assert result.fallback is True
    assert "missing a required field" in result.fallback_reason


def test_synthesize_lesson_skips_entirely_on_a_hard_budget_breach(tmp_path):
    cfg = load_config()
    path = ledger.ledger_path(cfg, tmp_path)
    for i in range(cfg.budget["max_heavy_iterations_per_block"]):
        ledger.append_record(path, ledger.LedgerRecord(
            iteration=i, block=0, ticket=1, kind="implement", tier="heavy",
            model="opus", wall_clock_seconds=1.0, attempts=1, outcome="merged",
        ))
    runner = _LessonRunner()

    result = _synthesize(cfg, tmp_path, runner)

    assert result.fallback is True
    assert "hard budget breach" in result.fallback_reason
    assert runner.claude_calls == []          # never spawned a claude process


def test_every_failure_mode_still_yields_a_lesson_a_test_can_write(tmp_path):
    """Acceptance criterion, restated directly: whatever goes wrong, the
    caller always gets something it can hand to `KnowledgeBase.write_lesson`."""
    cfg = load_config()
    modes = [
        _LessonRunner(output=""),                                   # empty
        _LessonRunner(output="", code=1, stderr="boom"),            # error
        _LessonRunner(output="", code=124, stderr="timeout after 1s"),  # timeout
        _LessonRunner(output=_envelope(MISSING_FIELD_JSON)),        # malformed
    ]
    for runner in modes:
        result = _synthesize(cfg, tmp_path, runner)
        assert result.lesson_text in (FALLBACK_PASS, FALLBACK_FAIL)
        assert result.render_lesson() == result.lesson_text


# --- SynthesizedLesson rendering ---------------------------------------------

def test_render_lesson_appends_a_citation_only_once():
    synthesized = lessons.SynthesizedLesson(
        what_attempted="a", what_happened="b",
        lesson_text="Prefer the smallest working slice.",
        references=("openai/swarm",),
    )
    rendered = synthesized.render_lesson()
    assert "openai/swarm" in rendered
    assert rendered.count("openai/swarm") == 1

    already_cited = lessons.SynthesizedLesson(
        what_attempted="a", what_happened="b",
        lesson_text="Borrowed openai/swarm's tiny-core discipline.",
        references=("openai/swarm",),
    )
    assert already_cited.render_lesson().count("openai/swarm") == 1


def test_fallback_lesson_is_byte_identical_to_the_deterministic_text():
    passed = lessons.fallback_lesson("pass", "test")
    failed = lessons.fallback_lesson("fail", "test")
    assert passed.render_lesson() == FALLBACK_PASS
    assert failed.render_lesson() == FALLBACK_FAIL
    assert passed.fallback is True and passed.tags == (FALLBACK_TAG,)
