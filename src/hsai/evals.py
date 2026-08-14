"""A labeled benchmark for the decision core, enforced as a pytest baseline.

Every claim of learning in this harness used to be unmeasured: ``models.py``
documented its thresholds as "calibrated by iterating and comparing against
actual task complexity over multiple runs", but no such comparison existed -
the numbers were asserted. This module makes the comparison real.

- ``evals/cases.yaml`` holds labeled cases seeded from this repo's own history
  (ledger records, lesson notes, past tickets, ADR-0001). Each case names the
  artifact it came from, so a label can always be audited back to evidence.
- :func:`run_suite` replays those cases through the five pure decision
  functions the loop actually steers on and returns a :class:`Scorecard`:
  per-function accuracy, a tier-cost proxy, and the mismatched case ids.
- ``evals/baseline.json`` freezes the current scorecard. :func:`compare` grades
  a fresh run against it, and ``tests/test_evals.py`` turns a regression into a
  failing test.

The enforcement point is deliberate. Workers may not edit ``.github/workflows``
(``run_once`` reverts any such diff), so pytest is the only gate a
self-improving worker can legitimately strengthen - and it is where this
benchmark lives.

Synthesis: microsoft/JARVIS's TaskBench (an explicit benchmark for controller
model-selection quality), SWE-agent's SWE-bench discipline of justifying every
agent change with a scored delta rather than an assertion, openai/swarm's
``evals/`` pattern of testing pure orchestration primitives directly, and
FoundationAgents/MetaGPT's per-role artifact evaluation (the per-function
breakdown).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import ledger, models, repro, tickets
from .config import CORE_PATH, CoreConfig
from .orchestrator import decide_path

CASES_FILE = "evals/cases.yaml"
BASELINE_FILE = "evals/baseline.json"
BASELINE_SCHEMA = 1

# The decision functions under test. Names are the audit surface: they appear in
# the scorecard, in the baseline, and in every mismatch line.
FUNCTIONS: tuple[str, ...] = (
    "decide_path",
    "ledger.evaluate_budget",
    "models.select",
    "repro.requires_repro_guard",
    "tickets.check_well_formed",
)

# Relative quota weight per tier. Not dollars - a proxy that makes "the suite
# got cheaper" and "the suite got better" two separately observable numbers, so
# a heuristic cannot buy accuracy with unbounded heavy-tier spend.
TIER_COST: dict[str, int] = {"light": 1, "standard": 2, "heavy": 3}

# Accuracies are stored rounded to 6 decimals so the baseline stays readable,
# which can round a value UP by as much as 5e-7 - an exact `<` would then read
# that rounding as a regression. The tolerance absorbs it and is still four
# orders of magnitude below the ~1/N a single flipped case moves.
_EPSILON = 1e-6

Selector = Callable[[models.Task, CoreConfig], models.ModelChoice]


class EvalError(ValueError):
    """A malformed case file - raised eagerly so a typo cannot score as a pass."""


# --- case loading -------------------------------------------------------------


@dataclass(frozen=True)
class EvalCase:
    """One labeled case: where it came from, and what each function must answer."""

    id: str
    source: str
    probes: dict[str, dict[str, Any]]


def repo_root(start: str | Path | None = None) -> Path:
    """The repo root, i.e. the nearest ancestor holding ``.ai-swarm/core.yaml``.

    Searched from ``start`` (or cwd) first - mirroring :func:`config.load_config`
    - then from this module, so the suite is loadable from an installed package
    inside a checkout regardless of the caller's working directory.
    """
    for origin in (Path(start or Path.cwd()), Path(__file__).resolve().parent):
        here = origin.resolve()
        for base in [here, *here.parents]:
            if (base / CORE_PATH).is_file():
                return base
    raise FileNotFoundError(f"could not locate {CORE_PATH} from {start or Path.cwd()}")


def cases_path(root: str | Path | None = None) -> Path:
    return Path(root) / CASES_FILE if root else repo_root() / CASES_FILE


def baseline_path(root: str | Path | None = None) -> Path:
    return Path(root) / BASELINE_FILE if root else repo_root() / BASELINE_FILE


def load_cases(path: str | Path | None = None) -> list[EvalCase]:
    """Parse ``evals/cases.yaml`` into :class:`EvalCase` objects.

    Validation is strict on purpose: an unknown probe name or a missing
    ``expect`` would otherwise silently shrink the benchmark while the
    scorecard still reported a clean 100%.
    """
    path = Path(path) if path is not None else cases_path()
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # one exception type for "the case file is bad"
        raise EvalError(f"{path}: not valid YAML - {exc}") from exc
    raw = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise EvalError(f"{path}: expected a non-empty 'cases:' list")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise EvalError(f"{path}: case #{i} is not a mapping")
        cid = str(entry.get("id", "")).strip()
        if not cid:
            raise EvalError(f"{path}: case #{i} has no id")
        if cid in seen:
            raise EvalError(f"{path}: duplicate case id {cid!r}")
        seen.add(cid)
        if not str(entry.get("source", "")).strip():
            raise EvalError(f"{path}: case {cid!r} has no 'source' (traceability)")
        probes = entry.get("probes")
        if not isinstance(probes, dict) or not probes:
            raise EvalError(f"{path}: case {cid!r} has no 'probes'")
        for name, spec in probes.items():
            if name not in FUNCTIONS:
                raise EvalError(f"{path}: case {cid!r} probes unknown function {name!r}")
            if not isinstance(spec, dict) or "expect" not in spec:
                raise EvalError(f"{path}: case {cid!r} probe {name!r} has no 'expect'")
        cases.append(EvalCase(id=cid, source=str(entry["source"]).strip(), probes=dict(probes)))
    return cases


# --- scoring ------------------------------------------------------------------


@dataclass(frozen=True)
class FunctionScore:
    """How one decision function did across every case that probes it."""

    name: str
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        """Share of probes answered correctly (1.0 when a function is unprobed)."""
        return 1.0 if not self.total else self.correct / self.total


@dataclass(frozen=True)
class Mismatch:
    case_id: str
    function: str
    expected: str
    actual: str

    def line(self) -> str:
        return f"{self.case_id}: {self.function} expected {self.expected}, got {self.actual}"


@dataclass(frozen=True)
class Scorecard:
    """One benchmark run: what the decision core answered, and what it cost."""

    cases: int = 0
    scores: dict[str, FunctionScore] = field(default_factory=dict)
    tier_cost: int = 0
    mismatches: tuple[Mismatch, ...] = ()

    @property
    def probes(self) -> int:
        return sum(s.total for s in self.scores.values())

    def accuracy(self, function: str) -> float:
        score = self.scores.get(function)
        return score.accuracy if score else 1.0

    def summary(self) -> str:
        """The one-liner the block review brief carries next to the cost line."""
        per_fn = ", ".join(
            f"{name}={self.scores[name].accuracy:.0%} "
            f"({self.scores[name].correct}/{self.scores[name].total})"
            for name in sorted(self.scores)
        )
        missed = ", ".join(m.case_id for m in self.mismatches) or "none"
        return (
            f"{self.cases} cases / {self.probes} probes - {per_fn}; "
            f"tier-cost={self.tier_cost}; mismatched: {missed}"
        )

    def render(self) -> str:
        """Readable scorecard for ``hsai eval``."""
        lines = [
            "hsai eval - decision-core scorecard",
            f"  cases: {self.cases}   probes: {self.probes}   "
            f"tier-cost proxy: {self.tier_cost}",
        ]
        for name in sorted(self.scores):
            s = self.scores[name]
            lines.append(f"  {name:<28} {s.accuracy:7.1%}  ({s.correct}/{s.total})")
        if self.mismatches:
            lines.append(f"  mismatched case ids ({len(self.mismatches)}):")
            lines.extend(f"    - {m.line()}" for m in self.mismatches)
        else:
            lines.append("  mismatched case ids: none")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable form - also the on-disk baseline schema."""
        return {
            "schema": BASELINE_SCHEMA,
            "cases": self.cases,
            "probes": self.probes,
            "tier_cost": self.tier_cost,
            "accuracy": {n: round(s.accuracy, 6) for n, s in sorted(self.scores.items())},
            "counts": {
                n: {"correct": s.correct, "total": s.total}
                for n, s in sorted(self.scores.items())
            },
            "mismatches": [m.line() for m in self.mismatches],
        }


