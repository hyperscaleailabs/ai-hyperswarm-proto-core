"""Offline replay bench: measure the harness instead of asserting about it.

Every other gate in this repo checks the *change* an iteration produced. Nothing
checked the orchestration itself, so a model-selection tweak, a retry-policy
change, or a new guard could only ever be argued for - the ledger knew what an
iteration cost but nothing knew whether it was any good. And because loop
workers cannot run pytest or ruff inside their worktrees, an offline measurement
is the only feedback channel that can exist at all.

The bench replays a corpus of hand-authored iteration scenarios through the
*real* decision code - :func:`hsai.orchestrator.decide_path`,
:func:`hsai.models.select`, :func:`hsai.orchestrator._requires_code`,
:func:`hsai.repro.requires_repro_guard` / :func:`hsai.repro.check_repro`,
:func:`hsai.review.parse_verdict`, :func:`hsai.ledger.evaluate_budget` - driven
by a fake :class:`~hsai.proc.Runner`. No model is invoked, no network is
touched, no quota is spent: the fake runner refuses a ``claude`` command
outright. That is what lets `hsai bench --check` run as an always-on CI gate
against a committed baseline.

Synthesis: SWE-agent (a run is a replayable, inspectable object and the harness
is benchmarked rather than assumed), langchain (record-and-replay cassettes plus
a performance gate wired as a required check with a committed baseline), and
FoundationAgents/MetaGPT (split the cheap always-on test tier from the heavy one
- the bench is the cheap tier, and it never calls a model).
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import ci, ledger, repro, review
from .config import CoreConfig
from .models import Task, select
from .orchestrator import _requires_code, decide_path
from .proc import Proc

DEFAULT_CORPUS_DIR = "tests/fixtures/trajectories"
DEFAULT_BASELINE = "bench/baseline.json"

# Bumped when a scenario file's shape changes incompatibly.
SCENARIO_SCHEMA_VERSION = 1
BASELINE_SCHEMA_VERSION = 1

# Terminal outcomes a replay can reach. These are the orchestrator's own ledger
# outcomes plus ``halted``, which is the budget gate refusing to start work.
MERGED = "merged"
RECOVERED = "recovered"
INCOMPLETE = "incomplete"
NO_REPRO = "no_repro"
REVIEW_BLOCKED = "review_blocked"
HALTED = "halted"

# How a ticket was left afterwards - the retry policy's observable effect.
RETRY = "retry"
BLOCKED = "blocked"
NONE = "none"

# The bench measures decisions, not wall-clock, so a slower corpus is only a
# regression once it is meaningfully slower.
SECONDS_TOLERANCE = 0.25


class ModelInvokedError(AssertionError):
    """Raised if a replay ever tries to spawn a model - the bench's core invariant."""


class BenchRunner:
    """Answers every shell-out a replayed guard makes, without a subprocess.

    Deliberately explicit rather than a catch-all: a command the bench did not
    expect returns a benign success, but a ``claude`` invocation raises, so the
    "never calls a model" property is enforced instead of hoped for.
    """

    def __init__(self, root: Path, *, fix_ok: bool, parent_ok: bool) -> None:
        self.root = root
        self.fix_ok = fix_ok
        self.parent_ok = parent_ok
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None,
                 timeout=None, input_text=None) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:1] == ["claude"]:
            raise ModelInvokedError("hsai bench must never invoke a model")
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, f"{self.root}\n", "")
        if cmd[:3] == ["git", "worktree", "add"]:
            # `create_detached_worktree` passes ["worktree","add","--detach",path,ref].
            Path(cmd[-2]).mkdir(parents=True, exist_ok=True)
            return Proc(cmd, 0, "", "")
        if cmd[:1] == ["pytest"]:
            # The pre-fix (parent) tree is the throwaway detached worktree; the
            # fix branch is everything else.
            is_parent = bool(cwd) and "repro-check-" in str(cwd)
            ok = self.parent_ok if is_parent else self.fix_ok
            return Proc(cmd, 0 if ok else 1, "", "" if ok else "pytest: 1 failed\n")
        return Proc(cmd, 0, "", "")


def _no_subprocess(cmd, **_kw) -> Proc:
    raise AssertionError(f"bench replay made an unexpected shell-out: {list(cmd)!r}")


