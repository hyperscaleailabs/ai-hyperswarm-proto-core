"""Per-iteration trajectory store: recording, redaction, truncation, rollups.

Everything here runs on a fake clock under ``tmp_path``: no network, no
subprocess, no dependence on the real environment.
"""
from __future__ import annotations

import json

import pytest

from hsai import trace
from hsai.config import load_config


class FakeClock:
    """Monotonic stand-in: every reading is `step` seconds after the last."""

    def __init__(self, start: float = 1700000000.0, step: float = 0.5) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        reading = self.now
        self.now += self.step
        return reading


def _traj(tmp_path, **kwargs) -> trace.Trajectory:
    kwargs.setdefault("clock", FakeClock())
    kwargs.setdefault("env", {})
    return trace.Trajectory(
        trace.trajectory_path(tmp_path, "hsai/iter-1700000000-1-abc123"),
        root=tmp_path, iteration=1, branch="hsai/iter-1700000000-1-abc123", block=7,
        **kwargs,
    )


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- recording -----------------------------------------------------------------
def test_step_records_name_timing_and_outcome(tmp_path):
    traj = _traj(tmp_path)
    with traj.step(trace.CI_BEFORE) as st:
        st.ok = False
        st.summary = "CI red (ruff=pass, pytest=FAIL)"
        st.detail["log"] = "pytest: 1 failed"

    (step,) = traj.steps
    assert step.name == trace.CI_BEFORE
    assert step.ok is False
    assert step.duration_s == 0.5           # one fake-clock tick
    assert step.started.startswith("2023-11-14T")
    assert step.detail == {"log": "pytest: 1 failed"}


def test_first_line_is_a_meta_record_and_steps_append_after_it(tmp_path):
    traj = _traj(tmp_path)
    traj.record(trace.WORKTREE_SETUP, summary="branch made")
    traj.record(trace.MERGE_OR_RECOVER, summary="merged")

    meta, first, second = _lines(traj.path)
    assert meta["record"] == "meta"
    assert (meta["iteration"], meta["block"]) == (1, 7)
    assert meta["branch"] == "hsai/iter-1700000000-1-abc123"
    assert [first["record"], second["record"]] == ["step", "step"]
    assert [first["name"], second["name"]] == [
        trace.WORKTREE_SETUP, trace.MERGE_OR_RECOVER
    ]


def test_a_raised_exception_is_recorded_as_a_failed_step_then_reraised(tmp_path):
    """A crash mid-iteration is precisely the run worth having evidence for."""
    traj = _traj(tmp_path)
    with pytest.raises(RuntimeError):
        with traj.step(trace.AGENT_RUN):
            raise RuntimeError("claude vanished")

    (step,) = traj.steps
    assert step.ok is False
    assert "claude vanished" in step.summary
    assert "RuntimeError" in step.detail["exception"]
    assert _lines(traj.path)[-1]["name"] == trace.AGENT_RUN


def test_no_file_is_created_until_a_step_is_recorded(tmp_path):
    traj = _traj(tmp_path)
    assert not traj.path.exists()
    traj.record(trace.CI_AFTER)
    assert traj.path.is_file()


def test_relpath_is_repo_relative_so_the_lesson_can_link_it(tmp_path):
    assert _traj(tmp_path).relpath == (
        "knowledge/trajectories/hsai-iter-1700000000-1-abc123.jsonl"
    )


def test_copy_to_mirrors_the_file_into_a_worktree_at_the_same_relative_path(tmp_path):
    traj = _traj(tmp_path)
    traj.record(trace.PR_OPEN, summary="PR #12")
    dest = traj.copy_to(tmp_path / "wt")

    assert dest == tmp_path / "wt" / traj.relpath
    assert dest.read_text() == traj.path.read_text()


def test_copy_to_is_a_no_op_when_the_destination_is_the_source(tmp_path):
    """`hsai loop --dry-run` runs in the repo itself: root and worktree coincide."""
    traj = _traj(tmp_path)
    traj.record(trace.PR_OPEN)
    before = traj.path.read_text()
    assert traj.copy_to(tmp_path) == traj.path
    assert traj.path.read_text() == before


# --- redaction -----------------------------------------------------------------
LEAKY_OUTPUT = """\
I exported ANTHROPIC_API_KEY=sk-ant-api03-LEAKEDKEYVALUE0001 to get going,
then used the token ghp_LEAKEDGITHUBTOKEN00000000000000000 and
Authorization: Bearer eyJLEAKEDBEARERTOKEN.value for the API.
"""

SECRETS = (
    "sk-ant-api03-LEAKEDKEYVALUE0001",
    "ghp_LEAKEDGITHUBTOKEN00000000000000000",
    "eyJLEAKEDBEARERTOKEN.value",
)


