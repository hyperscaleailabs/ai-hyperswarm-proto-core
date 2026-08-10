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

    # One JSON artifact per iteration, sharded by block so pruning can drop
    # whole blocks: knowledge/trajectories/<block>/<iteration>.json
    assert path == tmp_path / "knowledge" / "trajectories" / "0" / "12.json"
    back = trajectory.read(path)
    assert back == traj
    assert back.steps[0] == Step(index=1, kind="assistant", text="step 1")
    # The stored form is a plain JSON object, inspectable without hsai.
    assert json.loads(path.read_text())["model"] == "sonnet"


def test_write_shards_by_block(tmp_path):
    path = trajectory.write(_traj(iteration=703, block=7), tmp_path)
    assert path == tmp_path / "knowledge" / "trajectories" / "7" / "703.json"
    assert trajectory.load(tmp_path, "703").iteration == 703


def test_the_branch_is_part_of_the_file_name(tmp_path):
    """A record names the branch it came from, so it is self-describing."""
    path = trajectory.write(_traj(iteration=41, branch="hsai/iter-17-4-abc123"), tmp_path)

    assert path.name == "41-hsai-iter-17-4-abc123.json"      # the `/` is slugged away
    assert path.parent == tmp_path / "knowledge" / "trajectories" / "0"
    # It is still addressed by iteration alone - the branch is not part of the id.
    assert trajectory.load(tmp_path, "41").branch == "hsai/iter-17-4-abc123"
    assert trajectory.find(tmp_path, "41") == path
    assert trajectory.find(tmp_path, "4") is None            # no prefix collisions


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


def test_render_shows_the_forensic_fields():
    out = _traj(
        branch="hsai/iter-9", strategy="heuristic-v1",
        guards={"workflow": "clean", "completeness": "ok", "repro": "n/a"},
        ci_before={"ruff": True, "pytest": False},
        ci_after={"ruff": True, "pytest": True},
        remote_ci="FAILURE", diffstat={"total": 3, "code": 2, "tests": 1},
        phases={"agent": 12.5, "ci_after": 3.0},
        failure_class="remote_infra", failure_reason="remote CI concluded FAILURE",
    ).render()
    assert "branch: hsai/iter-9" in out
    assert "strategy=heuristic-v1" in out
    assert "failure: remote_infra - remote CI concluded FAILURE" in out
    assert "workflow=clean" in out and "completeness=ok" in out
    assert "remote: FAILURE" in out
    assert "agent=12.50s" in out


# --- the schema one iteration records ---------------------------------------

def test_a_record_carries_the_whole_forensic_picture(tmp_path):
    """Every field the review of a failed iteration needs, in one artifact."""
    traj = _traj(
        branch="hsai/iter-3", strategy="heuristic-v1",
        guards={"workflow": "clean", "completeness": "ok", "repro": "n/a"},
        ci_before={"ruff": True, "pytest": True},
        ci_after={"ruff": True, "pytest": False},
        remote_ci="FAILURE",
        changed_paths=["src/hsai/widget.py", "tests/test_widget.py"],
        diffstat=trajectory.diffstat(["src/hsai/widget.py", "tests/test_widget.py"]),
        phases={"agent": 30.0, "ci_before": 4.0, "ci_after": 5.0, "total": 42.0},
        stdout_tail="pytest: 1 failed", stderr_tail="",
        failure_class="test_failure", failure_reason="pytest failed locally",
        usage={"input_tokens": 10, "output_tokens": 4},
    )
    stored = json.loads(trajectory.write(traj, tmp_path).read_text())

    # prompt text and hash, model/tier/strategy
    assert stored["prompt"] == "Implement the widget."
    assert stored["prompt_digest"] == trajectory.prompt_digest("Implement the widget.")
    assert (stored["model"], stored["tier"], stored["strategy"]) == (
        "sonnet", "standard", "heuristic-v1"
    )
    # every guard verdict, local CI steps, remote outcome
    assert set(stored["guards"]) == {"workflow", "completeness", "repro"}
    assert stored["ci_before"] == {"ruff": True, "pytest": True}
    assert stored["ci_after"] == {"ruff": True, "pytest": False}
    assert stored["remote_ci"] == "FAILURE"
    # changed-path diffstat and phase durations
    assert stored["diffstat"] == {
        "workflows": 0, "tests": 1, "knowledge": 0, "docs": 0, "code": 1,
        "other": 0, "total": 2,
    }
    assert stored["phases"]["agent"] == 30.0
    # agent output tails and the classification
    assert stored["stdout_tail"] == "pytest: 1 failed"
    assert stored["failure_class"] == "test_failure"
    assert stored["truncated"] is False
    # ...and it parses straight back into a Trajectory.
    assert trajectory.read(trajectory.path_for(tmp_path, "12", 0, "hsai/iter-3")) == traj


def test_diffstat_buckets_each_path_exactly_once():
    stat = trajectory.diffstat([
        ".github/workflows/ci.yml",
        "tests/test_a.py",
        "src/hsai/b.py",
        "knowledge/lessons/x.md",
        "docs/ARCHITECTURE.md",
        "Makefile",
    ])
    assert stat == {
        "workflows": 1, "tests": 1, "code": 1, "knowledge": 1, "docs": 1,
        "other": 1, "total": 6,
    }
    # `total` always equals the sum of the buckets - no double counting.
    assert stat["total"] == sum(v for k, v in stat.items() if k != "total")
    assert trajectory.diffstat([])["total"] == 0


# --- the size cap ------------------------------------------------------------

