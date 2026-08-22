import json
import threading
from dataclasses import replace

import pytest

from hsai import trajectory
from hsai.ai import AIResult
from hsai.config import load_config
from hsai.trajectory import REDACTED, Step, Trajectory, redact, steps_from_output

MESSAGES_PAYLOAD = {
    "type": "result",
    "result": "Done: widget added.",
    "usage": {"input_tokens": 10, "output_tokens": 4},
    "messages": [
        {"role": "assistant", "content": "I will read the file first."},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Read", "input": {"path": "src/hsai/ai.py"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "content": "def build_command(): ..."}],
        },
    ],
}


def _traj(**kw) -> Trajectory:
    base = dict(
        iteration=12, ticket=7, kind="implement", tier="standard", model="sonnet",
        prompt="Implement the widget.",
        steps=[Step(index=i, kind="assistant", text=f"step {i}") for i in range(1, 9)],
    )
    base.update(kw)
    return Trajectory(**base)


# --- redaction --------------------------------------------------------------

def test_redact_scrubs_credentials():
    text = (
        "export ANTHROPIC_API_KEY=sk-ant-abcdef0123456789\n"
        "gh auth: ghp_0123456789abcdefghij\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9\n"
        "password = hunter2hunter2\n"
        "aws AKIAIOSFODNN7EXAMPLE\n"
    )
    scrubbed = redact(text)
    for secret in (
        "sk-ant-abcdef0123456789", "ghp_0123456789abcdefghij",
        "eyJhbGciOiJIUzI1NiJ9", "hunter2hunter2", "AKIAIOSFODNN7EXAMPLE",
    ):
        assert secret not in scrubbed
    assert REDACTED in scrubbed


def test_redact_leaves_ordinary_text_alone():
    assert redact("ruff check . passed, 41 tests green") == (
        "ruff check . passed, 41 tests green"
    )


# --- step extraction --------------------------------------------------------

def test_steps_from_messages_payload():
    steps = steps_from_output(MESSAGES_PAYLOAD, "")
    kinds = [s.kind for s in steps]
    assert kinds == ["assistant", "tool_use", "tool_result", "result"]
    assert [s.index for s in steps] == [1, 2, 3, 4]
    assert steps[1].name == "Read"
    assert "src/hsai/ai.py" in steps[1].text
    assert steps[3].text == "Done: widget added."


def test_steps_from_result_only_payload():
    steps = steps_from_output({"result": "all done"}, "")
    assert len(steps) == 1
    assert steps[0].kind == "result" and steps[0].text == "all done"


def test_steps_fallback_for_non_json_output():
    steps = steps_from_output(None, "plain text output\n")
    assert len(steps) == 1
    assert steps[0].kind == "output" and steps[0].text == "plain text output"
    assert steps_from_output(None, "   ") == []


def test_steps_are_redacted_at_capture():
    payload = {"result": "token=ghp_0123456789abcdefghij"}
    assert "ghp_0123456789abcdefghij" not in steps_from_output(payload, "")[0].text


def test_long_step_text_is_clipped():
    payload = {"result": "x" * (trajectory.STEP_CHARS + 500)}
    text = steps_from_output(payload, "")[0].text
    assert len(text) < trajectory.STEP_CHARS + 100
    assert "chars]" in text


# --- persistence ------------------------------------------------------------

def test_write_read_roundtrip(tmp_path):
    traj = _traj(usage={"input_tokens": 10, "output_tokens": 4}, duration_seconds=1.25)
    path = trajectory.write(traj, tmp_path)

    # One JSON artifact per run, sharded by block so pruning can drop whole
    # blocks: .hsai/traj/<block>/<iteration>.json
    assert path == tmp_path / ".hsai" / "traj" / "0" / "12.json"
    back = trajectory.read(path)
    assert back == traj
    assert back.steps[0] == Step(index=1, kind="assistant", text="step 1")
    # The stored form is a plain JSON object, inspectable without hsai.
    assert json.loads(path.read_text())["model"] == "sonnet"


def test_write_shards_by_block(tmp_path):
    path = trajectory.write(_traj(iteration=703, block=7), tmp_path)
    assert path == tmp_path / ".hsai" / "traj" / "7" / "703.json"
    assert trajectory.load(tmp_path, "703").iteration == 703


