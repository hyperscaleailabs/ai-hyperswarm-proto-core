---
tags:
  - article
  - persona/devops
---

# Twenty Runs, Zero Manual Interventions: What the Autonomous Loop Requires

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-08-synthesis-after-20-lessons]]

## The Operational Win: Subscription-Only, No Metered API Calls

The loop runs entirely on the Claude subscription model. This means:
- No risk of a runaway cost spike from a bug in model calling logic
- Predictable quota (capped per block in config)
- No on-call pager for API throttling or rate-limit recovery

The tradeoff: if the loop exceeds its quota mid-block, it halts rather than falling back to a cheaper model dynamically. In 20 iterations, this has not happened, but it's a known edge case worth monitoring.

## CI as the Source of Truth

The orchestrator treats the remote CI (GitHub Actions in our case) as the single source of truth for "is this change safe to merge?" This means:
- A change can pass local ruff/pytest and still be rejected if remote CI fails differently
- The loop polls remote CI explicitly before merging (not just trusting a status badge)
- If remote CI hangs, the loop has a timeout (300s by default) and marks the PR as waiting

Early on, we discovered mismatches between local CI and remote CI (different Python versions, for example). This explicit polling was the fix.

## The Durable Journal: Crash-Recovery at the Block Level

Every side-effecting operation in a block is idempotent:
- If a block crashes mid-flight and you resume with `hsai cycle --resume`, it picks up where it left off
- Already-filed tickets don't get re-filed
- Already-opened PRs don't get re-opened
- The journal itself is a simple jsonl file in `.ai-swarm/journal/` — human-readable, inspectable

This matters operationally: a human can manually edit the journal to skip a step if it's genuinely stuck (e.g., "mark synthesis-done even though it timed out"), and the block will resume correctly.

## Quota Gating: Soft Breach and Hard Halt

The system has two quota limits per block:
1. **Soft breach** (80% of ceiling): Log a warning but continue
2. **Hard halt** (100% of ceiling): Stop accepting new work, but complete in-flight PRs

This prevents runaway spending while allowing near-full utilization. If a block consumes 92% of quota and is in the middle of a PR merge, it finishes the PR but won't start new implementations.

## Operational Lessons from the Last 5 Iterations

The last window was 4 pass / 1 fail (80% success). The failure was in governance artifact generation — not in core orchestration logic. This suggests:
- The orchestrator itself is stable (core loop paths hold)
- Periphery code (synthesis, artifact writing) is still prone to edge cases
- The governance layer, while conceptually sound, needs more robustness in error handling

## Monitoring and Observability

Currently, the system produces:
- **Per-iteration**: A jsonl record with model, outcome, tokens, wall-clock time
- **Per-block**: A ledger aggregating block-level spend and outcome
- **Per-PR**: A lesson markdown file with context, what happened, and what we learned

This gives good signal for retrospectives but limited real-time observability. If a block hangs or loops infinitely, there's no in-flight alert—it'll just appear as a timeout once the per-iteration wall-clock limit fires.

## The Resilience Caveat

Zero manual interventions so far is good. But it's also partly a selection effect: the loop has only been fed well-scoped, ticket-driven work. Larger, more ambiguous work (e.g., "redesign the entire orchestrator") would likely hit failure modes we haven't seen yet. The next operational test is handling a genu inely hard ticket without human rescue.

## Reference
This operational model is informed by practices from MetaGPT's system health monitoring and our own trajectory ledger that enables forensic analysis of every run.
