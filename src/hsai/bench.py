"""Offline replay bench: a regression suite for the orchestration itself.

Every other test in this repo checks a function. This checks the *loop* - the
composed sequence of decisions one iteration makes - and it does so without a
model, a network call, or a second of quota. That closes the last measurement
gap in G4: before this, a change to model selection, the retry policy, or a
guard could only be *asserted* to be an improvement, because the only evidence
a real iteration left behind was unstructured stdout and a cost line with no
quality signal beside it.

How it works:

- A **corpus** of hand-authored scenarios (``tests/fixtures/trajectories``)
  describes iterations the loop has actually had to handle - a green implement,
  a red heal, an off-spec diff, a repro-guard block, a hard budget breach, a
  timeout, a merge conflict, a ticket out of attempts. Each names the inputs and
  the expected decisions.
- :func:`replay` runs each one through the **real** decision code -
  :func:`hsai.orchestrator.decide_path`, :func:`hsai.models.select`,
  :func:`hsai.ledger.evaluate_budget`, :func:`hsai.orchestrator._requires_code`,
  :func:`hsai.repro.check_repro`, :func:`hsai.orchestrator.retry_disposition` -
  in the order :func:`hsai.orchestrator._run_iteration` calls them. Nothing is
  reimplemented; the only fake is the :class:`~hsai.proc.Runner`, which answers
  the guard's ``pytest`` invocations from the scenario instead of spawning one.
- The output is an :class:`~hsai.trajectory.IterationTrajectory`: the same
  artifact a live iteration writes, so a replayed scenario and a real run are
  directly comparable objects.

The committed ``bench/baseline.json`` is what makes it a *gate* rather than a
report: ``hsai bench --check`` fails when pass-rate, tier agreement, recovery
accuracy, or corpus size regresses against it. Wall-clock is measured and
reported but deliberately never gated - it is a property of the CI runner, not
of the harness.

Synthesis: SWE-agent (the agent run is a replayable object, and the harness is
benchmarked rather than assumed), langchain (record-and-replay cassettes plus a
committed performance baseline wired as a required check), and MetaGPT (a cheap
always-on test tier that never calls a model, kept separate from the heavy one).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from . import ledger, orchestrator, repro, trajectory
from .config import CoreConfig
from .models import Task, select
from .proc import Proc

CORPUS_DIR = "tests/fixtures/trajectories"
BASELINE_PATH = "bench/baseline.json"

# Bump when a scenario's `given`/`expect` vocabulary changes incompatibly.
SCENARIO_SCHEMA_VERSION = 1

# The metrics `--check` gates on. Wall-clock is excluded on purpose: it measures
# the runner, not the harness, and gating on it would make CI flaky.
GATED_METRICS = ("pass_rate", "tier_agreement", "recovery_accuracy")


class CorpusError(RuntimeError):
    """The corpus could not be loaded (missing, empty, or malformed)."""


@dataclass(frozen=True)
class Scenario:
    """One hand-authored iteration: its inputs, and the decisions it must make."""

    name: str
    description: str
    given: dict[str, Any]
    expect: dict[str, Any]
    path: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: str = "") -> Scenario:
        version = data.get("schema_version")
        if version != SCENARIO_SCHEMA_VERSION:
            raise CorpusError(
                f"{path or data.get('name')}: schema_version {version!r} is not "
                f"{SCENARIO_SCHEMA_VERSION}"
            )
        if not data.get("name"):
            raise CorpusError(f"{path}: scenario has no name")
        if not isinstance(data.get("expect"), dict) or not data["expect"]:
            raise CorpusError(f"{path}: scenario {data['name']!r} expects nothing")
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            given=dict(data.get("given") or {}),
            expect=dict(data["expect"]),
            path=path,
        )

    def ticket(self) -> dict[str, Any]:
        return dict(self.given.get("ticket") or {})


def load_corpus(corpus_dir: str | Path = CORPUS_DIR) -> list[Scenario]:
    """Read every scenario in ``corpus_dir``, sorted by filename.

    Sorted so a bench run is reproducible: the report's scenario order, and
    therefore its diff against a previous run, never depends on the filesystem.
    """
    root = Path(corpus_dir)
    files = sorted(root.glob("*.json")) if root.is_dir() else []
    if not files:
        raise CorpusError(f"no scenarios found under {root}")
    return [Scenario.from_dict(json.loads(p.read_text(encoding="utf-8")), str(p)) for p in files]


class _ScriptedRunner:
    """A :class:`~hsai.proc.Runner` that answers from the scenario, not the OS.

    The reproduce-before-fix guard is real code that shells out; replaying it
    honestly means letting it shell out, into this. ``pytest`` returns the
    scenario's declared fix-branch / pre-fix verdicts (told apart by the
    detached worktree's path, exactly as the live guard's own tests do), and
    every git invocation is a no-op success.
    """

    def __init__(self, *, root: str, fix_passes: bool, parent_passes: bool) -> None:
        self.root = root
        self.fix_passes = fix_passes
        self.parent_passes = parent_passes
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, **_: Any) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, f"{self.root}\n", "")
        if cmd[:1] == ["pytest"]:
            is_parent = bool(cwd) and "repro-check-" in str(cwd)
            ok = self.parent_passes if is_parent else self.fix_passes
            return Proc(cmd, 0 if ok else 1, "", "" if ok else "pytest: scripted failure\n")
        if cmd[:1] == ["git"]:
            return Proc(cmd, 0, "", "")
        raise AssertionError(f"bench runner: unexpected command {cmd!r} (nothing may shell out)")


@dataclass
class Replay:
    """One scenario after the real decision code has run over it."""

    scenario: Scenario
    traj: trajectory.IterationTrajectory
    budget: str = ledger.OK
    disposition: str = ""  # retry | blocked | "" (the ticket was not returned)
    seconds: float = 0.0

    def actual(self) -> dict[str, Any]:
        """Every observable a scenario is allowed to set an expectation on."""
        t = self.traj
        return {
            "kind": t.kind,
            "tier": t.tier,
            "model": t.model,
            "outcome": t.outcome,
            "recovered": t.recovered,
            "merged": t.merged,
            "review": t.review,
            "local_ci_before": t.local_ci_before,
            "local_ci_after": t.local_ci_after,
            "remote_ci": t.remote_ci,
            "attempts": t.attempts,
            "diff_stat": t.diff_stat,
            "budget": self.budget,
            "disposition": self.disposition,
        }

    def mismatches(self) -> list[str]:
        """Expectations this replay failed. Empty means the scenario passed."""
        actual = self.actual()
        out: list[str] = []
        for key, want in sorted(self.scenario.expect.items()):
            if key not in actual:
                out.append(f"{key}: unknown expectation (not an observable)")
            elif actual[key] != want:
                out.append(f"{key}: expected {want!r}, got {actual[key]!r}")
        return out


def _prior_records(scenario: Scenario, block: int) -> list[ledger.LedgerRecord]:
    """Expand the scenario's shorthand block history into real ledger records."""
    records = []
    for i, prior in enumerate(scenario.given.get("prior_iterations") or []):
        records.append(
            ledger.LedgerRecord(
                iteration=i + 1,
                block=block,
                ticket=None,
                kind=str(prior.get("kind", orchestrator.IMPLEMENT)),
                tier=str(prior.get("tier", "standard")),
                model=str(prior.get("model", "sonnet")),
                wall_clock_seconds=float(prior.get("seconds", 0.0)),
                attempts=int(prior.get("attempts", 1)),
                outcome=str(prior.get("outcome", orchestrator.MERGED)),
            )
        )
    return records


