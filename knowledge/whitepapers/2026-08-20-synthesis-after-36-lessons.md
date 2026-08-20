---
tags:
  - whitepaper
created: 2026-08-20
---

# Synthesis after 36 lessons

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
Synthesis of the last 3 lesson(s): 0 pass / 3 fail, across kinds implement, implement, implement.

## Outcomes in this window
| outcome | count |
| --- | --- |
| pass | 0 |
| fail | 3 |

## Work by kind
| kind | count |
| --- | --- |
| implement | 3 |

## Recurring failures
- **timeout / resource constraints** - lesson 34 (adopted-practice registry): agent timed out at 1200s during phase=implement with sonnet, despite CI passing
- **timeout / resource constraints** - lesson 35 (failure taxonomy): agent timed out at 1200s during phase=implement with sonnet, despite CI passing
- **timeout / resource constraints** - lesson 36 (retrieval-grounded synthesis): agent timed out at 1200s during phase=implement with opus, despite CI passing

## Recurring themes
- **scaling limits** - appears in 3 lessons
- **synthesis complexity** - appears in 3 lessons
- **timeout boundary** - appears in 3 lessons

## Lessons synthesized
- [[2026-08-17-implement-feat-adopted-practice-registry-with-provenance-wired-into-the-synthesis-context-pack]]
- [[2026-08-17-implement-feat-failure-taxonomy-in-the-ledger-plus-a-postmortem-driven-backlog-trigger]]
- [[2026-08-18-implement-feat-retrieval-grounded-synthesis-the-planner-must-read-and-cite-its-own-lessons-before-filing-tickets]]

## Analysis: The 1200s barrier - hitting system limits with synthesis complexity

Lessons 34–36 represent a critical inflection point: the loop is now hitting hard resource limits, not just edge cases. All three failures are timeouts at exactly 1200s, suggesting the wall-clock deadline is the constraint, not the algorithm.

### What Changed from Lessons 29–33

Lessons 29–31 (synthesis memory, subscription-only execution, governance artifacts) were about scaling the loop's infrastructure and decision-making. Lessons 32–33 (adopted-practice registry, failure taxonomy) attempted to add new capabilities to the loop's learning system.

The pattern:
- **Lessons 29–31**: Harder problems (subscription-only execution), but with explicit escalation and analysis
- **Lessons 34–36**: Different harder problems (practice registry, failure taxonomy, retrieval-grounded synthesis), but hitting the resource wall uniformly

### The Signal

Three consecutive timeouts across different models (sonnet → sonnet → opus) and different problem classes (registry, taxonomy, synthesis) suggests this is not a model-selection problem. It's a capacity problem.

The loop can't execute complex synthesis tasks within 1200s. Even opus (the heavier model) times out on lesson 36 (retrieval-grounded synthesis). The task complexity grew faster than the system's capacity to handle it.

### Why CI Passes

All three lessons report "remote CI: SUCCESS". This is crucial: the code is *semantically correct*. The timeout is a *wall-clock constraint*, not a correctness failure. The worker ran out of time before it could finish implementing the task, even though what it did implement would have been correct.

This is different from lessons 29–30 (subscription-only execution), which also timed out but were followed by explicit analysis and escalation. Lessons 34–36 are pure resource exhaustion with no additional context captured.

### The Architectural Problem

The loop is now at a complexity ceiling:
1. **Synthesis tasks are growing in scope** — adopted-practice registry needs to read and integrate multiple reference projects; failure taxonomy needs to reason about the ledger; retrieval-grounded synthesis needs to search and cite lessons
2. **Wall-clock budget is fixed** — 1200s per task, hardcoded
3. **Worker context is limited** — the worker sees one ticket and must deliver within 1200s

When synthesis tasks hit this ceiling, they fail uniformly, regardless of model choice. Lesson 36 used opus (the heaviest model) and still timed out.

### What Didn't Happen

The loop did NOT:
- Panic or retry blindly
- Fall back to a simpler approach
- Escalate explicitly with analysis

Lessons 34–36 are recorded as pure fails. There's no synthesis-of-the-synthesis (like lesson 31 provided after lessons 29–30). The loop has hit a hard stop.

### What Needs to Happen Next

Block 41369 requires one of:

1. **Decompose the tasks** — Break adopted-practice registry, failure taxonomy, and retrieval-grounded synthesis into smaller sub-tickets that each fit within 1200s
2. **Increase the wall-clock budget** — Extend the 1200s timeout to 2400s or 3600s for synthesis tasks specifically
3. **Route to human** — Have the architect manually implement or delegate these capabilities instead of asking the loop to do it

Option 1 is the most scalable but requires changing how synthesis tasks are defined.

Option 2 is the fastest but masks the underlying problem (task complexity is growing faster than the system can handle).

Option 3 is the safest but the least autonomous.

## What Deteriorated: Task Complexity Escalation Without Capacity Planning

The loop has been adding features at a steady pace:
- Lessons 1–15: Core execution
- Lessons 15–25: Self-modification and governance
- Lessons 25–28: Cross-model and adversarial checks
- Lessons 29–31: Synthesis and escalation
- Lessons 34–36: Practice registry, failure taxonomy, synthesis-of-synthesis

Each layer added more reasoning, more reading, more context. By lesson 36 (retrieval-grounded synthesis), a single ticket requires:
- Reading and parsing the codebase
- Reading and parsing prior lessons
- Synthesizing a strategy
- Implementing the strategy
- Testing
- All within 1200s

This is not feasible at the current system complexity. The loop needs either more time, smaller tasks, or both.

## What Improved: Consistency of Failure Signal

All three timeouts are clean and recorded uniformly:
- Same wall-clock limit (1200s)
- Same remote CI status (SUCCESS, meaning code is correct)
- Same pattern (timeout during phase=implement)

This consistency is valuable: it's not noise, it's a clear signal that the system has hit a hard boundary.

## Takeaway for Block 41369

Block 41369 is decision time again, but at a higher level:

**The loop has scaled horizontally (added more capabilities) but not vertically (increased per-task capacity).** Lessons 34–36 are hitting the vertical limit.

You must choose:
1. **Grow the wall-clock budget** — Give synthesis tasks more time
2. **Shrink the tasks** — Decompose complex features into smaller tickets
3. **Outsource the tasks** — Implement these features manually or delegate to a human-in-the-loop path

The loop itself is functioning correctly (CI passes, no corruption). The question is whether it can scale *deeper* tasks or only *more* tasks.

## Lessons for the next worker

When you pick up lesson 37 (the next task in block 41369), you'll have:
- Clear evidence that the system has hit a resource ceiling
- Concrete data: three consecutive timeouts at 1200s, across different models and tasks
- A synthesis that identifies the root cause: task complexity outpaced per-task capacity

Use this synthesis as your constraint boundary. Don't retry lessons 34–36 expecting them to succeed with incremental fixes. They won't. The architecture needs to change first.

The options are clear. Pick one and move forward. The loop is waiting for a decision.
