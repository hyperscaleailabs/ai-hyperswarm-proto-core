import json
import subprocess
from dataclasses import replace

from hsai import ai
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.proc import Proc

CHOICE = ModelChoice(tier="standard", model="sonnet", rationale="test")

# What `claude -p --output-format json` actually prints on a successful run.
CLAUDE_JSON_PAYLOAD = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 4321,
        "num_turns": 3,
        "result": "Added the widget and a test for it.",
        "session_id": "b2f0e1d4",
        "total_cost_usd": 0.0,
        "usage": {
            "input_tokens": 1200,
            "cache_creation_input_tokens": 900,
            "cache_read_input_tokens": 3400,
            "output_tokens": 345,
        },
    }
)


def _runner(stdout: str, code: int = 0, stderr: str = ""):
    calls: list[list[str]] = []

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        return Proc(cmd, code, stdout, stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _cfg(**overrides):
    """The real config, with `execution.*` overrides applied."""
    cfg = load_config()
    return replace(cfg, **overrides)


def _traj(**overrides):
    """The real config with `execution.trajectories.*` overrides applied."""
    cfg = load_config()
    return replace(cfg, trajectories=replace(cfg.trajectories, **overrides))


def test_build_command_requests_a_structured_envelope():
    cfg = load_config()
    cmd = ai.build_command("do the thing", CHOICE, cfg)
    # The structured envelope is what makes usage + trajectories possible.
    assert "--output-format" in cmd
    assert cmd[:3] == ["claude", "-p", "do the thing"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_build_command_streams_json_when_trajectories_are_enabled():
    """Acceptance criterion: the enabled branch asks for the event stream."""
    cmd = ai.build_command("x", CHOICE, _traj(enabled=True))
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    # `-p` + stream-json is rejected by the CLI unless --verbose is passed.
    assert "--verbose" in cmd


def test_build_command_omits_stream_flags_when_trajectories_are_disabled():
    """...and the disabled branch falls back to the single-object envelope."""
    cmd = ai.build_command("x", CHOICE, _traj(enabled=False))
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "stream-json" not in cmd
    assert "--verbose" not in cmd


def test_output_format_flag_is_config_driven():
    # The shipped config asks for json, upgraded to stream-json by the
    # trajectory recorder...
    cfg = load_config()
    assert cfg.output_format == "json"
    assert cfg.trajectories.enabled is True
    assert ai.resolve_output_format(cfg) == "stream-json"

    # ...and with trajectories off the flag follows the key rather than a
    # hardcoded literal, so a `claude` CLI change is a YAML edit, not a code
    # change.
    off = replace(cfg, trajectories=replace(cfg.trajectories, enabled=False))
    cmd = ai.build_command("x", CHOICE, replace(off, output_format="stream-json"))
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd
    assert "--verbose" not in ai.build_command("x", CHOICE, off)


def test_build_command_drops_the_flag_for_plain_text():
    # The escape hatch: a broken flag can never brick the loop.
    for fmt in ("text", ""):
        cmd = ai.build_command("x", CHOICE, _cfg(output_format=fmt))
        assert "--output-format" not in cmd
        assert cmd[:3] == ["claude", "-p", "x"]


def test_parse_output_extracts_payload_and_usage():
    raw, usage = ai.parse_output(CLAUDE_JSON_PAYLOAD)
    assert raw is not None and raw["result"].startswith("Added the widget")
    assert usage == {
        "input_tokens": 1200,
        "cache_creation_input_tokens": 900,
        "cache_read_input_tokens": 3400,
        "output_tokens": 345,
    }


def test_parse_output_degrades_on_non_json():
    # An older `claude` binary (or a crash) prints plain text: degrade, never raise.
    assert ai.parse_output("ok\n") == (None, None)
    assert ai.parse_output("") == (None, None)
    assert ai.parse_output("{not json") == (None, None)
    assert ai.parse_output(json.dumps([1, 2])) == (None, None)
    # Valid JSON without a usage object still yields the payload.
    raw, usage = ai.parse_output(json.dumps({"result": "hi"}))
    assert raw == {"result": "hi"} and usage is None


def test_run_agent_populates_usage_and_payload():
    cfg = load_config()
    result = ai.run_agent("prompt", CHOICE, cfg, runner=_runner(CLAUDE_JSON_PAYLOAD))
    assert result.ok is True
    assert result.usage is not None and result.usage["output_tokens"] == 345
    assert result.payload is not None
    assert result.session_id == "b2f0e1d4"
    assert result.text == "Added the widget and a test for it."
    assert result.output == CLAUDE_JSON_PAYLOAD  # raw stdout is preserved


def test_run_agent_plain_text_fallback():
    cfg = load_config()
    result = ai.run_agent("prompt", CHOICE, cfg, runner=_runner("plain output\n"))
    assert result.payload is None and result.usage is None
    assert result.session_id == ""
    assert result.text == "plain output\n"  # falls back to the raw stdout


# --- stream-json: the shape a real (trajectory-enabled) iteration prints ----

CLAUDE_STREAM_PAYLOAD = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "session_id": "b2f0e1d4"}),
    json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/hsai/widget.py"}},
    ]}}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False,
                "num_turns": 3, "session_id": "b2f0e1d4",
                "result": "Added the widget and a test for it.",
                "usage": {"input_tokens": 1200, "output_tokens": 345}}),
])