@dataclass(frozen=True)
class Scenario:
    """One hand-authored iteration: the world it ran in, and how it should end."""

    id: str
    title: str
    given: dict
    expect: dict
    source: str = ""

    @classmethod
    def from_dict(cls, data: dict, source: str = "") -> Scenario:
        version = int(data.get("schema_version", 0))
        if version != SCENARIO_SCHEMA_VERSION:
            raise ValueError(
                f"{source or data.get('id')}: scenario schema_version {version} "
                f"is not readable (expected {SCENARIO_SCHEMA_VERSION})"
            )
        for key in ("id", "given", "expect"):
            if key not in data:
                raise ValueError(f"{source or '<scenario>'}: missing required key {key!r}")
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", "")),
            given=dict(data["given"]),
            expect=dict(data["expect"]),
            source=source,
        )

    @classmethod
    def load(cls, path: str | Path) -> Scenario:
        path = Path(path)
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")), str(path))


@dataclass(frozen=True)
class Replay:
    """What the real decision code did when handed a scenario."""

    kind: str
    tier: str
    model: str
    outcome: str
    recovery: str
    guard: str = ""
    seconds: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class Deviation:
    """One expectation the replay did not meet - the unit `--check` fails on."""

    scenario: str
    field: str
    expected: str
    actual: str

    def render(self) -> str:
        return (
            f"{self.scenario}: {self.field} expected {self.expected!r}, "
            f"got {self.actual!r}"
        )


@dataclass
class BenchReport:
    """Aggregate result over a corpus - what the baseline pins."""

    scenarios: int = 0
    passed: int = 0
    tier_matches: int = 0
    tier_checked: int = 0
    recovery_matches: int = 0
    recovery_checked: int = 0
    total_seconds: float = 0.0
    deviations: list[Deviation] = field(default_factory=list)
    replays: dict[str, Replay] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.deviations and self.scenarios > 0

    @property
    def pass_rate(self) -> float:
        return self.passed / self.scenarios if self.scenarios else 0.0

    @property
    def tier_agreement(self) -> float:
        return self.tier_matches / self.tier_checked if self.tier_checked else 1.0

    @property
    def recovery_accuracy(self) -> float:
        return (
            self.recovery_matches / self.recovery_checked if self.recovery_checked else 1.0
        )

    @property
    def mean_seconds(self) -> float:
        return round(self.total_seconds / self.scenarios, 3) if self.scenarios else 0.0

    def metrics(self) -> dict:
        """The comparable numbers - exactly what ``bench/baseline.json`` holds."""
        return {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "scenarios": self.scenarios,
            "pass_rate": round(self.pass_rate, 6),
            "tier_agreement": round(self.tier_agreement, 6),
            "recovery_accuracy": round(self.recovery_accuracy, 6),
            "mean_seconds_per_ticket": self.mean_seconds,
        }

    def to_dict(self) -> dict:
        return {
            "metrics": self.metrics(),
            "deviations": [asdict(d) for d in self.deviations],
            "replays": {k: asdict(v) for k, v in sorted(self.replays.items())},
        }

    def render(self) -> str:
        lines = [
            f"hsai bench: {self.passed}/{self.scenarios} scenarios pass "
            f"({self.pass_rate:.0%})",
            f"  tier agreement:    {self.tier_agreement:.0%} "
            f"({self.tier_matches}/{self.tier_checked})",
            f"  recovery accuracy: {self.recovery_accuracy:.0%} "
            f"({self.recovery_matches}/{self.recovery_checked})",
            f"  mean seconds/ticket: {self.mean_seconds:.1f}s",
        ]
        for name in sorted(self.replays):
            r = self.replays[name]
            mark = "ok  " if not any(d.scenario == name for d in self.deviations) else "FAIL"
            lines.append(
                f"  [{mark}] {name}: {r.kind}/{r.tier} -> {r.outcome}"
                + (f" (guard: {r.guard})" if r.guard else "")
            )
        for dev in self.deviations:
            lines.append(f"  ! {dev.render()}")
        return "\n".join(lines)


def _recovery_for(attempts: int, cfg: CoreConfig) -> str:
    """Mirror of ``_recover_failed``: retry, or blocked once attempts run out."""
    return BLOCKED if attempts >= cfg.max_ticket_attempts else RETRY


