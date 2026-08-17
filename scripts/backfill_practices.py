#!/usr/bin/env python3
"""One-time (idempotent) backfill of knowledge/practices/ from this repo's own
merged PRs and module docstrings.

The registry (see :mod:`hsai.practices`) started empty; every entry below is a
practice this loop had already adopted before the registry existed, cited to
the PR that shipped it and the module docstring that named the source project.
Re-running this script is safe: :func:`hsai.practices.append` refuses a
duplicate ``(source_project, title)`` pair, so an already-recorded practice is
skipped rather than overwritten.

Usage: ``python scripts/backfill_practices.py`` from the repo root.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsai.practices import DuplicatePracticeError, append, build_practice  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# (title, source_project, source_artifact, evidence, adopted_pr, adopted_date, notes, related)
ENTRIES = [
    (
        "session durability", "OpenBMB/ChatDev", "harness_design",
        "PR #104 (src/hsai/journal.py module docstring)", 104, "2026-08-05",
        "Landed as the per-block cycle journal: every side-effecting cycle step "
        "appends exactly one JournalRecord after it completes, so a block killed "
        "mid-flight can be resumed with `hsai cycle --resume`.",
        ("2026-08-05-implement-feat-durable-cycle-journal-with-idempotent-resume-for-interrupted-blocks",),
    ),
    (
        "reconciliation discipline", "assafelovic/gpt-researcher", "source_code",
        "PR #104 (src/hsai/journal.py module docstring)", 104, "2026-08-05",
        "journal.once(jr, step, key, fn) runs fn exactly once per (step, key) for "
        "a block and replays its recorded payload thereafter - no step is ever "
        "re-executed on resume.",
        ("2026-08-05-implement-feat-durable-cycle-journal-with-idempotent-resume-for-interrupted-blocks",),
    ),
    (
        "structured telemetry direction", "run-llama/llama_index", "harness_design",
        "PR #104 (src/hsai/journal.py module docstring)", 104, "2026-08-05",
        "Every cycle step's payload is journaled as one JSON line - a durable, "
        "inspectable record of what a block actually did.",
        ("2026-08-05-implement-feat-durable-cycle-journal-with-idempotent-resume-for-interrupted-blocks",),
    ),
    (
        "cost accounting", "assafelovic/gpt-researcher", "source_code",
        "PR #47 (src/hsai/ledger.py module docstring)", 47, "2026-07-26",
        "Landed as the quota/cost telemetry ledger: an append-only record of "
        "every model run's tier, wall-clock, attempts, and token counts.",
        ("2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate",),
    ),
    (
        "activate cheaper agents to cut compute", "OpenBMB/ChatDev", "harness_design",
        "PR #47 (src/hsai/ledger.py module docstring)", 47, "2026-07-26",
        "The budget gate biases model selection toward cheaper tiers on a soft "
        "breach instead of halting outright.",
        ("2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate",),
    ),
    (
        "a hard numeric CI gate", "run-llama/llama_index", "ci_cd",
        "PR #47 (src/hsai/ledger.py module docstring)", 47, "2026-07-26",
        "ledger.evaluate_budget is a pure numeric comparison against cfg.budget "
        "ceilings - no prose judgment call.",
        ("2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate",),
    ),
    (
        "review as a gate distinct from build/test", "microsoft/semantic-kernel", "harness_design",
        "PR #203 (src/hsai/review.py module docstring)", 203, "2026-08-12",
        "hsai.review.review_change runs after local CI passes and BEFORE a PR is "
        "opened - a distinct phase from ruff/pytest.",
        ("2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper",),
    ),
    (
        "machine-parseable pass/fail quality contract", "assafelovic/gpt-researcher", "harness_design",
        "PR #203 (src/hsai/review.py module docstring)", 203, "2026-08-12",
        "The reviewer must answer with a fenced JSON ReviewVerdict block; "
        "parse_verdict is deliberately fail-closed.",
        ("2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper",),
    ),
    (
        "reviewer role separated from the engineer role", "FoundationAgents/MetaGPT", "harness_design",
        "PR #203 (src/hsai/review.py module docstring)", 203, "2026-08-12",
        "hsai.models.select_reviewer never maps a tier to itself - the model that "
        "wrote a change is never the model that grades it.",
        ("2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper",),
    ),
    (
        "review phases run on cheaper agents", "OpenBMB/ChatDev", "harness_design",
        "PR #203 (src/hsai/review.py module docstring)", 203, "2026-08-12",
        "cfg.review.tier_policy biases cheap on purpose - the gate runs on EVERY "
        "change, so a heavy reviewer would spend the block's heavy budget on "
        "critique alone.",
        ("2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper",),
    ),
    (
        "persist a traj per run as the primary artifact", "SWE-agent/SWE-agent", "source_code",
        "PR #84 (src/hsai/trajectory.py module docstring)", 84, "2026-08-04",
        "Landed as hsai.trajectory: one JSON trajectory file per worker run, "
        "plus `hsai traj`/`hsai replay` to reconstruct it without spending quota.",
        ("2026-08-04-implement-feat-worker-trajectory-store-json-agent-output-and-hsai-replay",),
    ),
    (
        "per-step addressable stage results", "microsoft/JARVIS", "harness_design",
        "PR #84 (src/hsai/trajectory.py module docstring)", 84, "2026-08-04",
        "A Trajectory stores a list of Step records (tool calls, tokens, "
        "timing), not just the final output text.",
        ("2026-08-04-implement-feat-worker-trajectory-store-json-agent-output-and-hsai-replay",),
    ),
    (
        "observability as a cross-cutting layer at one choke point", "langchain-ai/langchain", "source_code",
        "PR #94 (src/hsai/trajectory.py module docstring)", 94, "2026-08-04",
        "Token/cost telemetry and the trajectory write both happen at the single "
        "point where run_agent returns.",
        ("2026-08-04-implement-feat-worker-trajectory-capture-and-working-token-cost-telemetry",),
    ),
    (
        "runner returns the full message list", "openai/swarm", "source_code",
        "PR #94 (src/hsai/trajectory.py module docstring)", 94, "2026-08-04",
        "`hsai traj`/`hsai replay` reconstruct a run from the stored Trajectory "
        "object directly - callers never re-derive what happened.",
        ("2026-08-04-implement-feat-worker-trajectory-capture-and-working-token-cost-telemetry",),
    ),
    (
        "agent memory scoping by outcome and kind", "OpenBMB/ChatDev", "harness_design",
        "PR #170 (src/hsai/recall.py module docstring)", 170, "2026-08-10",
        "The BM25 lesson-retrieval index up-weights outcome/fail notes and notes "
        "whose kind/* tag matches the current task.",
        ("2026-08-10-implement-feat-lesson-retrieval-memory-inject-prior-lessons-into-worker-and-synthesis-prompts",),
    ),
]


def main() -> int:
    written, skipped = 0, 0
    for title, project, artifact, evidence, pr, date, notes, related in ENTRIES:
        practice = build_practice(
            title=title, source_project=project, source_artifact=artifact,
            evidence=evidence, adopted_pr=pr, adopted_date=date, notes=notes,
            related=related,
        )
        try:
            path = append(ROOT, practice)
        except DuplicatePracticeError:
            skipped += 1
            continue
        written += 1
        print(f"wrote {path.relative_to(ROOT)}")
    print(f"backfill_practices: {written} written, {skipped} already recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