def test_identifier_is_the_iteration():
    # `hsai traj <iteration>` addresses a run; the ticket is a field, not the key.
    assert _traj(ticket=None).identifier == "12"
    assert _traj().identifier == "12"


def test_load_accepts_id_or_path(tmp_path):
    path = trajectory.write(_traj(), tmp_path)
    assert trajectory.load(tmp_path, "12").identifier == "12"
    assert trajectory.load(tmp_path, str(path)).identifier == "12"


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        trajectory.load(tmp_path, "999")


# --- redaction happens before anything reaches disk -------------------------

def test_written_artifact_has_no_home_paths_or_secrets(tmp_path):
    """Nothing identifying the machine, and no credential, survives `write()`."""
    home = "/Users/someuser/claude-projects/repo"
    secret = "sk-ant-abcdef0123456789"
    traj = _traj(
        # The prompt is the field nobody scrubs at capture time.
        prompt=f"Work only inside {home}. Do not leak.",
        steps=[
            Step(index=1, kind="tool_result", text=f"cwd={home}/src"),
            Step(index=2, kind="result", text=f"exported ANTHROPIC_API_KEY={secret}"),
        ],
        error=f"boom in {home}: token=ghp_0123456789abcdefghij",
    )
    path = trajectory.write(traj, tmp_path)
    written = path.read_text()

    assert home not in written
    assert "/Users/someuser" not in written
    assert secret not in written
    assert "ghp_0123456789abcdefghij" not in written
    # Redaction is lossy on purpose but leaves the record usable...
    assert "~/claude-projects/repo" in written
    # ...and structurally intact: it is still parseable JSON with live counters.
    back = trajectory.read(path)
    assert back.iteration == 12 and back.model == "sonnet"


def test_redaction_preserves_numeric_usage(tmp_path):
    """Token counts must survive: `input_tokens` looks secret-shaped but isn't."""
    traj = _traj(usage={"input_tokens": 1500, "output_tokens": 320})
    stored = json.loads(trajectory.write(traj, tmp_path).read_text())
    assert stored["usage"] == {"input_tokens": 1500, "output_tokens": 320}


def test_redact_strips_absolute_home_paths():
    assert redact("see /Users/alice/x/y.py") == "see ~/x/y.py"
    assert redact("see /home/bob/x") == "see ~/x"
    assert redact("relative/path/ok") == "relative/path/ok"


# --- retention: the store stays bounded -------------------------------------

def test_prune_drops_the_oldest_block_dirs(tmp_path):
    for block in range(6):
        trajectory.write(_traj(iteration=block * 100 + 1, block=block), tmp_path)

    dropped = trajectory.prune(tmp_path, keep_blocks=2)

    assert dropped == [0, 1, 2, 3]
    kept = sorted(p.name for p in trajectory.trajectory_dir(tmp_path).iterdir())
    assert kept == ["4", "5"]
    assert trajectory.load(tmp_path, "501").block == 5


def test_prune_is_a_noop_when_disabled_or_empty(tmp_path):
    trajectory.write(_traj(), tmp_path)
    assert trajectory.prune(tmp_path, keep_blocks=0) == []
    assert trajectory.prune(tmp_path, keep_blocks=9) == []
    assert trajectory.prune(tmp_path / "nothing-here", keep_blocks=2) == []


# --- digest (the compact line the lesson and PR body carry) -----------------

def test_digest_reports_tokens_duration_and_exit_status():
    traj = _traj(
        usage={"input_tokens": 1500, "output_tokens": 320},
        duration_seconds=42.5, exit_status="ok", outcome="merged",
    )
    digest = traj.digest()
    assert "tokens=1500in/320out" in digest
    assert "duration=42.5s" in digest
    assert "exit=ok" in digest
    assert "outcome=merged" in digest
    assert "hsai traj 12" in digest


def test_digest_without_usage_says_so():
    assert "tokens=unreported" in _traj(usage=None).digest()