def _replay_repro_guard(scenario: Scenario, cfg: CoreConfig,
                        changed: list[str]) -> repro.ReproResult:
    """Run the REAL reproduce-before-fix guard against a scratch tree.

    The guard copies the changed test files onto a pre-fix worktree and runs
    pytest on both sides; here both worktrees are temporary directories and
    both pytest runs are answered by :class:`BenchRunner` from the scenario.
    The control flow being exercised is the shipped one.
    """
    test_files = repro.changed_test_files(changed)
    if not test_files:
        # The guard short-circuits before it shells out at all.
        return repro.check_repro(
            repo_root=".", wt=".", base_ref="origin/main", test_files=[],
            worktrees_dir=cfg.worktrees_dir, runner=_no_subprocess,
        )
    spec = dict(scenario.given.get("repro") or {})
    with tempfile.TemporaryDirectory(prefix="hsai-bench-") as tmp:
        root = Path(tmp)
        wt = root / "fix"
        for rel in test_files:
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# replayed regression test\n", encoding="utf-8")
        runner = BenchRunner(
            root,
            fix_ok=bool(spec.get("fix_pytest_ok", True)),
            parent_ok=bool(spec.get("parent_pytest_ok", False)),
        )
        return repro.check_repro(
            repo_root=str(root), wt=str(wt), base_ref="origin/main",
            test_files=test_files, worktrees_dir="worktrees", runner=runner,
        )


def replay(scenario: Scenario, cfg: CoreConfig) -> Replay:
    """Drive one scenario through the orchestrator's decision path.

    The order here deliberately mirrors :func:`hsai.orchestrator.run_once`:
    budget gate, path choice, tier selection, completeness guard, repro guard,
    local CI, independent review, remote CI. A guard reordered in the
    orchestrator without being reordered here shows up as a corpus deviation,
    which is the point.
    """
    given = scenario.given
    ticket = dict(given.get("ticket") or {})
    title = str(ticket.get("title", ""))
    changed = [str(p) for p in given.get("changed_paths") or []]
    attempts = int(given.get("attempts", 1))
    agent = dict(given.get("agent") or {})
    agent_ok = bool(agent.get("ok", True))
    seconds = round(
        float(agent.get("seconds", 0.0)) + float(given.get("overhead_seconds", 0.0)), 3
    )

    # 1. Budget gate - graded before any work starts, exactly as `hsai cycle` does.
    spend = dict(given.get("block_spend") or {})
    agg = ledger.BlockAggregate(
        block=int(given.get("block", 0)),
        iterations=int(spend.get("iterations", 0)),
        heavy_iterations=int(spend.get("heavy_iterations", 0)),
        merged_iterations=int(spend.get("merged_iterations", 0)),
        total_seconds=float(spend.get("total_seconds", 0.0)),
    )
    budget = ledger.evaluate_budget(agg, dict(given.get("budget") or cfg.budget))

    # 2. Which branch of the loop, and on which tier.
    kind = decide_path(bool(given.get("ci_green", True)), bool(given.get("open_tickets", 0)))
    task = Task(
        kind=kind,
        title=title,
        body=str(ticket.get("body", "")),
        labels=tuple(str(x) for x in ticket.get("labels") or ()),
        est_files=max(1, len(changed)),
    )
    choice = select(task, cfg, demote=budget.demote)

    def done(outcome: str, recovery: str, guard: str = "", detail: str = "") -> Replay:
        return Replay(
            kind=kind, tier=choice.tier, model=choice.model, outcome=outcome,
            recovery=recovery, guard=guard, seconds=seconds, detail=detail,
        )

    if budget.halt:
        # Nothing is started, so nothing is spent.
        return Replay(
            kind=kind, tier=choice.tier, model=choice.model, outcome=HALTED,
            recovery=NONE, guard="budget", seconds=0.0, detail=budget.reason,
        )

    # 3. Completeness guard: a code ticket may not be closed by a knowledge-only diff.
    if _requires_code(title) and not [p for p in changed if not p.startswith("knowledge/")]:
        return done(
            INCOMPLETE, _recovery_for(attempts, cfg), guard="completeness",
            detail="knowledge-only diff on a code ticket",
        )

    # 4. Reproduce-before-fix guard.
    if repro.requires_repro_guard(kind, title):
        result = _replay_repro_guard(scenario, cfg, changed)
        if not result.ok:
            return done(NO_REPRO, _recovery_for(attempts, cfg), guard="repro",
                        detail=result.reason)

    # 5. Local CI. An agent that failed or timed out leaves a red branch unless
    #    the scenario says otherwise.
    ci_green = bool(given.get("local_ci_after_green", agent_ok))

    # 6. Independent review - skipped on a red branch, as the orchestrator does,
    #    because the CI gate already decides that one.
    if ci_green and review.is_enabled(cfg):
        verdict = review.parse_verdict(str(given.get("review_output", "")))
        if not verdict.approve:
            return done(REVIEW_BLOCKED, _recovery_for(attempts, cfg), guard="review",
                        detail="; ".join(verdict.blocking))

    # 7. Remote CI is the source of truth for whether this merges.
    remote = str(given.get("remote_ci", ""))
    if remote == ci.SUCCESS:
        return done(MERGED, NONE, detail=remote)
    return done(RECOVERED, _recovery_for(attempts, cfg), guard="remote_ci", detail=remote)


