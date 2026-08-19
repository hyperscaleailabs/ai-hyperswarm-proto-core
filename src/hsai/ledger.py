"""Quota/cost telemetry ledger + a warn-then-halt per-block budget gate.

G4 asks the loop to get *cheaper (in quota)*, which first needs an economic
signal. This module gives every orchestrator iteration an auditable cost record
and the block that runs them a budget guardrail - all subscription-only: we
measure locally and never issue a metered API call.

- **Ledger** - one structured JSONL record per iteration (tier/model,
  wall-clock seconds, attempts, outcome, and token counts when the ``claude -p``
  output exposes them), appended to an append-only file under ``knowledge/`` so
  it is auditable and Obsidian-adjacent.
- **Aggregate** - per half-day block we fold the records into a summary
  (heavy-tier count, total seconds, token totals) surfaced in the review brief.
- **Budget gate** - config-driven ceilings (max heavy-tier iterations and max
  cumulative seconds per block). A *soft* breach warns and biases subsequent
  selection toward cheaper tiers; a *hard* breach halts starting NEW work for
  the block while letting in-flight PRs finish and merge. It never merges a red
  PR and never relaxes any traceability invariant.

Synthesis: assafelovic/gpt-researcher (costs.py cost accounting),
OpenBMB/ChatDev (activate cheaper agents to cut compute), and
run-llama/llama_index (a hard numeric CI gate).
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import CoreConfig

# Budget-gate statuses.
OK = "ok"
SOFT = "soft"
HARD = "hard"

# Tiers ordered cheap -> expensive; used to demote under a soft breach.
_TIER_ORDER = ("light", "standard", "heavy")

DEFAULT_LEDGER_FILE = "knowledge/ledger/iterations.jsonl"

# Serializes appends so concurrent workers never interleave a partial line.
_LEDGER_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LedgerRecord:
    """One iteration's economic footprint - the unit the ledger appends."""

    iteration: int
    block: int
    ticket: int | None
    kind: str
    tier: str
    model: str
    wall_clock_seconds: float
    attempts: int
    outcome: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Causal taxonomy (see hsai.postmortem): empty for a merged/passing
    # iteration; else a member of hsai.postmortem.FAILURE_CLASSES. Both default
    # to "" so read_records() parses pre-existing records that lack them.
    failure_class: str = ""
    failure_detail: str = ""
    # How many prior notes recall injected into this iteration's prompt (see
    # hsai.recall). 0 means the worker started cold - either recall was
    # disabled, or nothing in the vault matched. Defaults to 0 so read_records()
    # parses records written before retrieval was measured.
    recalled_count: int = 0
    created: str = field(default_factory=_now)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def ledger_path(cfg: CoreConfig, repo_root: str | Path) -> Path:
    """Resolve the append-only ledger JSONL under the repo's knowledge base."""
    rel = cfg.knowledge.get("ledger_file", DEFAULT_LEDGER_FILE)
    return Path(repo_root) / rel


def append_record(path: str | Path, record: LedgerRecord) -> Path:
    """Append one record as a single JSON line (append-only, never rewrites)."""
    path = Path(path)
    line = record.to_json() + "\n"
    with _LEDGER_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return path


