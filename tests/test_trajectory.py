import json

import pytest

from hsai import trajectory
from hsai.ai import AIResult
from hsai.trajectory import (
    REDACTED,
    IterationTrajectory,
    Phase,
    Step,
    Trajectory,
    redact,
    steps_from_output,
)

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


# --- iteration trajectories -------------------------------------------------
#
# The other granularity: one record per loop iteration, describing what the
# HARNESS decided rather than what the model typed.


def _itraj(**kw) -> IterationTrajectory:
    base = dict(
        iteration=12, block=0, ticket=7, kind="implement", tier="standard",
        model="sonnet", rationale="score=-1 -> standard", attempts=1,
        local_ci_before="pass", local_ci_after="pass", review="approve",
        remote_ci="SUCCESS", merged=True, outcome="merged",
    )
    base.update(kw)
    return IterationTrajectory(**base)


def test_iteration_record_is_schema_versioned(tmp_path):
    path = trajectory.write_iteration(_itraj(), tmp_path)
    assert path == tmp_path / ".hsai" / "trajectories" / "0" / "12.json"
    assert json.loads(path.read_text())["schema_version"] == trajectory.SCHEMA_VERSION
    assert trajectory.read_iteration(path) == _itraj()


def test_iteration_record_lives_outside_the_obsidian_vault(tmp_path):
    """Trajectories are operational forensics, not repo content: a vault that
    filled up with them would poison every recall and whitepaper."""
    trajectory.write_iteration(_itraj(), tmp_path)
    assert not (tmp_path / "knowledge").exists()
    assert trajectory.ITERATION_DIR.startswith(".hsai/")


def test_reading_a_foreign_schema_version_raises(tmp_path):
    path = trajectory.write_iteration(_itraj(), tmp_path)
    stored = json.loads(path.read_text())
    stored["schema_version"] = trajectory.SCHEMA_VERSION + 1
    path.write_text(json.dumps(stored))
    with pytest.raises(ValueError, match="schema_version"):
        trajectory.read_iteration(path)


def test_iteration_paths_lists_every_stored_record(tmp_path):
    trajectory.write_iteration(_itraj(iteration=1, block=0), tmp_path)
    trajectory.write_iteration(_itraj(iteration=201, block=2), tmp_path)
    assert [p.name for p in trajectory.iteration_paths(tmp_path)] == ["1.json", "201.json"]
    assert trajectory.iteration_paths(tmp_path / "empty") == []


def test_prune_iterations_bounds_the_store(tmp_path):
    for block in range(5):
        trajectory.write_iteration(_itraj(iteration=block * 100 + 1, block=block), tmp_path)
    assert trajectory.prune_iterations(tmp_path, keep_blocks=2) == [0, 1, 2]
    assert len(trajectory.iteration_paths(tmp_path)) == 2


def test_describe_is_one_auditable_line():
    line = _itraj().describe()
    assert "iteration 12" in line and "ticket=7" in line
    assert "tier=standard" in line and "outcome=merged" in line
    assert "ci=pass->pass" in line and "remote=SUCCESS" in line


# --- redaction: no environment value may reach a record ---------------------