def _run_repro_guard(scenario: Scenario, touched: list[str], workspace: Path) -> repro.ReproResult:
    """Drive the real guard over a throwaway tree seeded with the declared tests."""
    settings = dict(scenario.given.get("repro") or {})
    wt = workspace / "wt"
    for rel in repro.changed_test_files(touched):
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# bench fixture: content is irrelevant, the verdict is scripted\n")
    runner = _ScriptedRunner(
        root=str(workspace),
        fix_passes=bool(settings.get("fix_passes", True)),
        parent_passes=bool(settings.get("parent_passes", False)),
    )
    return repro.check_repro(
        repo_root=str(workspace), wt=str(wt), base_ref="origin/main",
        test_files=repro.changed_test_files(touched),
        worktrees_dir="worktrees", runner=runner,
    )


def replay(scenario: Scenario, cfg: CoreConfig, *, iteration: int = 1, block: int = 0) -> Replay:
    """Run one scenario through the loop's real decision code.

    Mirrors the guard order of :func:`hsai.orchestrator._run_iteration`
    step for step. Any deviation here is a bug in the bench, not a judgement
    call - the point of the corpus is that it fails when the loop changes shape.
    """
    given = scenario.given
    ticket = scenario.ticket()
    title = str(ticket.get("title", ""))
    body = str(ticket.get("body", ""))
    labels = tuple(str(x) for x in ticket.get("labels") or ())
    prior_attempts = int(ticket.get("prior_attempts", 0))

    traj = trajectory.IterationTrajectory(iteration=iteration, block=block)
    out = Replay(scenario=scenario, traj=traj)
    started = time.monotonic()

    # 0. Budget gate (the cycle grades this *before* the iteration starts).
    spent = ledger.aggregate_block(_prior_records(scenario, block), block)
    decision = ledger.evaluate_budget(spent, dict(given.get("budget") or cfg.budget))
    out.budget = decision.status
    if decision.halt:
        traj.outcome = orchestrator.HALTED
        traj.note(f"budget hard breach: {decision.reason}")
        out.seconds = time.monotonic() - started
        return out

    # 1-3. Path choice and ticket claim.
    ci_green = bool(given.get("ci_green", True))
    traj.local_ci_before = "pass" if ci_green else "fail"
    kind = orchestrator.decide_path(ci_green, bool(given.get("has_tickets", True)))
    traj.kind = kind
    traj.ticket = ticket.get("number")
    traj.attempts = prior_attempts + 1

    # 4. Model selection, demoted under a soft breach exactly as the cycle does.
    choice = select(
        Task(kind=kind, title=title, body=body, labels=labels), cfg, demote=decision.demote
    )
    traj.tier, traj.model, traj.rationale = choice.tier, choice.model, choice.rationale

    # 5. The agent runs. The bench never invokes one - it only replays the
    # branch its declared outcome selects.
    agent = dict(given.get("agent") or {})
    traj.agent_ok = bool(agent.get("ok", True))
    traj.set_prompt(orchestrator._task_prompt(kind, cfg, title, body))
    if agent.get("timed_out"):
        traj.note("agent timed out")

    # Off-spec guard: unauthorized workflow edits are reverted before anything
    # else looks at the diff, so every guard below judges the post-revert tree.
    changed = [str(p) for p in given.get("changed_paths") or []]
    workflows = [p for p in changed if p.startswith(f"{orchestrator.WORKFLOW_DIR}/")]
    authorized = orchestrator.workflow_edits_authorized(title, body)
    touched = changed if authorized else [p for p in changed if p not in workflows]
    if workflows:
        traj.note(
            f"ticket authorizes workflow edits: {workflows}" if authorized
            else f"reverted {len(workflows)} off-spec workflow edit(s)"
        )
    traj.diff_stat = trajectory.diff_stat(touched)

    def _recover(outcome: str) -> Replay:
        traj.outcome = outcome
        traj.recovered = True
        out.disposition = orchestrator.retry_disposition(prior_attempts, cfg.max_ticket_attempts)
        out.seconds = time.monotonic() - started
        return out

    # 5a. Completeness guard: a code ticket needs code.
    if orchestrator._requires_code(title) and not [
        p for p in touched if not p.startswith("knowledge/")
    ]:
        traj.note("completeness guard: knowledge-only diff on a code ticket")
        return _recover(orchestrator.INCOMPLETE)

    # 5b. Reproduce-before-fix guard, run for real against a scripted runner.
    if repro.requires_repro_guard(kind, title):
        with TemporaryDirectory(prefix="hsai-bench-") as tmp:
            result = _run_repro_guard(scenario, touched, Path(tmp))
        traj.note(f"repro guard: {result.reason}")
        if not result.ok:
            return _recover(orchestrator.NO_REPRO)

    # 6. Local CI re-check.
    ci_after = bool(given.get("ci_after_green", True))
    traj.local_ci_after = "pass" if ci_after else "fail"

    # 6b. Independent review - skipped (and therefore approving) on a red branch,
    # because the CI gate already decides that one.
    approve = given.get("review_approve", True)
    if not ci_after:
        traj.review = "skipped"
    else:
        traj.review = "approve" if approve else "blocked"
        if not approve:
            traj.note("independent review blocked the change")
            return _recover(orchestrator.REVIEW_BLOCKED)

    # 12. Remote CI is the source of truth for whether the PR may merge.
    remote = str(given.get("remote_ci", "SUCCESS"))
    traj.remote_ci = remote
    traj.pr = ticket.get("pr")
    if remote == "SUCCESS":
        traj.merged = True
        traj.outcome = orchestrator.MERGED
        out.seconds = time.monotonic() - started
        return out
    traj.note(f"recovered: remote CI concluded {remote}")
    return _recover(orchestrator.RECOVERED)