def test_a_huge_run_is_capped_and_keeps_its_tail(tmp_path):
    """A runaway agent must not write a megabyte into the knowledge base."""
    traj = _traj(steps=[
        Step(index=i, kind="tool_result", text=f"MARKER-{i} " + "x" * 1500)
        for i in range(1, 200)
    ])
    path = trajectory.write(traj, tmp_path)
    written = path.read_text()

    assert len(written) <= trajectory.MAX_RECORD_CHARS
    back = trajectory.read(path)                    # still a complete record
    assert back.truncated is True
    assert back.iteration == 12 and back.model == "sonnet"
    # The END of the run survives - that is where a failure shows up.
    assert back.steps, "the cap must not empty the step stream"
    assert back.steps[-1].index == 199
    assert "MARKER-1 " not in written               # the earliest steps were shed


def test_a_pathological_prompt_is_clipped_rather_than_dropped(tmp_path):
    """Even with no steps to shed, the record stays parseable and capped."""
    traj = _traj(steps=[], prompt="P" * (trajectory.MAX_RECORD_CHARS * 2))
    path = trajectory.write(traj, tmp_path)

    assert len(path.read_text()) <= trajectory.MAX_RECORD_CHARS
    back = trajectory.read(path)
    assert back.truncated is True
    assert "chars]" in back.prompt                  # clipped, with the count kept


def test_an_ordinary_run_is_not_marked_truncated(tmp_path):
    stored = json.loads(trajectory.write(_traj(), tmp_path).read_text())
    assert stored["truncated"] is False


# --- redaction of agent output (the invariant that makes committing safe) ----

def test_api_keys_and_gh_tokens_in_agent_output_never_reach_disk(tmp_path):
    from hsai.ai import AIResult

    api_key = "sk-ant-api03-AAAABBBBCCCCDDDD"
    gh_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    payload = {
        "result": f"exported ANTHROPIC_API_KEY={api_key} then ran gh",
        "messages": [
            {"role": "assistant", "content": f"GH_TOKEN={gh_token} authenticated"},
        ],
    }
    ares = AIResult(
        ok=True, model="sonnet", output=json.dumps(payload),
        error=f"warning: {gh_token} leaked to stderr", cmd=["claude"], payload=payload,
    )

    traj = trajectory.record(
        tmp_path, iteration=21, ticket=7, kind="implement", tier="standard",
        model="sonnet", prompt=f"do it with {api_key}", result=ares,
        block=0, branch="hsai/iter-21",
    )
    written = trajectory.path_for(tmp_path, "21", 0, "hsai/iter-21").read_text()

    for secret in (api_key, gh_token):
        assert secret not in written
        assert secret not in traj.to_json()
        assert secret not in traj.failure_excerpt()
        assert secret not in traj.excerpt()
    assert REDACTED in written
    # The stdout/stderr tails are captured, and scrubbed, not simply dropped.
    assert traj.stdout_tail and traj.stderr_tail
    assert "leaked to stderr" in traj.stderr_tail


# --- what a retry is shown about the attempt before it ----------------------

def test_failure_excerpt_is_bounded_evidence_not_a_transcript():
    traj = _traj(
        branch="hsai/iter-8", failure_class="test_failure",
        failure_reason="pytest failed locally",
        guards={"completeness": "ok", "repro": "n/a"},
        ci_after={"ruff": True, "pytest": False},
        remote_ci="FAILURE",
    )
    excerpt = traj.failure_excerpt()

    assert len(excerpt) <= trajectory.PREVIOUS_ATTEMPT_CHARS
    assert "`test_failure`" in excerpt and "pytest failed locally" in excerpt
    assert "hsai/iter-8" in excerpt
    assert "completeness=ok" in excerpt
    assert "pytest=FAIL" in excerpt and "ruff=pass" in excerpt
    assert "remote CI: FAILURE" in excerpt
    assert "step 8" in excerpt                     # the tail of the run
    assert "Implement the widget." not in excerpt  # never the prompt


def test_failure_excerpt_respects_its_own_limit():
    traj = _traj(
        failure_class="lint",
        failure_reason="ruff check failed locally: " + "detail " * 500,
        steps=[Step(index=1, kind="tool_result", text="y" * 5000)],
    )
    excerpt = traj.failure_excerpt(limit=300)
    assert len(excerpt) <= 300 + 40                # the clip marker is not free
    assert "chars]" in excerpt


def test_latest_for_ticket_finds_the_most_recent_failed_attempt(tmp_path):
    trajectory.write(_traj(iteration=1, ticket=7, failure_class="lint"), tmp_path)
    trajectory.write(_traj(iteration=5, ticket=7, failure_class="test_failure"), tmp_path)
    trajectory.write(_traj(iteration=6, ticket=7, failure_class=""), tmp_path)  # clean
    trajectory.write(_traj(iteration=9, ticket=8, failure_class="timeout"), tmp_path)

    found = trajectory.latest_for_ticket(tmp_path, 7)
    assert found is not None
    assert found.iteration == 5 and found.failure_class == "test_failure"

    assert trajectory.latest_for_ticket(tmp_path, 99) is None   # no history
    assert trajectory.latest_for_ticket(tmp_path, None) is None


def test_latest_for_ticket_survives_a_corrupt_record(tmp_path):
    """A half-written file must not take down the next iteration."""
    trajectory.write(_traj(iteration=2, ticket=7, failure_class="lint"), tmp_path)
    (trajectory.block_dir(tmp_path, 0) / "junk.json").write_text("{not json")

    found = trajectory.latest_for_ticket(tmp_path, 7)
    assert found is not None and found.iteration == 2


def test_digest_names_the_failure_class():
    assert "failure=test_failure" in _traj(failure_class="test_failure").digest()
    assert "failure=none" in _traj().digest()