def test_digest_points_at_the_first_failing_step():
    traj = _traj(steps=[
        Step(index=1, kind="assistant", text="reading the file"),
        Step(index=2, kind="tool_result", text="pytest: 1 failed, 40 passed"),
        Step(index=3, kind="tool_result", text="Traceback (most recent call last)"),
    ])
    assert "first-failing-step=step 2 (tool_result)" in traj.digest()
    assert "first-failing-step=none" in _traj(
        steps=[Step(index=1, kind="result", text="all green")]
    ).digest()


def test_record_builds_from_an_ai_result(tmp_path):
    ares = AIResult(
        ok=False, model="sonnet", output=json.dumps(MESSAGES_PAYLOAD),
        error="boom: token=ghp_0123456789abcdefghij", cmd=["claude"],
        usage=MESSAGES_PAYLOAD["usage"], payload=MESSAGES_PAYLOAD,
    )
    traj = trajectory.record(
        tmp_path, iteration=3, ticket=None, kind="heal", tier="heavy", model="opus",
        prompt="fix it", result=ares, block=0, duration_seconds=2.5,
    )
    assert traj.exit_status == "error" and traj.ok is False
    assert traj.duration_seconds == 2.5
    assert traj.usage == {"input_tokens": 10, "output_tokens": 4}
    assert "ghp_0123456789abcdefghij" not in traj.error  # errors are scrubbed too
    assert traj.prompt_digest == trajectory.prompt_digest("fix it")
    assert trajectory.path_for(tmp_path, "3", 0).is_file()


def test_record_captures_num_turns_when_exposed(tmp_path):
    payload = dict(MESSAGES_PAYLOAD, num_turns=3)
    ares = AIResult(
        ok=True, model="sonnet", output=json.dumps(payload), error="",
        cmd=["claude"], usage=payload["usage"], payload=payload,
    )
    traj = trajectory.record(
        tmp_path, iteration=6, ticket=1, kind="implement", tier="standard",
        model="sonnet", prompt="do it", result=ares, block=0,
    )
    assert traj.num_turns == 3
    # A plain-text run exposes no `num_turns` - unavailable, not zero.
    plain = AIResult(ok=True, model="sonnet", output="done", error="", cmd=["claude"])
    assert trajectory.record(
        tmp_path, iteration=7, ticket=1, kind="implement", tier="standard",
        model="sonnet", prompt="do it", result=plain, block=0,
    ).num_turns is None


def test_record_captures_the_session_id_when_exposed(tmp_path):
    payload = dict(MESSAGES_PAYLOAD, session_id="b2f0e1d4")
    ares = AIResult(
        ok=True, model="sonnet", output=json.dumps(payload), error="",
        cmd=["claude"], usage=payload["usage"], payload=payload,
    )
    traj = trajectory.record(
        tmp_path, iteration=4, ticket=1, kind="implement", tier="standard",
        model="sonnet", prompt="do it", result=ares, block=0,
    )
    assert traj.session_id == "b2f0e1d4"
    # A plain-text run simply has none - not a crash.
    plain = AIResult(ok=True, model="sonnet", output="done", error="", cmd=["claude"])
    assert trajectory.record(
        tmp_path, iteration=5, ticket=1, kind="implement", tier="standard",
        model="sonnet", prompt="do it", result=plain, block=0,
    ).session_id == ""


# --- execution trace (the '## Execution trace' section in the lesson) ------

def test_tools_used_lists_distinct_tool_names_in_first_seen_order():
    steps = steps_from_output(MESSAGES_PAYLOAD, "")
    traj = _traj(steps=steps)
    assert traj.tools_used() == ["Read"]


def test_tools_used_is_empty_for_plain_text_output():
    assert _traj(steps=steps_from_output(None, "plain output")).tools_used() == []


def test_execution_trace_reports_turns_tools_tokens_exit_and_duration():
    traj = _traj(
        steps=steps_from_output(MESSAGES_PAYLOAD, ""),
        usage={"input_tokens": 1500, "output_tokens": 320},
        num_turns=3, duration_seconds=12.4, exit_status="ok",
    )
    trace = traj.execution_trace()
    assert "| turns | 3 |" in trace
    assert "| tools used | `Read` |" in trace
    assert "| tokens | 1500 in / 320 out |" in trace
    assert "| exit status | ok |" in trace
    assert "| duration | 12.4s |" in trace
    assert "| telemetry | ok |" in trace
    assert "hsai traj 12" in trace


