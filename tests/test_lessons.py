"""Evidence-driven lesson synthesis: the evidence bundle, the fail-safe model
call, and its interaction with the block budget gate."""
import json

from hsai import ledger, lessons
from hsai.ci import CIResult
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson, detect_boilerplate
from hsai.proc import Proc

TICKET_TITLE = "feat: gate merges on the remote CI rollup"


def _envelope(text: str, *, tokens: tuple[int, int] = (100, 20)) -> str:
    return json.dumps({
        "type": "result", "result": text,
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    })


def _synthesis_json(*, attempted="Add the widget.", happened="CI stayed green.",
                     rule="Keep the surface area small.", references=()) -> str:
    payload = {"attempted": attempted, "happened": happened, "rule": rule}
    if references:
        payload["references"] = list(references)
    return f"Reviewed the evidence.\n\n```json\n{json.dumps(payload)}\n```"


class _GitRunner:
    """Answers the two read-only git calls build_evidence makes."""

    def __init__(self, *, changed: str = "src/hsai/widget.py\n", diffstat: str = " 1 file changed\n"):
        self.changed = changed
        self.diffstat = diffstat
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:2] == ["git", "diff"] and "--name-only" in cmd:
            return Proc(cmd, 0, self.changed, "")
        if cmd[:2] == ["git", "diff"] and "--stat" in cmd:
            return Proc(cmd, 0, self.diffstat, "")
        raise AssertionError(f"unexpected git command {cmd!r}")


class _AIRunner:
    """Answers exactly one `claude -p` call with a canned Proc."""

    def __init__(self, proc: Proc | None = None, *, raises: Exception | None = None):
        self.proc = proc
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        return self.proc


def _evidence(**overrides) -> lessons.EvidenceBundle:
    defaults = dict(
        kind="implement", ticket_title=TICKET_TITLE, outcome="pass",
        wt="/tmp/wt", base_ref="parentsha", ci_result=CIResult(ok=True, steps={"pytest": True}),
        guards=["completeness guard: passed", "independent review: approve by `sonnet` (0 blocking)"],
    )
    defaults.update(overrides)
    return lessons.build_evidence(runner=_GitRunner(), **defaults)


# --- build_evidence -----------------------------------------------------------

def test_build_evidence_assembles_the_bundle_from_git():
    ci_result = CIResult(ok=False, steps={"pytest": False}, log="x" * 3000 + "PYTEST FAILED HERE")
    git = _GitRunner(changed="src/hsai/widget.py\ntests/test_widget.py\n", diffstat=" 2 files changed\n")
    evidence = lessons.build_evidence(
        kind="heal", ticket_title="ci: main is red - auto-heal", outcome="fail",
        wt="/tmp/wt", base_ref="parentsha", ci_result=ci_result,
        guards=["repro guard: reproduced - fix-branch passes, parent fails"],
        reverted_workflows=[".github/workflows/ci.yml"],
        repro_evidence="- verdict: **reproduced**",
        attempts=2, tier="standard", shadow_tier="light",
        references=("openai/swarm",), agent_error="boom" * 300,
        runner=git, max_log_chars=50,
    )
    assert evidence.changed_paths == ["src/hsai/widget.py", "tests/test_widget.py"]
    assert evidence.diffstat == "2 files changed"
    # Only the FAILING portion, bounded by max_log_chars.
    assert evidence.ci_log_tail == ci_result.log[-50:]
    assert len(evidence.ci_log_tail) == 50
    assert evidence.reverted_workflows == [".github/workflows/ci.yml"]
    assert evidence.attempts == 2 and evidence.tier == "standard" and evidence.shadow_tier == "light"
    assert evidence.references == ("openai/swarm",)
    # agent_error is capped at 800 chars regardless of caller input.
    assert len(evidence.agent_error) == 800
    # both read-only git calls were made, nothing else
    assert len(git.calls) == 2


def test_build_evidence_leaves_ci_log_tail_empty_on_a_green_build():
    evidence = _evidence(ci_result=CIResult(ok=True, steps={"pytest": True}, log="all good"))
    assert evidence.ci_log_tail == ""


def test_evidence_render_and_prompt_contain_the_ticket_and_the_guards():
    evidence = _evidence(guards=["completeness guard: passed", "repro guard: not applicable (not a heal/bugfix ticket)"])
    prompt = lessons.build_prompt(evidence)
    assert lessons.PROMPT_MARKER in prompt
    assert TICKET_TITLE in prompt
    assert "completeness guard: passed" in prompt
    assert "src/hsai/widget.py" in prompt
    assert "1 file changed" in prompt


