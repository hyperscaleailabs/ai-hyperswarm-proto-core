---
tags:
  - article
  - persona/devops
---

# Running an Autonomous Swarm: Operations and CI/CD Lessons

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-08-synthesis-after-20-lessons]]

## CI/CD as the Truth Source

The ai-hyperswarm-proto-core treats remote CI as the single source of truth. Every PR waits for GitHub checks to complete before merging—no local CI can override it. This keeps distributed agents from bypassing safety gates. The orchestrator polls with a 10-second cadence and a 5-minute timeout; if CI hangs, it fails gracefully rather than stalling the loop.

## Automation Mechanics

**Git Workflow**: The orchestrator creates ephemeral worktrees per iteration, reducing disk churn and supporting parallelism later. Each worktree is isolated, so 3 concurrent workers can run without conflicts. Branches follow a deterministic pattern (`hsai/cycle-{cycle_index}-{timestamp}`) for auditability.

**Model as a Service**: Claude runs headless via `claude -p` (CLI subscription quota). All agent invocations are billed against subscription limits, never metered APIs. Output is captured as JSON via `--output-format json`, feeding directly into quota ledger and trajectory storage.

**Journaling and Resumability**: Blocks that crash mid-flight can resume via `hsai cycle --resume`. The journal records which steps completed; resuming replays only undone steps. Crucially, the ticket is filed once (not retried), and quota spend is never double-counted.

## Quota Ledger & Budget Gates

Every iteration logs cost to `knowledge/ledger/iterations.jsonl`. The budget system has two thresholds:
- **Soft breach** (80%): bias selection toward cheaper tiers (haiku)
- **Hard breach** (100%): halt new work, but let in-flight PRs finish

If a block crosses the hard ceiling after starting an iteration, it still lets that iteration merge (no aborting mid-flight). The ledger is the source of truth for resume: the block re-grades budget from complete history, not cached values.

## Trajectory Storage & Replay

Agent outputs are stored as JSON in `.hsai/traj/{iteration_id}.json`. This enables offline replay: `hsai replay {iteration_id}` reconstructs the full agent run without spending quota. Blocks keep the last 8 blocks of trajectories by default; older ones are pruned to keep storage bounded.

## Deployment Readiness

**Two-Phase Parallelism**: Synthesis (heavy, nightly) → Implementation blocks (lighter, on-demand). The durable journal makes it safe to pause a block and resume later.

**Failure Modes**: If remote CI fails, the PR doesn't merge; the orchestrator records the failure as a lesson and retries up to 2 times. Humans still override via `/review-next` when needed.

**Observability**: Every PR carries model-used, lesson-learned, and iteration metadata. The quota ledger tracks cost per iteration and per PR. MOCs tie lessons together for pattern discovery.

## Operational Notes

- **Local machine protection**: Sequential blocks only (no local parallelism). Future scaling via distributed workers.
- **Permission mode**: `acceptEdits` allows the headless agent to commit, push, and open PRs without human approval.
- **Subscription model**: No metered API calls—pure subscription quota. Budget ceilings are soft controls, not hard limits enforced by the API.

Monitor tokens-per-merged-PR and heavy-tier usage; these are the metrics that drive optimization decisions.
