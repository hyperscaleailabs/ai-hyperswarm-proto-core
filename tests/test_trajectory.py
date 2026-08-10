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


def test_render_shows_the_iteration_context():
    out = _traj(
        branch="hsai/iter-1-abc", strategy="heuristic-v1",
        guards={"workflow_revert": "clean", "completeness": "ok"},
        local_ci={"before": {"ruff": True}, "after": {"pytest": False}},
        remote_ci="FAILURE", changed_paths=["src/hsai/a.py", "tests/test_a.py"],
        phase_seconds={"agent": 12.0}, failure_class="test_failure",
        retry_action="retry_with_remediation",
    ).render()
    assert "branch: hsai/iter-1-abc" in out
    assert "strategy=heuristic-v1" in out
    assert "failure: test_failure" in out and "retry action: retry_with_remediation" in out
    assert "workflow_revert=clean" in out and "completeness=ok" in out
    assert "remote CI: FAILURE" in out
    assert "agent=12.0s" in out


# --- the size cap -----------------------------------------------------------

def test_capped_json_is_a_no_op_below_the_cap(tmp_path):
    traj = _traj()
    assert traj.capped_json() == traj.to_json()
    assert traj.truncated == ""


def test_capped_json_sheds_steps_until_the_record_fits():
    """One runaway run must not be able to fill the store."""
    cap = 4000
    traj = _traj(steps=[
        Step(index=i, kind="tool_result", text=f"{i}-" + "x" * 500) for i in range(1, 60)
    ])
    assert len(traj.to_json()) > cap

    text = traj.capped_json(cap=cap)
    assert len(text) <= cap

    data = json.loads(text)
    assert "step(s) dropped" in data["truncated"]      # never silently partial
    assert len(data["steps"]) < 59
    # The head and the tail - where a post-mortem starts and ends - survive.
    assert data["steps"][0]["index"] == 1
    assert data["steps"][-1]["index"] == 59


def test_capped_json_clips_the_streams_when_steps_are_not_enough():
    """A record with two steps and a huge prompt still has to fit."""
    cap = 3000
    traj = _traj(
        steps=[Step(index=1, kind="output", text="a"), Step(index=2, kind="result", text="b")],
        prompt="p" * 20_000, stdout="o" * 20_000, stderr="e" * 20_000,
    )
    text = traj.capped_json(cap=cap)

    assert len(text) <= cap
    data = json.loads(text)
    assert "clipped" in data["truncated"]
    assert len(data["prompt"]) < 1000
    assert len(data["stdout"]) < 1000


def test_write_enforces_the_cap(tmp_path):
    """The cap is enforced on the way to disk, not merely available."""
    traj = _traj(steps=[
        Step(index=i, kind="tool_result", text="y" * trajectory.STEP_CHARS)
        for i in range(1, 400)
    ])
    path = trajectory.write(traj, tmp_path)

    assert len(path.read_text()) <= trajectory.MAX_RECORD_CHARS
    # Still a valid, readable record after shedding.
    back = trajectory.read(path)
    assert back.iteration == 12 and back.truncated


def test_read_tolerates_records_from_another_schema(tmp_path):
    """The store outlives the schema: unknown keys are dropped, not fatal."""
    path = trajectory.write(_traj(), tmp_path)
    data = json.loads(path.read_text())
    data["some_field_a_later_hsai_added"] = "hello"
    del data["strategy"]
    path.write_text(json.dumps(data))

    back = trajectory.read(path)
    assert back.iteration == 12
    assert back.strategy == ""


# --- secret redaction in *agent output*, end to end -------------------------

def test_api_key_and_gh_token_in_agent_output_are_redacted(tmp_path):
    """The two shapes that actually leak: an sk-ant key and a gh token."""
    api_key = "sk-ant-api03-Aa0Bb1Cc2Dd3Ee4Ff5Gg6Hh7Ii8Jj9"
    gh_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    payload = {
        "result": f"exported ANTHROPIC_API_KEY={api_key}",
        "usage": {"input_tokens": 3, "output_tokens": 1},
        "messages": [
            {"role": "assistant", "content": f"ran `gh auth login --with-token {gh_token}`"},
        ],
    }
    ares = AIResult(
        ok=True, model="sonnet", output=json.dumps(payload),
        error=f"warning: leaked {gh_token} and {api_key}", cmd=["claude"],
        usage=payload["usage"], payload=payload,
    )

    traj = trajectory.record(
        tmp_path, iteration=21, ticket=7, kind="implement", tier="standard",
        model="sonnet", prompt=f"do it with {api_key}", result=ares, block=0,
    )

    written = trajectory.path_for(tmp_path, "21", 0).read_text()
    for secret in (api_key, gh_token):
        assert secret not in written            # nothing reaches disk unscrubbed
        assert secret not in traj.to_json()
        assert secret not in traj.excerpt()
        assert secret not in traj.remediation()
        assert secret not in traj.stdout
        assert secret not in traj.stderr
    assert REDACTED in written
    # Redaction is targeted, not a blanket wipe: the record is still useful.
    assert json.loads(written)["usage"] == {"input_tokens": 3, "output_tokens": 1}


