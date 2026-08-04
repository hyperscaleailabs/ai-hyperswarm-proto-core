"""Offline replay harness: grade a model-selection strategy against recorded outcomes.

``models.select`` is the harness's economic control surface, and before this
module it was entirely uncalibrated - hand-chosen keyword weights and
thresholds defended by comments claiming a calibration that no artifact,
dataset, or measurement anywhere in the repo could substantiate. The ledger
recorded which tier ran each iteration and how it ended, but nothing ever read
those records back to *grade* the decision.

This module closes that loop. A :class:`LabeledTask` pairs a real (or
deliberately adversarial) task with the tier a human judged correct for it; the
committed, version-stamped corpus at :data:`CORPUS_PATH` is the fixed benchmark
set; :func:`replay` runs any registered strategy over it and returns a
:class:`ReplayReport` with a confusion matrix, over-/under-provision rates, and
an estimate of the quota the strategy saves. A single scalar
(:attr:`ReplayReport.score`) is what CI gates on, so an edit to ``_score`` that
regresses routing is a red build rather than a drift discovered months later.

Three properties make the benchmark trustworthy:

- **Offline.** Scoring spends no quota and touches no network: a strategy is a
  pure function of the task and the configured tiers. ``hsai replay`` therefore
  runs in CI on every PR, on a subscription-only harness, for free.
- **Committed.** The corpus is a reviewable file in git. The loop *proposes*
  candidates (``hsai corpus-build`` mines closed issues, the ledger, and the
  lessons); a human *labels* them. The loop never grades its own homework.
- **Asymmetric.** Under-provisioning and over-provisioning are not equally bad,
  and the score says so (see :data:`UNDER_WEIGHT` / :data:`OVER_WEIGHT`).

Synthesis: SWE-agent (grade the agent's decisions against a fixed, committed
instance set rather than asserting them in comments), microsoft/JARVIS
(model selection is an explicit, separately-benchmarkable stage - cf. TaskBench),
run-llama/llama_index (benchmark thresholds as hard numeric CI gates), and
OpenBMB/ChatDev (the economic thesis: activate cheaper agents where they
suffice, which is what over-provision rate measures).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from . import github
from .config import CoreConfig
from .knowledge import KnowledgeBase
from .ledger import LedgerRecord, ledger_path, read_records
from .models import Task, get_strategy
from .proc import Runner, run

CORPUS_PATH = "knowledge/eval/selection-corpus.jsonl"
DRAFT_PATH = "knowledge/eval/selection-corpus.draft.jsonl"

# Bumped whenever the record schema changes incompatibly. The header line
# carries it so a stale corpus fails loudly instead of scoring nonsense.
CORPUS_VERSION = 1
CORPUS_SCHEMA = "hsai.selection-corpus"

# Cheap -> expensive. Also the distance metric for over-/under-provisioning.
TIERS = ("light", "standard", "heavy")

# Relative quota weight of one iteration per tier, anchored on the published
# price ratios of the haiku/sonnet/opus families. Only the *ratios* matter:
# the report speaks in "quota units", never in currency, because a
# subscription-only harness has no per-call price to quote.
TIER_COST = {"light": 1.0, "standard": 5.0, "heavy": 25.0}

# The benchmark score is 1 - (weighted error rate). The weights encode the
# asymmetry the harness has actually paid for: an under-provisioned ticket
# costs a failed or off-spec PR and a full retry (ticket #4 burned two haiku
# attempts and still shipped nothing), while an over-provisioned one costs only
# quota. Under-provisioning is therefore twice as expensive as over-.
UNDER_WEIGHT = 1.0
OVER_WEIGHT = 0.5


class CorpusError(ValueError):
    """Raised when a corpus file is missing, malformed, or a future version."""


@dataclass(frozen=True)
class LabeledTask:
    """One benchmark instance: a task plus the tier a human judged correct.

    ``correct_tier`` is the label and the only field a human must supply;
    ``None`` marks an unlabeled draft row emitted by ``hsai corpus-build``.
    The ``observed_*`` fields are what actually happened when (or if) the loop
    ran this task - evidence for the label, never a substitute for it.
    """

    id: str
    kind: str  # heal | implement | improve
    title: str
    body: str = ""  # excerpt only: the corpus is committed and must stay reviewable
    labels: tuple[str, ...] = ()
    est_files: int = 1
    correct_tier: str | None = None
    observed_tier: str = ""
    observed_outcome: str = ""  # merged | failed | blocked | ""
    attempts: int = 0
    wall_clock_seconds: float = 0.0
    source: str = ""  # issue:42 | ledger:4133501 | lesson:<note> | adversarial
    note: str = ""  # why this label - the reviewable justification

    @property
    def labeled(self) -> bool:
        return self.correct_tier is not None

    def to_task(self) -> Task:
        """Project onto the record ``models.select`` actually consumes."""
        return Task(
            kind=self.kind,
            title=self.title,
            body=self.body,
            labels=tuple(self.labels),
            est_files=self.est_files,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["labels"] = list(self.labels)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> LabeledTask:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise CorpusError(f"unknown corpus field(s): {', '.join(sorted(unknown))}")
        payload = dict(data)
        payload["labels"] = tuple(payload.get("labels") or ())
        task = cls(**payload)
        if task.correct_tier is not None and task.correct_tier not in TIERS:
            raise CorpusError(f"{task.id}: correct_tier {task.correct_tier!r} is not a tier")
        return task


@dataclass(frozen=True)
class Corpus:
    """A version-stamped set of benchmark instances."""

    version: int
    tasks: tuple[LabeledTask, ...]
    path: Path | None = None
    description: str = ""

    @property
    def labeled(self) -> tuple[LabeledTask, ...]:
        return tuple(t for t in self.tasks if t.labeled)

    def __len__(self) -> int:
        return len(self.tasks)


def corpus_path(repo_root: str | Path = ".") -> Path:
    return Path(repo_root) / CORPUS_PATH


def load_corpus(path: str | Path | None = None, *, repo_root: str | Path = ".") -> Corpus:
    """Read a version-stamped corpus JSONL (header line first, then instances)."""
    p = Path(path) if path is not None else corpus_path(repo_root)
    if not p.is_file():
        raise CorpusError(f"no corpus at {p}")
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise CorpusError(f"empty corpus at {p}")

    try:
        header = json.loads(lines[0])
    except ValueError as exc:
        raise CorpusError(f"{p}: header line is not JSON: {exc}") from None
    if header.get("schema") != CORPUS_SCHEMA:
        raise CorpusError(f"{p}: first line must be a {CORPUS_SCHEMA!r} header")
    version = int(header.get("version", 0))
    if version > CORPUS_VERSION:
        raise CorpusError(f"{p}: corpus version {version} is newer than {CORPUS_VERSION}")

    tasks: list[LabeledTask] = []
    seen: set[str] = set()
    for i, line in enumerate(lines[1:], start=2):
        try:
            data = json.loads(line)
        except ValueError as exc:
            raise CorpusError(f"{p}:{i}: {exc}") from None
        task = LabeledTask.from_dict(data)
        if task.id in seen:
            raise CorpusError(f"{p}:{i}: duplicate instance id {task.id!r}")
        seen.add(task.id)
        tasks.append(task)
    return Corpus(
        version=version,
        tasks=tuple(tasks),
        path=p,
        description=str(header.get("description", "")),
    )


def write_corpus(
    path: str | Path,
    tasks: list[LabeledTask] | tuple[LabeledTask, ...],
    *,
    description: str = "",
    version: int = CORPUS_VERSION,
) -> Path:
    """Write a corpus (or an unlabeled draft) as header-plus-instances JSONL."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = {"schema": CORPUS_SCHEMA, "version": version, "description": description}
    lines = [json.dumps(header, sort_keys=True)]
    lines += [json.dumps(t.to_dict(), sort_keys=True) for t in tasks]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# --- scoring -----------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One instance's grade: what the strategy chose vs. what was labeled."""

    task: LabeledTask
    chosen: str
    correct: str
    rationale: str

    @property
    def distance(self) -> int:
        """Tier steps between the choice and the label (signed, + = too heavy)."""
        return TIERS.index(self.chosen) - TIERS.index(self.correct)

    @property
    def ok(self) -> bool:
        return self.distance == 0

    @property
    def over(self) -> bool:
        return self.distance > 0

    @property
    def under(self) -> bool:
        return self.distance < 0

    @property
    def cost(self) -> float:
        return TIER_COST[self.chosen]

    @property
    def oracle_cost(self) -> float:
        return TIER_COST[self.correct]


