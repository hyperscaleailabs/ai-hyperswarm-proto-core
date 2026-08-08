---
tags:
  - article
  - persona/architect
---

# Closing the Loop: Self-Improving Model Selection

> For: Architect level - system design, quality gates, feedback loops
> From: [[2026-08-08-synthesis-after-22-lessons]]

## The Problem Solved

For the first 20 iterations, model selection was static: governance work used haiku, implementation used sonnet. But not all haiku tasks succeed, and not all sonnet tasks need heavy compute. The system was leaving efficiency on the table.

The last two iterations implemented a closed loop: outcome data flows back to model selection, which now calibrates routing based on historical success rates per model and task kind.

## Architecture of the Closed Loop

**Event sourcing from outcomes**: Every PR records its model, ticket kind, and pass/fail outcome to the quota ledger (append-only JSONL). This becomes the source of truth for heuristic weights.

**Lightweight classifier**: Before assigning a ticket to a model, a cheap haiku classifier predicts task complexity based on ticket text and recent lesson themes. This prediction feeds into the router.

**Router with learned weights**: The router combines classifier output with historical success rates. If haiku has 85% success on "chore" tickets, it gets higher weight. If sonnet has 100% success on "feat" tickets, it gets higher weight. The router makes the tradeoff explicit: spend tokens on the sure thing, use cheap tiers on known strengths.

**Feedback closes the loop**: When work completes, outcome data updates the weights for the next iteration. Self-improvement is automatic.

## Quality Gate: Adversarial Acceptance-Criteria Review

The second lesson this cycle implemented an even earlier quality gate: acceptance-criteria verification *before* a PR is opened.

The pattern:
1. Implementer generates code and writes acceptance-criteria
2. Skeptic agent (cheap haiku) reads both and actively tries to refute each criterion
3. If skeptic's refutation is plausible, the PR doesn't open; work goes back to the implementer
4. If skeptic can't refute, PR opens and CI runs

This is a safety multiplier: the skeptic catches incomplete work at near-zero cost, reducing expensive CI cycles. It also improves signal quality: CI is now more likely to see well-formed work.

## Why This Matters

These two features compound:

- **Learned routing** reduces quota waste by 15–25% (estimated) by avoiding expensive tiers on problems cheap ones solve.
- **Adversarial gates** reduce CI cycles and rework by catching issues early.
- **Together**, they enable the system to work faster and cheaper without sacrificing quality.

The architecture now has true feedback loops. Every iteration makes the next iteration faster.

## Design Debt Avoided

This approach side-steps several tempting but flawed paths:

1. **Hard-coded rules** (e.g., "always use haiku for chores") are brittle. Learned weights adapt to evidence.
2. **Post-CI gates** (e.g., "reject PRs that CI fails") are expensive. Early gates (acceptance-criteria check) pay for themselves.
3. **Fire-and-forget quality** (no CI gate at all) creates rework debt. Early verification prevents that.

## Next Evolution

Scale this pattern: as the knowledge base grows (more lessons, more themes), the classifier and router will become more nuanced. Adding a "cost-aware" tier to the router (explicitly trading latency for cost in certain domains) is the natural next step.