def test_execution_trace_reports_telemetry_unavailable_without_usage():
    trace = _traj(usage=None, num_turns=None).execution_trace()
    assert "| tokens | unavailable |" in trace
    assert "| telemetry | unavailable |" in trace
    assert "| turns | unavailable |" in trace
    assert "| tools used | _(none recorded)_ |" in trace


# --- excerpt (what the committed lesson may quote) --------------------------

def test_excerpt_is_a_redacted_tail_only():
    excerpt = _traj().excerpt(steps=3)
    assert "step 8" in excerpt and "step 6" in excerpt
    assert "step 1" not in excerpt                  # earlier steps stay local
    assert "3 earlier step(s) elided" not in excerpt  # 8 - 3 = 5 elided
    assert "5 earlier step(s) elided" in excerpt
    assert "Implement the widget." not in excerpt   # never the prompt


def test_excerpt_scrubs_secrets():
    traj = _traj(steps=[Step(index=1, kind="output", text="key=sk-ant-abcdef0123456789")])
    assert "sk-ant-abcdef0123456789" not in traj.excerpt()


def test_excerpt_without_steps():
    assert _traj(steps=[]).excerpt() == "(no steps recorded)"


# --- human rendering (hsai traj / hsai replay) ------------------------------

def test_render_shows_prompt_steps_and_usage():
    out = _traj(usage={"input_tokens": 10, "output_tokens": 4}, outcome="merged").render()
    assert "trajectory 12" in out and "ticket #7" in out
    assert "--- prompt ---" in out and "Implement the widget." in out
    assert "--- steps (8) ---" in out and "step 1" in out and "step 8" in out
    assert "input_tokens=10" in out and "output_tokens=4" in out
    assert "outcome: merged" in out


def test_render_reports_missing_usage():
    assert "usage: (not reported)" in _traj(usage=None).render()


def test_render_and_trace_name_the_failure_class_when_there_is_one():
    traj = _traj(outcome="recovered", failure_class="timeout")
    assert "failure: timeout" in traj.render()
    assert "| failure class | `timeout` |" in traj.execution_trace()
    # A run that ended well says nothing at all - the row is not rendered empty.
    assert "failure class" not in _traj().execution_trace()


# --- the committed run index (knowledge/trajectories/*.jsonl) ----------------

def test_index_path_is_config_driven_and_sharded_by_block(tmp_path):
    cfg = load_config()
    path = trajectory.index_path(cfg, tmp_path, 41)
    assert path == tmp_path / "knowledge" / "trajectories" / "block-41.jsonl"
    assert trajectory.index_path(cfg, tmp_path, 42).name == "block-42.jsonl"

    moved = replace(cfg, knowledge={**cfg.knowledge, "trajectory_dir": "knowledge/runs"})
    assert trajectory.index_dir(moved, tmp_path) == tmp_path / "knowledge" / "runs"


def test_index_run_appends_one_valid_json_line_per_run(tmp_path):
    cfg = load_config()
    for i in (1, 2, 3):
        trajectory.index_run(
            cfg, tmp_path, _traj(iteration=i, block=4, outcome="merged"),
            guards={"completeness": "ok"}, remote_ci="SUCCESS",
        )

    path = trajectory.index_path(cfg, tmp_path, 4)
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:                       # JSONL: one valid object per line
        assert isinstance(json.loads(line), dict)

    runs = trajectory.read_runs(path)
    assert [r.iteration for r in runs] == [1, 2, 3]
    assert runs[0].guards == {"completeness": "ok"}
    assert runs[0].remote_ci == "SUCCESS"
    assert runs[0].prompt_digest == trajectory.prompt_digest("Implement the widget.")
    assert runs[0].steps == 8


def test_index_is_append_only(tmp_path):
    """A second write never rewrites or truncates what the first one recorded."""
    cfg = load_config()
    trajectory.index_run(cfg, tmp_path, _traj(iteration=1, block=0))
    path = trajectory.index_path(cfg, tmp_path, 0)
    first = path.read_text()

    trajectory.index_run(cfg, tmp_path, _traj(iteration=2, block=0))
    after = path.read_text()

    assert after.startswith(first)
    assert len(after.splitlines()) == 2