@dataclass(frozen=True)
class ReplayReport:
    """The graded result of one strategy over one corpus."""

    strategy: str
    corpus_version: int
    corpus_path: str
    verdicts: tuple[Verdict, ...] = ()
    skipped_unlabeled: int = 0

    # --- counts ---------------------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def matrix(self) -> dict[str, dict[str, int]]:
        """``matrix[correct][chosen]`` - the confusion matrix, all tiers present."""
        m = {c: dict.fromkeys(TIERS, 0) for c in TIERS}
        for v in self.verdicts:
            m[v.correct][v.chosen] += 1
        return m

    @property
    def correct(self) -> int:
        return sum(1 for v in self.verdicts if v.ok)

    @property
    def over_provisioned(self) -> int:
        return sum(1 for v in self.verdicts if v.over)

    @property
    def under_provisioned(self) -> int:
        return sum(1 for v in self.verdicts if v.under)

    # --- rates ----------------------------------------------------------------
    def _rate(self, n: int) -> float:
        return n / self.total if self.total else 0.0

    @property
    def accuracy(self) -> float:
        return self._rate(self.correct)

    @property
    def over_rate(self) -> float:
        return self._rate(self.over_provisioned)

    @property
    def under_rate(self) -> float:
        return self._rate(self.under_provisioned)

    # --- economics ------------------------------------------------------------
    @property
    def strategy_cost(self) -> float:
        """Quota units the strategy would spend over the corpus."""
        return sum(v.cost for v in self.verdicts)

    @property
    def oracle_cost(self) -> float:
        """Quota units a perfect router would spend - the achievable floor."""
        return sum(v.oracle_cost for v in self.verdicts)

    @property
    def ceiling_cost(self) -> float:
        """Quota units of the naive policy: route every task to the heavy tier."""
        return TIER_COST["heavy"] * self.total

    @property
    def quota_saved(self) -> float:
        """Fraction of the always-heavy bill the strategy avoids."""
        if not self.ceiling_cost:
            return 0.0
        return (self.ceiling_cost - self.strategy_cost) / self.ceiling_cost

    @property
    def excess_quota(self) -> float:
        """Quota units spent above a perfect router.

        Negative is not a win: it means the strategy under-spent the oracle,
        and the shortfall was paid in failed PRs and retries rather than saved.
        """
        return self.strategy_cost - self.oracle_cost

    # --- the gate -------------------------------------------------------------
    @property
    def score(self) -> float:
        """Benchmark score in ``[0, 1]``, higher is better - what CI gates on.

        ``1 - (UNDER_WEIGHT * under_rate + OVER_WEIGHT * over_rate)``: a
        weighted correctness that charges an under-provisioned routing twice
        what an over-provisioned one costs.
        """
        return 1.0 - (UNDER_WEIGHT * self.under_rate + OVER_WEIGHT * self.over_rate)

    def misses(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if not v.ok)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "corpus_version": self.corpus_version,
            "corpus_path": self.corpus_path,
            "total": self.total,
            "skipped_unlabeled": self.skipped_unlabeled,
            "matrix": self.matrix,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
            "over_provisioned": self.over_provisioned,
            "over_provision_rate": round(self.over_rate, 4),
            "under_provisioned": self.under_provisioned,
            "under_provision_rate": round(self.under_rate, 4),
            "strategy_cost": round(self.strategy_cost, 2),
            "oracle_cost": round(self.oracle_cost, 2),
            "ceiling_cost": round(self.ceiling_cost, 2),
            "quota_saved": round(self.quota_saved, 4),
            "excess_quota": round(self.excess_quota, 2),
            "score": round(self.score, 4),
            "misses": [
                {
                    "id": v.task.id,
                    "title": v.task.title,
                    "chosen": v.chosen,
                    "correct": v.correct,
                    "rationale": v.rationale,
                }
                for v in self.misses()
            ],
        }

    def render(self) -> str:
        """Human-readable confusion matrix plus the derived metrics."""
        m = self.matrix
        width = max(len(t) for t in TIERS)
        lines = [
            f"strategy: {self.strategy}   corpus: {self.corpus_path} "
            f"(v{self.corpus_version}, {self.total} labeled"
            + (f", {self.skipped_unlabeled} unlabeled skipped" if self.skipped_unlabeled else "")
            + ")",
            "",
            "confusion matrix (rows = labeled correct, cols = chosen):",
            " " * (width + 2) + "  ".join(f"{t:>{width}}" for t in TIERS) + "   total",
        ]
        for correct in TIERS:
            row = m[correct]
            total = sum(row.values())
            cells = "  ".join(f"{row[c]:>{width}}" for c in TIERS)
            lines.append(f"{correct:<{width}}  {cells}   {total:>5}")
        lines += [
            "",
            f"accuracy              {self.accuracy:6.1%}  ({self.correct}/{self.total})",
            f"over-provision rate   {self.over_rate:6.1%}  "
            f"({self.over_provisioned}/{self.total}) - quota burned on tasks a "
            f"cheaper tier would have merged",
            f"under-provision rate  {self.under_rate:6.1%}  "
            f"({self.under_provisioned}/{self.total}) - tasks routed below the tier "
            f"they needed",
            f"quota units           {self.strategy_cost:.0f} spent, "
            f"{self.oracle_cost:.0f} oracle floor, {self.ceiling_cost:.0f} all-heavy ceiling",
            f"estimated quota saved {self.quota_saved:6.1%}  vs. routing every task heavy "
            f"({self.excess_quota:+.0f} units vs. the oracle"
            + ("; under-spent - the shortfall is paid in failed PRs)"
               if self.excess_quota < 0 else ")"),
            f"score                 {self.score:6.4f}  "
            f"(1 - {UNDER_WEIGHT:g}*under - {OVER_WEIGHT:g}*over)",
        ]
        if self.misses():
            lines += ["", "misses:"]
            for v in self.misses():
                arrow = "over " if v.over else "under"
                lines.append(
                    f"  [{arrow}] {v.task.id}: chose {v.chosen}, labeled {v.correct}"
                    f" - {v.task.title}"
                )
        return "\n".join(lines)


