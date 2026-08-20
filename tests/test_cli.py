import json

from hsai import cli as cli_module
from hsai import trajectory
from hsai.cli import build_parser, main
from hsai.proc import Proc
from hsai.repro import ReproResult
from hsai.trajectory import Step, Trajectory


def test_parser_loop_args():
    parser = build_parser()
    args = parser.parse_args(["loop", "-n", "2", "--max-parallel", "3"])
    assert args.command == "loop"
    assert args.iterations == 2
    assert args.max_parallel == 3


def test_loop_alias_maps_to_subcommand():
    # `hsai --loop` should behave like `hsai loop`
    assert main is not None  # importable entry point
    parser = build_parser()
    args = parser.parse_args(["loop"])
    assert args.iterations == 1
    assert args.max_parallel is None


def test_status_command_returns_zero(capsys):
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "hyperscaleailabs/ai-hyperswarm-proto-core" in out


def test_doctor_reports_the_live_child_env_check_and_exits_zero_on_pass(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "child-environment guard: PASS" in out
    assert "ANTHROPIC_API_KEY" in out


def test_doctor_exits_nonzero_when_the_live_check_reports_a_leak(monkeypatch, capsys):
    # Isolate the live-check failure from the (separate) preflight guard.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        cli_module.ai, "check_child_env",
        lambda cfg: (False, "leaked into a real spawned child process: ANTHROPIC_API_KEY"),
    )
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "child-environment guard: FAIL" in out
    assert "ANTHROPIC_API_KEY" in out


def test_parser_cycle_resume_args():
    parser = build_parser()
    plain = parser.parse_args(["cycle"])
    assert plain.index is None and not plain.resume and not plain.dry_run

    resumed = parser.parse_args(["cycle", "--resume", "--cycle-index", "42"])
    assert resumed.resume and resumed.index == 42      # --cycle-index aliases --index
    assert parser.parse_args(["cycle", "--index", "42"]).index == 42


def test_cycle_command_passes_resume_through(monkeypatch, capsys):
    from hsai.cycle import CycleResult
    from hsai.governance import BlockReport

    seen = {}

    def fake_run_cycle(cfg, **kwargs):
        seen.update(kwargs)
        return CycleResult(
            report=BlockReport(cycle_index=42), review_issue=0, governance_pr=0,
            journal_path="/tmp/j.jsonl", resumed=True,
        )

    monkeypatch.setattr("hsai.cycle.run_cycle", fake_run_cycle)

    rc = main(["cycle", "--resume", "--cycle-index", "42"])
    out = capsys.readouterr().out

    assert rc == 0
    assert seen["resume"] is True and seen["cycle_index"] == 42 and seen["dry_run"] is False
    assert "block 42 (resumed)" in out and "/tmp/j.jsonl" in out


def test_parser_recall_defaults():
    parser = build_parser()
    args = parser.parse_args(["recall", "remote CI gate"])
    assert args.command == "recall"
    assert args.query == "remote CI gate"
    assert args.k == 5 and args.kind == "" and args.root == "."