def test_no_forbidden_env_value_survives_serialization(tmp_path, monkeypatch):
    """The acceptance invariant: a record may quote neither a value of
    `constraints.forbid_env` nor any credential-shaped environment value."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-zzz-super-secret-value")
    monkeypatch.setenv("GH_TOKEN", "ghp_thisisafaketokenvalue000")
    monkeypatch.setenv("DEPLOY_PASSWORD", "correct-horse-battery-staple")
    # Named nowhere and shaped like nothing: only the forbid_env list covers it.
    monkeypatch.setenv("CUSTOM_FORBIDDEN", "opaque-value-nobody-can-pattern-match")

    leaky = _itraj(
        prompt_excerpt=(
            "run with ANTHROPIC_API_KEY=sk-ant-zzz-super-secret-value and "
            "GH_TOKEN=ghp_thisisafaketokenvalue000"
        ),
        notes=[
            "deploy used correct-horse-battery-staple",
            "and opaque-value-nobody-can-pattern-match",
        ],
        rationale="cwd=/Users/someuser/repo",
    )
    written = trajectory.write_iteration(
        leaky, tmp_path, forbid_env=("ANTHROPIC_API_KEY", "CUSTOM_FORBIDDEN"),
    ).read_text()

    for secret in (
        "sk-ant-zzz-super-secret-value",
        "ghp_thisisafaketokenvalue000",
        "correct-horse-battery-staple",
        "opaque-value-nobody-can-pattern-match",
        "/Users/someuser",
    ):
        assert secret not in written, f"{secret!r} leaked into the record"
    assert REDACTED in written
    # Still a usable record: counters and identifiers survive intact.
    assert json.loads(written)["iteration"] == 12


def test_env_secret_values_ignores_short_and_absent_values(monkeypatch):
    monkeypatch.setenv("TINY_TOKEN", "abc")  # too short to substitute safely
    monkeypatch.setenv("REAL_TOKEN", "a-long-enough-token-value")
    values = trajectory.env_secret_values(("NOT_SET_ANYWHERE",))
    assert "a-long-enough-token-value" in values
    assert "abc" not in values
    # Longest first, so a value containing another is replaced before it.
    assert list(values) == sorted(values, key=len, reverse=True)


def test_redact_env_values_are_substituted_literally():
    text = "the key is opaque-but-secret and nothing about it looks secret"
    assert "opaque-but-secret" not in redact(text, env_values=("opaque-but-secret",))
    assert redact(text) == text  # without the value, nothing to match on


# --- size cap ---------------------------------------------------------------

def test_oversized_record_is_capped_not_corrupted(tmp_path):
    huge = _itraj(notes=["x" * 5000 for _ in range(50)])
    written = trajectory.write_iteration(huge, tmp_path).read_text()
    assert len(written.encode("utf-8")) <= trajectory.MAX_RECORD_BYTES
    # Trimmed, still parseable, and honest about what it dropped.
    assert json.loads(written)["notes"] == ["[record truncated: exceeded size cap]"]


def test_note_and_prompt_are_clipped_at_capture():
    traj = IterationTrajectory(iteration=1, block=0)
    traj.set_prompt("p" * (trajectory.PROMPT_CHARS + 500))
    traj.note("n" * (trajectory.NOTE_CHARS + 500))
    assert len(traj.prompt_excerpt) < trajectory.PROMPT_CHARS + 100
    assert len(traj.notes[0]) < trajectory.NOTE_CHARS + 100
    assert traj.prompt_hash == trajectory.prompt_digest("p" * (trajectory.PROMPT_CHARS + 500))

    for i in range(trajectory.MAX_NOTES + 10):
        traj.note(f"note {i}")
    assert len(traj.notes) == trajectory.MAX_NOTES


# --- phase timeline ---------------------------------------------------------

def test_phase_timer_records_a_span_per_mark():
    clock = iter([0.0, 1.5, 4.0, 4.25]).__next__
    timer = trajectory.PhaseTimer(clock=clock)
    timer.mark("agent")
    timer.mark("ci_after")
    timer.mark("review")
    assert [(p.name, p.seconds) for p in timer.phases] == [
        ("agent", 1.5), ("ci_after", 2.5), ("review", 0.25)
    ]
    assert timer.total() == 4.25


def test_phase_timer_accumulates_a_repeated_name():
    """A guard that re-runs a phase must not duplicate it in the timeline."""
    clock = iter([0.0, 1.0, 2.0, 5.0]).__next__
    timer = trajectory.PhaseTimer(clock=clock)
    timer.mark("guards")
    timer.mark("ci_after")
    timer.mark("guards")
    assert [p.name for p in timer.phases] == ["guards", "ci_after"]
    assert timer.phase_lookup() if False else timer.phases[0].seconds == 4.0


def test_phase_seconds_reads_the_timeline():
    traj = _itraj(phases=[Phase(name="agent", seconds=12.5), Phase(name="review", seconds=1.5)])
    assert traj.phase_seconds("agent") == 12.5
    assert traj.phase_seconds("never-ran") == 0.0
    assert traj.total_phase_seconds() == 14.0


# --- diff stat --------------------------------------------------------------

def test_diff_stat_counts_by_category():
    assert trajectory.diff_stat([
        "src/hsai/bench.py",
        "tests/test_bench.py",
        "knowledge/lessons/2026-08-16-note.md",
        ".github/workflows/ci.yml",
    ]) == {"files": 4, "code": 3, "knowledge": 1, "tests": 1, "workflows": 1}


def test_diff_stat_of_an_empty_diff():
    assert trajectory.diff_stat([]) == {
        "files": 0, "code": 0, "knowledge": 0, "tests": 0, "workflows": 0
    }