def replay(
    corpus: Corpus, cfg: CoreConfig, *, strategy: str | None = None
) -> ReplayReport:
    """Run ``strategy`` over every labeled instance in ``corpus``.

    Pure computation: no ``claude``, no ``gh``, no network, no quota. Unlabeled
    draft rows are counted and skipped rather than guessed at.
    """
    from .models import select  # local: keeps the module import graph one-way

    strat = get_strategy(strategy or cfg.selection_strategy)
    verdicts: list[Verdict] = []
    skipped = 0
    for item in corpus.tasks:
        if not item.labeled:
            skipped += 1
            continue
        choice = select(item.to_task(), cfg, strategy=strat.name)
        verdicts.append(
            Verdict(
                task=item,
                chosen=choice.tier,
                correct=str(item.correct_tier),
                rationale=choice.rationale,
            )
        )
    return ReplayReport(
        strategy=strat.name,
        corpus_version=corpus.version,
        corpus_path=str(corpus.path) if corpus.path else "(in-memory)",
        verdicts=tuple(verdicts),
        skipped_unlabeled=skipped,
    )


def compare(baseline: ReplayReport, candidate: ReplayReport) -> str:
    """Render the measured delta between two strategies on the same corpus."""

    def _row(label: str, a: float, b: float, pct: bool = True) -> str:
        fmt = "{:6.1%}" if pct else "{:8.2f}"
        delta = b - a
        sign = "+" if delta >= 0 else ""
        return (
            f"{label:<22}{fmt.format(a)} -> {fmt.format(b)}   "
            f"{sign}{fmt.format(delta).strip()}"
        )

    return "\n".join([
        f"{baseline.strategy} -> {candidate.strategy} on {baseline.corpus_path} "
        f"({baseline.total} labeled tasks)",
        _row("accuracy", baseline.accuracy, candidate.accuracy),
        _row("over-provision rate", baseline.over_rate, candidate.over_rate),
        _row("under-provision rate", baseline.under_rate, candidate.under_rate),
        _row("quota saved", baseline.quota_saved, candidate.quota_saved),
        _row("excess quota units", baseline.excess_quota, candidate.excess_quota, pct=False),
        _row("score", baseline.score, candidate.score, pct=False),
    ])


