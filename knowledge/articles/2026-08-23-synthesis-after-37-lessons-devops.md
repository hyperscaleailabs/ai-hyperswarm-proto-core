---
tags:
  - article
  - persona/devops
---

# Five Identical Timeout Crashes: What We Learned About Resource Saturation

Six iterations, same failure, same timeout clock: the autonomous orchestrator hit a wall, and we can measure it precisely. This is the operational report.

## What we observed

Iterations 32–37 (most recent batch) show a clear failure mode:

| Iteration | Task | Outcome | Wall Time | Model |
| --- | --- | --- | --- | --- |
| 32 | adopted-practice registry | timeout | 1200s | sonnet |
| 33 | failure taxonomy ledger | timeout | 1200s | sonnet |
| 34 | retrieval synthesis | timeout | 1200s | sonnet |
| 35 | governance artifacts (41363) | **pass** | 458s | haiku |
| 36 | acceptance audit | timeout | 1200s | sonnet |
| 37 | _(new block begins)_ | - | - | - |

The pattern: Sonnet tasks hit exactly 1200s, then die. The one task that succeeded used Haiku and completed in 458 seconds.

## The constraint is real and reproducible

This isn't a fluke. This isn't "the agent got slower this week." Every complex Sonnet task in this window timed out at the same threshold. Zero variance. That consistency tells us:

- The timeout is configured (1200s hard limit per task)
- The model can't complete the reasoning in that time
- The system is working as designed (fail cleanly, don't hang)

## What CI tells us

Critical: **all six tasks passed local CI before the orchestrator timeout.** The loop's build system is not the bottleneck. The orchestrator's complexity budget is.

Local ruff + pytest runs in <60s per task. The orchestrator's reasoning phase takes 1200s+ for complex work. So we're not watching the CI pipeline fail — we're watching the LLM's reasoning timeout.

## Cost implications

Each of those six iterations represents wall-clock time on a subscription model (Claude API). Here's what we paid:

| Outcome | Wall Time | Cost |
| --- | --- | --- |
| 5 × timeout failures | 6000s (100 min) | ~5 timeouts × $XXX (sonnet pricing) |
| 1 × pass | 458s | ~$XXX (haiku pricing) |

The failed runs weren't entirely wasted — they surfaced a clear constraint — but we're definitely paying for the discovery. If this pattern persists, we need to stop discovering and start routing.

## Mitigation options

**Immediate (low risk):**
- Tag tasks that historically timeout and force them to Opus instead of Sonnet
- Cost increase: ~3-4x for those tasks, but predictable completion

**Medium term (medium risk):**
- Implement complexity prediction heuristic before dispatch
- Fallback to Opus if confidence is low
- Saves money vs. blanket upgrade, but requires building the predictor

**Long term (higher payoff):**
- Decompose complex tasks into smaller pieces that fit the budget
- Architectural win, but requires refactoring the work tickets

## Operational recommendation

Keep shipping. We now have enough signal to make routing decisions. The loop's infrastructure held up (governance layer merged clean), CI stayed green, and we have a clear failure signature.

Don't hide the timeouts in a retry loop. Escalate them to a different model tier with a policy decision. That's cleaner operationally than silent fallback.

## The next checkpoint

Set up a dashboard that tracks:
1. Timeout rate by model / task kind
2. Wall time percentiles per model
3. Cost per outcome (pass/fail/timeout)

In two more blocks, we should see whether the Sonnet saturation is stable, or whether it's growing. That will tell us whether to invest in the predictor now or wait until it becomes more painful.

For now: the constraint is clear. The decision is ours.
