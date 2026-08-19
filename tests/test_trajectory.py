import json

import pytest

from hsai import trajectory
from hsai.ai import AIResult, parse_output
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


# --- raw stream capture (`--output-format stream-json`) ---------------------

def _event(obj) -> str:
    return json.dumps(obj)


# What `claude -p --output-format stream-json --verbose` actually prints: one
# JSON object per line, ending in a `result` event carrying cumulative usage.
STREAM_JSON = "\n".join([
    _event({"type": "system", "subtype": "init", "session_id": "s-42",
            "tools": ["Read", "Edit"]}),
    _event({"type": "assistant", "session_id": "s-42", "message": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Reading the module first."},
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "src/hsai/ai.py"}},
        ],
    }}),
    _event({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "def build_command(): ..."},
    ]}}),
    _event({"type": "assistant", "session_id": "s-42", "message": {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "t2", "name": "Edit",
             "input": {"file_path": "src/hsai/ai.py", "old_string": "a", "new_string": "b"}},
            {"type": "tool_use", "id": "t3", "name": "Bash",
             "input": {"command": "pytest -q"}},
        ],
    }}),
    _event({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t3", "is_error": True,
         "content": "pytest: 1 failed, 40 passed"},
    ]}}),
    _event({"type": "assistant", "session_id": "s-42", "message": {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t4", "name": "Edit",
                     "input": {"file_path": "tests/test_ai.py"}}],
    }}),
    _event({"type": "result", "subtype": "success", "is_error": False,
            "num_turns": 4, "session_id": "s-42",
            "result": "Fixed build_command and covered it with a test.",
            "usage": {"input_tokens": 2211, "output_tokens": 654}}),
])


def test_parse_stream_folds_tool_calls_files_turns_and_usage():
    s = trajectory.parse_stream(STREAM_JSON)
    assert s.empty is False
    assert s.tool_calls == {"Read": 1, "Edit": 2, "Bash": 1}
    assert s.total_tool_calls == 4
    assert s.files == ["src/hsai/ai.py", "tests/test_ai.py"]  # deduped, first-seen
    assert s.turns == 4                       # from the result event
    assert s.usage == {"input_tokens": 2211, "output_tokens": 654}
    assert s.tokens() == (2211, 654)
    assert s.session_id == "s-42"
    assert len(s.errors) == 1 and "1 failed" in s.errors[0]
    assert s.result.startswith("Fixed build_command")


def test_parse_stream_also_folds_the_legacy_single_object_envelope():
    """One parser, both CLI shapes - the legacy envelope is a one-event stream."""
    s = trajectory.parse_stream(json.dumps(MESSAGES_PAYLOAD))
    assert s.tool_calls == {"Read": 1}
    assert s.files == ["src/hsai/ai.py"]
    assert s.tokens() == (10, 4)
    assert s.turns == 2                       # no num_turns: assistant messages counted


def test_parse_stream_counts_assistant_turns_when_the_stream_is_cut_short():
    truncated = "\n".join(STREAM_JSON.splitlines()[:-1])   # no `result` event
    s = trajectory.parse_stream(truncated)
    assert s.turns == 3                        # three assistant events arrived
    assert s.usage is None and s.tokens() is None
    assert s.total_tool_calls == 4


@pytest.mark.parametrize("text", ["", "   ", "not json at all\n", "{oops", "[1, 2]"])
def test_parse_stream_degrades_to_an_empty_summary(text):
    s = trajectory.parse_stream(text)
    assert s.empty is True
    assert s.tool_calls == {} and s.files == [] and s.turns == 0
    assert s.tokens() is None


def test_parse_stream_tolerates_unknown_events_and_garbage_lines():
    """A CLI format change must cost observability, never an iteration."""
    text = "\n".join([
        _event({"type": "quantum_handshake", "payload": {"unknown": True}}),
        "<<< not json >>>",
        _event({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
        ]}}),
        _event([1, 2, 3]),                       # valid JSON, wrong shape
        _event({"type": "result", "num_turns": 1,
                "usage": {"input_tokens": 5, "output_tokens": 2}}),
    ])
    s = trajectory.parse_stream(text)
    assert s.empty is False                      # unknown events still count
    assert s.tool_calls == {"Read": 1}
    assert s.tokens() == (5, 2)


def test_parse_stream_records_an_error_result_event():
    s = trajectory.parse_stream(
        _event({"type": "result", "subtype": "error_max_turns", "is_error": True,
                "result": "hit the turn limit", "num_turns": 40})
    )
    assert s.errors and "turn limit" in s.errors[0]


# --- digest (the only committed part of a stream) ---------------------------

def test_digest_reports_tool_calls_files_turns_and_tokens():
    table = trajectory.digest(trajectory.parse_stream(STREAM_JSON))
    assert "| turns | 4 |" in table
    assert "`Edit`x2" in table and "`Read`x1" in table and "4 (" in table
    assert "`src/hsai/ai.py`" in table and "`tests/test_ai.py`" in table
    assert "| tokens | 2211 in / 654 out |" in table
    assert "| error events | 1 |" in table


def test_digest_of_an_empty_summary_says_so_instead_of_lying():
    assert "no parseable stream" in trajectory.digest(trajectory.parse_stream(""))


def test_digest_caps_the_file_list():
    files = [f"src/f{i}.py" for i in range(trajectory.DIGEST_FILES + 5)]
    summary = trajectory.TrajectorySummary(events=1, files=files)
    table = trajectory.digest(summary)
    assert "+5 more" in table
    assert f"`{files[trajectory.DIGEST_FILES]}`" not in table