# --- corpus mining -----------------------------------------------------------


@dataclass
class DraftReport:
    """What ``hsai corpus-build`` found, and where it looked."""

    candidates: list[LabeledTask] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _tier_for_model(cfg: CoreConfig, model: str) -> str:
    for name, tier in cfg.tiers.items():
        if tier.model == model:
            return name
    return ""


def build_draft(
    cfg: CoreConfig,
    repo_root: str | Path = ".",
    *,
    runner: Runner = run,
    include_github: bool = True,
    existing: Corpus | None = None,
) -> DraftReport:
    """Mine candidate instances for human labeling.

    The loop proposes, the architect labels. Every row comes out with
    ``correct_tier=None`` and the evidence attached (which tier ran it, how it
    ended, how many attempts it took) so labeling is a judgement call made
    against facts rather than from memory. Rows already present in ``existing``
    are dropped so re-running only surfaces what is new.
    """
    report = DraftReport()
    by_ticket: dict[int, dict] = {}

    # 1. Ledger: the economic record - tier, outcome, attempts, wall-clock.
    records: list[LedgerRecord] = read_records(ledger_path(cfg, repo_root))
    report.sources.append(f"ledger:{len(records)} records")
    for rec in records:
        if rec.ticket is None:
            continue
        by_ticket.setdefault(rec.ticket, {}).update(
            kind=rec.kind,
            observed_tier=rec.tier,
            observed_outcome=rec.outcome,
            attempts=rec.attempts,
            wall_clock_seconds=round(rec.wall_clock_seconds, 1),
            source=f"ledger:{rec.iteration}",
        )

    # 2. Lessons: titles and outcomes for iterations that predate the ledger.
    kb = KnowledgeBase.from_config(cfg, repo_root)
    lessons = kb.read_lessons()
    report.sources.append(f"lessons:{len(lessons)} notes")
    for lesson in lessons:
        if lesson.ticket is None:
            continue
        entry = by_ticket.setdefault(lesson.ticket, {})
        entry.setdefault("kind", lesson.kind)
        entry.setdefault("title", lesson.title)
        entry.setdefault("observed_tier", _tier_for_model(cfg, lesson.model))
        entry.setdefault("observed_outcome", "merged" if lesson.outcome == "pass" else "failed")
        entry.setdefault("source", f"lesson:{lesson.note_name}")

    # 3. Closed issues: the authoritative title, labels, and body text. Optional
    #    so the miner still works offline (with weaker rows).
    if include_github:
        try:
            issues = github.list_closed_issues(cfg.repo_slug, runner=runner)
        except Exception as exc:  # gh missing, unauthenticated, or offline
            issues = []
            report.notes.append(f"github unavailable, mined without issue text: {exc}")
        report.sources.append(f"closed-issues:{len(issues)}")
        for issue in issues:
            entry = by_ticket.setdefault(issue.number, {})
            entry["title"] = issue.title
            entry["labels"] = tuple(issue.labels)
            entry["body"] = _excerpt(issue.body)
            entry.setdefault("kind", "implement")
            entry.setdefault("source", f"issue:{issue.number}")
    else:
        report.notes.append("github mining disabled (--no-github)")

    known_ids = {t.id for t in existing.tasks} if existing else set()
    for ticket in sorted(by_ticket):
        entry = by_ticket[ticket]
        if not entry.get("title"):
            report.notes.append(f"skipped ticket #{ticket}: no title on any source")
            continue
        candidate = LabeledTask(
            id=f"ticket-{ticket:03d}",
            kind=entry.get("kind", "implement"),
            title=entry["title"],
            body=entry.get("body", ""),
            labels=tuple(entry.get("labels", ())),
            est_files=int(entry.get("est_files", 1)),
            correct_tier=None,  # the human's job
            observed_tier=entry.get("observed_tier", ""),
            observed_outcome=entry.get("observed_outcome", ""),
            attempts=int(entry.get("attempts", 0)),
            wall_clock_seconds=float(entry.get("wall_clock_seconds", 0.0)),
            source=entry.get("source", ""),
            note="DRAFT - set correct_tier and est_files, then move into the corpus",
        )
        if candidate.id in known_ids:
            continue
        report.candidates.append(candidate)
    return report


def _excerpt(body: str, limit: int = 400) -> str:
    """First meaningful paragraph of a ticket body, clipped.

    The corpus is committed and reviewed by humans, so it stores an excerpt -
    enough text for the keyword signals to fire, not a mirror of the backlog.
    """
    text = " ".join((body or "").split())
    return text[:limit]


def relabel(task: LabeledTask, tier: str, note: str = "") -> LabeledTask:
    """Return ``task`` with a human label applied (used by labeling tooling)."""
    if tier not in TIERS:
        raise CorpusError(f"{tier!r} is not a tier")
    return replace(task, correct_tier=tier, note=note or task.note)
