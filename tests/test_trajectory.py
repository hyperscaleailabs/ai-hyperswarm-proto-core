import json
from pathlib import Path

import pytest

from hsai import trajectory
from hsai.ai import AIResult
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


# --- iteration trajectories -------------------------------------------------
#
# The per-ITERATION record (what the loop decided), as opposed to the per-RUN
# record above (what one agent did). This is the granularity `hsai bench`
# replays, so it is versioned and read back strictly.

def _iter_traj(**kw) -> trajectory.IterationTrajectory:
    base = dict(
        iteration=41, block=3, ticket=264, kind="implement",
        tier="standard", model="sonnet", outcome="merged",
    )
    base.update(kw)
    return trajectory.IterationTrajectory(**base)


def test_iteration_trajectories_live_outside_the_obsidian_vault(tmp_path):
    """A raw trajectory must never land in `knowledge/` - the vault is curated."""
    path = trajectory.write_iteration(_iter_traj(), tmp_path)
    relative = path.relative_to(tmp_path)
    assert relative.parts[0] == ".hsai"
    assert "knowledge" not in relative.parts
    assert relative == Path(trajectory.ITERATION_DIR) / "41.json"


def test_iteration_trajectory_round_trips(tmp_path):
    original = trajectory.record_iteration(
        tmp_path,
        iteration=7, block=2, ticket=99, kind="heal", tier="heavy", model="opus",
        rationale="score=6 -> heavy", strategy="heuristic-v1",
        phases=[trajectory.Phase("agent", 12.5), trajectory.Phase("ci_after", 3.0)],
        wall_clock_seconds=402.25,
        prompt="Diagnose and fix the red build.",
        changed_paths=["src/hsai/ci.py", "tests/test_ci.py", "knowledge/lessons/x.md"],
        ci_local_before=trajectory.RED, ci_local=trajectory.GREEN, ci_remote="SUCCESS",
        review="approve", agent_ok=True, agent_trajectory="7",
        attempts=2, merged=True, pr=88, notes=["repro guard: reproduced"],
    )
    restored = trajectory.load_iteration(tmp_path, "7")

    assert restored == original
    assert restored.schema_version == trajectory.ITERATION_SCHEMA_VERSION
    assert restored.wall_clock_seconds == 402.25
    assert [p.name for p in restored.phases] == ["agent", "ci_after"]
    assert (restored.diff.files, restored.diff.code_files, restored.diff.knowledge_files) == (
        3, 2, 1
    )
    assert restored.ledger_ref == "block=2,iteration=7"
    assert restored.prompt_digest == trajectory.prompt_digest(
        "Diagnose and fix the red build."
    )


def test_reading_an_unsupported_schema_version_is_an_error_not_a_misread(tmp_path):
    path = trajectory.write_iteration(_iter_traj(), tmp_path)
    data = json.loads(path.read_text())
    data["schema_version"] = trajectory.ITERATION_SCHEMA_VERSION + 1
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="schema_version"):
        trajectory.read_iteration(path)


def test_an_unknown_field_from_a_newer_writer_is_dropped_not_fatal(tmp_path):
    """The versioning rule's other half: additive fields stay backwards compatible."""
    path = trajectory.write_iteration(_iter_traj(), tmp_path)
    data = json.loads(path.read_text())
    data["a_field_added_later"] = {"anything": [1, 2, 3]}
    path.write_text(json.dumps(data))
    assert trajectory.read_iteration(path).iteration == 41


def test_loading_an_absent_iteration_trajectory_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        trajectory.load_iteration(tmp_path, "404")


def test_iteration_summary_is_one_readable_line():
    traj = _iter_traj(
        phases=[trajectory.Phase("agent", 214.0)], wall_clock_seconds=310.0,
        ci_local=trajectory.GREEN, ci_remote="SUCCESS", attempts=1,
    )
    line = traj.summary()
    assert "iteration 41" in line and "[implement]" in line and "#264" in line
    assert "outcome=merged" in line and "agent=214s" in line


# --- iteration trajectory: redaction ----------------------------------------

def test_secret_env_values_picks_forbidden_and_credential_shaped_names():
    env = {
        "ANTHROPIC_API_KEY": "sk-live-abcdef",
        "GITHUB_TOKEN": "tok-abcdefgh",
        "MY_PASSWORD": "hunter2hunter2",
        "PATH": "/usr/local/bin",
        "DEBUG": "1",
        "SHORT_TOKEN": "abc",
    }
    values = trajectory.secret_env_values(["ANTHROPIC_API_KEY"], env)
    assert {"sk-live-abcdef", "tok-abcdefgh", "hunter2hunter2"} <= set(values)
    assert "/usr/local/bin" not in values     # not credential-shaped
    assert "1" not in values and "abc" not in values   # too short to be a secret
    # Longest first, so a value that contains a shorter one is blanked first.
    assert list(values) == sorted(values, key=len, reverse=True)


def test_redact_blanks_extra_literals():
    scrubbed = trajectory.redact("the operator pasted swordfish-1234 here", ["swordfish-1234"])
    assert "swordfish-1234" not in scrubbed
    assert trajectory.REDACTED in scrubbed