def _as_task(spec: dict[str, Any]) -> models.Task:
    task = spec.get("task") or {}
    return models.Task(
        kind=str(task.get("kind", "implement")),
        title=str(task.get("title", "")),
        body=str(task.get("body", "")),
        labels=tuple(str(x) for x in (task.get("labels") or ())),
        est_files=int(task.get("est_files", 1)),
    )


def _as_aggregate(spec: dict[str, Any]) -> ledger.BlockAggregate:
    agg = spec.get("aggregate") or {}
    return ledger.BlockAggregate(
        block=int(agg.get("block", 0)),
        iterations=int(agg.get("iterations", 0)),
        heavy_iterations=int(agg.get("heavy_iterations", 0)),
        total_seconds=float(agg.get("total_seconds", 0.0)),
    )


def run_suite(
    cfg: CoreConfig,
    cases: Iterable[EvalCase],
    *,
    selector: Selector = models.select,
) -> Scorecard:
    """Replay ``cases`` through the decision core and score the answers.

    ``selector`` exists so a test can inject a deliberately degraded
    ``models.select`` and prove the baseline gate actually fails on regression -
    the alternative (monkeypatching the module) would leave the gate itself
    untested.
    """
    correct: dict[str, int] = {}
    total: dict[str, int] = {}
    mismatches: list[Mismatch] = []
    tier_cost = 0
    case_count = 0

    for case in cases:
        case_count += 1
        for name, spec in sorted(case.probes.items()):
            expected = str(spec["expect"])
            if name == "decide_path":
                actual = decide_path(
                    bool(spec.get("ci_green", True)), bool(spec.get("has_tickets", False))
                )
            elif name == "models.select":
                choice = selector(_as_task(spec), cfg)
                actual = choice.tier
                tier_cost += TIER_COST.get(choice.tier, 0)
            elif name == "tickets.check_well_formed":
                actual = str(
                    tickets.check_well_formed(
                        str(spec.get("title", "")), str(spec.get("body", ""))
                    ).ok
                )
            elif name == "repro.requires_repro_guard":
                actual = str(
                    repro.requires_repro_guard(
                        str(spec.get("kind", "implement")), str(spec.get("title", ""))
                    )
                )
            else:  # ledger.evaluate_budget
                budget = spec.get("budget")
                actual = ledger.evaluate_budget(
                    _as_aggregate(spec), cfg.budget if budget is None else budget
                ).status

            total[name] = total.get(name, 0) + 1
            if str(actual) == expected:
                correct[name] = correct.get(name, 0) + 1
            else:
                mismatches.append(Mismatch(case.id, name, expected, str(actual)))

    scores = {
        name: FunctionScore(name=name, correct=correct.get(name, 0), total=total[name])
        for name in total
    }
    return Scorecard(
        cases=case_count,
        scores=scores,
        tier_cost=tier_cost,
        mismatches=tuple(mismatches),
    )


