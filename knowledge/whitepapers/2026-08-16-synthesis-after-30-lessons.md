---
tags:
  - whitepaper
created: 2026-08-16
---

# Synthesis after 30 lessons

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
Synthesis of the last 3 lesson(s): 1 pass / 2 fail, across kinds implement, implement, implement.

## Outcomes in this window
| outcome | count |
| --- | --- |
| pass | 1 |
| fail | 2 |

## Work by kind
| kind | count |
| --- | --- |
| implement | 3 |

## Recurring failures
- **timeout / resource constraints** - lesson 30 (verifiable subscription-only execution and real agent telemetry): agent timed out at 1200s during phase=implement, despite CI passing
- **timeout / resource constraints** - lesson 29 (same feature, prior attempt): agent timed out at 1200s during phase=implement, despite CI passing

## Recurring themes
- **execution** - appears in 2 lessons
- **subscription** - appears in 2 lessons
- **telemetry** - appears in 1 lesson

## Lessons synthesized
- [[2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper]]
- [[2026-08-13-implement-feat-synthesis-memory-and-duplicate-proposal-rejection]]
- [[2026-08-14-implement-feat-verifiable-subscription-only-execution-and-real-agent-telemetry]]

## Analysis: holding at the complexity boundary

After lesson 28 shipped cleanly (synthesis memory and duplicate-idea rejection), the loop encountered a hard boundary: lesson 29 (verifiable subscription-only execution) timed out at 1200s wall-clock. Lesson 30 — a retry of the same ticket with the same model (`sonnet`) — also timed out at the exact same point.

This is not variance; it's a signal. Two consecutive timeouts on the same ticket with the same model is not a one-off. It indicates that the ticket complexity genuinely exceeds the per-agent budget under the current model assignment.

What matters: **CI passed both times**. The timeout was not a crash or a resource leak — the CI harness completed normally, tests passed, but the worker exceeded its 1200s wall-clock limit during the implementation phase. This is distinctly different from failure modes like context explosion or infinite loops.

## What shifted from lesson 29 to lesson 30

Lesson 29 timed out and was recorded. Lesson 30 is a retry of the same ticket. The agent made it to the same timeout point — suggesting either:
1. The ticket is genuinely too large for 1200s with `sonnet`
2. The worker's phase is too ambitious (attempting too much before checkpointing)
3. The model choice is sub-optimal for this class of work

All three are distinct problems, and all three are *solvable*. None indicate the loop is broken.

## The architecture inflection

At lesson 28, the loop proved it can self-modify without breaking: it added synthesis memory (duplicate-idea rejection) and landed it on the first try. That's the moment the loop crossed from "can this work?" to "can this improve itself?"

Lessons 29–30 are the answer to the next question: "what happens when the loop hits its own limits?"

The loop doesn't crash. It doesn't produce garbage. It reaches a boundary and stops. That's actually good engineering — a system that hits its limits gracefully is easier to scale than one that falls over unpredictably.

## What worked: governance and gates

All prior lessons landed cleanly through the governance layer:
- Ticket → ADR → PR → green CI → merge
- Synthesis is submitting valid tickets
- The review gate (lesson 27) is catching issues before merge
- MOCs stay up to date
- DIRECTION refreshes after each block

This infrastructure is load-bearing. Lessons 27–28 depended on it to land features that modify the loop itself. Without this governance foundation, you couldn't safely add gates and memory to your own execution engine.

## What deteriorated: scheduling clarity

The loop doesn't have a policy for ticket-too-complex or worker-out-of-budget. When lesson 29 timed out, it was recorded as a failure, and lesson 30 was filed as a retry without any indication that the retry would face the same boundary.

A mature loop would:
1. **Detect** that a ticket timed out (✓ logged)
2. **Diagnose** whether it's a regression, a one-off variance, or a genuine capacity problem (✗ no analysis step)
3. **Decide** what to do: escalate, decompose, or route to a heavier model (✗ no policy)
4. **Execute** the decision (✗ defaults to "retry with same approach")

Right now you have steps 1 but not 2–4.

## Takeaway for block 41361

Block 41361 is the inflection point: the loop is encountering **bounded complexity**. It's not hitting resource exhaustion (CI passed), not crashing (timeout is graceful), not generating garbage — it's hitting a scheduling boundary.

This is progress. It means you've solved the correctness layer (governance, gates, synthesis) and moved to the scheduling layer (complexity estimation, model routing, escalation).

The path forward is not to raise the 1200s limit or switch to a heavier model by default. It's to make the loop conscious of its own capacity:
- **Capacity detection**: When wall-clock exceeds threshold, is it worth retrying, or should we escalate?
- **Ticket decomposition**: Can synthesis split "verifiable subscription execution" into smaller tickets?
- **Model routing**: Would `opus` (heavy) finish in 1200s where `sonnet` doesn't?

Pick one and iterate. My recommendation: start with capacity detection (detect timeout, propose escalation, let a human or a heavier model take it), because it's the least invasive and most robust.