def test_serialized_record_carries_no_forbidden_env_value(monkeypatch):
    """The acceptance invariant: nothing from `forbid_env` reaches the record.

    The value here looks like ordinary prose, so the credential *patterns*
    cannot catch it - only knowing the live value can.
    """
    secret = "correct-horse-battery-staple-42"
    session = "sess-not-a-key-9f3c2b18"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("HSAI_SESSION_TOKEN", session)

    traj = _iter_traj(
        prompt_excerpt=f"Ticket body quoted {secret} verbatim.",
        notes=[f"child env carried {session}"],
        redacted_env=["ANTHROPIC_API_KEY"],
    )
    raw = traj.to_json()

    assert secret not in raw
    assert session not in raw               # credential-shaped NAME, also scrubbed
    assert trajectory.REDACTED in raw
    assert "ANTHROPIC_API_KEY" in json.loads(raw)["redacted_env"]   # the name is safe


def test_serialized_record_still_scrubs_credential_patterns(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    traj = _iter_traj(prompt_excerpt="export KEY=sk-ant-abcdef0123456789")
    assert "sk-ant-abcdef0123456789" not in traj.to_json()


# --- iteration trajectory: size caps ----------------------------------------

def test_record_iteration_clips_an_oversized_prompt(tmp_path):
    traj = trajectory.record_iteration(
        tmp_path, iteration=1, kind="implement", tier="standard", model="sonnet",
        outcome="merged", prompt="x" * (trajectory.PROMPT_EXCERPT_CHARS * 3),
    )
    assert len(traj.prompt_excerpt) < trajectory.PROMPT_EXCERPT_CHARS + 60
    assert traj.prompt_excerpt.endswith("chars]")


def test_record_iteration_caps_diff_paths_and_notes(tmp_path):
    traj = trajectory.record_iteration(
        tmp_path, iteration=1, kind="implement", tier="standard", model="sonnet",
        outcome="merged",
        changed_paths=[f"src/f{i:04d}.py" for i in range(trajectory.MAX_DIFF_PATHS * 2)],
        notes=[f"note {i}" for i in range(trajectory.MAX_NOTES * 3)],
    )
    # The counts stay honest even though the listing is truncated.
    assert traj.diff.files == trajectory.MAX_DIFF_PATHS * 2
    assert len(traj.diff.paths) == trajectory.MAX_DIFF_PATHS
    assert len(traj.notes) == trajectory.MAX_NOTES


def test_to_json_drops_an_oversized_prompt_excerpt_rather_than_writing_it():
    traj = _iter_traj(prompt_excerpt="x" * (trajectory.MAX_RECORD_CHARS + 1000))
    text = traj.to_json()
    assert len(text) < trajectory.MAX_RECORD_CHARS
    assert "dropped" in json.loads(text)["prompt_excerpt"]


def test_prune_iterations_keeps_only_the_newest(tmp_path):
    for i in (1, 2, 3, 10, 11):
        trajectory.write_iteration(_iter_traj(iteration=i), tmp_path)

    dropped = trajectory.prune_iterations(tmp_path, keep=2)

    assert dropped == ["1", "2", "3"]
    remaining = sorted(p.stem for p in trajectory.iteration_dir(tmp_path).glob("*.json"))
    assert remaining == ["10", "11"]


def test_prune_iterations_is_disabled_by_a_non_positive_keep(tmp_path):
    trajectory.write_iteration(_iter_traj(iteration=1), tmp_path)
    assert trajectory.prune_iterations(tmp_path, keep=0) == []
    assert trajectory.prune_iterations(tmp_path, keep=-1) == []
    assert trajectory.prune_iterations(tmp_path / "absent", keep=5) == []


# --- phase timeline ---------------------------------------------------------

def test_phase_timeline_records_each_phase_in_order():
    ticks = iter([0.0, 1.5, 4.0, 4.25])
    timeline = trajectory.PhaseTimeline(clock=lambda: next(ticks))

    timeline.mark("setup")
    timeline.mark("agent")

    assert [(p.name, p.seconds) for p in timeline.phases()] == [
        ("setup", 1.5), ("agent", 2.5)
    ]
    assert timeline.elapsed == 4.25


def test_phase_timeline_is_bounded():
    counter = iter(range(trajectory.MAX_PHASES * 4))
    timeline = trajectory.PhaseTimeline(clock=lambda: float(next(counter)))
    for i in range(trajectory.MAX_PHASES * 2):
        timeline.mark(f"phase-{i}")
    assert len(timeline.phases()) == trajectory.MAX_PHASES


def test_diff_stat_splits_code_from_knowledge():
    stat = trajectory.DiffStat.from_paths(
        ["src/hsai/bench.py", "tests/test_bench.py", "knowledge/lessons/a.md", ""]
    )
    assert (stat.files, stat.code_files, stat.knowledge_files) == (3, 2, 1)
    assert stat.paths == sorted(stat.paths)