def test_digest_never_quotes_run_content():
    """Committed telemetry is counters and paths, never the model's text."""
    table = trajectory.digest(trajectory.parse_stream(STREAM_JSON))
    assert "Reading the module first." not in table
    assert "def build_command()" not in table


# --- stream paths + on-disk storage -----------------------------------------

def test_stream_path_nests_a_branch_name(tmp_path):
    path = trajectory.stream_path(tmp_path, "hsai/iter-123-4-abcdef")
    assert path == tmp_path / ".hsai/trajectories/hsai/iter-123-4-abcdef.jsonl"


@pytest.mark.parametrize(
    "branch", ["", "   ", "../escape", "a/../../etc/passwd", "/abs/branch", "a//b"]
)
def test_stream_path_refuses_anything_that_could_escape_the_store(tmp_path, branch):
    assert trajectory.stream_path(tmp_path, branch) is None


def test_stream_path_honors_a_configured_directory(tmp_path):
    path = trajectory.stream_path(tmp_path, "b", ".hsai/elsewhere")
    assert path == tmp_path / ".hsai/elsewhere/b.jsonl"


def test_write_stream_round_trips_and_redacts(tmp_path):
    raw = STREAM_JSON + "\n" + _event({"type": "note", "text": "token=ghp_0123456789abcdefghij"})
    path = trajectory.write_stream(tmp_path / "nested" / "b.jsonl", raw)
    stored = trajectory.read_stream(path)
    assert "ghp_0123456789abcdefghij" not in stored
    assert REDACTED in stored
    # Redaction leaves the stream parseable - the summary survives it.
    assert trajectory.parse_stream(stored).tool_calls == {"Read": 1, "Edit": 2, "Bash": 1}


def test_write_stream_can_skip_redaction(tmp_path):
    path = trajectory.write_stream(
        tmp_path / "b.jsonl", '{"type":"note","text":"token=abc"}', redact_text=False
    )
    assert "token=abc" in trajectory.read_stream(path)


def test_write_stream_caps_the_file_at_max_bytes(tmp_path):
    lines = [_event({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": f"f{i}.py"}}
    ]}}) for i in range(4000)]
    raw = "\n".join([_event({"type": "system", "session_id": "head-marker"}), *lines])
    assert len(raw.encode()) > 20_000

    path = trajectory.write_stream(tmp_path / "big.jsonl", raw, max_bytes=20_000)

    assert path.stat().st_size <= 20_000
    stored = trajectory.read_stream(path)
    assert "head-marker" in stored                       # head kept
    assert 'file_path": "f3999.py' in stored             # tail kept
    assert "hsai_truncated" in stored                    # the splice is declared
    # Still valid JSONL: every line parses, so a capped file is still replayable.
    for line in stored.splitlines():
        json.loads(line)


def test_truncate_stream_is_a_no_op_below_the_cap():
    raw = b"a\nb\nc\n"
    assert trajectory.truncate_stream(raw, 1000) == raw
    assert trajectory.truncate_stream(raw, 0) == raw     # 0 disables the cap


def test_truncate_stream_survives_an_absurdly_small_cap():
    raw = b"x" * 5000
    assert len(trajectory.truncate_stream(raw, 10)) == 10


# --- human rendering (`hsai replay <branch>`) -------------------------------

def test_render_summary_reproduces_the_tool_call_sequence():
    out = trajectory.render_summary(
        trajectory.parse_stream(STREAM_JSON), source="/tmp/b.jsonl"
    )
    assert "/tmp/b.jsonl" in out
    assert "Read: 1" in out and "Edit: 2" in out and "Bash: 1" in out
    assert "src/hsai/ai.py" in out and "tests/test_ai.py" in out
    assert "2211 in / 654 out" in out
    assert "turns: 4" in out
    assert "1 failed" in out                              # the error is surfaced


def test_render_summary_of_an_empty_stream_is_explicit():
    assert "no parseable events" in trajectory.render_summary(trajectory.parse_stream(""))


# --- steps from a stream (the per-iteration record keeps its detail) --------

def test_steps_from_stream_keeps_the_per_turn_detail():
    steps = trajectory.steps_from_stream(STREAM_JSON)
    assert [s.kind for s in steps] == [
        "assistant", "tool_use", "tool_result", "tool_use", "tool_use",
        "tool_result", "tool_use", "result",
    ]
    assert [s.name for s in steps if s.kind == "tool_use"] == ["Read", "Edit", "Bash", "Edit"]


def test_steps_from_stream_ignores_the_single_envelope_shape():
    """The legacy path owns that shape; this one must not double-handle it."""
    assert trajectory.steps_from_stream(json.dumps(MESSAGES_PAYLOAD)) == []
    assert trajectory.steps_from_stream("plain text") == []
    assert trajectory.steps_from_stream("") == []


def test_record_keeps_the_step_stream_for_a_stream_json_run(tmp_path):
    """Enabling trajectories must not thin the per-iteration record out."""
    payload, _ = parse_output(STREAM_JSON)
    ares = AIResult(
        ok=True, model="sonnet", output=STREAM_JSON, error="", cmd=["claude"],
        usage=payload["usage"], payload=payload,
    )
    traj = trajectory.record(
        tmp_path, iteration=8, ticket=2, kind="implement", tier="standard",
        model="sonnet", prompt="do it", result=ares, block=0,
    )
    assert traj.tools_used() == ["Read", "Edit", "Bash"]
    assert traj.num_turns == 4
    assert traj.usage == {"input_tokens": 2211, "output_tokens": 654}
    assert "| tools used | `Read`, `Edit`, `Bash` |" in traj.execution_trace()
    assert traj.first_failing_step() != "none"      # the failing pytest is findable