# --- parse_synthesis: the fail-open-on-a-real-answer, else-None contract -----

def test_parse_synthesis_reads_the_fenced_json_block():
    result = lessons.parse_synthesis(_synthesis_json(references=["openai/swarm"]))
    assert result.attempted == "Add the widget."
    assert result.happened == "CI stayed green."
    assert result.rule == "Keep the surface area small."
    assert result.references == ("openai/swarm",)
    assert result.used_fallback is False


def test_parse_synthesis_returns_none_on_garbage_or_missing_fields():
    for bad in (
        "", "   ", "no json here",
        '```json\n{"attempted": "x", "happened": "y"}\n```',   # missing rule
        '```json\n[1, 2]\n```',                                 # not an object
        '```json\n{not json}\n```',
    ):
        assert lessons.parse_synthesis(bad) is None


# --- synthesize_lesson: every failure mode still yields a lesson ------------

def _kb_lesson_exists(tmp_path, result: lessons.SynthesisResult, outcome: str = "pass"):
    """Mirrors what hsai.orchestrator does with a SynthesisResult: write a
    Lesson and confirm the file exists - the acceptance criterion in
    concrete form."""
    kb = KnowledgeBase(tmp_path)
    lesson = Lesson(
        title="implement: widget", outcome=outcome, kind="implement",
        context="ctx", what_happened="did the thing", lesson=result.lesson_text(),
        synthesis_fallback=result.used_fallback,
    )
    path = kb.write_lesson(lesson)
    assert path.exists()
    return path.read_text()


def test_synthesize_lesson_success_returns_the_three_fields(tmp_path):
    cfg = load_config()
    ai_runner = _AIRunner(Proc(["claude"], 0, _envelope(_synthesis_json()), ""))
    evidence = _evidence()

    result = lessons.synthesize_lesson(
        cfg, evidence, repo_root=tmp_path, wt="/tmp/wt", block=0, iteration=1,
        ticket=7, attempts=1, ai_runner=ai_runner,
    )

    assert result.used_fallback is False
    assert result.attempted and result.happened and result.rule
    assert result.tier in ("light", "standard")
    text = _kb_lesson_exists(tmp_path, result)
    assert "**Attempted:**" in text and "**Happened:**" in text and "**Rule:**" in text

    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert [r.kind for r in records] == ["lesson_synthesis"]
    assert records[0].outcome == "ok"
    assert records[0].input_tokens == 100 and records[0].output_tokens == 20
    # never routes to the heavy tier
    assert records[0].tier != "heavy"


def test_synthesize_lesson_falls_back_on_a_nonzero_exit(tmp_path):
    cfg = load_config()
    ai_runner = _AIRunner(Proc(["claude"], 1, "", "boom: model unavailable"))
    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="fail"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )
    assert result.used_fallback is True
    assert result.lesson_text() == lessons.FALLBACK_FAIL
    assert "synthesis call failed" in result.fallback_reason
    _kb_lesson_exists(tmp_path, result, outcome="fail")
    assert ledger.read_records(ledger.ledger_path(cfg, tmp_path))[0].outcome == "error"


def test_synthesize_lesson_falls_back_on_a_timeout(tmp_path):
    cfg = load_config()
    # hsai.proc.run's own TimeoutExpired handler stamps stderr exactly this way.
    ai_runner = _AIRunner(Proc(["claude"], 124, "", "timeout after 180.0s"))
    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="pass"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )
    assert result.used_fallback is True
    assert result.lesson_text() == lessons.FALLBACK_PASS
    assert "timeout after" in result.fallback_reason
    _kb_lesson_exists(tmp_path, result, outcome="pass")


def test_synthesize_lesson_falls_back_on_empty_output(tmp_path):
    cfg = load_config()
    ai_runner = _AIRunner(Proc(["claude"], 0, _envelope(""), ""))
    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="fail"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )
    assert result.used_fallback is True
    assert result.fallback_reason == "synthesis call returned empty output"
    _kb_lesson_exists(tmp_path, result, outcome="fail")
    assert ledger.read_records(ledger.ledger_path(cfg, tmp_path))[0].outcome == "empty"