def test_parse_output_lifts_the_result_event_out_of_a_stream():
    """stream-json is JSONL, so the whole-text parse fails - lift `result` out."""
    raw, usage = ai.parse_output(CLAUDE_STREAM_PAYLOAD)
    assert raw is not None and raw["result"].startswith("Added the widget")
    assert raw["session_id"] == "b2f0e1d4"
    assert usage == {"input_tokens": 1200, "output_tokens": 345}


def test_parse_output_degrades_on_a_stream_without_a_result_event():
    partial = "\n".join(CLAUDE_STREAM_PAYLOAD.splitlines()[:-1])
    assert ai.parse_output(partial) == (None, None)


def test_run_agent_summarizes_the_stream():
    result = ai.run_agent("prompt", CHOICE, load_config(),
                          runner=_runner(CLAUDE_STREAM_PAYLOAD))
    assert result.summary is not None
    assert result.summary.tool_calls == {"Edit": 1}
    assert result.summary.files == ["src/hsai/widget.py"]
    assert result.summary.turns == 3
    # Every downstream consumer keeps working on the stream shape.
    assert result.text == "Added the widget and a test for it."
    assert result.session_id == "b2f0e1d4"
    assert result.usage == {"input_tokens": 1200, "output_tokens": 345}


def test_run_agent_stores_the_raw_stream_for_a_branch(tmp_path):
    result = ai.run_agent(
        "prompt", CHOICE, load_config(), runner=_runner(CLAUDE_STREAM_PAYLOAD),
        branch="hsai/iter-1-2-abc", repo_root=str(tmp_path),
    )
    path = tmp_path / ".hsai/trajectories/hsai/iter-1-2-abc.jsonl"
    assert path.is_file()
    assert result.trajectory_path == str(path)
    assert path.read_text() == CLAUDE_STREAM_PAYLOAD   # verbatim, nothing to redact


def test_run_agent_stores_nothing_without_a_branch(tmp_path):
    result = ai.run_agent(
        "prompt", CHOICE, load_config(), runner=_runner(CLAUDE_STREAM_PAYLOAD),
        repo_root=str(tmp_path),
    )
    assert result.trajectory_path == ""
    assert not (tmp_path / ".hsai").exists()


def test_run_agent_skips_the_stream_when_trajectories_are_disabled(tmp_path):
    result = ai.run_agent(
        "prompt", CHOICE, _traj(enabled=False), runner=_runner(CLAUDE_STREAM_PAYLOAD),
        branch="hsai/iter-1-2-abc", repo_root=str(tmp_path),
    )
    assert result.summary is None and result.trajectory_path == ""
    assert not (tmp_path / ".hsai").exists()