def test_agent_output_seeded_with_credentials_reaches_disk_with_none_of_them(tmp_path):
    """Acceptance: these files are committed, so nothing secret may survive."""
    cfg = load_config()
    traj = _traj(tmp_path, forbid_env=cfg.forbidden_env,
                 env={"ANTHROPIC_API_KEY": "an-env-value-nobody-should-see"})
    with traj.step(trace.AGENT_RUN) as st:
        st.summary = f"agent leaked {SECRETS[0]}"
        st.detail["stdout"] = LEAKY_OUTPUT
        st.detail["stderr"] = "env had an-env-value-nobody-should-see set"

    written = traj.path.read_text()
    for secret in SECRETS:
        assert secret not in written
    # The *value* of a forbidden variable is scrubbed too, whatever its shape.
    assert "an-env-value-nobody-should-see" not in written
    assert "ANTHROPIC_API_KEY=[redacted]" in written
    # Redaction is not deletion: the surrounding evidence is still readable.
    assert "to get going" in written


def test_keys_named_by_forbid_env_are_dropped_from_detail_at_any_depth(tmp_path):
    traj = _traj(tmp_path, forbid_env=("ANTHROPIC_API_KEY",))
    traj.record(
        trace.AGENT_RUN,
        detail={"env": {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "whatever"},
                "anthropic_api_key": "lowercase spelling counts too"},
    )
    detail = traj.steps[0].detail
    assert detail == {"env": {"PATH": "/usr/bin"}}
    assert "whatever" not in traj.path.read_text()


def test_home_paths_collapse_so_a_trajectory_can_be_shared_as_is(tmp_path):
    assert trace.redact("cd /Users/someone/repo && ls", env={}) == "cd ~/repo && ls"
    assert trace.redact("/home/runner/work/x", env={}) == "~/work/x"


def test_redaction_reads_the_real_environment_by_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "value-from-the-real-environment")
    scrubbed = trace.redact(
        "leaked value-from-the-real-environment here", forbid_env=("ANTHROPIC_API_KEY",)
    )
    assert "value-from-the-real-environment" not in scrubbed


