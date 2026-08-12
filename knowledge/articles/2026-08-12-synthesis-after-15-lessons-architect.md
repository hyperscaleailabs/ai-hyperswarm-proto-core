---
tags:
  - article
  - persona/architect
---

# Lessons from Five Green Runs in an Autonomous Build Loop

We run a loop that generates, tests, and merges its own changes — implement and improve tickets, no human in the merge path. This cycle: 5/5 passed. That streak is worth being suspicious of, not proud of, so here's what the architecture actually looks like and where it's still fragile.

## Model selection by task complexity
The loop now routes tickets to different model tiers based on estimated task complexity rather than using one model for everything. This is a straightforward cost/latency tradeoff, but the harder part was defining "complexity" in a way that's cheap to compute upfront and doesn't require running the task first to know how hard it was. We settled on a heuristic over ticket metadata (scope of touched files, whether it's a net-new implement vs. a bounded improve) rather than anything model-based — simpler, and it fails predictably when it's wrong, which matters more than being occasionally smarter.

## Reliability was the actual bottleneck, not intelligence
The most consequential work this window wasn't a new capability — it was retry logic and CI parity for the loop's own execution. Before this, failures weren't concentrated in "the model got the code wrong"; they were concentrated in flaky infrastructure (transient CI failures, environment drift between the loop's sandbox and real CI) getting misattributed as ticket failures and burning a cycle. That's a common trap in autonomous-loop design: you build elaborate quality gates around the artifact and skip hardening the harness that runs them. The fix was unglamorous — make local execution match CI closely enough that a pass locally means a pass remotely, and retry on the specific failure signatures known to be infra rather than logic. This is why the "5/5 pass" streak reads as a loop-health signal now, not just a code-quality one — and also why it's an untrustworthy signal in isolation: a quieter loop can just mean fewer real problems got attempted.

## Explicit phase artifacts, borrowed from MetaGPT
We adopted the MetaGPT-style discipline of making each phase (design, implement, review) emit a durable artifact rather than passing implicit state through a single agent's context. The tradeoff is real: more structure, more latency, more surface area to keep in sync. What it bought us is inspectability — when a lesson fails, you can point at which phase's artifact was wrong instead of re-running the whole thing to guess. Given we can't trust the loop to always self-report accurately, externalizing state we can audit after the fact was the right call.

## What we didn't validate
A synthesis over five green runs, two kinds (implement, improve), is a small, self-selected sample — it says the current gates catch what they're tuned to catch, not that the loop generalizes. The recurring "themes" (build, change, merged cleanly, green) are lagging indicators of loop mechanics, not of decision quality. The open risk is the same one every autonomous-merge system has: quality gates that are well-calibrated for the failure modes we've already seen, and untested against the ones we haven't.