def test_run_agent_survives_an_unwritable_trajectory_store(tmp_path, monkeypatch):
    """Observability is additive: a full disk costs a trajectory, not a run."""
    def boom(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr("hsai.trajectory.write_stream", boom)
    result = ai.run_agent(
        "prompt", CHOICE, load_config(), runner=_runner(CLAUDE_STREAM_PAYLOAD),
        branch="hsai/iter-1-2-abc", repo_root=str(tmp_path),
    )
    assert result.ok is True and result.trajectory_path == ""
    assert result.summary is not None and result.summary.turns == 3


def test_run_agent_summary_is_empty_not_none_on_garbage(tmp_path):
    """Callers branch on `.empty`, so a broken CLI must not hand them None."""
    result = ai.run_agent("prompt", CHOICE, load_config(), runner=_runner("<<< junk"))
    assert result.summary is not None and result.summary.empty is True


def test_run_agent_strips_billing_env(monkeypatch):
    """Acceptance criterion #1: a capturing fake runner never sees the key."""
    cfg = load_config()
    seen: dict[str, object] = {}

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        seen["env"] = dict(env or {})
        seen["env_remove"] = tuple(env_remove or ())
        return Proc(cmd, 0, CLAUDE_JSON_PAYLOAD, "")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-stripped")
    ai.run_agent("prompt", CHOICE, cfg, runner=runner)
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    # ...and the runner was actually TOLD to remove it, not just left it out of
    # an override map (which is what let the real leak through proc.run).
    assert "ANTHROPIC_API_KEY" in seen["env_remove"]


def test_sanitized_env_reports_removals_even_when_not_in_os_environ():
    """`removals` names what MUST be absent - independent of the parent's own
    environment, so proc.run can enforce it whether or not the key is set."""
    cfg = load_config()
    env, removals = ai._sanitized_env(cfg)
    assert "ANTHROPIC_API_KEY" in removals
    assert "ANTHROPIC_API_KEY" not in env


def test_run_agent_env_leak_is_blocked_at_the_real_runner(monkeypatch):
    """Regression pin for the actual defect.

    Unlike the fake-runner test above, this goes through the DEFAULT runner
    (`hsai.proc.run`, the real subprocess wrapper) - only `subprocess.run`
    itself is mocked, so it exercises proc.run's env-merge logic. Before
    proc.run supported `env_remove`, `full_env.update(env)` could never
    remove a key `_sanitized_env` had merely omitted, and this test would
    catch that: revert only the proc.run fix and it goes red.
    """
    cfg = load_config()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-stripped")
    captured: dict[str, dict] = {}

    def fake_subprocess_run(cmd, *, cwd, env, input, capture_output, text, timeout):
        captured["env"] = dict(env or {})
        return subprocess.CompletedProcess(cmd, 0, CLAUDE_JSON_PAYLOAD, "")

    monkeypatch.setattr("hsai.proc.subprocess.run", fake_subprocess_run)
    ai.run_agent("prompt", CHOICE, cfg)  # default runner = the real hsai.proc.run
    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_check_child_env_passes_with_a_real_spawned_child(monkeypatch):
    """The live doctor check actually spawns a process and reads its env back."""
    cfg = load_config()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-stripped")
    ok, msg = ai.check_child_env(cfg)
    assert ok is True
    assert "ANTHROPIC_API_KEY" in msg


def test_check_child_env_fails_when_a_key_leaks():
    cfg = load_config()

    def leaking_runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None,
                        input_text=None):
        return Proc(cmd, 0, "ANTHROPIC_API_KEY", "")

    ok, msg = ai.check_child_env(cfg, runner=leaking_runner)
    assert ok is False
    assert "ANTHROPIC_API_KEY" in msg


def test_check_child_env_passes_trivially_with_nothing_configured():
    cfg = replace(load_config(), constraints={"subscription_only": False, "forbid_env": []})
    ok, msg = ai.check_child_env(cfg)
    assert ok is True
    assert "no forbidden variables" in msg
