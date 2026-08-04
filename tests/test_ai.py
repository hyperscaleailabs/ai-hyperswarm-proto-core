import json
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

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        return Proc(cmd, code, stdout, stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _cfg(**overrides):
    """The real config, with `execution.*` overrides applied."""
    cfg = load_config()
    return replace(cfg, **overrides)


def test_build_command_requests_json_output():
    cfg = load_config()
    cmd = ai.build_command("do the thing", CHOICE, cfg)
    # The structured envelope is what makes usage + trajectories possible.
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[:3] == ["claude", "-p", "do the thing"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_output_format_flag_is_config_driven():
    # The shipped config asks for json...
    assert load_config().output_format == "json"

    # ...and the flag follows the key rather than a hardcoded literal, so a
    # `claude` CLI change is a YAML edit, not a code change.
    cmd = ai.build_command("x", CHOICE, _cfg(output_format="stream-json"))
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    # `-p` + stream-json is rejected by the CLI unless --verbose is passed.
    assert "--verbose" in cmd
    assert "--verbose" not in ai.build_command("x", CHOICE, _cfg(output_format="json"))


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


def test_run_agent_strips_billing_env(monkeypatch):
    cfg = load_config()
    seen: dict[str, dict] = {}

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        seen["env"] = dict(env or {})
        return Proc(cmd, 0, CLAUDE_JSON_PAYLOAD, "")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-stripped")
    ai.run_agent("prompt", CHOICE, cfg, runner=runner)
    assert "ANTHROPIC_API_KEY" not in seen["env"]