def test_recall_command_prints_ranked_notes_with_scores(tmp_path, capsys):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "2026-01-01-remote-ci-gate.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/fail\n  - kind/implement\n---\n\n"
        "# Remote CI gate\n\n## Lesson learned\nPoll the rollup before merging.\n"
    )
    (lessons / "2026-01-02-unrelated.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/improve\n---\n\n"
        "# Obsidian layout\n\n## Lesson learned\nWikilinks make a graph.\n"
    )

    rc = main(["recall", "remote CI gate", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "2026-01-01-remote-ci-gate" in out
    assert "(fail/implement)" in out
    # a score is printed alongside every name
    first = out.splitlines()[0].split()
    assert float(first[0]) > 0 and first[1] == "2026-01-01-remote-ci-gate"


def test_reindex_rebuilds_the_mocs_and_the_retrieval_index(tmp_path, capsys):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "2026-01-01-remote-ci-gate.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/fail\n  - kind/implement\ncreated: 2026-01-01\n---\n\n"
        "# Remote CI gate\n\n## Lesson learned\nPoll the rollup before merging.\n"
    )

    rc = main(["reindex", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Lessons MOC" in out

    index = tmp_path / "knowledge" / "index" / "notes.json"
    assert index.is_file()
    assert str(index) in out and "1 note(s)" in out
    payload = json.loads(index.read_text())
    assert [n["id_"] for n in payload["nodes"]] == ["2026-01-01-remote-ci-gate"]
    assert payload["nodes"][0]["metadata"]["outcome"] == "fail"

    # Idempotent: a second run on an unchanged vault produces no diff.
    before = index.read_bytes()
    assert main(["reindex", "--root", str(tmp_path)]) == 0
    assert index.read_bytes() == before


def test_parser_observe_defaults():
    parser = build_parser()
    args = parser.parse_args(["observe"])
    assert args.command == "observe"
    assert args.root == "." and args.refresh is False
    assert parser.parse_args(["observe", "--refresh"]).refresh is True


def _gh_api_runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
    """A fake `gh api` serving the same canned digest for every project."""
    target = cmd[2] if len(cmd) > 2 else ""
    if target.endswith("/readme"):
        return Proc(cmd, 0, "# a reference project\n", "")
    if "/commits" in target:
        return Proc(cmd, 0, "aaa111\tfeat: something\n", "")
    if "/contents/.github/workflows" in target:
        return Proc(cmd, 0, "ci.yml\n", "")
    return Proc(cmd, 0, "main\n", "")


def test_observe_refreshes_digests_and_dossiers_only(tmp_path, monkeypatch, capsys):
    """`hsai observe` owns knowledge/reference and touches nothing else."""
    from hsai.config import load_config

    cfg = load_config()
    monkeypatch.setattr(cli_module, "run", _gh_api_runner)

    rc = main(["observe", "--refresh", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    reference = tmp_path / "knowledge" / "reference"
    assert len(list(reference.glob("*.json"))) == len(cfg.reference_top10)
    assert len(list(reference.glob("*.md"))) == len(cfg.reference_top10)
    assert "refreshed - baseline recorded (first observation)" in out
    assert "Reference set:" in out
    # the MOCs and the retrieval index stay with `hsai reindex`
    assert not (tmp_path / "knowledge" / "MOCs" / "Lessons MOC.md").exists()
    assert not (tmp_path / "knowledge" / "index").exists()

    # A second run without --refresh serves the cache instead of re-fetching.
    def refuse(cmd, **kwargs):
        raise AssertionError(f"a fresh cache must not re-fetch: {cmd!r}")

    monkeypatch.setattr(cli_module, "run", refuse)
    assert main(["observe", "--root", str(tmp_path)]) == 0
    assert "cached - baseline recorded (first observation)" in capsys.readouterr().out


def test_recall_command_reports_an_empty_vault_without_crashing(tmp_path, capsys):
    rc = main(["recall", "anything", "--root", str(tmp_path)])
    assert rc == 1
    assert "no indexable notes" in capsys.readouterr().err


def test_recall_command_reports_a_miss(tmp_path, capsys):
    lessons = tmp_path / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "n.md").write_text("# A note\n\n## Lesson learned\nSomething.\n")
    rc = main(["recall", "zzzznomatch", "--root", str(tmp_path)])
    assert rc == 1
    assert "no match" in capsys.readouterr().err


def test_parser_practices_subcommands():
    parser = build_parser()
    args = parser.parse_args(["practices", "list", "--root", "/tmp/x"])
    assert args.command == "practices"
    assert args.practices_command == "list"
    assert args.root == "/tmp/x"

    add_args = parser.parse_args([
        "practices", "add", "--title", "t", "--source-project", "o/r",
        "--source-artifact", "source_code", "--evidence", "PR #1",
    ])
    assert add_args.practices_command == "add"
    assert add_args.status == "adopted"
    assert add_args.adopted_pr is None


def test_practices_list_prints_the_registry(tmp_path, capsys):
    from hsai.practices import append, build_practice

    append(
        tmp_path,
        build_practice(
            title="session durability", source_project="OpenBMB/ChatDev",
            source_artifact="harness_design", evidence="PR #104", adopted_pr=104,
        ),
    )
    rc = main(["practices", "list", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "session durability" in out
    assert "OpenBMB/ChatDev" in out
    assert "PR #104" in out


def test_practices_list_reports_an_empty_registry(tmp_path, capsys):
    rc = main(["practices", "list", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "empty" in out.lower()


def test_practices_add_writes_a_note(tmp_path, capsys):
    rc = main([
        "practices", "add", "--root", str(tmp_path),
        "--title", "cost accounting", "--source-project", "assafelovic/gpt-researcher",
        "--source-artifact", "source_code", "--evidence", "PR #47",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "wrote" in out
    notes = list((tmp_path / "knowledge" / "practices").glob("*.md"))
    assert len(notes) == 1
    assert "cost accounting" in notes[0].read_text()


def test_practices_add_refuses_a_duplicate(tmp_path, capsys):
    args = [
        "practices", "add", "--root", str(tmp_path),
        "--title", "cost accounting", "--source-project", "assafelovic/gpt-researcher",
        "--source-artifact", "source_code", "--evidence", "PR #47",
    ]
    assert main(args) == 0
    rc = main(args)
    err = capsys.readouterr().err
    assert rc == 1
    assert "refused" in err
    notes = list((tmp_path / "knowledge" / "practices").glob("*.md"))
    assert len(notes) == 1  # the duplicate attempt never wrote a second note


def test_parser_repro_check_defaults():
    parser = build_parser()
    args = parser.parse_args(["repro-check"])
    assert args.command == "repro-check"
    assert args.base_ref == "origin/main"
    assert args.pr_title is None


def test_repro_check_command_blocks_and_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.repro, "evaluate_pr",
        lambda **kwargs: ReproResult(ok=False, reason="blocked for testing"),
    )
    rc = main(["repro-check", "--pr-title", "implement: fix: x", "--base-ref", "origin/main"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out
    assert "blocked for testing" in out


def test_repro_check_command_passes_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.repro, "evaluate_pr",
        lambda **kwargs: ReproResult(ok=True, reason="exempt: not a heal/bugfix ticket"),
    )
    rc = main(["repro-check", "--pr-title", "implement: docs: x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


# --- replay (reads the local trajectory store, spends no quota) -------------

class _RunnerSpy:
    """Records - and refuses - any attempt to shell out."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        raise AssertionError(f"replay must not run a subprocess: {cmd!r}")


def _seed_trajectory(root) -> Trajectory:
    traj = Trajectory(
        iteration=12, ticket=7, kind="implement", tier="standard", model="sonnet",
        prompt="Implement the widget END TO END.",
        steps=[
            Step(index=1, kind="tool_use", name="Read", text='{"path": "src/hsai/ai.py"}'),
            Step(index=2, kind="result", text="Widget implemented."),
        ],
        usage={"input_tokens": 1500, "output_tokens": 320},
        duration_seconds=42.5, outcome="merged",
    )
    trajectory.write(traj, root)
    return traj


def _no_subprocess(monkeypatch) -> _RunnerSpy:
    spy = _RunnerSpy()
    monkeypatch.setattr("hsai.proc.run", spy)
    monkeypatch.setattr("hsai.ai.run", spy)
    return spy


def test_traj_prints_a_stored_run_for_post_mortem(tmp_path, monkeypatch, capsys):
    """`hsai traj <iteration>` is the post-mortem entrance."""
    _seed_trajectory(tmp_path)
    spy = _no_subprocess(monkeypatch)

    rc = main(["traj", "12", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "trajectory 12" in out
    assert "ticket #7" in out
    assert "input_tokens=1500" in out
    assert spy.calls == []                       # reading disk spends no quota


def test_traj_unknown_iteration_exits_nonzero(tmp_path, monkeypatch, capsys):
    _no_subprocess(monkeypatch)
    rc = main(["traj", "999", "--root", str(tmp_path)])
    assert rc == 1
    assert "no trajectory" in capsys.readouterr().err


def test_replay_prints_a_human_reconstruction(tmp_path, monkeypatch, capsys):
    _seed_trajectory(tmp_path)
    spy = _no_subprocess(monkeypatch)

    rc = main(["replay", "12", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "trajectory 12" in out
    assert "Implement the widget END TO END." in out      # prompt
    assert "tool_use(Read)" in out and "Widget implemented." in out  # step stream
    assert "input_tokens=1500" in out and "output_tokens=320" in out  # usage
    assert "outcome: merged" in out
    assert spy.calls == []                                 # no `claude`, no network


def test_replay_json_flag_round_trips(tmp_path, monkeypatch, capsys):
    traj = _seed_trajectory(tmp_path)
    spy = _no_subprocess(monkeypatch)

    rc = main(["replay", "12", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(out)
    assert payload["iteration"] == 12 and payload["ticket"] == 7
    assert payload["usage"]["output_tokens"] == 320
    assert [s["kind"] for s in payload["steps"]] == ["tool_use", "result"]
    assert payload == json.loads(traj.to_json())
    assert spy.calls == []


def test_replay_accepts_a_file_path(tmp_path, monkeypatch, capsys):
    _seed_trajectory(tmp_path)
    _no_subprocess(monkeypatch)
    path = trajectory.path_for(tmp_path, "12", 0)

    assert main(["replay", str(path)]) == 0
    assert "trajectory 12" in capsys.readouterr().out


def test_replay_unknown_id_exits_nonzero(tmp_path, monkeypatch, capsys):
    _no_subprocess(monkeypatch)
    rc = main(["replay", "999", "--root", str(tmp_path)])
    assert rc == 1
    assert "no trajectory" in capsys.readouterr().err