def test_concurrent_appends_never_interleave_a_partial_line(tmp_path):
    """The lock is what keeps parallel workers from corrupting the index."""
    cfg = load_config()
    path = trajectory.index_path(cfg, tmp_path, 9)

    def append(i: int) -> None:
        trajectory.index_run(
            cfg, tmp_path, _traj(iteration=i, block=9, steps=[
                Step(index=1, kind="output", text="x" * 500)
            ]),
        )

    threads = [threading.Thread(target=append, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = path.read_text().splitlines()
    assert len(lines) == 24
    assert sorted(json.loads(line)["iteration"] for line in lines) == list(range(24))


def test_stored_transcript_is_capped_at_the_configured_size(tmp_path):
    cfg = replace(
        load_config(),
        knowledge={**load_config().knowledge, "trajectory_transcript_chars": 200},
    )
    traj = _traj(steps=[Step(index=i, kind="output", text="y" * 400) for i in range(1, 12)])
    trajectory.index_run(cfg, tmp_path, traj)

    run = trajectory.read_runs(trajectory.index_path(cfg, tmp_path, 0))[0]
    assert 0 < len(run.transcript) <= 200
    assert run.steps == 11                 # the count survives; the text does not


def test_transcript_tail_keeps_the_end_and_says_what_it_dropped():
    traj = _traj(steps=[
        Step(index=1, kind="output", text="A" * 300),
        Step(index=2, kind="output", text="OMEGA"),
    ])
    tail = traj.transcript_tail(120)
    assert len(tail) <= 120
    assert "OMEGA" in tail                 # a tail, not a head
    assert "chars total; tail only" in tail  # and it says so, never silently
    # Under the cap, nothing is dropped and no marker is added.
    short = _traj(steps=[Step(index=1, kind="output", text="tiny")]).transcript_tail(120)
    assert short.endswith("tiny") and "tail only" not in short
    assert _traj().transcript_tail(0) == ""


def test_index_lines_are_redacted_like_every_other_artifact(tmp_path):
    cfg = load_config()
    traj = _traj(steps=[
        Step(index=1, kind="output", text="export ANTHROPIC_API_KEY=sk-ant-abcdef0123456789")
    ])
    trajectory.index_run(cfg, tmp_path, traj)

    text = trajectory.index_path(cfg, tmp_path, 0).read_text()
    assert "sk-ant-abcdef0123456789" not in text
    assert REDACTED in text


def test_index_records_tokens_and_failure_class(tmp_path):
    cfg = load_config()
    trajectory.index_run(cfg, tmp_path, _traj(
        block=2, usage={"input_tokens": 1500, "output_tokens": 320},
        outcome="recovered", failure_class="timeout",
    ))
    run = trajectory.read_runs(trajectory.index_path(cfg, tmp_path, 2))[0]
    assert (run.input_tokens, run.output_tokens) == (1500, 320)
    assert run.failure_class == "timeout"
    assert run.outcome == "recovered"

    # A run whose CLI reported nothing keeps null token columns rather than 0.
    trajectory.index_run(cfg, tmp_path, _traj(iteration=13, block=2, usage=None))
    plain = trajectory.read_runs(trajectory.index_path(cfg, tmp_path, 2))[1]
    assert plain.input_tokens is None and plain.output_tokens is None


def test_read_runs_of_a_missing_index_is_empty(tmp_path):
    cfg = load_config()
    assert trajectory.read_runs(tmp_path / "nope.jsonl") == []
    assert trajectory.read_block_runs(cfg, tmp_path, 3) == []


def test_build_does_not_write_to_the_local_store(tmp_path):
    """The reviewer shares the worker's iteration id, so it indexes only."""
    ares = AIResult(ok=True, model="haiku", output="ok", error="", cmd=["claude"])
    traj = trajectory.build(
        iteration=8, ticket=7, kind="review", tier="light", model="haiku",
        prompt="grade it", result=ares, block=1, outcome="approve",
    )
    assert traj.kind == "review" and traj.outcome == "approve"
    assert trajectory.find(tmp_path, "8") is None