def test_short_env_values_are_left_alone(monkeypatch):
    """A three-letter value is a word, not a secret; blanking it corrupts prose."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dev")
    assert trace.redact(
        "the dev branch", forbid_env=("ANTHROPIC_API_KEY",)
    ) == "the dev branch"


# --- truncation ----------------------------------------------------------------
def test_a_detail_over_the_cap_is_truncated_with_an_explicit_marker(tmp_path):
    traj = _traj(tmp_path, max_chars=100)
    traj.record(trace.AGENT_RUN, detail={"stdout": "x" * 250})

    stdout = traj.steps[0].detail["stdout"]
    assert stdout.startswith("x" * 100)
    assert "[truncated: 150 of 250 chars omitted]" in stdout
    assert len(stdout.split("\n")[0]) == 100
    # ...and what is on disk is the truncated form, not the original.
    assert "x" * 101 not in traj.path.read_text()


def test_a_detail_under_the_cap_is_untouched(tmp_path):
    traj = _traj(tmp_path, max_chars=100)
    traj.record(trace.AGENT_RUN, detail={"stdout": "y" * 100})
    assert traj.steps[0].detail["stdout"] == "y" * 100


def test_truncation_runs_after_redaction_so_a_secret_cannot_survive_by_being_cut():
    """Cutting first could leave the tail of a token in the kept prefix."""
    text = "padding " + SECRETS[0] + " tail"
    scrubbed = trace.scrub(text, forbid_env=(), max_chars=20, env={})
    assert SECRETS[0][:20] not in scrubbed
    assert trace.REDACTED in scrubbed


def test_the_cap_is_configurable_from_core_yaml(tmp_path):
    cfg = load_config()
    traj = trace.Trajectory.for_iteration(
        tmp_path, cfg, branch="hsai/iter-1-2-abc", iteration=2, block=3, clock=FakeClock()
    )
    assert traj.max_chars == int(cfg.knowledge["trajectory_max_chars"])
    assert traj.forbid_env == cfg.forbidden_env
    assert traj.relpath.startswith("knowledge/trajectories/")


# --- naming --------------------------------------------------------------------
def test_each_branch_gets_its_own_file_so_parallel_workers_never_collide(tmp_path):
    branches = [
        "hsai/iter-1700000000-1-aaaaaa",
        "hsai/iter-1700000000-2-bbbbbb",
        "hsai/iter-1700000000-1-cccccc",
    ]
    paths = {trace.trajectory_path(tmp_path, b) for b in branches}
    assert len(paths) == len(branches)
    assert all(p.suffix == ".jsonl" and "/" not in p.stem for p in paths)


# --- reading -------------------------------------------------------------------
def test_read_round_trips_meta_and_steps(tmp_path):
    traj = _traj(tmp_path)
    traj.record(trace.CI_BEFORE, ok=True, summary="CI green", detail={"log": "ok"},
                duration_s=0.25)
    traj.record(trace.MERGE_OR_RECOVER, ok=False, summary="recovered", duration_s=0.75)

    parsed = trace.read(traj.path)
    assert (parsed.iteration, parsed.block) == (1, 7)
    assert parsed.branch == "hsai/iter-1700000000-1-abc123"
    assert [s.name for s in parsed.steps] == [trace.CI_BEFORE, trace.MERGE_OR_RECOVER]
    assert [s.name for s in parsed.failures()] == [trace.MERGE_OR_RECOVER]
    assert parsed.duration_s == 1.0
    assert parsed.steps[0].detail == {"log": "ok"}


def test_read_skips_a_corrupt_line_instead_of_failing_the_whole_file(tmp_path):
    traj = _traj(tmp_path)
    traj.record(trace.CI_BEFORE)
    with traj.path.open("a") as fh:
        fh.write("{not json\n")
    traj.record(trace.CI_AFTER)

    assert [s.name for s in trace.read(traj.path).steps] == [
        trace.CI_BEFORE, trace.CI_AFTER
    ]


def test_render_lists_every_step_with_its_duration(tmp_path):
    traj = _traj(tmp_path)
    traj.record(trace.CI_BEFORE, summary="CI green")
    traj.record(trace.AGENT_RUN, ok=False, summary="agent failed")

    rendered = trace.read(traj.path).render()
    assert "iteration 1  block 7" in rendered
    assert trace.CI_BEFORE in rendered and "CI green" in rendered
    assert "FAIL" in rendered and "agent failed" in rendered
    assert "2 step(s), 1 failed" in rendered
    assert f"failed: {trace.AGENT_RUN}" in rendered


def _seed(tmp_path, branch, block, steps) -> trace.Trajectory:
    traj = trace.Trajectory(
        trace.trajectory_path(tmp_path, branch), root=tmp_path, branch=branch,
        block=block, clock=FakeClock(step=1.0), env={},
    )
    for name, ok in steps:
        traj.record(name, ok=ok, duration_s=2.0 if name == trace.AGENT_RUN else 0.5)
    return traj


def test_load_all_and_aggregate_roll_up_across_iterations(tmp_path):
    _seed(tmp_path, "hsai/iter-1", 4, [(trace.AGENT_RUN, True), (trace.CI_AFTER, True)])
    _seed(tmp_path, "hsai/iter-2", 4, [(trace.AGENT_RUN, False), (trace.CI_AFTER, True)])
    _seed(tmp_path, "hsai/iter-3", 9, [(trace.AGENT_RUN, True)])

    traces = trace.load_all(tmp_path)
    assert len(traces) == 3

    stats = {s.name: s for s in trace.aggregate(traces)}
    assert stats[trace.AGENT_RUN].runs == 3
    assert stats[trace.AGENT_RUN].failures == 1
    assert stats[trace.AGENT_RUN].total_s == pytest.approx(6.0)
    assert stats[trace.AGENT_RUN].mean_s == pytest.approx(2.0)
    assert stats[trace.AGENT_RUN].failure_rate == pytest.approx(1 / 3)
    assert stats[trace.CI_AFTER].runs == 2 and stats[trace.CI_AFTER].failures == 0
    # Slowest step first: that is what a reader is looking for.
    assert trace.aggregate(traces)[0].name == trace.AGENT_RUN


def test_load_all_can_be_scoped_to_one_block(tmp_path):
    _seed(tmp_path, "hsai/iter-1", 4, [(trace.AGENT_RUN, True)])
    _seed(tmp_path, "hsai/iter-3", 9, [(trace.AGENT_RUN, True)])

    assert [t.block for t in trace.load_all(tmp_path, block=9)] == [9]
    assert trace.load_all(tmp_path, block=1234) == []
    assert trace.load_all(tmp_path / "nothing-here") == []


def test_render_stats_reports_runs_failures_and_durations(tmp_path):
    _seed(tmp_path, "hsai/iter-1", 4, [(trace.AGENT_RUN, True), (trace.CI_AFTER, True)])
    _seed(tmp_path, "hsai/iter-2", 4, [(trace.AGENT_RUN, False)])

    rendered = trace.render_stats(trace.load_all(tmp_path))
    assert "trajectories: 2" in rendered
    assert "1 with at least one failed step" in rendered
    assert trace.AGENT_RUN in rendered and "50.0%" in rendered
    assert "0.0%" in rendered           # ci_after never failed