def test_synthesize_lesson_falls_back_on_unparseable_output(tmp_path):
    cfg = load_config()
    ai_runner = _AIRunner(Proc(["claude"], 0, _envelope("prose only, no json here"), ""))
    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="pass"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )
    assert result.used_fallback is True
    assert result.fallback_reason == "synthesis call returned unparseable output"
    _kb_lesson_exists(tmp_path, result, outcome="pass")
    assert ledger.read_records(ledger.ledger_path(cfg, tmp_path))[0].outcome == "unparseable"


def test_synthesize_lesson_falls_back_on_an_unexpected_exception(tmp_path):
    cfg = load_config()
    ai_runner = _AIRunner(raises=RuntimeError("subprocess exploded"))
    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="fail"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )
    assert result.used_fallback is True
    assert "subprocess exploded" in result.fallback_reason
    _kb_lesson_exists(tmp_path, result, outcome="fail")
    assert ledger.read_records(ledger.ledger_path(cfg, tmp_path))[0].outcome == "exception"


def test_synthesize_lesson_skips_on_a_hard_budget_breach(tmp_path):
    """A budget-exhausted block must not spend more quota just to write a
    lesson - and must still finish with a lesson on disk."""
    cfg = load_config()
    path = ledger.ledger_path(cfg, tmp_path)
    for i in range(cfg.budget["max_heavy_iterations_per_block"]):
        ledger.append_record(path, ledger.LedgerRecord(
            iteration=i, block=0, ticket=1, kind="implement", tier="heavy",
            model="opus", wall_clock_seconds=1.0, attempts=1, outcome="merged",
        ))
    ai_runner = _AIRunner(Proc(["claude"], 0, _envelope(_synthesis_json()), ""))

    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="pass"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )

    assert result.used_fallback is True
    assert "hard budget breach" in result.fallback_reason
    assert ai_runner.calls == []          # spent nothing to say so
    _kb_lesson_exists(tmp_path, result, outcome="pass")
    # no NEW ledger record was appended by the (skipped) synthesis call
    assert [r.kind for r in ledger.read_records(path)] == ["implement"] * 3


def test_synthesize_lesson_skips_when_disabled(tmp_path):
    from dataclasses import replace
    cfg = load_config()
    cfg = replace(cfg, knowledge={**cfg.knowledge, "lesson_synthesis": {"enabled": False}})
    ai_runner = _AIRunner(Proc(["claude"], 0, _envelope(_synthesis_json()), ""))

    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="pass"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )

    assert result.used_fallback is True
    assert "disabled" in result.fallback_reason
    assert ai_runner.calls == []
    _kb_lesson_exists(tmp_path, result, outcome="pass")


def test_synthesize_lesson_never_routes_to_the_heavy_tier_even_if_forced(tmp_path):
    cfg = load_config()
    ai_runner = _AIRunner(Proc(["claude"], 0, _envelope(_synthesis_json()), ""))
    # synthesis_tier() itself never returns "heavy"; assert that directly too.
    assert lessons.synthesis_tier(cfg) != "heavy"

    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="pass"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )
    assert result.tier != "heavy"


def test_dry_run_result_is_a_marked_fallback():
    result = lessons.dry_run_result("pass")
    assert result.used_fallback is True
    assert result.lesson_text() == lessons.FALLBACK_PASS
    assert "dry-run" in result.fallback_reason


# --- practice-driven citation --------------------------------------------

def test_a_practice_driven_lesson_can_cite_the_reference_project(tmp_path):
    cfg = load_config()
    ai_runner = _AIRunner(Proc(
        ["claude"], 0,
        _envelope(_synthesis_json(
            rule="Per openai/swarm's small-core practice, keep the added surface minimal.",
            references=["openai/swarm"],
        )),
        "",
    ))
    evidence = _evidence(kind="improve", outcome="pass", references=("openai/swarm",))

    result = lessons.synthesize_lesson(
        cfg, evidence, repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )

    assert result.references == ("openai/swarm",)
    assert "openai/swarm" in result.lesson_text()


# --- the fallback stays recognizable as boilerplate ------------------------

def test_a_fallback_lesson_is_byte_identical_to_the_boilerplate_phrase(tmp_path):
    cfg = load_config()
    ai_runner = _AIRunner(Proc(["claude"], 1, "", "boom"))
    result = lessons.synthesize_lesson(
        cfg, _evidence(outcome="fail"), repo_root=tmp_path, wt="/tmp/wt", block=0,
        ai_runner=ai_runner,
    )
    assert detect_boilerplate(result.lesson_text()) is True