# --- baseline -----------------------------------------------------------------


@dataclass(frozen=True)
class Regression:
    """One reason a scorecard is worse than the committed baseline."""

    kind: str  # accuracy | coverage | tier-cost
    detail: str

    def line(self) -> str:
        return f"[{self.kind}] {self.detail}"


def load_baseline(path: str | Path | None = None) -> dict[str, Any] | None:
    """Read the committed baseline (``None`` when it has not been written yet)."""
    path = Path(path) if path is not None else baseline_path()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(path: str | Path, card: Scorecard) -> Path:
    """Freeze ``card`` as the new baseline (``hsai eval --update-baseline``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def compare(card: Scorecard, baseline: dict[str, Any]) -> list[Regression]:
    """Grade ``card`` against ``baseline``; an empty list means no regression.

    Three ways to regress:

    - **accuracy** - any per-function accuracy drops below its baseline;
    - **coverage** - a function is probed by fewer cases than the baseline
      recorded, which would let accuracy "improve" by deleting hard cases;
    - **tier-cost** - the tier-cost proxy rises with no accuracy gain anywhere,
      i.e. the heuristic bought nothing with the extra quota.
    """
    regressions: list[Regression] = []
    base_acc: dict[str, Any] = baseline.get("accuracy") or {}
    base_counts: dict[str, Any] = baseline.get("counts") or {}

    gained = False
    for name, expected in sorted(base_acc.items()):
        actual = card.accuracy(name)
        if actual < float(expected) - _EPSILON:
            regressions.append(
                Regression(
                    "accuracy",
                    f"{name}: {actual:.1%} < baseline {float(expected):.1%}",
                )
            )
        elif actual > float(expected) + _EPSILON:
            gained = True

    for name, counts in sorted(base_counts.items()):
        was = int((counts or {}).get("total", 0))
        now = card.scores[name].total if name in card.scores else 0
        if now < was:
            regressions.append(
                Regression("coverage", f"{name}: {now} probe(s) < baseline {was}")
            )

    base_cost = baseline.get("tier_cost")
    if base_cost is not None and card.tier_cost > int(base_cost) and not gained:
        regressions.append(
            Regression(
                "tier-cost",
                f"tier-cost proxy {card.tier_cost} > baseline {int(base_cost)} "
                "with no accuracy gain",
            )
        )
    return regressions


def render_regressions(regressions: Sequence[Regression]) -> str:
    if not regressions:
        return "eval: no regression against the committed baseline"
    return "\n".join(
        ["eval: REGRESSION against the committed baseline:", *(f"  - {r.line()}" for r in regressions)]
    )


def score_repo(cfg: CoreConfig, root: str | Path | None = None) -> Scorecard:
    """Load ``evals/cases.yaml`` under ``root`` and score it - the common path."""
    return run_suite(cfg, load_cases(cases_path(root)))


def score_or_none(cfg: CoreConfig, root: str | Path | None = None) -> Scorecard | None:
    """:func:`score_repo`, but a missing or malformed suite never breaks a block.

    The review brief is a reporting surface: a broken case file should show up
    as "not scored" in the brief, not abort the governance block that renders it.
    """
    try:
        return score_repo(cfg, root)
    except (OSError, EvalError):
        return None