@dataclass
class BenchReport:
    """What one bench run measured. Comparable to a committed baseline."""

    total: int = 0
    passed: int = 0
    tier_expected: int = 0
    tier_agreed: int = 0
    recovery_expected: int = 0
    recovery_correct: int = 0
    seconds: float = 0.0
    failures: list[dict[str, Any]] = field(default_factory=list)
    scenarios: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.passed == self.total

    @staticmethod
    def _ratio(num: int, den: int) -> float:
        """An undefined ratio is 1.0, not 0.0: a dimension nothing exercised
        must not read as a total failure of that dimension."""
        return 1.0 if den == 0 else round(num / den, 4)

    @property
    def pass_rate(self) -> float:
        return self._ratio(self.passed, self.total)

    @property
    def tier_agreement(self) -> float:
        """How often selection picked the tier the corpus says it should."""
        return self._ratio(self.tier_agreed, self.tier_expected)

    @property
    def recovery_accuracy(self) -> float:
        """How often the loop recovered exactly when it was supposed to."""
        return self._ratio(self.recovery_correct, self.recovery_expected)

    @property
    def mean_seconds(self) -> float:
        return 0.0 if not self.total else round(self.seconds / self.total, 4)

    def metrics(self) -> dict[str, Any]:
        return {
            "scenarios": self.total,
            "pass_rate": self.pass_rate,
            "tier_agreement": self.tier_agreement,
            "recovery_accuracy": self.recovery_accuracy,
        }

    def baseline_dict(self) -> dict[str, Any]:
        """The committed form: gated metrics only.

        No wall-clock and no per-scenario detail, so the baseline diff on a PR
        is one or two numbers a human can actually read and argue with.
        """
        return {"schema_version": SCENARIO_SCHEMA_VERSION, **self.metrics()}

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.baseline_dict(),
            "passed": self.passed,
            "mean_seconds_per_scenario": self.mean_seconds,
            "failures": self.failures,
            "results": self.scenarios,
        }

    def render(self) -> str:
        lines = [
            f"bench: {self.passed}/{self.total} scenarios passed "
            f"({self.pass_rate:.0%} pass-rate)",
            f"  tier agreement:    {self.tier_agreement:.0%} "
            f"({self.tier_agreed}/{self.tier_expected})",
            f"  recovery accuracy: {self.recovery_accuracy:.0%} "
            f"({self.recovery_correct}/{self.recovery_expected})",
            f"  mean seconds/ticket: {self.mean_seconds:.4f}s "
            f"(total {self.seconds:.3f}s, no model invoked)",
        ]
        for failure in self.failures:
            lines.append(f"  FAIL {failure['scenario']}")
            for reason in failure["mismatches"]:
                lines.append(f"    - {reason}")
        return "\n".join(lines)


