import json

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

    assert path == tmp_path / ".hsai" / "trajectories" / "12-7.json"
    back = trajectory.read(path)
    assert back == traj
    assert back.steps[0] == Step(index=1, kind="assistant", text="step 1")
    # The stored form is a plain JSON object, inspectable without hsai.
    assert json.loads(path.read_text())["model"] == "sonnet"


def test_identifier_without_ticket():
    assert _traj(ticket=None).identifier == "12-none"


def test_load_accepts_id_or_path(tmp_path):
    path = trajectory.write(_traj(), tmp_path)
    assert trajectory.load(tmp_path, "12-7").identifier == "12-7"
    assert trajectory.load(tmp_path, str(path)).identifier == "12-7"


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        trajectory.load(tmp_path, "999-1")


def test_record_builds_from_an_ai_result(tmp_path):
    ares = AIResult(
        ok=False, model="sonnet", output=json.dumps(MESSAGES_PAYLOAD),
        error="boom: token=ghp_0123456789abcdefghij", cmd=["claude"],
        usage=MESSAGES_PAYLOAD["usage"], raw=MESSAGES_PAYLOAD,
    )
    traj = trajectory.record(
        tmp_path, iteration=3, ticket=None, kind="heal", tier="heavy", model="opus",
        prompt="fix it", result=ares, duration_seconds=2.5,
    )
    assert traj.exit_status == "error" and traj.ok is False
    assert traj.duration_seconds == 2.5
    assert traj.usage == {"input_tokens": 10, "output_tokens": 4}
    assert "ghp_0123456789abcdefghij" not in traj.error  # errors are scrubbed too
    assert trajectory.path_for(tmp_path, "3-none").is_file()


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


# --- human rendering (hsai replay) ------------------------------------------

def test_render_shows_prompt_steps_and_usage():
    out = _traj(usage={"input_tokens": 10, "output_tokens": 4}, outcome="merged").render()
    assert "trajectory 12-7" in out
    assert "--- prompt ---" in out and "Implement the widget." in out
    assert "--- steps (8) ---" in out and "step 1" in out and "step 8" in out
    assert "input_tokens=10" in out and "output_tokens=4" in out
    assert "outcome: merged" in out


def test_render_reports_missing_usage():
    assert "usage: (not reported)" in _traj(usage=None).render()