def load_corpus(directory: str | Path = DEFAULT_CORPUS_DIR) -> list[Scenario]:
    """Read every scenario in ``directory``, sorted by id for a stable report."""
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"no bench corpus at {root}")
    scenarios = [Scenario.load(p) for p in sorted(root.glob("*.json"))]
    if not scenarios:
        raise FileNotFoundError(f"bench corpus at {root} is empty")
    seen: set[str] = set()
    for s in scenarios:
        if s.id in seen:
            raise ValueError(f"duplicate scenario id {s.id!r} in {root}")
        seen.add(s.id)
    return scenarios


def run_bench(
    cfg: CoreConfig, corpus_dir: str | Path = DEFAULT_CORPUS_DIR
) -> BenchReport:
    """Replay the whole corpus and fold the results into one report."""
    report = BenchReport()
    for scenario in load_corpus(corpus_dir):
        result = replay(scenario, cfg)
        report.scenarios += 1
        report.replays[scenario.id] = result
        report.total_seconds += result.seconds

        deviations: list[Deviation] = []
        for name, actual in (
            ("kind", result.kind),
            ("tier", result.tier),
            ("outcome", result.outcome),
            ("recovery", result.recovery),
            ("guard", result.guard),
        ):
            if name not in scenario.expect:
                continue
            expected = str(scenario.expect[name])
            matched = expected == actual
            if name == "tier":
                report.tier_checked += 1
                report.tier_matches += int(matched)
            if name == "recovery":
                report.recovery_checked += 1
                report.recovery_matches += int(matched)
            if not matched:
                deviations.append(Deviation(scenario.id, name, expected, actual))
        report.deviations.extend(deviations)
        report.passed += int(not deviations)
    return report


def read_baseline(path: str | Path = DEFAULT_BASELINE) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = int(data.get("schema_version", 0))
    if version != BASELINE_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: baseline schema_version {version} is not readable "
            f"(expected {BASELINE_SCHEMA_VERSION})"
        )
    return data


def write_baseline(report: BenchReport, path: str | Path = DEFAULT_BASELINE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.metrics(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def check_regression(report: BenchReport, baseline: dict) -> list[str]:
    """Grade a report against the committed baseline. Empty list = no regression.

    Quality metrics may only go up; the corpus may only grow (shrinking it is
    coverage loss, which is exactly the regression a benchmark exists to catch);
    mean seconds may drift up by :data:`SECONDS_TOLERANCE` before it counts.
    """
    problems: list[str] = []
    now = report.metrics()
    for key in ("pass_rate", "tier_agreement", "recovery_accuracy"):
        was = float(baseline.get(key, 0.0))
        # Float tolerance: these are ratios of small integers, never near-misses.
        if now[key] + 1e-9 < was:
            problems.append(f"{key} regressed: {now[key]:.4f} < baseline {was:.4f}")
    if now["scenarios"] < int(baseline.get("scenarios", 0)):
        problems.append(
            f"corpus shrank: {now['scenarios']} scenarios < baseline "
            f"{baseline.get('scenarios')}"
        )
    was_seconds = float(baseline.get("mean_seconds_per_ticket", 0.0))
    ceiling = was_seconds * (1.0 + SECONDS_TOLERANCE)
    if was_seconds and now["mean_seconds_per_ticket"] > ceiling:
        problems.append(
            f"mean_seconds_per_ticket regressed: "
            f"{now['mean_seconds_per_ticket']:.1f}s > {ceiling:.1f}s "
            f"(baseline {was_seconds:.1f}s + {SECONDS_TOLERANCE:.0%})"
        )
    return problems