def read_records(path: str | Path) -> list[LedgerRecord]:
    """Parse every record back off disk (empty list if the ledger is absent)."""
    path = Path(path)
    if not path.exists():
        return []
    records: list[LedgerRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(LedgerRecord(**json.loads(line)))
    return records


def parse_tokens(output: str | dict | None) -> tuple[int, int] | None:
    """Best-effort ``(input, output)`` token counts from a ``claude -p`` run.

    Accepts either the raw stdout or the already-parsed envelope
    (:attr:`hsai.ai.AIResult.payload`) - callers that have the parsed form pass
    it directly rather than re-parsing the text. Subscription-safe either way:
    this only reads what the CLI already printed (its JSON ``--output-format``
    carries a ``usage`` object) and never issues a metered call. Returns
    ``None`` when no counts are exposed.
    """
    if isinstance(output, dict):
        data: object = output
    else:
        text = (output or "").strip()
        if not text.startswith("{"):
            return None
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    if inp is None and out is None:
        return None
    return int(inp or 0), int(out or 0)


def _rate(part: int, whole: int) -> str:
    """``"3/4 (75%)"``, or an explicit "none ran" when the denominator is zero."""
    if not whole:
        return "0/0 (none ran)"
    return f"{part}/{whole} ({100 * part / whole:.0f}%)"


@dataclass
class BlockAggregate:
    """Folded cost of one half-day block (input to the brief + budget gate)."""

    block: int
    iterations: int = 0
    heavy_iterations: int = 0
    merged_iterations: int = 0
    review_iterations: int = 0  # kind='review': second opinions, not authored work
    total_seconds: float = 0.0
    total_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tier_counts: dict[str, int] = field(default_factory=dict)
    # Per-class failure counts this block (see hsai.postmortem.pareto_table for
    # the richer share/exemplar breakdown the review brief renders).
    failure_histogram: dict[str, int] = field(default_factory=dict)
    # Did being shown prior lessons help? Merge rates for the iterations that
    # got a recall pack vs the ones that started cold (see hsai.recall).
    recalled_iterations: int = 0
    merged_with_recall: int = 0
    merged_without_recall: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cold_iterations(self) -> int:
        """Iterations that ran with no prior lessons in their prompt."""
        return self.iterations - self.recalled_iterations

    def recall_effect(self) -> str:
        """One sentence comparing merge rates with and without a recall pack.

        Deliberately states the sample sizes rather than the rates alone: at a
        block size of five, "100% vs 0%" is one iteration each way and reads as
        proof when it is barely evidence. Whichever side is empty is named as
        such, so the comparison is never silently one-sided.
        """
        if not self.iterations:
            return "_No iterations recorded for this block._"
        return (
            f"Iterations given a recall pack merged "
            f"{_rate(self.merged_with_recall, self.recalled_iterations)}; "
            f"iterations that started cold merged "
            f"{_rate(self.merged_without_recall, self.cold_iterations)}. "
            "Too few iterations per block to be conclusive on its own - the "
            "comparison is cumulative across blocks via `knowledge/ledger/`."
        )

    def tokens_per_merged_pr(self) -> float | None:
        """Quota spent per unit of delivered work - the block's efficiency.

        ``None`` when the block merged nothing (the ratio would be undefined)
        or when no run reported token counts.
        """
        if not self.merged_iterations or not self.total_tokens:
            return None
        return self.total_tokens / self.merged_iterations

    def summary(self) -> str:
        tiers = ", ".join(f"{t}={self.tier_counts[t]}" for t in sorted(self.tier_counts))
        toks = self.total_tokens
        per_pr = self.tokens_per_merged_pr()
        return (
            f"{self.iterations} iterations, heavy-tier={self.heavy_iterations}, "
            f"{self.total_seconds:.0f}s wall-clock, {self.total_attempts} attempts"
            + (
                f", {self.review_iterations} independent review(s)"
                if self.review_iterations else ""
            )
            + (f", tiers[{tiers}]" if tiers else "")
            + (f", {toks} tokens" if toks else "")
            + (f", {per_pr:.0f} tokens/merged PR" if per_pr else "")
        )


def aggregate_block(records: list[LedgerRecord], block: int) -> BlockAggregate:
    """Fold every record belonging to ``block`` into a :class:`BlockAggregate`."""
    agg = BlockAggregate(block=block)
    for r in records:
        if r.block != block:
            continue
        agg.iterations += 1
        agg.total_seconds += r.wall_clock_seconds
        agg.total_attempts += r.attempts
        agg.tier_counts[r.tier] = agg.tier_counts.get(r.tier, 0) + 1
        if r.tier == "heavy":
            agg.heavy_iterations += 1
        if r.outcome == "merged":
            agg.merged_iterations += 1
        if r.recalled_count:
            agg.recalled_iterations += 1
            if r.outcome == "merged":
                agg.merged_with_recall += 1
        elif r.outcome == "merged":
            agg.merged_without_recall += 1
        if r.kind == "review":
            agg.review_iterations += 1
        if r.failure_class:
            agg.failure_histogram[r.failure_class] = (
                agg.failure_histogram.get(r.failure_class, 0) + 1
            )
        agg.input_tokens += r.input_tokens or 0
        agg.output_tokens += r.output_tokens or 0
    agg.total_seconds = round(agg.total_seconds, 3)
    return agg


@dataclass(frozen=True)
class BudgetDecision:
    """The budget gate's verdict for the *next* piece of work in a block."""

    status: str
    reason: str

    @property
    def demote(self) -> bool:
        """A soft breach: proceed, but bias selection toward cheaper tiers."""
        return self.status == SOFT

    @property
    def halt(self) -> bool:
        """A hard breach: do not start new work for the rest of the block."""
        return self.status == HARD


def evaluate_budget(agg: BlockAggregate, budget: dict) -> BudgetDecision:
    """Grade a block's spend so far against its ceilings.

    A hard ceiling met or exceeded -> ``HARD`` (halt new work). Reaching
    ``soft_ratio`` of a ceiling -> ``SOFT`` (warn + prefer cheaper tiers).
    Unset ceilings disable that dimension, so an empty ``budget`` never gates.
    """
    max_heavy = budget.get("max_heavy_iterations_per_block")
    max_seconds = budget.get("max_seconds_per_block")
    soft_ratio = float(budget.get("soft_ratio", 0.8))

    hard: list[str] = []
    soft: list[str] = []
    if max_heavy is not None:
        if agg.heavy_iterations >= max_heavy:
            hard.append(f"heavy-tier {agg.heavy_iterations} >= {max_heavy}")
        elif agg.heavy_iterations >= soft_ratio * max_heavy:
            soft.append(f"heavy-tier {agg.heavy_iterations} >= {soft_ratio:g}x{max_heavy}")
    if max_seconds is not None:
        if agg.total_seconds >= max_seconds:
            hard.append(f"wall-clock {agg.total_seconds:.0f}s >= {max_seconds}s")
        elif agg.total_seconds >= soft_ratio * max_seconds:
            soft.append(
                f"wall-clock {agg.total_seconds:.0f}s >= {soft_ratio:g}x{max_seconds}s"
            )

    if hard:
        return BudgetDecision(HARD, "; ".join(hard))
    if soft:
        return BudgetDecision(SOFT, "; ".join(soft))
    return BudgetDecision(OK, "within budget")


def demote_tier(tier: str) -> str:
    """Return the next cheaper tier (or ``tier`` itself if already cheapest)."""
    try:
        i = _TIER_ORDER.index(tier)
    except ValueError:
        return tier
    return _TIER_ORDER[max(0, i - 1)]