def test_record_keeps_the_redacted_raw_streams(tmp_path):
    ares = AIResult(
        ok=True, model="sonnet", output="x" * (trajectory.STREAM_CHARS + 500),
        error="stderr noise", cmd=["claude"],
    )
    traj = trajectory.record(
        tmp_path, iteration=22, ticket=1, kind="implement", tier="standard",
        model="sonnet", prompt="do it", result=ares, block=0,
    )
    assert traj.stderr == "stderr noise"
    assert "chars]" in traj.stdout          # clipped, and says so
    assert len(traj.stdout) < trajectory.STREAM_CHARS + 100


def test_record_without_a_result_is_a_dry_run_record(tmp_path):
    """A rehearsal still leaves one record - never mistakable for a real run."""
    traj = trajectory.record(
        tmp_path, iteration=23, ticket=None, kind="improve", tier="light",
        model="haiku", prompt="rehearse", result=None, block=0,
        branch="hsai/iter-x", strategy="heuristic-v1", outcome="dry-run",
    )
    assert traj.exit_status == "dry-run"
    assert traj.steps == [] and traj.usage is None
    assert traj.stdout == "" and traj.stderr == ""
    assert traj.branch == "hsai/iter-x" and traj.strategy == "heuristic-v1"
    assert traj.prompt_digest == trajectory.prompt_digest("rehearse")
    assert trajectory.path_for(tmp_path, "23", 0).is_file()


# --- diffstat + the retry excerpt -------------------------------------------

def test_diffstat_buckets_changed_paths_by_area():
    traj = _traj(changed_paths=[
        "src/hsai/a.py", "src/hsai/b.py", "tests/test_a.py", "README.md",
    ])
    assert traj.diffstat() == {"src": 2, "tests": 1, "README.md": 1}
    assert _traj().diffstat() == {}


def test_remediation_states_the_facts_then_a_tail():
    traj = _traj(
        failure_class="test_failure", retry_action="retry_with_remediation",
        local_ci={"after": {"ruff": True, "pytest": False}},
        remote_ci="FAILURE", guards={"completeness": "ok"},
        changed_paths=["src/hsai/a.py"], error="pytest exited 1",
    )
    text = traj.remediation()

    assert "failure class: `test_failure`" in text
    assert "retry policy: `retry_with_remediation`" in text
    assert "local CI red: after:pytest" in text
    assert "remote CI: FAILURE" in text
    assert "guards: completeness=ok" in text
    assert "files touched: src=1" in text
    assert "pytest exited 1" in text
    assert "step 8" in text                       # the tail is quoted...
    assert "Implement the widget." not in text    # ...never the prior prompt


def test_remediation_is_bounded():
    traj = _traj(
        failure_class="lint",
        steps=[Step(index=i, kind="tool_result", text="z" * 900) for i in range(1, 40)],
        error="e" * 5000,
    )
    text = traj.remediation()
    assert len(text) <= trajectory.REMEDIATION_CHARS + 60   # + the "[+N chars]" marker
    assert traj.remediation(limit=200).startswith("- failure class: `lint`")


def test_remediation_of_an_unclassified_run():
    assert "failure class: `unknown`" in _traj().remediation()


# --- finding the previous failed attempt on a ticket ------------------------

def test_last_failure_for_ticket_picks_the_newest_non_merged_run(tmp_path):
    trajectory.write(_traj(iteration=1, ticket=7, outcome="recovered"), tmp_path)
    trajectory.write(_traj(iteration=5, ticket=7, outcome="incomplete"), tmp_path)
    trajectory.write(_traj(iteration=9, ticket=8, outcome="recovered"), tmp_path)

    found = trajectory.last_failure_for_ticket(tmp_path, 7)
    assert found is not None and found.iteration == 5
    assert trajectory.last_failure_for_ticket(tmp_path, 8).iteration == 9


def test_last_failure_for_ticket_ignores_merged_runs_and_misses(tmp_path):
    trajectory.write(_traj(iteration=1, ticket=7, outcome="merged"), tmp_path)
    assert trajectory.last_failure_for_ticket(tmp_path, 7) is None
    assert trajectory.last_failure_for_ticket(tmp_path, 99) is None
    assert trajectory.last_failure_for_ticket(tmp_path, None) is None
    assert trajectory.last_failure_for_ticket(tmp_path / "empty", 7) is None


def test_last_failure_for_ticket_survives_a_corrupt_record(tmp_path):
    """A half-written file from a killed run must not break the next one."""
    trajectory.write(_traj(iteration=1, ticket=7, outcome="recovered"), tmp_path)
    (trajectory.block_dir(tmp_path, 0) / "666.json").write_text("{not json")

    found = trajectory.last_failure_for_ticket(tmp_path, 7)
    assert found is not None and found.iteration == 1
