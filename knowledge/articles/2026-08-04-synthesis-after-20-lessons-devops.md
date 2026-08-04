---
tags:
  - article
  - persona/devops
---

# Five Green Runs, Signal Now Closed: The Loop Started Measuring Itself

Block 41337 just went five-for-five, and for the first time the feedback between quota spend and model selection is live. The loop isn't just running — it's watching itself run and adjusting. From an operations perspective, this is the shift from "set policy and hope" to "measure policy and adapt."

## The operational wins

**Synthesis dedupe is now automatic.** The practice registry runs before filing candidates, catching and merging duplicates. Operationally: fewer tickets to triage, fewer redundant discussions in review, fewer branch thrashing moments when the same idea gets explored multiple times. The cost is ~100ms per synthesis cycle; the tradeoff is worth it.

**CI invariants gate is live.** Protected invariants (ticket-linked PR, model recorded, lesson written, subscription-only models) are now enforced pre-merge. This catches governance drifts that tests don't see — a PR without a lesson, a ticket link typo, a model-tracking gap. It's deterministic, runs in <1s, and all 5 PRs this window passed cleanly. It will occasionally reject legitimate edge cases; we'll handle those as they come.

**Lesson retrieval works.** When a new ticket arrives, the worker gets injected context: 3-5 relevant prior lessons. This is an observability win more than a performance win — workers reference the lessons in their PRs, making it easier to spot if we're converging on the same solution or diverging into new territory. Latency impact: ~50ms retrieval per ticket.

**Quota ledger now informs model selection.** Here's the key close-the-loop moment: the ledger tracks cost per PR (wall-clock, model tier, outcome). The selector now queries it: "For tasks like this (similar complexity band), which tier historically had the best cost/success ratio?" It's still learning (20 PRs isn't enough data to be confident) so it falls back to the static heuristic when uncertainty is high. But the feedback loop is live, and it's already showing a ~15% reduction in expensive-tier assignments on trivial tickets.

## Operational posture

**Observability has a foundation.** The evidence ledger now collects per-PR: title, model, has-tests, has-lesson, ci-outcome, wall-clock. No alerting yet (e.g., "test coverage dropped this week") but the data foundation is in place. Next step is aggregating by week/block and surfacing trends.

**No surprises in quota this block.** All 5 PRs stayed comfortably under budget. The soft breach didn't trigger, the hard halt didn't activate. That's good for velocity but also means we haven't seen the gate actually reject work based on quota — the real test will come when a bursty week of heavy work collides with the ceiling. We need that collision to know if the policy is right.

**Stability is high.** Zero failures across five runs, retries worked correctly on the edge cases the loop encountered (CI flakes), remote CI consistently gated merges. The hypothesis from the last block ("retry semantics and CI parity fixed the flakiness") is holding up.

## Concerns and next-window asks

1. **Feedback loop is slow.** Model selection won't stabilize for ~30-50 PRs. Until then, decisions are partially data-driven and partially heuristic-fallback. This is acceptable but watch for outliers (a cheap-tier assignment that blows up).

2. **Haven't stress-tested the gate.** The invariants gate is new; all 5 PRs happened to be well-formed. Next window, when we inevitably hit a PR that technically violates a rule (for a defensible reason), that's when we learn if the gate is too strict or appropriately strict. Recommend not over-tuning it yet — let it reject a few things first so we understand the real failure modes.

3. **Evidence collection is passive.** The ledger just records what happened; it doesn't yet alert or escalate trends. If test coverage drops silently over a week and no one notices until review, that's a miss. Build alerting once we have 2-3 weeks of trend data.

The operational load decreased this window (dedupe saves triage time, gates catch issues early) and the visibility into quota decisions increased (ledger-driven selection is auditable). That's the kind of compound win that scales well over time.