def run_bench(cfg: CoreConfig, corpus_dir: str | Path = CORPUS_DIR) -> BenchReport:
    """Replay the whole corpus and fold the results into a report."""
    report = BenchReport()
    for i, scenario in enumerate(load_corpus(corpus_dir)):
        result = replay(scenario, cfg, iteration=i + 1)
        mismatches = result.mismatches()
        report.total += 1
        report.seconds += result.seconds
        if not mismatches:
            report.passed += 1
        else:
            report.failures.append({"scenario": scenario.name, "mismatches": mismatches})
        if "tier" in scenario.expect:
            report.tier_expected += 1
            report.tier_agreed += scenario.expect["tier"] == result.traj.tier
        if "recovered" in scenario.expect:
            report.recovery_expected += 1
            report.recovery_correct += scenario.expect["recovered"] == result.traj.recovered
        report.scenarios.append(
            {
                "scenario": scenario.name,
                "ok": not mismatches,
                "trajectory": json.loads(result.traj.to_json(forbid_env=cfg.forbidden_env)),
                "budget": result.budget,
                "disposition": result.disposition,
            }
        )
    return report


def read_baseline(path: str | Path = BASELINE_PATH) -> dict[str, Any]:
    baseline = Path(path)
    if not baseline.is_file():
        raise CorpusError(f"no baseline at {baseline}; create one with `hsai bench --update-baseline`")
    return json.loads(baseline.read_text(encoding="utf-8"))


def write_baseline(report: BenchReport, path: str | Path = BASELINE_PATH) -> Path:
    """Record ``report`` as the new gate. Only ever run deliberately, by hand."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report.baseline_dict(), indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return target


def regressions(report: BenchReport, baseline: dict[str, Any]) -> list[str]:
    """Every way ``report`` is worse than ``baseline``. Empty means no regression.

    Corpus size is checked too, so deleting an inconvenient scenario is a
    regression rather than a way to make the gate green.
    """
    found = []
    expected = int(baseline.get("scenarios", 0))
    if report.total < expected:
        found.append(f"scenarios: {report.total} < baseline {expected} (scenarios were removed)")
    for metric in GATED_METRICS:
        want = float(baseline.get(metric, 0.0))
        got = float(report.metrics()[metric])
        if got < want:
            found.append(f"{metric}: {got:.4f} < baseline {want:.4f}")
    return found
